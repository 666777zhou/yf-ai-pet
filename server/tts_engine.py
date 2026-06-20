"""Text-to-Speech engine: Qwen3-TTS (default) + Fish-Speech + Piper + GPT-SoVITS.

Qwen3-TTS:   Local HTTP API — real-time voice clone, 1.7B params, ~2s per utterance.
             Start: conda activate qwen3-tts && python tts_server.py  (port 9874)
Fish-Speech: Local HTTP API — high quality, 4B params, ~14s per utterance.
             Start: conda activate fish-speech && python tts_server.py  (port 9873)
Piper:        ONNX-based, ~50ms synthesis, fixed voices, ~60MB/voice.
GPT-SoVITS:   HTTP API, zero-shot voice cloning.
"""
import asyncio
import logging
import os
import io
import wave

import numpy as np

logger = logging.getLogger(__name__)

TTS_MODELS_DIR = os.path.join(os.path.dirname(__file__), "tts_models")

# Qwen3-TTS API endpoint
QWEN_TTS_API_URL = os.environ.get(
    "QWEN_TTS_API_URL", "http://localhost:9874/v1/tts"
)

# Fish-Speech API endpoint
FISH_SPEECH_API_URL = os.environ.get(
    "FISH_SPEECH_API_URL", "http://localhost:9873/v1/tts"
)

# GPT-SoVITS API endpoint
GSV_API_URL = os.environ.get("GSV_API_URL", "http://localhost:9872/tts")


