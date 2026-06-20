#include <esp_log.h>
#include <esp_err.h>
#include <esp_heap_caps.h>
#include <nvs_flash.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/stream_buffer.h>
#include <driver/gpio.h>
#include <sys/time.h>

#include "ai_cat_board.h"
#include "wifi_manager.h"
#include "cat_websocket.h"
#include "audio_pipeline.h"

#define TAG "main"

static CatWebSocket g_websocket;
static AudioEncoder g_audio_encoder;
static AudioDecoder g_audio_decoder;
static volatile bool g_audio_playing = false;
static volatile int64_t g_last_audio_ms = 0;

// Stream buffer for PCM frames: main loop → encoder task.
// 40 frames × 1920 bytes gives ~2.4s of buffering headroom.
#define ENCODER_STREAM_SIZE (AUDIO_FRAME_SAMPLES * sizeof(int16_t) * 40)
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

    // Initialize board (I2C, audio, GPIO)
    auto& board = AiCatBoard::GetInstance();
    if (!board.Initialize()) {
        ESP_LOGE(TAG, "Board initialization failed");
        return;
    }

    // Connect WiFi
    auto& wifi = WifiManager::GetInstance();
    if (!wifi.Connect(WIFI_SSID, WIFI_PASSWORD)) {
        ESP_LOGE(TAG, "WiFi connection failed — entering offline mode");
        // TODO: enter offline cat behavior mode
    }

    // Connect WebSocket to server
    if (wifi.IsConnected()) {
        if (g_websocket.Connect(SERVER_WEBSOCKET_URL)) {
            ESP_LOGI(TAG, "Connected to AI Cat server");

            // Set up command handler
            g_websocket.OnCommand([](const CommandPacket& cmd) {
                ESP_LOGI(TAG, "Command: emotion=%s ears=(%d,%d) vib=%d audio=%d",
                         cmd.emotion.c_str(), cmd.ear_left_deg, cmd.ear_right_deg,
                         cmd.vibration, cmd.has_audio);
                // Update eye display based on emotion
                auto* display = AiCatBoard::GetInstance().GetDisplay();
                if (display) {
                    display->SetEmotion(cmd.emotion);
                    display->Render();
                }
                // TODO: execute servo/vibration actions
            });

            // Set up audio handler: decode Opus → PCM → play through speaker
            g_websocket.OnAudio([](const std::vector<uint8_t>& opus_frame) {
                static int cb_count = 0;
                cb_count++;
                g_audio_playing = true;
                g_last_audio_ms = GetTimestampMs();

                // Log only first frame to reduce serial spam during playback
                if (cb_count == 1) {
                    ESP_LOGI(TAG, "AUDIO START: %d bytes", (int)opus_frame.size());
                }

                std::vector<int16_t> pcm(AUDIO_FRAME_SAMPLES);
                if (g_audio_decoder.Decode(opus_frame, pcm)) {
                    auto* codec = AiCatBoard::GetInstance().GetAudioCodec();
                    if (codec && codec->output_enabled()) {
                        codec->OutputData(pcm);
                    }
                }
            });
        } else {
            ESP_LOGW(TAG, "WebSocket connection failed — offline mode");
        }
    }

    // Set speaker volume
    if (auto* c = board.GetAudioCodec()) {
        c->SetOutputVolume(90);
    }

    // Status LED on = ready
    gpio_set_level(STATUS_LED_GPIO, 1);

    // Create encoder stream buffer (wake encoder only when full frame arrives)
    g_encoder_stream = xStreamBufferCreate(ENCODER_STREAM_SIZE,
                                           AUDIO_FRAME_SAMPLES * sizeof(int16_t));
    xTaskCreate(EncoderTask, "encoder", 32768, nullptr, 5, nullptr);

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

        // Send sensor data at 10Hz (skip during audio playback to avoid lock contention)
        if (now - last_sensor_send >= 100 && !g_audio_playing) {
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
            if (read == AUDIO_FRAME_SAMPLES) {
                // Send raw PCM bytes to encoder task via stream buffer (zero heap alloc)
                xStreamBufferSend(g_encoder_stream,
                                  pcm.data(), AUDIO_FRAME_SAMPLES * sizeof(int16_t),
                                  0);
            }
        }

        // Process incoming WebSocket messages
        g_websocket.ProcessIncoming(50);

        // Log disconnection events
        static bool was_connected = false;
        bool is_connected = g_websocket.IsConnected();
        if (was_connected && !is_connected) {
            ESP_LOGW(TAG, "!!! WebSocket DISCONNECTED !!! heap=%lu", esp_get_free_heap_size());
        }
        was_connected = is_connected;

        // Reconnection — but don't block! Just try once
        if (!is_connected && wifi.IsConnected()) {
            ESP_LOGI(TAG, "Attempting reconnection...");
            if (g_websocket.Connect(SERVER_WEBSOCKET_URL)) {
                ESP_LOGI(TAG, "Reconnected successfully");
                was_connected = true;
            }
        }

        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
