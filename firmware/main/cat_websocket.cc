#include "cat_websocket.h"

#include <esp_log.h>
#include <esp_websocket_client.h>
#include <esp_crt_bundle.h>
#include <esp_event.h>
#include <cJSON.h>
#include <cstring>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#define TAG "CatWebSocket"

// Global pointer for event handler (simplified: single instance)
static CatWebSocket* g_ws_instance = nullptr;

void WsEventHandler(void* handler_args, esp_event_base_t base,
                            int32_t event_id, void* event_data) {
    auto* data = static_cast<esp_websocket_event_data_t*>(event_data);
    auto* self = static_cast<CatWebSocket*>(handler_args);

    switch (event_id) {
        case WEBSOCKET_EVENT_CONNECTED:
            ESP_LOGI(TAG, "WebSocket connected");
            self->connected_ = true;
            break;

        case WEBSOCKET_EVENT_DISCONNECTED:
            ESP_LOGW(TAG, "WebSocket disconnected");
            self->connected_ = false;
            break;

        case WEBSOCKET_EVENT_DATA: {
            static int data_count = 0;
            data_count++;
            if (data->op_code == 0x01) {
                // Text frame: JSON command
                ESP_LOGI(TAG, "WS recv#%d: TEXT %d bytes", data_count, data->data_len);
                CommandPacket cmd = {};

                cJSON* root = cJSON_ParseWithLength(data->data_ptr, data->data_len);
                if (root) {
                    cJSON* type = cJSON_GetObjectItem(root, "type");
                    if (type && cJSON_IsString(type) && strcmp(type->valuestring, "command") == 0) {
                        cJSON* emotion = cJSON_GetObjectItem(root, "emotion");
                        cJSON* ear_l = cJSON_GetObjectItem(root, "ear_left_deg");
                        cJSON* ear_r = cJSON_GetObjectItem(root, "ear_right_deg");
                        cJSON* vib = cJSON_GetObjectItem(root, "vibration");
                        cJSON* has_audio = cJSON_GetObjectItem(root, "has_audio");

                        if (emotion && cJSON_IsString(emotion)) cmd.emotion = emotion->valuestring;
                        if (ear_l && cJSON_IsNumber(ear_l)) cmd.ear_left_deg = ear_l->valueint;
                        if (ear_r && cJSON_IsNumber(ear_r)) cmd.ear_right_deg = ear_r->valueint;
                        if (vib && cJSON_IsNumber(vib)) cmd.vibration = vib->valueint;
                        if (has_audio && cJSON_IsNumber(has_audio)) cmd.has_audio = (has_audio->valueint == 1);
                    }
                    cJSON_Delete(root);

                    if (self->command_cb_) {
                        self->command_cb_(cmd);
                    }
                }
            } else if (data->op_code == 0x02) {
                // Binary frame: Opus audio — only log first and every 20th
                static int bin_count = 0;
                bin_count++;
                if (bin_count <= 3 || bin_count % 20 == 0) {
                    ESP_LOGI(TAG, "BIN#%d: %d bytes (total recv=%d)",
                             bin_count, data->data_len, data_count);
                }
                std::vector<uint8_t> audio(data->data_ptr, data->data_ptr + data->data_len);
                if (self->audio_cb_) {
                    self->audio_cb_(audio);
                }
            } else {
                ESP_LOGW(TAG, "WS recv#%d: opcode=%02x len=%d", data_count, data->op_code, data->data_len);
            }
            break;
        }

        case WEBSOCKET_EVENT_ERROR:
            ESP_LOGE(TAG, "WebSocket error");
            break;
    }
}

CatWebSocket::CatWebSocket()
    : ws_handle_(nullptr)
    , connected_(false)
    , send_mutex_(nullptr)
    , send_signal_(nullptr)
    , sender_task_(nullptr)
    , sender_stop_(false) {
    g_ws_instance = this;
}

CatWebSocket::~CatWebSocket() {
    sender_stop_ = true;
    if (send_signal_) {
        xSemaphoreGive(send_signal_);  // wake sender task so it can exit
    }
    // Give sender task time to exit
    vTaskDelay(pdMS_TO_TICKS(200));
    Disconnect();
    if (send_mutex_) {
        vSemaphoreDelete(send_mutex_);
    }
    if (send_signal_) {
        vSemaphoreDelete(send_signal_);
    }
    g_ws_instance = nullptr;
}

