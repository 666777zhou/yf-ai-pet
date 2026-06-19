"""Text-to-Speech engine. Uses Edge-TTS for v1; outputs Opus-encoded audio."""
import asyncio
import io
import logging

logger = logging.getLogger(__name__)


class TTSEngine:
    """TTS engine that returns Opus-encoded audio bytes."""

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural"):
        self.voice = voice

    async def synthesize(self, text: str) -> bytes:
        """Convert text to speech, return raw MP3 bytes.

        For direct compatibility with older code or MP3-capable clients.
        Prefer synthesize_opus() for ESP32 streaming.
        """
        try:
            import edge_tts

            communicate = edge_tts.Communicate(text, self.voice)
            audio_chunks = []

            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_chunks.append(chunk["data"])

            return b"".join(audio_chunks)

        except ImportError:
            logger.warning("edge-tts not installed, returning empty data")
            return b""

        except Exception as e:
            logger.error(f"TTS error: {e}")
            return b""

    async def synthesize_opus_frames(self, text: str) -> list[bytes]:
        """Convert text to speech, return list of individual Opus frames.

        Pipeline: Edge-TTS → MP3 → decode to PCM → encode to Opus
        Output: list of 16kHz mono Opus frames (each ~60ms), suitable for ESP32 streaming.
        """
        mp3_data = await self.synthesize(text)
        if not mp3_data:
            return []

        try:
            import opuslib
            from pydub import AudioSegment

            # Decode MP3 → PCM (16kHz, mono, 16-bit)
            audio = AudioSegment.from_file(io.BytesIO(mp3_data), format="mp3")
            audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
            pcm_bytes = audio.raw_data

            # Encode PCM → Opus in 60ms frames
            frame_size = 960  # 16000 * 0.060
            encoder = opuslib.Encoder(16000, 1, opuslib.APPLICATION_VOIP)
            encoder.bitrate = 32000

            opus_frames = []
            for i in range(0, len(pcm_bytes) - frame_size * 2 + 1, frame_size * 2):
                frame = pcm_bytes[i:i + frame_size * 2]
                if len(frame) == frame_size * 2:
                    opus_frames.append(encoder.encode(frame, frame_size))

            logger.info(f"TTS: {text[:20]}... → {len(mp3_data)}B MP3 → {len(opus_frames)} Opus frames")
            return opus_frames

        except ImportError as e:
            logger.warning(f"Opus/Pydub not available: {e}")
            return []

        except Exception as e:
            logger.error(f"Opus conversion error: {e}")
            return []

    async def synthesize_opus(self, text: str) -> bytes:
        """Convert text to speech, return concatenated Opus bytes (legacy)."""
        frames = await self.synthesize_opus_frames(text)
        return b"".join(frames) if frames else b""
