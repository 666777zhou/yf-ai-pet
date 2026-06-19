#include <esp_log.h>
#include <esp_err.h>
#include <esp_heap_caps.h>
#include <nvs_flash.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
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

static int64_t GetTimestampMs() {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

static void ReadTouchSensors(int& head, int& back, int& belly) {
    // Read capacitive touch values via ADC or touch pad API
    // For now, use GPIO digital read as placeholder
    // TODO: replace with touch_pad_read() when sensors are installed
    head  = gpio_get_level(TOUCH_HEAD_GPIO) ? 0 : 100;
    back  = gpio_get_level(TOUCH_BACK_GPIO) ? 0 : 100;
    belly = gpio_get_level(TOUCH_BELLY_GPIO) ? 0 : 100;
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
                // TODO: execute servo/vibration/LED actions
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

        // Mic capture → send raw PCM to server (skip during TTS playback)
        auto* codec = board.GetAudioCodec();
        if (codec && codec->input_enabled() && !g_audio_playing) {
            static int pcm_frame_count = 0;
            std::vector<int16_t> pcm(AUDIO_FRAME_SAMPLES, 0);
            int read = codec->Read(pcm.data(), AUDIO_FRAME_SAMPLES);
            if (read == AUDIO_FRAME_SAMPLES) {
                pcm_frame_count++;
                // Send raw PCM as binary frame
                const uint8_t* raw = reinterpret_cast<const uint8_t*>(pcm.data());
                std::vector<uint8_t> pcm_bytes(raw, raw + AUDIO_FRAME_SAMPLES * 2);
                if (g_websocket.IsConnected()) {
                    g_websocket.SendAudio(pcm_bytes);
                    audio_sent_count++;
                }
                if (pcm_frame_count == 1) {
                    ESP_LOGI(TAG, "PCM send OK: %d bytes/frame", (int)pcm_bytes.size());
                }
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
