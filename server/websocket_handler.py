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

    # Max conversation turns to keep for STT context
    MAX_CONTEXT_TURNS = 5

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
        # Conversation history for STT context priming
        self.conversation_history: list[dict[str, str]] = []

    def _build_stt_context(self) -> str:
        """Build initial_prompt for Whisper from recent conversation history."""
        if not self.conversation_history:
            return ""  # STTEngine will use its DEFAULT_INITIAL_PROMPT

        lines = ["最近对话："]
        for turn in self.conversation_history[-self.MAX_CONTEXT_TURNS:]:
            if turn.get("user"):
                lines.append(f"主人说：{turn['user']}")
            if turn.get("cat"):
                lines.append(f"猫说：{turn['cat']}")
        return "\n".join(lines)

    def _record_conversation(self, user_text: str, cat_text: str = ""):
        """Record a conversation turn for STT context."""
        self.conversation_history.append({"user": user_text, "cat": cat_text})
        # Keep only recent history
        if len(self.conversation_history) > self.MAX_CONTEXT_TURNS * 2:
            self.conversation_history = self.conversation_history[-self.MAX_CONTEXT_TURNS:]

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
                    await self._handle_audio(message)
                else:
                    await self._handle_json(message)
        except Exception as e:
            if self.running:
                logger.warning(f"Connection closed: {e}")
        finally:
            self.running = False  # signal _process_loop to stop

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

    async def _handle_audio(self, opus_frame: bytes):
        """Handle incoming Opus-encoded audio from ESP32 microphone.

        Decode Opus → PCM, then append to VAD buffer.
        Each decoded frame is 1920 bytes (960 int16_t samples @ 16kHz mono).
        """
        try:
            # Decode Opus → PCM
            pcm_bytes = self.codec.decode(opus_frame)
            if len(pcm_bytes) != FRAME_BYTES:
                logger.warning(f"Unexpected decoded PCM size: {len(pcm_bytes)}, expected {FRAME_BYTES}")
                if len(pcm_bytes) < FRAME_BYTES:
                    return  # skip partial frames

            # Append to VAD buffer
            self.pcm_buffer.extend(pcm_bytes[:FRAME_BYTES])
        except Exception as e:
            logger.error(f"Audio receive error: {e}")

    async def _process_loop(self):
        """Periodically process sensor data and generate responses.

        Streaming pipeline:
        1. VAD on each 60ms frame (energy-based)
        2. Speech frames accumulated, silence frames NOT appended
        3. Background STT task starts once minimum speech gathered (~500ms)
        4. When silence threshold hits (~480ms), finalize STT → LLM → TTS
        """
        accumulated_speech_pcm = bytearray()
        speech_frames = 0
        silence_frames = 0
        speech_energies: list[float] = []
        stt_task: asyncio.Task | None = None  # background streaming STT
        last_command_json = ""
        last_command_send_time = time.monotonic()

        # Thresholds (tuned for responsive conversation)
        MIN_SPEECH_FRAMES = 5       # ~300ms minimum speech
        STREAM_STT_TRIGGER = 10     # start background STT after ~600ms of speech
        SILENCE_THRESHOLD = 8       # ~480ms silence → end of utterance
        MAX_UTTERANCE_FRAMES = 50   # ~3s max utterance (force-finalize)

        async def _finalize_utterance(pcm_copy: bytes, frames: int, energies: list[float]):
            """Run STT on collected speech and trigger LLM+TTS response."""
            avg_energy = sum(energies) / len(energies) if energies else 0
            logger.info(f"Utterance end: {frames} frames, avg_energy={avg_energy:.4f}")

            if avg_energy > 0.01:
                user_text = await self._transcribe(pcm_copy)
                if user_text:
                    logger.info(f"User said: {user_text}")
                    await self._respond(user_text)
            else:
                logger.info(f"Utterance rejected (avg energy {avg_energy:.4f} < 0.01)")

        while self.running:
            try:
                await asyncio.sleep(0.06)  # 60ms = one audio frame

                # Process incoming audio frame
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
                            logger.info(f"VAD ON: energy={energy:.4f}, threshold={self.vad.current_threshold:.4f}, "
                                        f"noise_floor={self.vad.noise_floor:.4f}")

                        # Start background STT once we have enough speech
                        # This runs concurrently while user is still speaking
                        if speech_frames == STREAM_STT_TRIGGER and stt_task is None:
                            audio_snapshot = bytes(accumulated_speech_pcm)
                            stt_task = asyncio.create_task(
                                self._transcribe(audio_snapshot)
                            )
                            logger.debug(f"Background STT started with {speech_frames} frames")

                    elif speech_frames > 0:
                        silence_frames += 1
                        # Short hangover: keep 2 frames (120ms) of leading silence for context
                        if silence_frames <= 2:
                            accumulated_speech_pcm.extend(frame)

                    # --- End-of-utterance detection ---

                    # Condition A: silence threshold reached
                    if speech_frames >= MIN_SPEECH_FRAMES and silence_frames > SILENCE_THRESHOLD:
                        pcm_copy = bytes(accumulated_speech_pcm)
                        frames_snapshot = speech_frames
                        energies_snapshot = list(speech_energies)

                        # Reset state immediately so new speech can accumulate
                        accumulated_speech_pcm = bytearray()
                        speech_frames = 0
                        silence_frames = 0
                        speech_energies = []
                        stt_pending = stt_task
                        stt_task = None

                        # If background STT already running, wait for it
                        # (it may have partial result, but that's better than waiting fresh)
                        if stt_pending and not stt_pending.done():
                            try:
                                partial_text = await stt_pending
                                if partial_text:
                                    logger.info(f"Using streaming STT result: {partial_text}")
                                    # Streaming STT caught something — finalize with it
                                    avg_energy = sum(energies_snapshot) / len(energies_snapshot)
                                    if avg_energy > 0.015:
                                        logger.info(f"User said: {partial_text}")
                                        await self._respond(partial_text)
                                    continue  # skip running fresh STT
                            except Exception:
                                pass  # fall through to fresh STT

                        # Run fresh STT on complete utterance
                        await _finalize_utterance(pcm_copy, frames_snapshot, energies_snapshot)

                    # Condition B: max utterance length reached (force-finalize)
                    elif speech_frames > MAX_UTTERANCE_FRAMES:
                        pcm_copy = bytes(accumulated_speech_pcm)
                        frames_snapshot = speech_frames
                        energies_snapshot = list(speech_energies)

                        accumulated_speech_pcm = bytearray()
                        speech_frames = 0
                        silence_frames = 0
                        speech_energies = []
                        if stt_task and not stt_task.done():
                            stt_task.cancel()
                        stt_task = None

                        await _finalize_utterance(pcm_copy, frames_snapshot, energies_snapshot)

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
        """Run STT on accumulated PCM data with conversation context."""
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

            context = self._build_stt_context()
            text = self.stt.transcribe(pcm_data, initial_prompt=context)
            return text
        except Exception as e:
            logger.error(f"STT error: {e}")
            return ""

    async def _respond(self, user_text: str):
        """Generate and send cat response to user speech.

        Pipeline: CatBrain (emotion + LLM) → TTS audio
        (STT correction skipped — DeepSeek rarely corrects short utterances, costs ~1s/API)
        """
        # Run cat personality engine
        command = await self.brain.process(
            user_text=user_text,
            touch_values=self.touch_values
        )

        # Send command
        await self._send_command(command)

        # Synthesize and send audio response
        if command.get("response_text"):
            raw_text = command["response_text"]
            logger.info(f"Cat says: {raw_text}")
            # Record conversation turn for STT context in future utterances
            self._record_conversation(user_text, raw_text)
            self.brain.record_conversation(user_text, raw_text)
            # Strip parenthetical stage directions before TTS
            import re
            speak_text = re.sub(r'[（(][^)）]*[)）]', '', raw_text).strip()
            try:
                opus_frames = await self.tts.synthesize_opus_frames(speak_text or raw_text)
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
        if not self.running:
            return
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
        except Exception:
            self.running = False  # connection dead, stop all loops
