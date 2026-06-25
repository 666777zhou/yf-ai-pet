"""Speech-to-Text engine using Faster-Whisper."""
import os
import glob
import logging

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

logger = logging.getLogger(__name__)

# Whisper 中文识别默认上下文 — 引导模型理解"猫对话"场景
DEFAULT_INITIAL_PROMPT = (
    "这是一段主人和AI宠物猫的对话。主人可能会说：摸头、抱抱、过来、饿了吗、"
    "乖不乖、小咪、喵喵、真可爱、吃饭了、睡觉吧、出去玩、"
    "今天怎么样、想你了、别闹、过来让我抱抱。"
)


class STTEngine:
    """Whisper-based STT with VAD pre-filtering and conversation context."""

    def __init__(self, model_size: str = "large-v3", device: str = "cuda",
                 compute_type: str = "float16"):
        print(f"Loading Whisper model '{model_size}' on {device} ({compute_type})...")
        from faster_whisper import WhisperModel
        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            num_workers=2,
            local_files_only=True,  # skip HuggingFace network check (model must be cached)
        )
        print("Whisper model loaded.")

    def transcribe(self, pcm_bytes: bytes, sample_rate: int = 16000,
                   initial_prompt: str = "") -> str:
        """Transcribe PCM audio to text. Returns empty string if no speech.

        Args:
            pcm_bytes: 16-bit PCM audio data
            sample_rate: audio sample rate (default 16000)
            initial_prompt: optional context text to guide recognition.
                           If empty, uses DEFAULT_INITIAL_PROMPT.
        """
        audio = pcm_to_numpy(pcm_bytes).astype(np.float32) / 32768.0

        prompt = initial_prompt or DEFAULT_INITIAL_PROMPT

        segments, info = self.model.transcribe(
            audio,
            language="zh",
            beam_size=8,
            best_of=8,
            initial_prompt=prompt,
            vad_filter=True,
            vad_parameters=dict(
                threshold=0.5,
                min_speech_duration_ms=250,
                min_silence_duration_ms=200,
                speech_pad_ms=400,
            ),
            repetition_penalty=1.1,
            no_repeat_ngram_size=2,
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
        )

        text = ""
        for segment in segments:
            text += segment.text

        result = text.strip()
        if result:
            logger.info(f"STT result: '{result}' (detected language: {info.language}, "
                        f"probability: {info.language_probability:.3f})")
        return result
