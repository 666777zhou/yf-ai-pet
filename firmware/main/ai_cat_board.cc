#include "ai_cat_board.h"
#include "pca9557.h"

#include <esp_log.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <driver/ledc.h>
#include <driver/spi_common.h>
#include <esp_heap_caps.h>

#define TAG "AiCatBoard"

AiCatBoard& AiCatBoard::GetInstance() {
    static AiCatBoard instance;
    return instance;
}

bool AiCatBoard::Initialize() {
    ESP_LOGI(TAG, "Initializing AI Cat board...");

    InitializeI2c();
    InitializeAudio();
    InitializeGpio();
    InitializeDisplay();

    ESP_LOGI(TAG, "AI Cat board initialized successfully");
    return true;
}

void AiCatBoard::InitializeI2c() {
    ESP_LOGI(TAG, "Initializing I2C bus (SDA=%d, SCL=%d)", I2C_SDA_PIN, I2C_SCL_PIN);

    i2c_master_bus_config_t i2c_bus_cfg = {
        .i2c_port = (i2c_port_t)1,
        .sda_io_num = I2C_SDA_PIN,
        .scl_io_num = I2C_SCL_PIN,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .intr_priority = 0,
        .trans_queue_depth = 0,
        .flags = {
            .enable_internal_pullup = 1,
        },
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_bus_cfg, &i2c_bus_));

    // Initialize PCA9557 (audio PA control, address 0x19)
    pca9557_ = new Pca9557(i2c_bus_, 0x19);
    ESP_LOGI(TAG, "PCA9557 initialized at 0x19");
}

void AiCatBoard::InitializeAudio() {
    ESP_LOGI(TAG, "Initializing audio (ES8311 + ES7210)...");

    audio_codec_ = new BoxAudioCodec(
        i2c_bus_,
        AUDIO_INPUT_SAMPLE_RATE,
        AUDIO_OUTPUT_SAMPLE_RATE,
        AUDIO_I2S_GPIO_MCLK,
        AUDIO_I2S_GPIO_BCLK,
        AUDIO_I2S_GPIO_WS,
        AUDIO_I2S_GPIO_DOUT,
        AUDIO_I2S_GPIO_DIN,
        GPIO_NUM_NC,  // PA pin handled by PCA9557, not GPIO
        AUDIO_CODEC_ES8311_ADDR,
        AUDIO_CODEC_ES7210_ADDR,
        AUDIO_INPUT_REFERENCE
    );

    // Start audio codec (enables I2S channels, input and output)
    audio_codec_->Start();

    // Enable speaker via PCA9557 (bit 1 = PA_EN)
    pca9557_->SetOutputState(1, 1);

    ESP_LOGI(TAG, "Audio initialized, speaker enabled");
}

void AiCatBoard::InitializeGpio() {
    // Status LED
    gpio_config_t led_cfg = {
        .pin_bit_mask = (1ULL << STATUS_LED_GPIO),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&led_cfg);
    gpio_set_level(STATUS_LED_GPIO, 0);

    // BOOT button (input, pull-up)
    gpio_config_t btn_cfg = {
        .pin_bit_mask = (1ULL << BOOT_BUTTON_GPIO),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&btn_cfg);

    ESP_LOGI(TAG, "GPIO initialized");
}

void AiCatBoard::InitializeDisplay() {
    ESP_LOGI(TAG, "Initializing display (ST7789 320x240 SPI)...");

    // ---- Step 1: Backlight LEDC PWM (matches 06-lcd bsp_display_brightness_init) ----
    const ledc_timer_config_t bl_timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_10_BIT,
        .timer_num = LEDC_TIMER_1,
        .freq_hz = 5000,
        .clk_cfg = LEDC_AUTO_CLK
    };
    ESP_ERROR_CHECK(ledc_timer_config(&bl_timer));

    const ledc_channel_config_t bl_channel = {
        .gpio_num = GPIO_NUM_42,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = LEDC_CHANNEL_0,
        .intr_type = LEDC_INTR_DISABLE,
        .timer_sel = LEDC_TIMER_1,
        .duty = 0,
        .hpoint = 0,
        .flags = { .output_invert = true }
    };
    ESP_ERROR_CHECK(ledc_channel_config(&bl_channel));

    // ---- Step 2: SPI bus init (matches 06-lcd bsp_display_new) ----
    const spi_bus_config_t buscfg = {
        .mosi_io_num = GPIO_NUM_40,
        .miso_io_num = GPIO_NUM_NC,
        .sclk_io_num = GPIO_NUM_41,
        .quadwp_io_num = GPIO_NUM_NC,
        .quadhd_io_num = GPIO_NUM_NC,
        .max_transfer_sz = 320 * 240 * sizeof(uint16_t),
    };
    ESP_ERROR_CHECK(spi_bus_initialize(SPI3_HOST, &buscfg, SPI_DMA_CH_AUTO));

    // ---- Step 3: Panel IO init (matches 06-lcd, field order per ESP-IDF 5.5.1) ----
    const esp_lcd_panel_io_spi_config_t io_config = {
        .cs_gpio_num = GPIO_NUM_NC,
        .dc_gpio_num = GPIO_NUM_39,
        .spi_mode = 2,
        .pclk_hz = 80 * 1000 * 1000,
        .trans_queue_depth = 10,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(SPI3_HOST, &io_config, &lcd_io_));

    // ---- Step 4: ST7789 driver init (matches 06-lcd) ----
    const esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = GPIO_NUM_NC,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_st7789(lcd_io_, &panel_config, &lcd_panel_));

    // ---- Step 5: Reset, then CS LOW, then init (EXACT 06-lcd order) ----
    esp_lcd_panel_reset(lcd_panel_);
    pca9557_->SetOutputState(0, 0);   // CS LOW (bit 0 = 0)
    esp_lcd_panel_init(lcd_panel_);
    esp_lcd_panel_invert_color(lcd_panel_, true);
    esp_lcd_panel_swap_xy(lcd_panel_, true);
    esp_lcd_panel_mirror(lcd_panel_, true, false);

    // ---- Step 6: Turn on display and backlight (matches 06-lcd bsp_lcd_init) ----
    esp_lcd_panel_disp_on_off(lcd_panel_, true);
    // Backlight ON: duty=1023, output_invert=true → GPIO LOW → backlight ON
    ESP_ERROR_CHECK(ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, 1023));
    ESP_ERROR_CHECK(ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0));

    ESP_LOGI(TAG, "Display initialized (06-lcd compatible mode)");
}
