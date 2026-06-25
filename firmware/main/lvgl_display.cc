#include "lvgl_display.h"
#include "lvgl_kawaii_face.h"

#include <esp_log.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>

#define TAG "LvglDisplay"

// Draw buffer: 1/10 screen = 7680 px per buffer, two buffers for double-buffering.
static const size_t DRAW_BUF_PX = (LVGL_DISPLAY_WIDTH * LVGL_DISPLAY_HEIGHT) / 10;

// ---- Singleton ----

LvglEyeDisplay& LvglEyeDisplay::GetInstance() {
    static LvglEyeDisplay instance;
    return instance;
}

LvglEyeDisplay::LvglEyeDisplay() = default;

LvglEyeDisplay::~LvglEyeDisplay() {
    if (tick_timer_) {
        esp_timer_stop(tick_timer_);
        esp_timer_delete(tick_timer_);
    }
    face_animation_deinit();
    if (display_) lv_display_delete(display_);
    if (draw_buf_1_) heap_caps_free(draw_buf_1_);
    if (draw_buf_2_) heap_caps_free(draw_buf_2_);
}

// ---- Initialization ----

bool LvglEyeDisplay::Initialize(esp_lcd_panel_handle_t panel) {
    if (initialized_) {
        ESP_LOGW(TAG, "Already initialized");
        return true;
    }
    if (!panel) {
        ESP_LOGE(TAG, "Null panel handle");
        return false;
    }
    panel_ = panel;

    // ---- LVGL core ----
    lv_init();

    // ---- Create display ----
    display_ = lv_display_create(LVGL_DISPLAY_WIDTH, LVGL_DISPLAY_HEIGHT);
    if (!display_) {
        ESP_LOGE(TAG, "lv_display_create failed");
        return false;
    }

    // ---- Double draw buffers in PSRAM ----
    size_t buf_bytes = DRAW_BUF_PX * sizeof(lv_color_t);
    draw_buf_1_ = (lv_color_t*)heap_caps_malloc(buf_bytes,
                                                 MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    draw_buf_2_ = (lv_color_t*)heap_caps_malloc(buf_bytes,
                                                 MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!draw_buf_1_ || !draw_buf_2_) {
        ESP_LOGE(TAG, "Draw buffer alloc failed (%zu bytes × 2)", buf_bytes);
        return false;
    }

    lv_display_set_buffers(display_, draw_buf_1_, draw_buf_2_,
                           buf_bytes, LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(display_, FlushCallback);

    // ---- Dark background (near-black with warm tint) ----
    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_make(0x10, 0x0E, 0x12), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

    // ---- Initialize kawaii_face (fills the screen) ----
    face_config_t cfg = {
        .parent          = nullptr,  // use screen as parent
        .animation_speed = 30,       // ~33 fps
        .blink_interval  = 3000,     // blink every 3 s
        .auto_blink      = true,
    };
    esp_err_t ret = face_animation_init(&cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "face_animation_init failed: %d", ret);
        return false;
    }

    // ---- WiFi provisioning overlay labels (hidden by default) ----
    // Title label at top
    wifi_label_ssid_ = lv_label_create(scr);
    lv_obj_set_style_text_color(wifi_label_ssid_, lv_color_white(), 0);
    lv_obj_set_style_text_font(wifi_label_ssid_, &lv_font_montserrat_14, 0);
    lv_label_set_text(wifi_label_ssid_, "");
    lv_obj_align(wifi_label_ssid_, LV_ALIGN_TOP_MID, 0, 10);
    lv_obj_add_flag(wifi_label_ssid_, LV_OBJ_FLAG_HIDDEN);

    // URL label at bottom
    wifi_label_url_ = lv_label_create(scr);
    lv_obj_set_style_text_color(wifi_label_url_, lv_color_hex(0x00FF88), 0);
    lv_obj_set_style_text_font(wifi_label_url_, &lv_font_montserrat_14, 0);
    lv_label_set_text(wifi_label_url_, "");
    lv_obj_align(wifi_label_url_, LV_ALIGN_BOTTOM_MID, 0, -10);
    lv_obj_add_flag(wifi_label_url_, LV_OBJ_FLAG_HIDDEN);

    // ---- 1ms tick timer ----
    const esp_timer_create_args_t tick_args = {
        .callback = TickCallback,
        .arg = nullptr,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "lvgl_tick",
        .skip_unhandled_events = false,
    };
    if (esp_timer_create(&tick_args, &tick_timer_) != ESP_OK) {
        ESP_LOGE(TAG, "Tick timer create failed");
        return false;
    }
    esp_timer_start_periodic(tick_timer_, 1000);  // 1 ms

    initialized_ = true;
    ESP_LOGI(TAG, "LVGL 9 + kawaii_face ready (%dx%d)", LVGL_DISPLAY_WIDTH, LVGL_DISPLAY_HEIGHT);
    return true;
}

// ---- Emotion control ----

void LvglEyeDisplay::SetEmotion(const std::string& emotion) {
    if (!initialized_) return;

    face_emotion_t fe;
    if (emotion == "sleepy") {
        fe = FACE_SLEEPY;
    } else if (emotion == "playful") {
        fe = FACE_PLAYFUL;
    } else if (emotion == "annoyed") {
        fe = FACE_ANGRY;
    } else if (emotion == "scared") {
        fe = FACE_SURPRISED;
    } else if (emotion == "curious") {
        fe = FACE_CONFUSED;
    } else {
        // content and anything else → neutral relaxed
        fe = FACE_NEUTRAL;
    }

    face_set_emotion(fe, true);  // smooth transition
    ESP_LOGI(TAG, "Emotion: %s → face_%d", emotion.c_str(), (int)fe);
}

// ---- WiFi provisioning overlay ----

void LvglEyeDisplay::ShowWifiInfo(const std::string& ssid, const std::string& url) {
    if (!initialized_) return;
    lv_label_set_text(wifi_label_ssid_, ssid.c_str());
    lv_obj_remove_flag(wifi_label_ssid_, LV_OBJ_FLAG_HIDDEN);
    if (!url.empty()) {
        lv_label_set_text(wifi_label_url_, url.c_str());
        lv_obj_remove_flag(wifi_label_url_, LV_OBJ_FLAG_HIDDEN);
    }
}

void LvglEyeDisplay::HideWifiInfo() {
    if (!initialized_) return;
    lv_obj_add_flag(wifi_label_ssid_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(wifi_label_url_, LV_OBJ_FLAG_HIDDEN);
}

// ---- LVGL 9 callbacks ----

void LvglEyeDisplay::FlushCallback(lv_display_t *disp, const lv_area_t *area,
                                    uint8_t *px_map) {
    auto& self = GetInstance();
    // Blocking call — waits for SPI DMA to finish before returning
    esp_err_t ret = esp_lcd_panel_draw_bitmap(
        self.panel_,
        area->x1, area->y1,
        area->x2 + 1, area->y2 + 1,
        px_map);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Flush err 0x%x area=(%d,%d)-(%d,%d)",
                 ret, area->x1, area->y1, area->x2, area->y2);
    }
    lv_display_flush_ready(disp);
}

void LvglEyeDisplay::TickCallback(void *arg) {
    lv_tick_inc(1);
}

void LvglEyeDisplay::RunLvgl() {
    lv_timer_handler();
}
