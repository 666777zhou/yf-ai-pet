"""Speech-to-Text engine using Faster-Whisper."""
import os
import glob

# nvidia-cublas-cu12 installs libcublas.so.12 inside its package dir,
# which the dynamic linker doesn't search. Add it before importing ctranslate2.
_cublas_paths = glob.glob(
    os.path.expanduser("~") + "/miniconda3/envs/*/lib/python3.*/site-packages/nvidia/cublas/lib"
)
if _cublas_paths:
    _cublas_paths.sort(reverse=True)
    os.environ["LD_LIBRARY_PATH"] = _cublas_paths[0] + ":" + os.environ.get("LD_LIBRARY_PATH", "")

import numpy as np
from audio_utils import pcm_to_numpy


class STTEngine:
    """Whisper-based STT with VAD pre-filtering."""

    def __init__(self, model_size: str = "small", device: str = "cuda"):
        print(f"Loading Whisper model '{model_size}' on {device}...")
        from faster_whisper import WhisperModel
        self.model = WhisperModel(model_size, device=device, compute_type="float16")
        print("Whisper model loaded.")

    def transcribe(self, pcm_bytes: bytes, sample_rate: int = 16000) -> str:
        """Transcribe PCM audio to text. Returns empty string if no speech."""
        audio = pcm_to_numpy(pcm_bytes).astype(np.float32) / 32768.0

        segments, info = self.model.transcribe(audio, language="zh", beam_size=5)

        text = ""
        for segment in segments:
            text += segment.text

        return text.strip()
