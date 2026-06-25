#include <esp_log.h>
#include <esp_err.h>
#include <esp_heap_caps.h>
#include <esp_lcd_panel_ops.h>
#include <esp_wifi.h>
#include <nvs_flash.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/stream_buffer.h>
#include <driver/gpio.h>
#include <sys/time.h>

#include "ai_cat_board.h"
#include "wifi_provisioning.h"
#include "cat_websocket.h"
#include "audio_pipeline.h"
#include "lvgl_display.h"

#define TAG "main"

static CatWebSocket g_websocket;
static AudioEncoder g_audio_encoder;
static AudioDecoder g_audio_decoder;
static volatile bool g_audio_playing = false;
static volatile int64_t g_last_audio_ms = 0;

// Audio output queue: decouple WebSocket callback (fast, non-blocking)
// from I2S writes (can block when DMA buffer is full).
// 30 frames × 60ms = 1.8s buffering — enough for typical TTS sentences.
#define AUDIO_OUT_QUEUE_SIZE 30
static QueueHandle_t g_audio_out_queue = nullptr;
static StaticQueue_t g_audio_out_queue_struct;
static uint8_t* g_audio_out_queue_storage = nullptr;

// Display deferred render state
static volatile bool g_display_needs_render = false;
static std::string g_display_emotion = "content";

// Stream buffer for PCM frames: main loop → encoder task.
// 40 frames × 1920 bytes gives ~2.4s of buffering headroom.
#define ENCODER_STREAM_SIZE (AUDIO_FRAME_SAMPLES * sizeof(int16_t) * 40)

// Static allocation for the stream buffer: control struct in DRAM (BSS),
// storage buffer in PSRAM to avoid exhausting internal DRAM (~243KB).
// pvPortMalloc used by xStreamBufferCreate is restricted to internal DRAM
// (MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT), so a 77KB allocation easily fails
// after WiFi/audio/Opus init fragments the heap.
static StaticStreamBuffer_t g_encoder_stream_struct;
static uint8_t* g_encoder_stream_storage = nullptr;
static StreamBufferHandle_t g_encoder_stream = nullptr;

