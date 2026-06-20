#ifndef DISPLAY_H
#define DISPLAY_H

#include <cstdint>
#include <string>
#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_panel_vendor.h>

// Display: ST7789 320×240 SPI, renders cat eyes with emotion-based expressions.
// No LVGL — direct framebuffer drawing for minimal footprint.

constexpr int DISPLAY_WIDTH = 320;
constexpr int DISPLAY_HEIGHT = 240;

// SPI pins (same as LiChuang Dev board — verified working)
constexpr int DISPLAY_MOSI_PIN = 40;
constexpr int DISPLAY_SCLK_PIN = 41;
constexpr int DISPLAY_DC_PIN   = 39;
constexpr int DISPLAY_CS_PIN   = -1;   // NC
constexpr int DISPLAY_RST_PIN  = -1;   // NC
constexpr int DISPLAY_BL_PIN   = 42;   // backlight (inverted)

enum class EyeState {
    NORMAL,     // round eyes, pupils centered — content
    HAPPY,      // curved happy eyes — playful, happy
    SLEEPY,     // half-closed — sleepy
    ANNOYED,    // narrowed — annoyed
    SURPRISED,  // wide open — surprised
};

class EyeDisplay {
public:
    EyeDisplay();
    ~EyeDisplay();

    bool Initialize();
    void SetEmotion(const std::string& emotion);
    void Render();
    void SetBacklight(bool on);

private:
    void FillRect(int x, int y, int w, int h, uint16_t color);
    void FillCircle(int cx, int cy, int r, uint16_t color);
    void DrawCircle(int cx, int cy, int r, uint16_t color);
    void FillEllipse(int cx, int cy, int rx, int ry, uint16_t color);

    void Clear(uint16_t color = 0x0000);
    void Flush();

    // Eye rendering helpers
    void DrawEye(int cx, int cy, int radius, EyeState state, bool left_eye);
    void DrawNormalEye(int cx, int cy, int r);
    void DrawHappyEye(int cx, int cy, int r);
    void DrawSleepyEye(int cx, int cy, int r);
    void DrawAnnoyedEye(int cx, int cy, int r);
    void DrawSurprisedEye(int cx, int cy, int r);

    esp_lcd_panel_io_handle_t io_ = nullptr;
    esp_lcd_panel_handle_t panel_ = nullptr;
    uint16_t* fb_ = nullptr;          // framebuffer in PSRAM
    EyeState state_ = EyeState::NORMAL;
    bool initialized_ = false;
};

// Map emotion string to EyeState
EyeState EmotionToEyeState(const std::string& emotion);

#endif // DISPLAY_H
