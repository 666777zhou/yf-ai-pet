#include "display.h"
#include <esp_log.h>
#include <esp_heap_caps.h>
#include <driver/spi_common.h>
#include <driver/gpio.h>
#include <cmath>
#include <algorithm>

#define TAG "Display"

// ---- RGB565 color helpers ----
static constexpr uint16_t COLOR_BG       = 0x0000;  // black
static constexpr uint16_t COLOR_WHITE    = 0xFFFF;  // white
static constexpr uint16_t COLOR_IRIS     = 0xED03;  // warm amber/gold
static constexpr uint16_t COLOR_PUPIL    = 0x18C3;  // dark blue-gray (cute)
static constexpr uint16_t COLOR_HIGHLIGHT = 0xFFFF; // white sparkle

static inline uint16_t RGB565(uint8_t r, uint8_t g, uint8_t b) {
    return ((uint16_t)(r >> 3) << 11) | ((uint16_t)(g >> 2) << 5) | (b >> 3);
}

EyeDisplay::EyeDisplay() = default;

EyeDisplay::~EyeDisplay() {
    if (fb_) {
        heap_caps_free(fb_);
    }
    if (panel_) {
        esp_lcd_panel_del(panel_);
    }
    if (io_) {
        esp_lcd_panel_io_del(io_);
    }
}

bool EyeDisplay::Initialize() {
    // ---- Backlight ----
    gpio_config_t bl_cfg = {};
    bl_cfg.pin_bit_mask = 1ULL << DISPLAY_BL_PIN;
    bl_cfg.mode = GPIO_MODE_OUTPUT;
    bl_cfg.pull_up_en = GPIO_PULLUP_DISABLE;
    bl_cfg.pull_down_en = GPIO_PULLDOWN_DISABLE;
    gpio_config(&bl_cfg);
    // Backlight inverted: 0 = on, 1 = off
    gpio_set_level((gpio_num_t)DISPLAY_BL_PIN, 0);  // off during init

    // ---- SPI bus ----
    spi_bus_config_t buscfg = {};
    buscfg.mosi_io_num = DISPLAY_MOSI_PIN;
    buscfg.miso_io_num = GPIO_NUM_NC;
    buscfg.sclk_io_num = DISPLAY_SCLK_PIN;
    buscfg.quadwp_io_num = GPIO_NUM_NC;
    buscfg.quadhd_io_num = GPIO_NUM_NC;
    buscfg.max_transfer_sz = DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t);
    ESP_ERROR_CHECK(spi_bus_initialize(SPI3_HOST, &buscfg, SPI_DMA_CH_AUTO));

    // ---- Panel IO ----
    esp_lcd_panel_io_spi_config_t io_config = {};
    io_config.cs_gpio_num = DISPLAY_CS_PIN;
    io_config.dc_gpio_num = DISPLAY_DC_PIN;
    io_config.spi_mode = 2;
    io_config.pclk_hz = 80 * 1000 * 1000;
    io_config.trans_queue_depth = 10;
    io_config.lcd_cmd_bits = 8;
    io_config.lcd_param_bits = 8;
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(SPI3_HOST, &io_config, &io_));

    // ---- Panel driver (ST7789) ----
    esp_lcd_panel_dev_config_t panel_config = {};
    panel_config.reset_gpio_num = DISPLAY_RST_PIN;
    panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
    panel_config.bits_per_pixel = 16;
    ESP_ERROR_CHECK(esp_lcd_new_panel_st7789(io_, &panel_config, &panel_));

    ESP_ERROR_CHECK(esp_lcd_panel_reset(panel_));
    ESP_ERROR_CHECK(esp_lcd_panel_init(panel_));
    ESP_ERROR_CHECK(esp_lcd_panel_invert_color(panel_, true));
    ESP_ERROR_CHECK(esp_lcd_panel_swap_xy(panel_, true));
    ESP_ERROR_CHECK(esp_lcd_panel_mirror(panel_, true, false));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel_, true));

    // ---- Framebuffer in PSRAM ----
    size_t fb_size = DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t);
    fb_ = (uint16_t*)heap_caps_malloc(fb_size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!fb_) {
        ESP_LOGE(TAG, "Failed to allocate framebuffer (%zu bytes)", fb_size);
        return false;
    }
    ESP_LOGI(TAG, "Framebuffer allocated: %zu bytes in PSRAM", fb_size);

    // Clear screen to black
    Clear();
    Flush();
    SetBacklight(true);

    initialized_ = true;
    ESP_LOGI(TAG, "Display initialized: ST7789 %dx%d SPI3", DISPLAY_WIDTH, DISPLAY_HEIGHT);
    return true;
}

void EyeDisplay::SetBacklight(bool on) {
    // Inverted: 0 = on, 1 = off
    gpio_set_level((gpio_num_t)DISPLAY_BL_PIN, on ? 0 : 1);
}