class TTSEngine:
    """Multi-engine TTS with voice profile support.

    Routes synthesis to Fish-Speech, Piper, or GPT-SoVITS based on active voice profile.
    Fish-Speech model lives in its own process (fish-speech conda env), called via HTTP.
    """

    def __init__(self, voice_manager=None):
        if voice_manager is None:
            from voice_manager import VoiceManager
            voice_manager = VoiceManager()
        self.vm = voice_manager
        self._piper_voices: dict[str, object] = {}    # model_name → PiperVoice
        self._http_client = None                        # httpx.AsyncClient (shared)

    # ---- public API ----------------------------------------------------------

    async def synthesize_opus_frames(self, text: str) -> list[bytes]:
        """Convert text to speech, return list of individual Opus frames."""
        profile = self.vm.get_active()

        if profile.engine == "qwen-tts":
            pcm_bytes = await self._qwen_tts_synthesize(
                text, profile.ref_audio, profile.prompt_text,
            )
        elif profile.engine == "fish-speech":
            pcm_bytes = await self._fish_speech_synthesize(
                text, profile.ref_audio, profile.prompt_text,
            )
        elif profile.engine == "piper":
            pcm_bytes = await self._piper_synthesize(text, profile.model)
        elif profile.engine == "gpt-sovits":
            pcm_bytes = await self._gsv_synthesize(text, profile.ref_audio)
        else:
            logger.error(f"Unknown engine: {profile.engine}")
            return []

        if not pcm_bytes:
            return []

        return await self._pcm_to_opus(pcm_bytes, text)

    async def _pcm_to_opus(self, pcm_bytes: bytes, text_hint: str = "") -> list[bytes]:
        """Encode 16kHz mono PCM to 60ms Opus frames."""
        import opuslib

        frame_size = 960  # 16000 * 0.060
        encoder = opuslib.Encoder(16000, 1, opuslib.APPLICATION_VOIP)
        encoder.bitrate = 32000

        opus_frames = []
        for i in range(0, len(pcm_bytes) - frame_size * 2 + 1, frame_size * 2):
            frame = pcm_bytes[i:i + frame_size * 2]
            if len(frame) == frame_size * 2:
                opus_frames.append(encoder.encode(frame, frame_size))

        preview = text_hint[:20] if text_hint else ""
        logger.info(
            f"TTS: {preview}... → {len(pcm_bytes)}B PCM → "
            f"{len(opus_frames)} Opus frames"
        )
        return opus_frames

    # ---- Qwen3-TTS engine (HTTP API) ---------------------------------------

    async def _qwen_tts_synthesize(self, text: str, ref_audio: str = "",
                                     prompt_text: str = "") -> bytes:
        """Call Qwen3-TTS HTTP API, return 16kHz mono int16 PCM bytes."""
        client = self._get_http_client()
        try:
            payload = {
                "text": text,
                "language": "Chinese",
            }
            if ref_audio and prompt_text:
                payload["ref_audio"] = os.path.abspath(ref_audio)
                payload["ref_text"] = prompt_text

            resp = await client.post(QWEN_TTS_API_URL, json=payload, timeout=60.0)

            if resp.status_code == 200:
                return self._wav_to_pcm(resp.content)
            else:
                logger.error(f"Qwen3-TTS API error {resp.status_code}: {resp.text[:200]}")
                return b""

        except Exception as e:
            logger.error(f"Qwen3-TTS API call failed: {e}")
            return b""

    # ---- Fish-Speech engine (HTTP API) --------------------------------------

    async def _fish_speech_synthesize(self, text: str, ref_audio: str = "",
                                       prompt_text: str = "") -> bytes:
        """Call Fish-Speech HTTP API, return 16kHz mono int16 PCM bytes."""
        client = self._get_http_client()
        try:
            payload = {"text": text}
            if ref_audio and prompt_text:
                payload["ref_audio"] = os.path.abspath(ref_audio)
                payload["prompt_text"] = prompt_text

            resp = await client.post(FISH_SPEECH_API_URL, json=payload, timeout=60.0)

            if resp.status_code == 200:
                return self._wav_to_pcm(resp.content)
            else:
                logger.error(f"Fish-Speech API error {resp.status_code}: {resp.text[:200]}")
                return b""

        except Exception as e:
            logger.error(f"Fish-Speech API call failed: {e}")
            return b""

    # ---- Piper engine --------------------------------------------------------

    async def _piper_synthesize(self, text: str, model_name: str) -> bytes:
        voice = self._get_piper_voice(model_name)
        if voice is None:
            return b""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._piper_run, voice, text)

    def _get_piper_voice(self, model_name: str):
        if model_name in self._piper_voices:
            return self._piper_voices[model_name]
        model_path = os.path.join(TTS_MODELS_DIR, f"{model_name}.onnx")
        if not os.path.exists(model_path):
            logger.error(f"Piper model not found: {model_path}")
            return None
        from piper.voice import PiperVoice
        logger.info(f"Loading Piper voice: {model_name}")
        voice = PiperVoice.load(model_path)
        self._piper_voices[model_name] = voice
        return voice

    @staticmethod
    def _piper_run(voice, text: str) -> bytes:
        try:
            chunks = []
            for chunk in voice.synthesize(text):
                chunks.append(chunk.audio_int16_bytes)
            return b"".join(chunks)
        except Exception as e:
            logger.error(f"Piper synthesis error: {e}")
            return b""

    # ---- GPT-SoVITS engine (HTTP API) ----------------------------------------

    async def _gsv_synthesize(self, text: str, ref_audio_path: str) -> bytes:
        if not os.path.exists(ref_audio_path):
            logger.error(f"Reference audio not found: {ref_audio_path}")
            return b""
        client = self._get_http_client()
        try:
            with open(ref_audio_path, "rb") as f:
                files = {
                    "text": (None, text),
                    "text_lang": (None, "zh"),
                    "ref_audio": (os.path.basename(ref_audio_path), f, "audio/wav"),
                    "prompt_text": (None, ""),
                    "prompt_lang": (None, "zh"),
                    "speed_factor": (None, "1.0"),
                }
                resp = await client.post(GSV_API_URL, files=files, timeout=30.0)
            if resp.status_code == 200:
                return self._wav_to_pcm(resp.content)
            else:
                logger.error(f"GPT-SoVITS API error {resp.status_code}: {resp.text[:200]}")
                return b""
        except Exception as e:
            logger.error(f"GPT-SoVITS API call failed: {e}")
            return b""

    # ---- shared HTTP client --------------------------------------------------

    def _get_http_client(self):
        if self._http_client is None:
            import httpx
            self._http_client = httpx.AsyncClient(timeout=60.0)
        return self._http_client

    # ---- WAV → PCM conversion -----------------------------------------------

    @staticmethod
    def _wav_to_pcm(wav_bytes: bytes) -> bytes:
        """Extract raw 16kHz mono int16 PCM bytes from WAV."""
        try:
            with wave.open(io.BytesIO(wav_bytes)) as wf:
                sr = wf.getframerate()
                nch = wf.getnchannels()
                sw = wf.getsampwidth()
                pcm = wf.readframes(wf.getnframes())

            if sr != 16000 or nch != 1 or sw != 2:
                arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                if nch > 1:
                    arr = arr.reshape(-1, nch).mean(axis=1)
                if sr != 16000:
                    from scipy.signal import resample
                    new_len = int(len(arr) * 16000 / sr)
                    arr = resample(arr, new_len)
                pcm = arr.clip(-32768, 32767).astype(np.int16).tobytes()
                logger.info(f"Resampled WAV: {sr}Hz/{nch}ch/{sw}B → 16000Hz/mono/2B")

            return pcm
        except Exception as e:
            logger.error(f"WAV parse error: {e}")
            return b""
