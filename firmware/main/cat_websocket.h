#ifndef CAT_WEBSOCKET_H
#define CAT_WEBSOCKET_H

#include <string>
#include <functional>
#include <vector>
#include <queue>
#include <cstdint>
#include <esp_event.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>

// Simplified WebSocket protocol for AI Cat
// JSON commands + raw Opus audio (no binary header, no OTA, no auth)

struct SensorPacket {
    int64_t ts;
    int touch_head;
    int touch_back;
    int touch_belly;
    int battery_pct;
};

struct CommandPacket {
    std::string emotion;
    int ear_left_deg;       // 0=flat, 90=upright
    int ear_right_deg;
    int vibration;          // 0-255 PWM
    bool has_audio;         // true if audio data follows
};

class CatWebSocket {
public:
    CatWebSocket();
    ~CatWebSocket();

    using CommandCallback = std::function<void(const CommandPacket&)>;
    using AudioCallback = std::function<void(const std::vector<uint8_t>&)>;

    bool Connect(const std::string& url);
    void Disconnect();
    bool IsConnected();

    // Send sensor data (JSON) — non-blocking, pushes to sender queue
    void SendSensorData(const SensorPacket& sensor);

    // Send audio frame (raw Opus bytes) — non-blocking, pushes to sender queue
    void SendAudio(const std::vector<uint8_t>& opus_frame);

    // Set callbacks for incoming data
    void OnCommand(CommandCallback cb);
    void OnAudio(AudioCallback cb);

    // Process incoming messages (call from main loop)
    void ProcessIncoming(int timeout_ms = 10);

private:
    // Event handler needs access to private members
    friend void WsEventHandler(void* handler_args, esp_event_base_t base,
                               int32_t event_id, void* event_data);

    // Dedicated sender task: pops from queue, sends with portMAX_DELAY
    // to avoid transport_poll_write timeout → abort_connection.
    struct SendItem {
        bool is_binary;
        std::vector<uint8_t> data;
    };

    static void SenderTaskFn(void* arg);
    void SenderLoop();

    void* ws_handle_;  // opaque WebSocket handle
    bool connected_;

    // Sender queue + task
    std::queue<SendItem> send_queue_;
    SemaphoreHandle_t send_mutex_;    // protects send_queue_
    SemaphoreHandle_t send_signal_;   // wakes sender task
    TaskHandle_t sender_task_;
    volatile bool sender_stop_;

    CommandCallback command_cb_;
    AudioCallback audio_cb_;
};

#endif // CAT_WEBSOCKET_H
