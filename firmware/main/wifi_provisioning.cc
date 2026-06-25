#include "wifi_provisioning.h"

#include <esp_log.h>
#include <esp_wifi.h>
#include <esp_netif.h>
#include <esp_event.h>
#include <nvs_flash.h>
#include <nvs.h>

#include <wifi_station.h>
#include <wifi_configuration_ap.h>
#include <ssid_manager.h>

#define TAG "WifiProvisioning"

WifiProvisioning& WifiProvisioning::GetInstance() {
    static WifiProvisioning instance;
    return instance;
}

// ---- Public API ----

void WifiProvisioning::Start() {
    // One-time init: network stack must be up before WiFi operations
    static bool netif_inited = false;
    if (!netif_inited) {
        ESP_ERROR_CHECK(esp_netif_init());
        ESP_ERROR_CHECK(esp_event_loop_create_default());
        netif_inited = true;
    }

    // Check if force_ap flag is set (from ResetToProvisioning)
    bool force_ap = false;
    {
        nvs_handle_t nvs;
        if (nvs_open("wifi", NVS_READWRITE, &nvs) == ESP_OK) {
            uint8_t val = 0;
            if (nvs_get_u8(nvs, "force_ap", &val) == ESP_OK && val == 1) {
                force_ap = true;
                nvs_set_u8(nvs, "force_ap", 0);  // clear flag (one-shot)
                nvs_commit(nvs);
                ESP_LOGI(TAG, "force_ap flag detected — entering provisioning");
            }
            nvs_close(nvs);
        }
    }

    if (force_ap) {
        EnterProvisioningMode();
        // EnterProvisioningMode never returns (loops until reboot)
        return;
    }

    // Check for saved networks
    auto& ssid_mgr = SsidManager::GetInstance();
    auto ssid_list = ssid_mgr.GetSsidList();
    if (ssid_list.empty()) {
        ESP_LOGI(TAG, "No saved WiFi networks — entering provisioning");
        EnterProvisioningMode();
        return;
    }

    ESP_LOGI(TAG, "Found %d saved network(s), trying to connect...", (int)ssid_list.size());

    // Try STA mode with saved networks
    auto& station = WifiStation::GetInstance();
    if (status_cb_) {
        station.OnScanBegin([this]() {
            status_cb_("Scanning WiFi...");
        });
        station.OnConnect([this](const std::string& ssid) {
            status_cb_("Connecting: " + ssid);
        });
        station.OnConnected([this](const std::string& ssid) {
            status_cb_("Connected: " + ssid);
        });
    }

    station.Start();
    if (station.WaitForConnected(60 * 1000)) {
        connected_ = true;
        ESP_LOGI(TAG, "WiFi connected: %s, IP: %s",
                 station.GetSsid().c_str(), station.GetIpAddress().c_str());
        return;
    }

    // Failed to connect with saved networks
    ESP_LOGW(TAG, "Could not connect to any saved network — entering provisioning");
    station.Stop();
    EnterProvisioningMode();
}

void WifiProvisioning::ResetToProvisioning() {
    ESP_LOGI(TAG, "Resetting WiFi — force provisioning on next boot");
    nvs_handle_t nvs;
    if (nvs_open("wifi", NVS_READWRITE, &nvs) == ESP_OK) {
        nvs_set_u8(nvs, "force_ap", 1);
        nvs_commit(nvs);
        nvs_close(nvs);
    }
    // Small delay so log output can flush
    vTaskDelay(pdMS_TO_TICKS(500));
    esp_restart();
}

bool WifiProvisioning::IsConnected() {
    if (!connected_) return false;
    // Double-check with the station
    auto& station = WifiStation::GetInstance();
    return station.IsConnected();
}

std::string WifiProvisioning::GetIPAddress() {
    if (!connected_) return "0.0.0.0";
    auto& station = WifiStation::GetInstance();
    return station.GetIpAddress();
}

void WifiProvisioning::OnStatusChange(StatusCallback cb) {
    status_cb_ = std::move(cb);
}

// ---- Private ----

void WifiProvisioning::EnterProvisioningMode() {
    ESP_LOGI(TAG, "=== WiFi Provisioning Mode ===");

    auto& ap = WifiConfigurationAp::GetInstance();
    ap.SetLanguage("zh");
    ap.SetSsidPrefix("AI-Cat");
    ap.Start();

    std::string ssid = ap.GetSsid();
    std::string url = ap.GetWebServerUrl();

    ESP_LOGI(TAG, "SoftAP SSID: %s", ssid.c_str());
    ESP_LOGI(TAG, "Config URL:  %s", url.c_str());

    if (status_cb_) {
        std::string msg = "WiFi: " + ssid + "\n" + url;
        status_cb_(msg);
    }

    // Wait forever — user configures via web portal → device reboots
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(10000));
    }
}

bool WifiProvisioning::HasSavedNetworks() {
    auto& ssid_mgr = SsidManager::GetInstance();
    return !ssid_mgr.GetSsidList().empty();
}

void WifiProvisioning::ClearSavedNetworks() {
    auto& ssid_mgr = SsidManager::GetInstance();
    ssid_mgr.Clear();
}