// ---- Drawing primitives ----

void EyeDisplay::FillRect(int x, int y, int w, int h, uint16_t color) {
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x + w > DISPLAY_WIDTH)  w = DISPLAY_WIDTH - x;
    if (y + h > DISPLAY_HEIGHT) h = DISPLAY_HEIGHT - y;
    if (w <= 0 || h <= 0) return;

    for (int row = 0; row < h; row++) {
        uint16_t* line = fb_ + (y + row) * DISPLAY_WIDTH + x;
        std::fill_n(line, w, color);
    }
}

void EyeDisplay::FillCircle(int cx, int cy, int r, uint16_t color) {
    for (int dy = -r; dy <= r; dy++) {
        int y = cy + dy;
        if (y < 0 || y >= DISPLAY_HEIGHT) continue;
        int dx = (int)std::sqrt((float)(r * r - dy * dy));
        int x0 = std::max(0, cx - dx);
        int x1 = std::min(DISPLAY_WIDTH - 1, cx + dx);
        if (x0 <= x1) {
            std::fill_n(fb_ + y * DISPLAY_WIDTH + x0, x1 - x0 + 1, color);
        }
    }
}

void EyeDisplay::DrawCircle(int cx, int cy, int r, uint16_t color) {
    // Bresenham circle outline
    int x = r, y = 0;
    int err = 0;
    while (x >= y) {
        auto plot = [&](int px, int py) {
            if (px >= 0 && px < DISPLAY_WIDTH && py >= 0 && py < DISPLAY_HEIGHT)
                fb_[py * DISPLAY_WIDTH + px] = color;
        };
        plot(cx + x, cy + y); plot(cx + y, cy + x);
        plot(cx - y, cy + x); plot(cx - x, cy + y);
        plot(cx - x, cy - y); plot(cx - y, cy - x);
        plot(cx + y, cy - x); plot(cx + x, cy - y);
        y++;
        err += 1 + 2 * y;
        if (2 * (err - x) + 1 > 0) {
            x--;
            err += 1 - 2 * x;
        }
    }
}

void EyeDisplay::FillEllipse(int cx, int cy, int rx, int ry, uint16_t color) {
    for (int dy = -ry; dy <= ry; dy++) {
        int y = cy + dy;
        if (y < 0 || y >= DISPLAY_HEIGHT) continue;
        float t = (float)dy / ry;
        int dx = (int)(rx * std::sqrt(1.0f - t * t));
        int x0 = std::max(0, cx - dx);
        int x1 = std::min(DISPLAY_WIDTH - 1, cx + dx);
        if (x0 <= x1) {
            std::fill_n(fb_ + y * DISPLAY_WIDTH + x0, x1 - x0 + 1, color);
        }
    }
}

void EyeDisplay::Clear(uint16_t color) {
    std::fill_n(fb_, DISPLAY_WIDTH * DISPLAY_HEIGHT, color);
}

void EyeDisplay::Flush() {
    esp_lcd_panel_draw_bitmap(panel_, 0, 0,
                              DISPLAY_WIDTH, DISPLAY_HEIGHT,
                              fb_);
}

// ---- Emotion mapping ----

EyeState EmotionToEyeState(const std::string& emotion) {
    if (emotion == "happy" || emotion == "playful")    return EyeState::HAPPY;
    if (emotion == "sleepy")                            return EyeState::SLEEPY;
    if (emotion == "annoyed" || emotion == "angry")    return EyeState::ANNOYED;
    if (emotion == "surprised")                         return EyeState::SURPRISED;
    return EyeState::NORMAL;  // content, neutral, anything else
}

void EyeDisplay::SetEmotion(const std::string& emotion) {
    state_ = EmotionToEyeState(emotion);
}

// ---- Top-level render ----

void EyeDisplay::Render() {
    if (!initialized_ || !fb_) return;

    Clear(COLOR_BG);

    // Two eyes on a 320×240 canvas
    DrawEye( 85, 120, 60, state_, true);   // left eye
    DrawEye(235, 120, 60, state_, false);  // right eye

    Flush();
}

void EyeDisplay::DrawEye(int cx, int cy, int r, EyeState state, bool left_eye) {
    switch (state) {
        case EyeState::HAPPY:     DrawHappyEye(cx, cy, r);     break;
        case EyeState::SLEEPY:    DrawSleepyEye(cx, cy, r);    break;
        case EyeState::ANNOYED:   DrawAnnoyedEye(cx, cy, r);   break;
        case EyeState::SURPRISED: DrawSurprisedEye(cx, cy, r); break;
        default:                  DrawNormalEye(cx, cy, r);    break;
    }
}

// ---- Eye state implementations ----