bool CatWebSocket::Connect(const std::string& url) {
    // Clean up any previous connection before starting a new one
    Disconnect();

    esp_websocket_client_config_t ws_cfg = {};
    ws_cfg.uri = url.c_str();
    // Use ESP-IDF built-in Mozilla CA bundle for TLS verification
    if (url.find("wss://") == 0) {
        ws_cfg.crt_bundle_attach = esp_crt_bundle_attach;
    }
    ws_cfg.disable_auto_reconnect = true;
    ws_cfg.reconnect_timeout_ms = 0;
    ws_cfg.network_timeout_ms = 10000;
    // WebSocket keepalive: send ping every 10s, wait 30s for pong
    ws_cfg.ping_interval_sec = 10;
    ws_cfg.pingpong_timeout_sec = 30;
    ws_cfg.disable_pingpong_discon = false;
    // TCP keepalive
    ws_cfg.keep_alive_enable = true;
    ws_cfg.keep_alive_idle = 5;
    ws_cfg.keep_alive_interval = 5;
    ws_cfg.keep_alive_count = 3;
    // Receive buffer
    ws_cfg.buffer_size = 16384;
    // Internal task
    ws_cfg.task_prio = 8;
    ws_cfg.task_stack = 16384;

    esp_websocket_client_handle_t client = esp_websocket_client_init(&ws_cfg);
    if (!client) {
        ESP_LOGE(TAG, "Failed to create WebSocket client");
        return false;
    }

    // Register event handler for all WebSocket events
    esp_websocket_register_events(client, WEBSOCKET_EVENT_ANY, WsEventHandler, this);

    esp_err_t err = esp_websocket_client_start(client);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start WebSocket client: %d", err);
        esp_websocket_client_destroy(client);
        return false;
    }

    // Wait for connection (max 10s: 100 retries × 100ms)
    int retries = 0;
    while (!esp_websocket_client_is_connected(client) && retries < 100) {
        vTaskDelay(pdMS_TO_TICKS(100));
        retries++;
    }

    if (!esp_websocket_client_is_connected(client)) {
        ESP_LOGW(TAG, "WebSocket connection timeout after 10s");
        esp_websocket_client_stop(client);
        esp_websocket_client_destroy(client);
        return false;
    }

    ws_handle_ = client;
    connected_ = true;

    // Create sender queue synchronization primitives
    if (!send_mutex_) {
        send_mutex_ = xSemaphoreCreateMutex();
    }
    if (!send_signal_) {
        send_signal_ = xSemaphoreCreateBinary();
    }

    // Start dedicated sender task (priority 3: below ws internal task at 8,
    // above main loop at 1, so it doesn't starve the main loop)
    sender_stop_ = false;
    if (xTaskCreate(SenderTaskFn, "ws_sender", 4096, this, 3, &sender_task_) != pdPASS) {
        ESP_LOGE(TAG, "Failed to create sender task");
    }

    ESP_LOGI(TAG, "WebSocket connected to %s", url.c_str());
    return true;
}

void CatWebSocket::Disconnect() {
    sender_stop_ = true;
    if (send_signal_) {
        xSemaphoreGive(send_signal_);  // wake sender task
    }
    // Wait briefly for sender task to drain
    vTaskDelay(pdMS_TO_TICKS(100));

    if (ws_handle_) {
        auto client = static_cast<esp_websocket_client_handle_t>(ws_handle_);
        esp_websocket_client_stop(client);
        esp_websocket_client_destroy(client);
        ws_handle_ = nullptr;
    }
    connected_ = false;

    // Drain any pending items in the send queue (they're stale)
    if (send_mutex_) {
        xSemaphoreTake(send_mutex_, portMAX_DELAY);
        while (!send_queue_.empty()) {
            send_queue_.pop();
        }
        xSemaphoreGive(send_mutex_);
    }
}

bool CatWebSocket::IsConnected() {
    if (ws_handle_) {
        auto client = static_cast<esp_websocket_client_handle_t>(ws_handle_);
        connected_ = esp_websocket_client_is_connected(client);
    }
    return connected_;
}

// ---- Sender task ----

void CatWebSocket::SenderTaskFn(void* arg) {
    auto* self = static_cast<CatWebSocket*>(arg);
    self->SenderLoop();
    vTaskDelete(nullptr);
}

