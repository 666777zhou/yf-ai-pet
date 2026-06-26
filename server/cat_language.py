"""Cat Language Engine — real cat sounds instead of TTS.

Two modes of operation:
1. VAD slicing: python cat_language.py --slice-all
   Processes long WAV recordings in cat_sounds/ into short clips (0.3-2s).
   Outputs clips/ directory + clips.json manifest.

2. Runtime: generates Opus frames from emotion → clip mapping.
   CatLanguageEngine.generate(emotion) → list[bytes] (Opus frames, 60ms each)
"""

from __future__ import annotations

import json
import logging
import os
import random
import struct
import sys
import wave

logger = logging.getLogger(__name__)

CAT_SOUNDS_DIR = os.path.join(os.path.dirname(__file__), "cat_sounds")
CLIPS_DIR = os.path.join(CAT_SOUNDS_DIR, "clips")
CLIPS_JSON = os.path.join(CAT_SOUNDS_DIR, "clips.json")

SAMPLE_RATE = 16000
FRAME_MS = 60
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 960

# Emotion → source file basenames (without .wav)
EMOTION_SOURCE_MAP: dict[str, list[str]] = {
    "content":  ["purring"],
    "sleepy":   ["purring"],
    "playful":  ["trill", "chatter"],
    "curious":  ["chatter", "miaow", "trill"],
    "annoyed":  ["hiss_and_spit", "snarl_and_growl"],
    "scared":   ["scream_screech", "distress_call", "howl_and_yowl"],
}


# ---------------------------------------------------------------------------
# VAD slicing — offline, for processing long recordings into short clips
# ---------------------------------------------------------------------------