void EyeDisplay::DrawNormalEye(int cx, int cy, int r) {
    // White eyeball
    FillCircle(cx, cy, r, COLOR_WHITE);
    // Iris
    FillCircle(cx, cy + 3, r * 0.65f, COLOR_IRIS);
    // Pupil
    FillCircle(cx, cy + 3, r * 0.35f, COLOR_PUPIL);
    // Eye outline
    DrawCircle(cx, cy, r, RGB565(40, 40, 40));
    // Highlight sparkle
    FillCircle(cx - r * 0.3f, cy - r * 0.25f, r * 0.18f, COLOR_HIGHLIGHT);
}

void EyeDisplay::DrawHappyEye(int cx, int cy, int r) {
    // Happy = curved ^ shape: upward arch (inverted U), iris at bottom
    // Draw white lower half-ellipse as the eye body
    int eye_h = r * 0.55f;
    FillEllipse(cx, cy + r * 0.15f, r * 0.85f, eye_h, COLOR_WHITE);
    // Draw arched top line (^ shape)
    for (int dx = -r; dx <= r; dx++) {
        int x = cx + dx;
        if (x < 0 || x >= DISPLAY_WIDTH) continue;
        float t = (float)dx / r;
        int arch_y = cy - r * 0.1f - (int)(r * 0.45f * (1.0f - t * t));
        if (arch_y >= 0 && arch_y < DISPLAY_HEIGHT) {
            fb_[arch_y * DISPLAY_WIDTH + x] = RGB565(50, 50, 50);
        }
    }
    // Iris (small, positioned lower)
    FillCircle(cx, cy + r * 0.2f, r * 0.45f, COLOR_IRIS);
    FillCircle(cx, cy + r * 0.2f, r * 0.25f, COLOR_PUPIL);
    FillCircle(cx - r * 0.15f, cy + r * 0.02f, r * 0.12f, COLOR_HIGHLIGHT);
}

void EyeDisplay::DrawSleepyEye(int cx, int cy, int r) {
    // Sleepy = half-closed: top eyelid droops down
    // White eye base
    FillCircle(cx, cy, r, COLOR_WHITE);
    // Iris visible at bottom
    FillCircle(cx, cy + r * 0.15f, r * 0.5f, COLOR_IRIS);
    FillCircle(cx, cy + r * 0.15f, r * 0.28f, COLOR_PUPIL);
    // Heavy eyelid from top — cover ~60%
    FillRect(cx - r - 1, cy - r - 1, r * 2 + 2, r * 1.06f, COLOR_BG);
    // Restore a thin eye slit at bottom
    FillEllipse(cx, cy + r * 0.35f, r * 0.9f, r * 0.3f, COLOR_WHITE);
    FillCircle(cx, cy + r * 0.15f, r * 0.5f, COLOR_IRIS);
    FillCircle(cx, cy + r * 0.15f, r * 0.28f, COLOR_PUPIL);
    // Outline
    DrawCircle(cx, cy, r, RGB565(40, 40, 40));
}

void EyeDisplay::DrawAnnoyedEye(int cx, int cy, int r) {
    // Annoyed = narrowed/squinting
    FillEllipse(cx, cy, r, r * 0.4f, COLOR_WHITE);
    // Small iris
    FillCircle(cx, cy + 3, r * 0.4f, COLOR_IRIS);
    FillCircle(cx, cy + 3, r * 0.22f, COLOR_PUPIL);
    // Angled eyebrow line
    int brow_y = cy - r * 0.8f;
    for (int dx = -r * 0.7f; dx <= r * 0.7f; dx++) {
        int x = cx + dx;
        int y = brow_y - std::abs(dx) * 1.2f;
        if (x >= 0 && x < DISPLAY_WIDTH && y >= 0 && y < DISPLAY_HEIGHT) {
            for (int th = -1; th <= 1; th++) {
                int py = y + th;
                if (py >= 0 && py < DISPLAY_HEIGHT)
                    fb_[py * DISPLAY_WIDTH + x] = COLOR_WHITE;
            }
        }
    }
    DrawCircle(cx, cy, r, RGB565(40, 40, 40));
}

void EyeDisplay::DrawSurprisedEye(int cx, int cy, int r) {
    // Surprised = wide open, bigger white, small pupil
    int big_r = r * 1.15f;
    FillCircle(cx, cy, big_r, COLOR_WHITE);
    // Small iris (constricted)
    FillCircle(cx, cy, r * 0.45f, COLOR_IRIS);
    FillCircle(cx, cy, r * 0.22f, COLOR_PUPIL);
    // Large highlight
    FillCircle(cx - r * 0.25f, cy - r * 0.25f, r * 0.15f, COLOR_HIGHLIGHT);
    DrawCircle(cx, cy, big_r, RGB565(40, 40, 40));
}
