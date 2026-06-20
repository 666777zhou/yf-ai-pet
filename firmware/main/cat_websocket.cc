#include "cat_websocket.h"

#include <esp_log.h>
#include <esp_websocket_client.h>
#include <esp_event.h>
#include <cJSON.h>
#include <cstring>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#define TAG "CatWebSocket"

// Event group bits
#define WS_CONNECTED_BIT BIT0
#define WS_DATA_READY_BIT BIT1

// Global pointer for event handler (simplified: single instance)
static CatWebSocket* g_ws_instance = nullptr;

// Message types for internal queue
struct WsMessage {
    bool is_binary;
    std::vector<uint8_t> data;
};

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
                std::string json((const char*)data->data_ptr, data->data_len);
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
    , connected_(false) {
    g_ws_instance = this;
}

CatWebSocket::~CatWebSocket() {
    Disconnect();
    g_ws_instance = nullptr;
}

bool CatWebSocket::Connect(const std::string& url) {
    esp_websocket_client_config_t ws_cfg = {};
    ws_cfg.uri = url.c_str();
    ws_cfg.disable_auto_reconnect = false;
    ws_cfg.reconnect_timeout_ms = 5000;
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
    // Receive buffer: 4KB default may be too small for TTS audio frames
    ws_cfg.buffer_size = 16384;
    // Internal task: priority 8, stack 16KB (Opus decode + I2S write need headroom)
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

    // Wait for connection (max 10 seconds)
    int retries = 0;
    while (!esp_websocket_client_is_connected(client) && retries < 50) {
        vTaskDelay(pdMS_TO_TICKS(200));
        retries++;
    }

    if (!esp_websocket_client_is_connected(client)) {
        ESP_LOGE(TAG, "WebSocket connection timeout");
        esp_websocket_client_stop(client);
        esp_websocket_client_destroy(client);
        return false;
    }

    ws_handle_ = client;
    connected_ = true;
    ESP_LOGI(TAG, "WebSocket connected to %s", url.c_str());
    return true;
}

void CatWebSocket::Disconnect() {
    if (ws_handle_) {
        auto client = static_cast<esp_websocket_client_handle_t>(ws_handle_);
        esp_websocket_client_stop(client);
        esp_websocket_client_destroy(client);
        ws_handle_ = nullptr;
    }
    connected_ = false;
}

bool CatWebSocket::IsConnected() {
    if (ws_handle_) {
        auto client = static_cast<esp_websocket_client_handle_t>(ws_handle_);
        connected_ = esp_websocket_client_is_connected(client);
    }
    return connected_;
}

void CatWebSocket::SendSensorData(const SensorPacket& sensor) {
    if (!IsConnected()) return;
    auto client = static_cast<esp_websocket_client_handle_t>(ws_handle_);

    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "sensors");
    cJSON_AddNumberToObject(root, "ts", sensor.ts);
    cJSON_AddNumberToObject(root, "touch_head", sensor.touch_head);
    cJSON_AddNumberToObject(root, "touch_back", sensor.touch_back);
    cJSON_AddNumberToObject(root, "touch_belly", sensor.touch_belly);
    cJSON_AddNumberToObject(root, "battery_pct", sensor.battery_pct);

    char* str = cJSON_PrintUnformatted(root);
    esp_websocket_client_send_text(client, str, strlen(str), pdMS_TO_TICKS(500));
    cJSON_free(str);
    cJSON_Delete(root);
}

void CatWebSocket::SendAudio(const std::vector<uint8_t>& opus_frame) {
    if (!IsConnected()) return;
    auto client = static_cast<esp_websocket_client_handle_t>(ws_handle_);
    esp_websocket_client_send_bin(client, (const char*)opus_frame.data(),
                                   opus_frame.size(), pdMS_TO_TICKS(500));
}

void CatWebSocket::OnCommand(CommandCallback cb) {
    command_cb_ = std::move(cb);
}

void CatWebSocket::OnAudio(AudioCallback cb) {
    audio_cb_ = std::move(cb);
}

void CatWebSocket::ProcessIncoming(int timeout_ms) {
    // Event callbacks handle all incoming data
    // This function just checks connection status
    if (ws_handle_) {
        auto client = static_cast<esp_websocket_client_handle_t>(ws_handle_);
        connected_ = esp_websocket_client_is_connected(client);
    }
    vTaskDelay(pdMS_TO_TICKS(timeout_ms > 0 ? timeout_ms : 10));
}
