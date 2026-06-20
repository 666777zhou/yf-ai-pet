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
    """Adaptive energy-based Voice Activity Detection.

    Tracks ambient noise floor and adjusts threshold dynamically,
    so it works for both loud and quiet speakers without tuning.
    """

    def __init__(self, silence_frames: int = 30):
        # Adaptive threshold parameters
        self._noise_floor = 0.005          # estimated ambient noise energy
        self._noise_alpha = 0.05           # smoothing factor for noise floor update
        self._speech_alpha = 0.2           # smoothing factor for speech energy
        self._min_threshold = 0.008        # absolute minimum threshold
        self._threshold_margin = 3.0       # multiplier: speech must be N× noise floor
        self._current_threshold = 0.02     # initial threshold (updated adaptively)

        self.silence_frames = silence_frames
        self.silent_count = 0
        self.speaking = False
        self._frame_count = 0

    def is_speech(self, pcm_data: np.ndarray) -> bool:
        """Check if PCM frame contains speech based on adaptive energy threshold.

        Returns True only when current frame has energy above threshold.
        The internal speaking/silent state tracks hangover separately.
        """
        if len(pcm_data) == 0:
            return False

        energy = np.sqrt(np.mean(pcm_data.astype(np.float32) ** 2)) / 32768.0
        self._frame_count += 1

        # Periodically update noise floor from quiet frames
        if not self.speaking and energy < self._current_threshold:
            self._noise_floor = (
                self._noise_alpha * energy +
                (1 - self._noise_alpha) * self._noise_floor
            )
            # Recompute threshold: max of (margin × noise_floor) and min_threshold
            self._current_threshold = max(
                self._min_threshold,
                self._threshold_margin * self._noise_floor
            )

        if energy > self._current_threshold:
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
    def current_threshold(self) -> float:
        """Current adaptive threshold (read-only, for debugging)."""
        return self._current_threshold

    @property
    def noise_floor(self) -> float:
        """Estimated ambient noise floor (read-only, for debugging)."""
        return self._noise_floor


def pcm_to_numpy(pcm_bytes: bytes) -> np.ndarray:
    """Convert 16-bit PCM bytes to numpy int16 array."""
    return np.frombuffer(pcm_bytes, dtype=np.int16)


def numpy_to_pcm(arr: np.ndarray) -> bytes:
    """Convert numpy int16 array to 16-bit PCM bytes."""
    return arr.astype(np.int16).tobytes()