def _read_wav(path: str) -> tuple[list[int], int]:
    """Read 16-bit mono WAV, return (samples, sample_rate)."""
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    samples = list(struct.unpack(f"<{len(raw)//2}h", raw))
    # Downmix stereo → mono by averaging
    if nch == 2:
        samples = [(samples[i] + samples[i+1]) // 2 for i in range(0, len(samples), 2)]
    # Resample to 16kHz if needed
    if sr != SAMPLE_RATE:
        import numpy as np
        arr = np.array(samples, dtype=np.float32)
        new_len = int(len(arr) * SAMPLE_RATE / sr)
        arr = np.interp(
            np.linspace(0, len(arr) - 1, new_len),
            np.arange(len(arr)),
            arr,
        ).astype(np.int16)
        samples = arr.tolist()
    return samples, SAMPLE_RATE


def _write_wav(path: str, samples: list[int], sr: int = SAMPLE_RATE):
    """Write 16-bit mono WAV."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    raw = struct.pack(f"<{len(samples)}h", *samples)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(raw)


def _short_time_energy(samples: list[int], window_ms: int = 30) -> list[float]:
    """Compute short-time energy (RMS normalized) for each window."""
    win = SAMPLE_RATE * window_ms // 1000
    energies = []
    for i in range(0, len(samples) - win + 1, win // 2):  # 50% overlap
        chunk = samples[i:i + win]
        rms = (sum(s * s for s in chunk) / len(chunk)) ** 0.5
        energies.append(rms / 32768.0)
    return energies


def vad_slice_file(wav_path: str,
                   min_duration_s: float = 0.3,
                   max_duration_s: float = 2.0,
                   gap_merge_s: float = 0.2,
                   energy_factor: float = 0.15) -> list[tuple[int, int]]:
    """Slice a long WAV into short utterance segments using energy VAD.

    Returns list of (start_sample, end_sample) tuples.
    """
    samples, sr = _read_wav(wav_path)
    energies = _short_time_energy(samples, window_ms=30)
    if not energies:
        return []

    step = SAMPLE_RATE * 30 // 1000 // 2  # 15ms step (50% overlap of 30ms windows)

    # Adaptive threshold: floor + factor × (max - floor)
    sorted_e = sorted(energies)
    floor = sorted_e[max(0, len(sorted_e) * 2 // 10)]  # 20th percentile
    peak = sorted_e[-min(5, len(sorted_e))]  # near-max (avoid outliers)
    threshold = max(floor + energy_factor * (peak - floor), 0.005)

    logger.info(
        f"  {os.path.basename(wav_path)}: floor={floor:.4f}, peak={peak:.4f}, "
        f"threshold={threshold:.4f}"
    )

    # Find active regions
    active = [e > threshold for e in energies]
    segments: list[tuple[int, int]] = []
    in_active = False
    seg_start = 0

    for i, a in enumerate(active):
        sample_pos = i * step
        if a and not in_active:
            seg_start = sample_pos
            in_active = True
        elif not a and in_active:
            seg_end = sample_pos
            duration = (seg_end - seg_start) / sr
            if min_duration_s <= duration <= max_duration_s:
                segments.append((seg_start, seg_end))
            in_active = False

    # Don't forget trailing segment
    if in_active:
        seg_end = len(samples)
        duration = (seg_end - seg_start) / sr
        if min_duration_s <= duration <= max_duration_s:
            segments.append((seg_start, seg_end))

    # Merge close gaps
    gap_samples = int(gap_merge_s * sr)
    merged = []
    for seg in segments:
        if merged and seg[0] - merged[-1][1] < gap_samples:
            # Merge: extend previous segment, but respect max_duration
            new_end = seg[1]
            new_start = merged[-1][0]
            if (new_end - new_start) / sr <= max_duration_s * 1.3:  # 30% tolerance
                merged[-1] = (new_start, new_end)
                continue
        merged.append(seg)

    logger.info(f"    → {len(segments)} raw segments, {len(merged)} after merging")
    return merged


def slice_all_sounds():
    """Process all cat_sounds/*.wav files into clips/. Returns clips.json data."""
    os.makedirs(CLIPS_DIR, exist_ok=True)

    wav_files = sorted(
        f for f in os.listdir(CAT_SOUNDS_DIR)
        if f.endswith(".wav") and os.path.isfile(os.path.join(CAT_SOUNDS_DIR, f))
    )

    if not wav_files:
        logger.error("No WAV files found in %s", CAT_SOUNDS_DIR)
        return []

    # Clear old clips
    for old in os.listdir(CLIPS_DIR):
        if old.endswith(".wav"):
            os.remove(os.path.join(CLIPS_DIR, old))

    all_clips = []
    for wav_file in wav_files:
        wav_path = os.path.join(CAT_SOUNDS_DIR, wav_file)
        base = wav_file.replace(".wav", "")

        logger.info(f"Slicing {wav_file} ({os.path.getsize(wav_path)/1024:.0f} KB)...")
        segments = vad_slice_file(wav_path)

        samples, sr = _read_wav(wav_path)
        for i, (start, end) in enumerate(segments):
            clip_name = f"{base}_{i:02d}.wav"
            clip_path = os.path.join(CLIPS_DIR, clip_name)
            _write_wav(clip_path, samples[start:end], sr)

            duration = (end - start) / sr
            all_clips.append({
                "file": clip_name,
                "source": base,
                "duration_s": round(duration, 2),
            })
            logger.info(f"  → {clip_name} ({duration:.2f}s)")

    # Build emotion index
    # For each clip, determine which emotions it maps to based on source file
    for clip in all_clips:
        source = clip["source"]
        emotions = [
            emo for emo, sources in EMOTION_SOURCE_MAP.items()
            if source in sources
        ]
        clip["emotions"] = emotions if emotions else ["curious"]  # fallback

    # Write manifest
    manifest = {
        "total_clips": len(all_clips),
        "clips": all_clips,
    }
    with open(CLIPS_JSON, "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # Stats
    by_emotion: dict[str, int] = {}
    for clip in all_clips:
        for emo in clip["emotions"]:
            by_emotion[emo] = by_emotion.get(emo, 0) + 1

    logger.info(f"✓ Sliced {len(all_clips)} clips total")
    for emo, count in sorted(by_emotion.items()):
        logger.info(f"  {emo}: {count} clips")
    logger.info(f"✓ Manifest saved to {CLIPS_JSON}")

    return all_clips


# ---------------------------------------------------------------------------
# CatLanguageEngine — runtime
# ---------------------------------------------------------------------------

class CatLanguageEngine:
    """Picks and plays real cat sounds based on emotion.

    Usage:
        engine = CatLanguageEngine()
        opus_frames = await engine.generate("playful")
        # → list of Opus-encoded bytes, ready to send to ESP32
    """

    def __init__(self, clips_dir: str = CLIPS_DIR, manifest_path: str = CLIPS_JSON):
        self.clips_dir = clips_dir
        self.clips: list[dict] = []
        self._by_emotion: dict[str, list[dict]] = {}
        self._opus_encoder = None
        self._load(manifest_path)

    def _load(self, manifest_path: str):
        if not os.path.exists(manifest_path):
            logger.warning(f"No clips manifest found at {manifest_path}. "
                           f"Run: python cat_language.py --slice-all")
            return

        with open(manifest_path) as f:
            data = json.load(f)

        self.clips = data.get("clips", [])
        self._by_emotion.clear()
        for clip in self.clips:
            for emo in clip.get("emotions", []):
                self._by_emotion.setdefault(emo, []).append(clip)

        logger.info(
            f"CatLanguageEngine: {len(self.clips)} clips, "
            f"{len(self._by_emotion)} emotions"
        )

    def _get_opus_encoder(self):
        """Lazy-init Opus encoder (shared across calls)."""
        if self._opus_encoder is None:
            import opuslib
            enc = opuslib.Encoder(16000, 1, opuslib.APPLICATION_AUDIO)
            enc.bitrate = 32000
            self._opus_encoder = enc
        return self._opus_encoder

    async def generate(self, emotion: str) -> list[bytes]:
        """Pick a random clip for the given emotion, return Opus frames.

        Falls back to 'curious' if the emotion has no clips.
        Returns empty list if no clips available at all.
        """
        candidates = self._by_emotion.get(emotion)
        if not candidates:
            # Try fallback emotions
            for fallback in ["curious", "playful", "content"]:
                candidates = self._by_emotion.get(fallback)
                if candidates:
                    logger.debug(
                        f"Cat language: '{emotion}'→'{fallback}' fallback"
                    )
                    break
        if not candidates:
            logger.warning(f"No cat sound clips for emotion '{emotion}'")
            return []

        clip = random.choice(candidates)
        clip_path = os.path.join(self.clips_dir, clip["file"])

        if not os.path.exists(clip_path):
            logger.error(f"Clip not found: {clip_path}")
            return []

        # Read WAV
        import io
        pcm_bytes = _wav_to_pcm_bytes(clip_path)
        if not pcm_bytes:
            return []

        # Encode to Opus frames (60ms each)
        return await self._pcm_to_opus(pcm_bytes, clip["duration_s"])

    async def _pcm_to_opus(self, pcm_bytes: bytes, duration_hint: float = 0) -> list[bytes]:
        """Encode 16kHz mono int16 PCM to 60ms Opus frames."""
        import asyncio
        encoder = self._get_opus_encoder()
        frame_bytes = FRAME_SAMPLES * 2  # 1920

        loop = asyncio.get_running_loop()

        def _encode():
            frames = []
            for i in range(0, len(pcm_bytes) - frame_bytes + 1, frame_bytes):
                chunk = pcm_bytes[i:i + frame_bytes]
                if len(chunk) == frame_bytes:
                    frames.append(encoder.encode(chunk, FRAME_SAMPLES))
            return frames

        opus_frames = await loop.run_in_executor(None, _encode)

        logger.info(
            f"Cat sound: {len(pcm_bytes)}B PCM ({duration_hint:.1f}s) "
            f"→ {len(opus_frames)} Opus frames"
        )
        return opus_frames

    @property
    def has_clips(self) -> bool:
        return len(self.clips) > 0


def _wav_to_pcm_bytes(wav_path: str) -> bytes:
    """Read a WAV file and return raw 16kHz mono int16 PCM bytes."""
    try:
        samples, sr = _read_wav(wav_path)
        return struct.pack(f"<{len(samples)}h", *samples)
    except Exception as e:
        logger.error(f"Failed to read WAV {wav_path}: {e}")
        return b""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "--slice-all":
        logger.info("Slicing all cat_sounds into clips...")
        slice_all_sounds()
    else:
        print("Usage: python cat_language.py --slice-all")
        print("  Processes cat_sounds/*.wav into clips/ + clips.json")
