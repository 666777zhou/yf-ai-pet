#ifndef LVGL_DISPLAY_H
#define LVGL_DISPLAY_H

#include <cstdint>
#include <string>
#include <esp_lcd_panel_ops.h>
#include <esp_timer.h>
#include <lvgl.h>

// LVGL 9 + lvgl_kawaii_face — animated cat eyes on ST7789 320×240.
// Server emotion commands drive the face via face_set_emotion().
//
// Emotion mapping (server → kawaii_face):
//   content  → FACE_NEUTRAL   (relaxed)
//   sleepy   → FACE_SLEEPY    (half-closed eyes)
//   playful  → FACE_PLAYFUL   (tongue out, bouncing)
//   annoyed  → FACE_ANGRY     (furrowed brows)
//   scared   → FACE_SURPRISED (wide eyes, open mouth)
//   curious  → FACE_CONFUSED  (wandering pupils, asymmetric brows)

constexpr int LVGL_DISPLAY_WIDTH  = 320;
constexpr int LVGL_DISPLAY_HEIGHT = 240;

class LvglEyeDisplay {
public:
    static LvglEyeDisplay& GetInstance();

    /// Initialize LVGL 9 display driver and kawaii_face animation.
    bool Initialize(esp_lcd_panel_handle_t panel);

    /// Set emotion from server command string.
    void SetEmotion(const std::string& emotion);

    /// Drive LVGL timer handler — call from main loop periodically.
    static void RunLvgl();

private:
    LvglEyeDisplay();
    ~LvglEyeDisplay();

    /// LVGL 9 display flush callback — pushes pixels to ST7789 via SPI.
    static void FlushCallback(lv_display_t *disp, const lv_area_t *area,
                              uint8_t *px_map);

    /// 1ms tick callback (esp_timer) — feeds LVGL internal clock.
    static void TickCallback(void *arg);

    esp_lcd_panel_handle_t panel_ = nullptr;
    lv_display_t   *display_    = nullptr;
    lv_color_t     *draw_buf_1_ = nullptr;
    lv_color_t     *draw_buf_2_ = nullptr;
    esp_timer_handle_t tick_timer_ = nullptr;
    bool initialized_ = false;
};

#endif // LVGL_DISPLAY_H
