#include "ai_cat_board.h"
#include "pca9557.h"

#include <esp_log.h>

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
    // TODO: Display disabled until SPI driver verified
    // InitializeDisplay();

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
    display_ = new EyeDisplay();
    if (!display_->Initialize()) {
        ESP_LOGE(TAG, "Display initialization failed");
        delete display_;
        display_ = nullptr;
    }
}
