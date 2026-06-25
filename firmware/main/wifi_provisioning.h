#ifndef WIFI_PROVISIONING_H
#define WIFI_PROVISIONING_H

#include <string>
#include <functional>

/// WiFi provisioning wrapper around 78/esp-wifi-connect component.
///
/// Flow:
///   Start() → check NVS for saved credentials
///     ├── found → STA mode, try 60s to connect
///     │            ├── success → return (IsConnected() == true)
///     │            └── timeout → EnterProvisioningMode() → loop forever → reboot
///     └── none  → EnterProvisioningMode() → loop forever → reboot
///
/// Provisioning mode creates a SoftAP "AI-Cat-XXXX" with a captive portal.
/// User connects phone to the hotspot, browser opens WiFi config page,
/// selects WiFi + password, submits → device saves to NVS and reboots.
class WifiProvisioning {
public:
    static WifiProvisioning& GetInstance();

    /// Start WiFi: try saved networks first, fall back to provisioning.
    /// Returns only when connected; provisioning mode loops until reboot.
    void Start();

    /// Force-enter provisioning mode on next boot, then reboot.
    void ResetToProvisioning();

    bool IsConnected();
    std::string GetIPAddress();

    /// Callbacks for UI updates during scanning/connecting.
    using StatusCallback = std::function<void(const std::string&)>;
    void OnStatusChange(StatusCallback cb);

private:
    WifiProvisioning() = default;
    WifiProvisioning(const WifiProvisioning&) = delete;
    WifiProvisioning& operator=(const WifiProvisioning&) = delete;

    void EnterProvisioningMode();
    bool HasSavedNetworks();
    void ClearSavedNetworks();

    bool connected_ = false;
    StatusCallback status_cb_;
};

#endif // WIFI_PROVISIONING_H
