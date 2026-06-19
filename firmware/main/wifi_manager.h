#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include <string>

// Simple WiFi manager: connect to a hardcoded SSID/password
// No AP config mode, no OTA, no multi-network management

class WifiManager {
public:
    static WifiManager& GetInstance();

    bool Connect(const char* ssid, const char* password);
    bool IsConnected();
    std::string GetIPAddress();
    void WaitForConnection(int timeout_seconds = 30);

private:
    WifiManager() = default;
    void InitNvs();

    bool connected_ = false;
};

#endif // WIFI_MANAGER_H
