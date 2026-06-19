#ifndef PCA9557_H
#define PCA9557_H

#include "i2c_device.h"

// PCA9557 8-bit I2C IO expander
// Used on 立创·实战派 ESP32-S3 to control audio PA enable and other signals
class Pca9557 : public I2cDevice {
public:
    Pca9557(i2c_master_bus_handle_t i2c_bus, uint8_t addr);

    void SetOutputState(uint8_t bit, uint8_t level);
};

#endif // PCA9557_H
