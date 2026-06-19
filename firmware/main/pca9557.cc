#include "pca9557.h"

Pca9557::Pca9557(i2c_master_bus_handle_t i2c_bus, uint8_t addr)
    : I2cDevice(i2c_bus, addr) {
    // Configure IO: bits 0-2 as outputs (0=LCD_RST, 1=PA_EN, 2=CAM_PWDN)
    WriteReg(0x01, 0x03);
    // Set polarity: default
    WriteReg(0x03, 0xf8);
}

void Pca9557::SetOutputState(uint8_t bit, uint8_t level) {
    uint8_t data = ReadReg(0x01);
    data = (data & ~(1 << bit)) | (level << bit);
    WriteReg(0x01, data);
}
