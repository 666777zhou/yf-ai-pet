"""WebSocket handler: manages ESP32 connections and audio pipeline."""
import asyncio
import json
import logging
import numpy as np
import struct
import time

from audio_utils import OpusCodec, VAD, pcm_to_numpy
from cat_brain import CatBrain
from stt_engine import STTEngine
from tts_engine import TTSEngine

logger = logging.getLogger(__name__)

# Audio frame: 60ms @ 16kHz = 960 samples × 2 bytes = 1920 bytes
FRAME_BYTES = 1920


class CatConnection:
    """Handles a single AI Cat ESP32 connection."""

    def __init__(self, websocket, stt: STTEngine, tts: TTSEngine, brain: CatBrain):
        self.ws = websocket
        self.stt = stt
        self.tts = tts
        self.brain = brain
        self.codec = OpusCodec()
        self.vad = VAD()
        self.pcm_buffer = bytearray()
        self.touch_values = [0, 0, 0]
        self.battery_pct = 100
        self.running = True

    async def handle(self):
        """Main connection handler."""
        logger.info("Cat connection established")

        recv_task = asyncio.create_task(self._recv_loop())
        sensor_task = asyncio.create_task(self._process_loop())

        try:
            await asyncio.gather(recv_task, sensor_task)
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            logger.info("Cat connection closed")

    async def _recv_loop(self):
        """Receive messages from ESP32."""
        try:
            async for message in self.ws:
                if isinstance(message, bytes):
                    # Opus audio frame from ESP32
                    await self._handle_audio(message)
                else:
                    # JSON sensor data from ESP32
                    await self._handle_json(message)
        except Exception as e:
            logger.error(f"Receive error: {e}")

    async def _handle_json(self, text: str):
        """Handle incoming JSON sensor data."""
        try:
            data = json.loads(text)
            if data.get("type") == "sensors":
                self.touch_values = [
                    data.get("touch_head", 0),
                    data.get("touch_back", 0),
                    data.get("touch_belly", 0),
                ]
                self.battery_pct = data.get("battery_pct", 100)
        except json.JSONDecodeError:
            pass

    async def _handle_audio(self, pcm_frame: bytes):
        """Handle incoming raw PCM audio from ESP32 microphone.

        Each frame is exactly 1920 bytes (960 int16_t samples @ 16kHz mono).
        """
        try:
            # Validate frame size
            if len(pcm_frame) != FRAME_BYTES:
                logger.warning(f"Unexpected PCM frame size: {len(pcm_frame)}, expected {FRAME_BYTES}")
                if len(pcm_frame) < FRAME_BYTES:
                    return  # skip partial frames

            # Append to VAD buffer
            self.pcm_buffer.extend(pcm_frame[:FRAME_BYTES])
        except Exception as e:
            logger.error(f"Audio receive error: {e}")

    async def _process_loop(self):
        """Periodically process sensor data and generate responses."""
        accumulated_speech_pcm = bytearray()
        speech_frames = 0
        silence_frames = 0
        speech_energies = []  # track energy of speech frames for quality check
        last_command_json = ""
        last_command_send_time = time.monotonic()

        while self.running:
            try:
                await asyncio.sleep(0.06)  # 60ms = one audio frame

                # Accumulate speech from VAD
                if len(self.pcm_buffer) >= FRAME_BYTES:
                    frame = bytes(self.pcm_buffer[:FRAME_BYTES])
                    self.pcm_buffer = self.pcm_buffer[FRAME_BYTES:]

                    audio_np = pcm_to_numpy(frame)
                    energy = np.sqrt(np.mean(audio_np.astype(np.float32) ** 2)) / 32768.0
                    is_speech = self.vad.is_speech(audio_np)

                    if is_speech:
                        accumulated_speech_pcm.extend(frame)
                        speech_frames += 1
                        silence_frames = 0
                        speech_energies.append(energy)
                        if speech_frames == 1:
                            logger.info(f"VAD ON: energy={energy:.4f}")
                    elif speech_frames > 0:
                        silence_frames += 1
                        accumulated_speech_pcm.extend(frame)

                    # End of utterance: silence for > 1 second
                    if silence_frames > 17 and speech_frames > 5:  # min 300ms speech
                        # Quality check: average energy > 0.03 (filter pure noise)
                        avg_energy = sum(speech_energies) / len(speech_energies) if speech_energies else 0
                        logger.info(f"Utterance end: {speech_frames} frames, avg_energy={avg_energy:.4f}")

                        if avg_energy > 0.03:
                            user_text = await self._transcribe(bytes(accumulated_speech_pcm))
                            if user_text:
                                logger.info(f"User said: {user_text}")
                                await self._respond(user_text)
                        else:
                            logger.info(f"Utterance rejected (avg energy {avg_energy:.4f} < 0.03)")

                        accumulated_speech_pcm = bytearray()
                        speech_frames = 0
                        silence_frames = 0
                        speech_energies = []

                    elif speech_frames > 50:  # Max utterance length ~3s
                        avg_energy = sum(speech_energies) / len(speech_energies) if speech_energies else 0
                        if avg_energy > 0.03:
                            user_text = await self._transcribe(bytes(accumulated_speech_pcm))
                            if user_text:
                                logger.info(f"User said: {user_text}")
                                await self._respond(user_text)
                        accumulated_speech_pcm = bytearray()
                        speech_frames = 0
                        silence_frames = 0
                        speech_energies = []

                # Send emotion updates only on change, at most once per 1s
                cmd = self.brain.emotion_fsm.to_command()
                cmd_json = json.dumps(cmd, sort_keys=True)
                now = time.monotonic()
                if cmd_json != last_command_json or (now - last_command_send_time) >= 1.0:
                    last_command_json = cmd_json
                    last_command_send_time = now
                    await self._send_command(cmd)

            except Exception as e:
                logger.error(f"Process loop error: {e}")

    async def _transcribe(self, pcm_data: bytes) -> str:
        """Run STT on accumulated PCM data."""
        try:
            # Save raw PCM to WAV for debugging (first 3 utterances only)
            self._debug_save_count = getattr(self, '_debug_save_count', 0) + 1
            if self._debug_save_count <= 3:
                import wave
                import os
                dump_dir = "/tmp/ai-cat-debug"
                os.makedirs(dump_dir, exist_ok=True)
                wav_path = os.path.join(dump_dir, f"utterance_{self._debug_save_count}.wav")
                with wave.open(wav_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)  # 16-bit
                    wf.setframerate(16000)
                    wf.writeframes(pcm_data)
                logger.info(f"DEBUG: saved {wav_path} ({len(pcm_data)} bytes) — play with: ffplay {wav_path}")

            text = self.stt.transcribe(pcm_data)
            return text
        except Exception as e:
            logger.error(f"STT error: {e}")
            return ""

    async def _respond(self, user_text: str):
        """Generate and send cat response to user speech."""
        command = await self.brain.process(
            user_text=user_text,
            touch_values=self.touch_values
        )

        # Send command
        await self._send_command(command)

        # Synthesize and send audio response
        if command.get("response_text"):
            logger.info(f"Cat says: {command['response_text']}")
            try:
                opus_frames = await self.tts.synthesize_opus_frames(command["response_text"])
                if opus_frames:
                    logger.info(f"TTS: {len(opus_frames)} Opus frames, sending to ESP32...")
                    for i, frame in enumerate(opus_frames):
                        await self.ws.send(frame)
                        await asyncio.sleep(0.01)  # 10ms gap between frames
                    logger.info(f"TTS: sent {len(opus_frames)} frames OK")
                else:
                    logger.warning("TTS returned empty data")
            except Exception as e:
                logger.error(f"TTS error: {e}")
        else:
            logger.info("No response text generated")

    async def feed_text(self, text: str):
        """Accept text input from console (bypasses STT pipeline)."""
        logger.info(f"Console input: {text}")
        await self._respond(text)

    async def _send_command(self, command: dict):
        """Send JSON command to ESP32."""
        cmd = {
            "type": "command",
            "emotion": command.get("emotion", "content"),
            "ear_left_deg": command.get("ear_left_deg", 60),
            "ear_right_deg": command.get("ear_right_deg", 55),
            "vibration": command.get("vibration", 100),
            "has_audio": command.get("has_audio", False),
        }
        try:
            await self.ws.send(json.dumps(cmd))
        except Exception as e:
            logger.error(f"Send command error: {e}")
