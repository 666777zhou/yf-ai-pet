"""Voice profile manager for AI Cat TTS.

Supports:
- Fish-Speech S2 Pro (local, high-quality, voice cloning, CUDA)
- Piper ONNX voices (fast, fixed timbre, ~60MB each)
- GPT-SoVITS (zero-shot cloning from 5s reference audio, via HTTP API)

Voice profiles are stored in server/voices/profiles.json.
Reference audio for cloned voices is stored in server/voices/ref_<id>.wav.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

VOICES_DIR = os.path.join(os.path.dirname(__file__), "voices")
PROFILES_PATH = os.path.join(VOICES_DIR, "profiles.json")

# Built-in voices
BUILTIN_QWEN_TTS_VOICES = {
    "qwen-xiaoye": {
        "name": "小叶（克隆音色）",
        "ref_audio": "/home/zyf/Code/fish-speech/scripts/reference.wav",
        "prompt_text": "那肯定是可以的啦，这是绝对没问题的。而且，不但会相处到一块儿去，还会关系贼拉好。而且，而且最重要的是，他俩会争风吃醋，为了你呀。",
    },
}

BUILTIN_FISH_SPEECH_VOICES = {
    "fish-default": "Fish Speech 默认音色（纯合成）",
}

BUILTIN_PIPER_VOICES = {
    "zh_CN-huayan-medium": "花烟（女声）",
}

DEFAULT_ACTIVE = "qwen-xiaoye"


@dataclass
class VoiceProfile:
    """A voice profile for TTS."""
    id: str           # unique id, e.g. "fish-default" or "piper-huayan"
    name: str         # display name, e.g. "Fish Speech 默认音色"
    engine: str       # "fish-speech", "piper", or "gpt-sovits"
    model: str = ""   # Piper model filename (without path) or ""
    ref_audio: str = ""     # path to reference WAV for voice cloning
    prompt_text: str = ""   # transcript of ref_audio (required for fish-speech cloning)


class VoiceManager:
    """Manages voice profiles — list, add, switch, persist."""

    def __init__(self):
        os.makedirs(VOICES_DIR, exist_ok=True)
        self.profiles: dict[str, VoiceProfile] = {}
        self.active_id: str = DEFAULT_ACTIVE
        self._load_or_init()

    # ---- persistence ----

    def _load_or_init(self):
        if os.path.exists(PROFILES_PATH):
            try:
                with open(PROFILES_PATH) as f:
                    data = json.load(f)
                self.active_id = data.get("active", DEFAULT_ACTIVE)
                for pid, pdata in data.get("profiles", {}).items():
                    self.profiles[pid] = VoiceProfile(**pdata)
                logger.info(f"Loaded {len(self.profiles)} voice profiles, active={self.active_id}")
                return
            except Exception as e:
                logger.warning(f"Failed to load profiles: {e}, re-initializing")

        # First run: create default profiles
        self._init_defaults()
        self._save()

    def _init_defaults(self):
        # Qwen3-TTS default voice (cloned)
        for vid, info in BUILTIN_QWEN_TTS_VOICES.items():
            if isinstance(info, dict):
                self.profiles[vid] = VoiceProfile(
                    id=vid,
                    name=info["name"],
                    engine="qwen-tts",
                    ref_audio=info["ref_audio"],
                    prompt_text=info["prompt_text"],
                )
            else:
                self.profiles[vid] = VoiceProfile(
                    id=vid,
                    name=info,
                    engine="qwen-tts",
                )

        # Fish-Speech default voice (no cloning)
        for vid, display_name in BUILTIN_FISH_SPEECH_VOICES.items():
            self.profiles[vid] = VoiceProfile(
                id=vid,
                name=display_name,
                engine="fish-speech",
            )

        # Piper voices
        for model_name, display_name in BUILTIN_PIPER_VOICES.items():
            pid = f"piper-{model_name.split('-')[1]}"  # "piper-huayan"
            self.profiles[pid] = VoiceProfile(
                id=pid,
                name=display_name,
                engine="piper",
                model=model_name,
            )

    def _save(self):
        data = {
            "active": self.active_id,
            "profiles": {pid: asdict(p) for pid, p in self.profiles.items()},
        }
        with open(PROFILES_PATH, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---- queries ----

    def get_active(self) -> VoiceProfile:
        """Return the currently active voice profile."""
        if self.active_id in self.profiles:
            return self.profiles[self.active_id]
        # Fallback to first available
        if self.profiles:
            first = next(iter(self.profiles.values()))
            self.active_id = first.id
            return first
        raise RuntimeError("No voice profiles available")

    def list_voices(self) -> list[VoiceProfile]:
        """Return all voice profiles."""
        return list(self.profiles.values())

    def get_profile(self, profile_id: str) -> VoiceProfile | None:
        return self.profiles.get(profile_id)

    # ---- mutations ----

    def set_active(self, profile_id: str) -> bool:
        """Switch to a different voice profile. Returns True on success."""
        if profile_id in self.profiles:
            self.active_id = profile_id
            self._save()
            logger.info(f"Voice switched to: {self.profiles[profile_id].name}")
            return True
        logger.warning(f"Voice profile not found: {profile_id}")
        return False

    def add_custom_voice(self, name: str, ref_audio_path: str,
                         prompt_text: str = "", engine: str = "fish-speech") -> VoiceProfile | None:
        """Register a new cloned voice from a reference audio file.

        Copies the reference audio into voices/ directory.
        For fish-speech, prompt_text (transcript of the reference) is required for cloning.
        """
        if not os.path.exists(ref_audio_path):
            logger.error(f"Reference audio not found: {ref_audio_path}")
            return None

        # Generate a safe id from name
        safe_id = engine[:3] + "-" + name.lower().replace(" ", "-")[:30]
        ref_dest = os.path.join(VOICES_DIR, f"ref_{safe_id}.wav")
        shutil.copy2(ref_audio_path, ref_dest)

        profile = VoiceProfile(
            id=safe_id,
            name=name,
            engine=engine,
            ref_audio=ref_dest,
            prompt_text=prompt_text,
        )
        self.profiles[safe_id] = profile
        self._save()
        logger.info(f"Added custom voice: {name} (id={safe_id}, engine={engine}, ref={ref_dest})")
        return profile

    def delete_voice(self, profile_id: str) -> bool:
        """Delete a voice profile. Cannot delete built-in Piper voices."""
        if profile_id not in self.profiles:
            return False
        profile = self.profiles[profile_id]
        if profile.engine == "piper":
            logger.warning("Cannot delete built-in Piper voices")
            return False
        # Clean up reference audio
        if profile.ref_audio and os.path.exists(profile.ref_audio):
            os.remove(profile.ref_audio)
        del self.profiles[profile_id]
        if self.active_id == profile_id:
            self.active_id = DEFAULT_ACTIVE
        self._save()
        return True
