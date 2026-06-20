#ifndef AUDIO_PIPELINE_H
#define AUDIO_PIPELINE_H

#include <memory>
#include <vector>
#include <cstdint>

// Audio pipeline: PCM ↔ Opus conversion for the AI Cat
// Wraps the esp-opus-encoder component for 16kHz mono 60ms frames

constexpr int AUDIO_SAMPLE_RATE = 16000;
constexpr int AUDIO_FRAME_DURATION_MS = 60;
constexpr int AUDIO_FRAME_SAMPLES = 960;  // 16000 * 0.060

class AudioEncoder {
public:
    explicit AudioEncoder(int sample_rate = AUDIO_SAMPLE_RATE, int duration_ms = AUDIO_FRAME_DURATION_MS);
    ~AudioEncoder();

    // Encode PCM samples to Opus frame (vector overload).
    // pcm: input — exactly AUDIO_FRAME_SAMPLES (960) int16_t samples
    // opus: output — encoded Opus bytes
    // Returns true on success
    bool Encode(std::vector<int16_t>&& pcm, std::vector<uint8_t>& opus);

    // Encode PCM samples to Opus frame (raw pointer overload).
    // Zero heap allocation — reads from caller-provided buffer.
    bool Encode(const int16_t* pcm, int num_samples, std::vector<uint8_t>& opus);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

class AudioDecoder {
public:
    explicit AudioDecoder(int sample_rate = AUDIO_SAMPLE_RATE, int duration_ms = AUDIO_FRAME_DURATION_MS);
    ~AudioDecoder();

    // Decode Opus frame to PCM samples.
    // opus: input — encoded Opus bytes
    // pcm: output — exactly AUDIO_FRAME_SAMPLES (960) int16_t samples
    // Returns true on success
    bool Decode(const std::vector<uint8_t>& opus, std::vector<int16_t>& pcm);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

#endif // AUDIO_PIPELINE_H
