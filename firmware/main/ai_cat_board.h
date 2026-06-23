#ifndef AI_CAT_BOARD_H
#define AI_CAT_BOARD_H

#include "box_audio_codec.h"
#include "pca9557.h"
#include "i2c_device.h"
#include "display.h"

#include <driver/gpio.h>
#include <driver/i2c_master.h>
#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_panel_vendor.h>

// Pin assignments for 立创·实战派 ESP32-S3 标准版
// Audio: ES8311 + ES7210 (board built-in)
// I2C bus shared: PCA9557 (audio PA) + PCA9685 (servos)

#define AUDIO_INPUT_SAMPLE_RATE  16000
#define AUDIO_OUTPUT_SAMPLE_RATE 16000
#define AUDIO_INPUT_REFERENCE    false  // mono mic only, no reference channel

// I2S pins (board built-in audio)
#define AUDIO_I2S_GPIO_MCLK  GPIO_NUM_38
#define AUDIO_I2S_GPIO_BCLK  GPIO_NUM_14
#define AUDIO_I2S_GPIO_WS    GPIO_NUM_13
#define AUDIO_I2S_GPIO_DIN   GPIO_NUM_12   // Mic data in
#define AUDIO_I2S_GPIO_DOUT  GPIO_NUM_45   // Speaker data out

// I2C pins (shared bus: PCA9557 + PCA9685)
#define I2C_SDA_PIN  GPIO_NUM_1
#define I2C_SCL_PIN  GPIO_NUM_2

// Audio codec I2C addresses
#define AUDIO_CODEC_ES8311_ADDR  ES8311_CODEC_DEFAULT_ADDR  // 0x18
#define AUDIO_CODEC_ES7210_ADDR  0x82

// Servo driver (PCA9685)
#define PCA9685_I2C_ADDR  0x40

// Touch sensor pins (ESP32 native capacitive touch)
#define TOUCH_HEAD_GPIO   GPIO_NUM_3
#define TOUCH_BACK_GPIO   GPIO_NUM_4
#define TOUCH_BELLY_GPIO  GPIO_NUM_5

// Vibration motor (LEDC PWM)
#define VIBRATION_PWM_GPIO  GPIO_NUM_10

// Status LED (board built-in)
#define STATUS_LED_GPIO     GPIO_NUM_48

// BOOT button
#define BOOT_BUTTON_GPIO    GPIO_NUM_0

// WebSocket server config
#define SERVER_WEBSOCKET_URL  "ws://yfcat.x3322.net:8765/ws"
#define WIFI_SSID             "503"
#define WIFI_PASSWORD         "13306716600"

class AiCatBoard {
public:
    static AiCatBoard& GetInstance();

    bool Initialize();
    AudioCodec* GetAudioCodec() { return audio_codec_; }
    Pca9557* GetPca9557() { return pca9557_; }
    i2c_master_bus_handle_t GetI2cBus() { return i2c_bus_; }
    EyeDisplay* GetDisplay() { return display_; }
    esp_lcd_panel_handle_t GetLcdPanel() { return lcd_panel_; }

private:
    AiCatBoard() = default;

    void InitializeI2c();
    void InitializeAudio();
    void InitializeGpio();
    void InitializeDisplay();

    i2c_master_bus_handle_t i2c_bus_ = nullptr;
    Pca9557* pca9557_ = nullptr;
    BoxAudioCodec* audio_codec_ = nullptr;
    EyeDisplay* display_ = nullptr;
    esp_lcd_panel_io_handle_t lcd_io_ = nullptr;
    esp_lcd_panel_handle_t lcd_panel_ = nullptr;
};

#endif // AI_CAT_BOARD_H
