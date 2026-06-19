"""Audio utilities: Opus encode/decode, VAD, resampling."""
import opuslib
import numpy as np
from collections import deque

SAMPLE_RATE = 16000
FRAME_DURATION_MS = 60  # 60ms Opus frames
FRAME_SIZE = SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 960 samples


class OpusCodec:
    """Opus encoder/decoder wrapper for 16kHz mono audio."""

    def __init__(self):
        self.encoder = opuslib.Encoder(SAMPLE_RATE, 1, opuslib.APPLICATION_VOIP)
        self.encoder.bitrate = 32000  # 32kbps for voice
        self.decoder = opuslib.Decoder(SAMPLE_RATE, 1)

    def encode(self, pcm_bytes: bytes) -> bytes:
        """Encode 16-bit PCM to Opus."""
        return self.encoder.encode(pcm_bytes, FRAME_SIZE)

    def decode(self, opus_bytes: bytes) -> bytes:
        """Decode Opus to 16-bit PCM."""
        return self.decoder.decode(opus_bytes, FRAME_SIZE)


class VAD:
    """Simple energy-based Voice Activity Detection."""

    def __init__(self, threshold: float = 0.05, silence_frames: int = 30):
        self.threshold = threshold
        self.silence_frames = silence_frames
        self.silent_count = 0
        self.speaking = False

    def is_speech(self, pcm_data: np.ndarray) -> bool:
        """Check if PCM frame contains speech based on energy.

        Returns True only when current frame has energy above threshold.
        The internal speaking/silent state tracks hangover separately.
        """
        if len(pcm_data) == 0:
            return False

        energy = np.sqrt(np.mean(pcm_data.astype(np.float32) ** 2)) / 32768.0

        if energy > self.threshold:
            self.silent_count = 0
            self.speaking = True
            return True
        else:
            if self.speaking:
                self.silent_count += 1
                if self.silent_count > self.silence_frames:
                    self.speaking = False
            return False  # Only return True for actual speech, not hangover

    @property
    def energy_threshold(self) -> float:
        return self.threshold

    @energy_threshold.setter
    def energy_threshold(self, value: float):
        self.threshold = value


def pcm_to_numpy(pcm_bytes: bytes) -> np.ndarray:
    """Convert 16-bit PCM bytes to numpy int16 array."""
    return np.frombuffer(pcm_bytes, dtype=np.int16)


def numpy_to_pcm(arr: np.ndarray) -> bytes:
    """Convert numpy int16 array to 16-bit PCM bytes."""
    return arr.astype(np.int16).tobytes()
