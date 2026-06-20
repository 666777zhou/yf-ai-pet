#include "audio_pipeline.h"

#include <opus.h>           // esp-opus C API
#include <opus_decoder.h>   // OpusDecoderWrapper (decode works fine)
#include <esp_log.h>

#define TAG "AudioPipeline"

// Maximum Opus packet size for 60ms mono 16kHz @ 32kbps: ~240 bytes
// We use 1500 to be safe
#define SAFE_OPUS_BUF_SIZE 1500

// PIMPL: hide Opus component headers from public header

struct AudioEncoder::Impl {
    OpusEncoder* enc = nullptr;
    int frame_size;

    Impl(int sr, int dur_ms) {
        int err;
        enc = opus_encoder_create(sr, 1, OPUS_APPLICATION_VOIP, &err);
        if (enc) {
            opus_encoder_ctl(enc, OPUS_SET_COMPLEXITY(0));
            opus_encoder_ctl(enc, OPUS_SET_BITRATE(32000));
            opus_encoder_ctl(enc, OPUS_SET_DTX(0));  // Disable DTX to avoid tiny frames
            frame_size = sr / 1000 * dur_ms;          // 960
            ESP_LOGI(TAG, "AudioEncoder: opus_encoder_create OK, frame=%d", frame_size);
        } else {
            ESP_LOGE(TAG, "AudioEncoder: opus_encoder_create FAILED, err=%d", err);
        }
    }

    ~Impl() {
        if (enc) opus_encoder_destroy(enc);
    }
};

AudioEncoder::AudioEncoder(int sample_rate, int duration_ms)
    : impl_(std::make_unique<Impl>(sample_rate, duration_ms)) {
}

AudioEncoder::~AudioEncoder() = default;

bool AudioEncoder::Encode(std::vector<int16_t>&& pcm, std::vector<uint8_t>& opus) {
    return Encode(pcm.data(), (int)pcm.size(), opus);
}

bool AudioEncoder::Encode(const int16_t* pcm, int num_samples, std::vector<uint8_t>& opus) {
    if (!impl_ || !impl_->enc) return false;
    if (num_samples != impl_->frame_size) {
        ESP_LOGW(TAG, "Encode: wrong PCM size %d, expected %d", num_samples, impl_->frame_size);
        return false;
    }

    uint8_t buf[SAFE_OPUS_BUF_SIZE];
    int ret = opus_encode(impl_->enc, pcm, impl_->frame_size, buf, SAFE_OPUS_BUF_SIZE);

    if (ret < 0) {
        ESP_LOGE(TAG, "opus_encode error: %d", ret);
        return false;
    }
    if (ret > SAFE_OPUS_BUF_SIZE) {
        ESP_LOGE(TAG, "opus_encode overflow: ret=%d > buf=%d", ret, SAFE_OPUS_BUF_SIZE);
        return false;
    }

    opus.assign(buf, buf + ret);
    return true;
}


struct AudioDecoder::Impl {
    OpusDecoderWrapper decoder;
    Impl(int sr, int dur_ms) : decoder(sr, 1, dur_ms) {}
};

AudioDecoder::AudioDecoder(int sample_rate, int duration_ms)
    : impl_(std::make_unique<Impl>(sample_rate, duration_ms)) {
    ESP_LOGI(TAG, "AudioDecoder initialized: %dHz %dms frames",
             sample_rate, duration_ms);
}

AudioDecoder::~AudioDecoder() = default;

bool AudioDecoder::Decode(const std::vector<uint8_t>& opus, std::vector<int16_t>& pcm) {
    if (!impl_) return false;
    // OpusDecoderWrapper takes rvalue reference, make a copy
    std::vector<uint8_t> opus_copy(opus);
    return impl_->decoder.Decode(std::move(opus_copy), pcm);
}