void CatWebSocket::SenderLoop() {
    while (!sender_stop_) {
        // Wait for a signal that data is available (with 1s timeout so we can
        // check the stop flag periodically)
        if (xSemaphoreTake(send_signal_, pdMS_TO_TICKS(1000)) != pdTRUE) {
            continue;  // timeout — loop back to check stop flag
        }

        // Pop all available items from the queue
        while (!sender_stop_) {
            SendItem item;
            bool has_item = false;

            xSemaphoreTake(send_mutex_, portMAX_DELAY);
            if (!send_queue_.empty()) {
                item = std::move(send_queue_.front());
                send_queue_.pop();
                has_item = true;
            }
            xSemaphoreGive(send_mutex_);

            if (!has_item) break;  // queue drained

            if (!ws_handle_ || !connected_) {
                continue;  // connection gone, drop this item
            }

            auto client = static_cast<esp_websocket_client_handle_t>(ws_handle_);

            // Use portMAX_DELAY: block until socket is writable.
            // If the connection is dead, the internal ping/pong mechanism
            // will eventually abort it (pingpong_timeout_sec=30s), which
            // closes the transport and unblocks this call.
            int ret;
            if (item.is_binary) {
                ret = esp_websocket_client_send_bin(
                    client, (const char*)item.data.data(),
                    item.data.size(), portMAX_DELAY);
            } else {
                ret = esp_websocket_client_send_text(
                    client, (const char*)item.data.data(),
                    item.data.size(), portMAX_DELAY);
            }

            if (ret <= 0) {
                // Send failed — connection is dead or dying.
                // Don't flood logs; the event handler will handle cleanup.
                static int fail_count = 0;
                fail_count++;
                if (fail_count <= 3 || fail_count % 20 == 0) {
                    ESP_LOGW(TAG, "Sender: send failed (count=%d, ret=%d)", fail_count, ret);
                }
                // Drain remaining items on failure — they're all going to fail
                xSemaphoreTake(send_mutex_, portMAX_DELAY);
                while (!send_queue_.empty()) send_queue_.pop();
                xSemaphoreGive(send_mutex_);
                break;  // stop trying until next signal
            }
        }
    }
}

// ---- Enqueue helpers (non-blocking, safe to call from any task) ----

void CatWebSocket::SendSensorData(const SensorPacket& sensor) {
    if (!ws_handle_ || !connected_ || sender_stop_) return;

    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "sensors");
    cJSON_AddNumberToObject(root, "ts", sensor.ts);
    cJSON_AddNumberToObject(root, "touch_head", sensor.touch_head);
    cJSON_AddNumberToObject(root, "touch_back", sensor.touch_back);
    cJSON_AddNumberToObject(root, "touch_belly", sensor.touch_belly);
    cJSON_AddNumberToObject(root, "battery_pct", sensor.battery_pct);

    char* str = cJSON_PrintUnformatted(root);
    size_t len = strlen(str);

    SendItem item;
    item.is_binary = false;
    item.data.assign(reinterpret_cast<uint8_t*>(str),
                     reinterpret_cast<uint8_t*>(str) + len);

    cJSON_free(str);
    cJSON_Delete(root);

    xSemaphoreTake(send_mutex_, portMAX_DELAY);
    send_queue_.push(std::move(item));
    xSemaphoreGive(send_mutex_);
    xSemaphoreGive(send_signal_);  // wake sender task
}

void CatWebSocket::SendAudio(const std::vector<uint8_t>& opus_frame) {
    if (!ws_handle_ || !connected_ || sender_stop_) return;

    SendItem item;
    item.is_binary = true;
    item.data = opus_frame;  // copy

    xSemaphoreTake(send_mutex_, portMAX_DELAY);
    send_queue_.push(std::move(item));
    xSemaphoreGive(send_mutex_);
    xSemaphoreGive(send_signal_);  // wake sender task
}

// ---- Callbacks ----

void CatWebSocket::OnCommand(CommandCallback cb) {
    command_cb_ = std::move(cb);
}

void CatWebSocket::OnAudio(AudioCallback cb) {
    audio_cb_ = std::move(cb);
}

void CatWebSocket::ProcessIncoming(int timeout_ms) {
    // Event callbacks handle all incoming data via the internal websocket task.
    // This function just checks connection status periodically.
    if (ws_handle_) {
        auto client = static_cast<esp_websocket_client_handle_t>(ws_handle_);
        connected_ = esp_websocket_client_is_connected(client);
    }
    vTaskDelay(pdMS_TO_TICKS(timeout_ms > 0 ? timeout_ms : 10));
}