static int64_t GetTimestampMs() {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

static void ReadTouchSensors(int& head, int& back, int& belly) {
    head  = gpio_get_level(TOUCH_HEAD_GPIO) ? 0 : 100;
    back  = gpio_get_level(TOUCH_BACK_GPIO) ? 0 : 100;
    belly = gpio_get_level(TOUCH_BELLY_GPIO) ? 0 : 100;
}

// Dedicated encoder task — opus_encode() needs 8-12KB stack,
// so this task gets its own 32KB to avoid blowing up main task.
static void EncoderTask(void* arg) {
    int frame_count = 0;
    int16_t pcm_buf[AUDIO_FRAME_SAMPLES];  // 1920 bytes on task stack
    while (true) {
        if (g_encoder_stream == nullptr) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        size_t received = xStreamBufferReceive(g_encoder_stream,
                                               pcm_buf, sizeof(pcm_buf),
                                               portMAX_DELAY);
        if (received == sizeof(pcm_buf)) {
            frame_count++;
            std::vector<uint8_t> opus_frame;
            // Encode from stack buffer (no heap allocation for PCM)
            if (g_audio_encoder.Encode(pcm_buf, AUDIO_FRAME_SAMPLES, opus_frame)) {
                if (g_websocket.IsConnected()) {
                    g_websocket.SendAudio(opus_frame);
                }
                if (frame_count == 1) {
                    ESP_LOGI(TAG, "Opus send OK: %d bytes/frame (was 1920 bytes raw PCM)",
                             (int)opus_frame.size());
                }
            } else if (frame_count <= 3) {
                ESP_LOGW(TAG, "Opus encode failed on frame %d", frame_count);
            }
        }
    }
}

// Audio output task: dequeues PCM frames and writes to I2S.
// Blocking I2S writes are fine here — this task has no other responsibilities.
static void AudioOutputTask(void* arg) {
    int16_t pcm_buf[AUDIO_FRAME_SAMPLES];
    int played = 0;
    while (true) {
        if (g_audio_out_queue == nullptr) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        if (xQueueReceive(g_audio_out_queue, pcm_buf, pdMS_TO_TICKS(100)) != pdTRUE) {
            continue;  // no audio yet — keep waiting
        }
        auto* codec = AiCatBoard::GetInstance().GetAudioCodec();
        if (codec && codec->output_enabled()) {
            std::vector<int16_t> pcm_vec(pcm_buf, pcm_buf + AUDIO_FRAME_SAMPLES);
            codec->OutputData(pcm_vec);
        }
        played++;
        if (played == 1) {
            ESP_LOGI(TAG, "Audio output started");
        }
        g_last_audio_ms = GetTimestampMs();
    }
}

// Main application entry point
extern "C" void app_main(void) {
    ESP_LOGI(TAG, "=== AI Cat Firmware Starting ===");

    // Initialize NVS
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // Initialize board (I2C, audio, GPIO, display)
    auto& board = AiCatBoard::GetInstance();
    if (!board.Initialize()) {
        ESP_LOGE(TAG, "Board initialization failed");
        return;
    }

    // Initialize LVGL eye display (uses panel from AiCatBoard)
    auto& eye_display = LvglEyeDisplay::GetInstance();
    if (eye_display.Initialize(board.GetLcdPanel())) {
        // Show default NORMAL eyes immediately
        LvglEyeDisplay::RunLvgl();
        ESP_LOGI(TAG, "LVGL eye display ready — waiting for server commands");
    } else {
        ESP_LOGE(TAG, "LVGL eye display init failed — continuing without eyes");
    }

    // WiFi provisioning: try saved networks, fall back to captive portal
    auto& wifi = WifiProvisioning::GetInstance();

    // Show WiFi status on the display during scanning/connecting/provisioning
    wifi.OnStatusChange([](const std::string& msg) {
        auto& disp = LvglEyeDisplay::GetInstance();
        size_t nl = msg.find('\n');
        if (nl != std::string::npos) {
            disp.ShowWifiInfo(msg.substr(0, nl), msg.substr(nl + 1));
        } else {
            disp.ShowWifiInfo(msg, "");
        }
    });

    wifi.Start();  // blocks until connected (may reboot during provisioning)

    // Disable WiFi power saving — prevents TCP write stalls that cause
    // WebSocket disconnections (transport_poll_write timeout).
    esp_wifi_set_ps(WIFI_PS_NONE);
    ESP_LOGI(TAG, "WiFi power save: OFF");

    // Hide WiFi overlay — show cat eyes
    eye_display.HideWifiInfo();

    ESP_LOGI(TAG, "WiFi connected, IP: %s", wifi.GetIPAddress().c_str());

    // Connect WebSocket to server
    if (g_websocket.Connect(SERVER_WEBSOCKET_URL)) {
        ESP_LOGI(TAG, "Connected to AI Cat server");

        // Set up command handler
        g_websocket.OnCommand([](const CommandPacket& cmd) {
            ESP_LOGI(TAG, "Command: emotion=%s ears=(%d,%d) vib=%d audio=%d",
                     cmd.emotion.c_str(), cmd.ear_left_deg, cmd.ear_right_deg,
                     cmd.vibration, cmd.has_audio);

            // Update display emotion — set immediately
            if (!cmd.emotion.empty()) {
                g_display_emotion = cmd.emotion;
                g_display_needs_render = true;
            }
            // TODO: servo/vibration actions
        });

        // Set up audio handler: decode Opus → enqueue PCM → audio output task plays it.
        // This decouples WebSocket receive from blocking I2S writes.
        g_websocket.OnAudio([](const std::vector<uint8_t>& opus_frame) {
            static int cb_count = 0;
            cb_count++;
            g_audio_playing = true;

            // Log only first frame to reduce serial spam during playback
            if (cb_count == 1) {
                ESP_LOGI(TAG, "AUDIO START: %d bytes", (int)opus_frame.size());
            }

            std::vector<int16_t> pcm(AUDIO_FRAME_SAMPLES);
            if (g_audio_decoder.Decode(opus_frame, pcm)) {
                // Block enqueue with timeout: waits for queue space (at most
                // one frame duration = 60ms). This provides backpressure —
                // if I2S is behind, the WS callback slows down to match
                // real-time playback speed. No frames are dropped.
                if (g_audio_out_queue) {
                    xQueueSend(g_audio_out_queue, pcm.data(),
                               pdMS_TO_TICKS(60));
                }
            }
        });
    } else {
        ESP_LOGW(TAG, "WebSocket connection failed — offline mode");
    }

    // Set speaker volume
    if (auto* c = board.GetAudioCodec()) {
        c->SetOutputVolume(90);
    }

    // Status LED on = ready
    gpio_set_level(STATUS_LED_GPIO, 1);

    // Create encoder stream buffer with PSRAM storage (static allocation).
    // Using heap_caps_calloc in PSRAM avoids the 77KB internal DRAM requirement
    // that xStreamBufferCreate (pvPortMalloc → internal only) can't satisfy.
    g_encoder_stream_storage = (uint8_t*)heap_caps_calloc(1, ENCODER_STREAM_SIZE,
                                                          MALLOC_CAP_SPIRAM);
    if (g_encoder_stream_storage) {
        g_encoder_stream = xStreamBufferCreateStatic(
            ENCODER_STREAM_SIZE,
            AUDIO_FRAME_SAMPLES * sizeof(int16_t),
            g_encoder_stream_storage,
            &g_encoder_stream_struct);
    }

    if (g_encoder_stream == nullptr) {
        ESP_LOGE(TAG, "Encoder stream buffer creation FAILED (size=%u). "
                 "Internal DRAM may be too fragmented. "
                 "Free internal heap: %lu",
                 ENCODER_STREAM_SIZE, esp_get_free_internal_heap_size());
    } else {
        ESP_LOGI(TAG, "Encoder stream buffer OK: %u bytes in PSRAM",
                 ENCODER_STREAM_SIZE);
        xTaskCreate(EncoderTask, "encoder", 32768, nullptr, 5, nullptr);
    }

    // Create audio output queue (PSRAM storage) and task — decouples
    // WebSocket callback from blocking I2S writes to prevent audio stuttering.
    {
        size_t q_item_size = AUDIO_FRAME_SAMPLES * sizeof(int16_t);
        size_t q_storage = AUDIO_OUT_QUEUE_SIZE * q_item_size;
        g_audio_out_queue_storage = (uint8_t*)heap_caps_calloc(1, q_storage, MALLOC_CAP_SPIRAM);
        if (g_audio_out_queue_storage) {
            g_audio_out_queue = xQueueCreateStatic(
                AUDIO_OUT_QUEUE_SIZE, q_item_size,
                g_audio_out_queue_storage, &g_audio_out_queue_struct);
        }
        if (g_audio_out_queue) {
            // Priority 8 = same as WS internal task.  Without this, the WS
            // callback (pri 8) starves this task (pri 4) during audio bursts,
            // the queue fills but nobody writes I2S → DMA underrun → stutter.
            // Equal priority means round-robin scheduling in FreeRTOS.
            xTaskCreate(AudioOutputTask, "audio_out", 4096, nullptr, 8, nullptr);
            ESP_LOGI(TAG, "Audio output queue OK: %d frames (%d ms buffering) in PSRAM",
                     AUDIO_OUT_QUEUE_SIZE, AUDIO_OUT_QUEUE_SIZE * 60);
        } else {
            ESP_LOGE(TAG, "Audio output queue creation FAILED (size=%u)", q_storage);
        }
    }

    // Main loop
    ESP_LOGI(TAG, "Entering main loop...");
    int64_t last_sensor_send = 0;
    int loop_count = 0;
    int audio_sent_count = 0;

    while (true) {
        int64_t now = GetTimestampMs();
        loop_count++;

        // Log heartbeat every 5 seconds
        if (loop_count % 500 == 0) {
            ESP_LOGI(TAG, "Heartbeat: loop=%d ws=%d audio=%d heap=%lu",
                     loop_count, g_websocket.IsConnected() ? 1 : 0,
                     audio_sent_count, esp_get_free_heap_size());
        }

        // Send sensor data at 2Hz (every 500ms) — skip during audio playback
        if (now - last_sensor_send >= 500 && !g_audio_playing) {
            SensorPacket sensor = {};
            sensor.ts = now;
            ReadTouchSensors(sensor.touch_head, sensor.touch_back, sensor.touch_belly);
            sensor.battery_pct = 100; // TODO: read battery ADC

            if (g_websocket.IsConnected()) {
                g_websocket.SendSensorData(sensor);
            }
            last_sensor_send = now;
        }

        // Clear audio playing flag after 800ms of silence
        if (g_audio_playing && (now - g_last_audio_ms > 800)) {
            g_audio_playing = false;
        }

        // Mic capture → stream to encoder task (skip during TTS playback)
        auto* codec = board.GetAudioCodec();
        if (codec && codec->input_enabled() && !g_audio_playing) {
            std::vector<int16_t> pcm(AUDIO_FRAME_SAMPLES, 0);
            int read = codec->Read(pcm.data(), AUDIO_FRAME_SAMPLES);
            if (read == AUDIO_FRAME_SAMPLES && g_encoder_stream != nullptr) {
                // Send raw PCM bytes to encoder task via stream buffer (zero heap alloc)
                xStreamBufferSend(g_encoder_stream,
                                  pcm.data(), AUDIO_FRAME_SAMPLES * sizeof(int16_t),
                                  0);
            }
        }

        // Process incoming WebSocket messages
        g_websocket.ProcessIncoming(50);

        // Deferred display render — update kawaii_face emotion
        if (g_display_needs_render) {
            auto& eye_disp = LvglEyeDisplay::GetInstance();
            eye_disp.SetEmotion(g_display_emotion);
            g_display_needs_render = false;
        }

        // Log disconnection events
        static bool was_connected = false;
        bool is_connected = g_websocket.IsConnected();
        if (was_connected && !is_connected) {
            ESP_LOGW(TAG, "!!! WebSocket DISCONNECTED !!! heap=%lu", esp_get_free_heap_size());
        }
        was_connected = is_connected;

        // Reconnection — with 5s cooldown to avoid flooding
        static int64_t last_reconnect_attempt = 0;
        if (!is_connected && wifi.IsConnected() && (now - last_reconnect_attempt > 5000)) {
            last_reconnect_attempt = now;
            ESP_LOGI(TAG, "Attempting reconnection...");
            if (g_websocket.Connect(SERVER_WEBSOCKET_URL)) {
                ESP_LOGI(TAG, "Reconnected successfully");
                was_connected = true;
            }
        }

        // BOOT button long-press (3s) → reset WiFi and reboot into provisioning
        // Uses elapsed time (not counter) so it survives blocking calls like WebSocket Connect
        static int64_t boot_press_start = 0;
        bool boot_pressed = (gpio_get_level(BOOT_BUTTON_GPIO) == 0);
        if (boot_pressed && boot_press_start == 0) {
            boot_press_start = now;  // press started
        } else if (boot_pressed && (now - boot_press_start > 3000)) {
            ESP_LOGI(TAG, "BOOT button held 3s — resetting WiFi");
            auto& eye_disp = LvglEyeDisplay::GetInstance();
            eye_disp.ShowWifiInfo("Resetting WiFi...", "Restarting in AP mode");
            LvglEyeDisplay::RunLvgl();
            vTaskDelay(pdMS_TO_TICKS(500));
            WifiProvisioning::GetInstance().ResetToProvisioning();
        } else if (!boot_pressed) {
            boot_press_start = 0;  // released
        }

        // Drive LVGL timers and rendering
        LvglEyeDisplay::RunLvgl();

        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
