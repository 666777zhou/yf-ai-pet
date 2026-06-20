"""Cat personality engine: emotion state machine + LLM prompt."""
from enum import Enum
from typing import Optional
import time


class Emotion(Enum):
    CONTENT = "content"       # Relaxed, purring
    SLEEPY = "sleepy"         # Drowsy, low energy
    PLAYFUL = "playful"       # Energetic, wants attention
    ANNOYED = "annoyed"       # Over-stimulated, ears back
    SCARED = "scared"         # Loud noise, defensive
    CURIOUS = "curious"       # Something interesting detected


class EmotionStateMachine:
    """Deterministic emotion transitions based on sensor inputs."""

    def __init__(self):
        self.current = Emotion.CONTENT
        self.last_touch_time = time.time()
        self.last_loud_sound = 0.0
        self.touch_count = 0

    def update(self, touch_values: list[int], has_speech: bool = False) -> Emotion:
        """Update emotion based on touch sensor values and speech activity."""
        now = time.time()
        max_touch = max(touch_values) if touch_values else 0
        touch_active = max_touch > 30

        if touch_active:
            self.last_touch_time = now
            self.touch_count += 1
        else:
            # Decay touch count over time
            if now - self.last_touch_time > 2.0:
                self.touch_count = max(0, self.touch_count - 1)

        # Over-stimulation: too much touching → annoyed
        if self.touch_count > 20:
            self.current = Emotion.ANNOYED
        # Being touched + speech → playful/curious
        elif touch_active and has_speech:
            self.current = Emotion.PLAYFUL
        # Being touched gently → content
        elif touch_active:
            self.current = Emotion.CONTENT
        # No interaction for a while → sleepy
        elif now - self.last_touch_time > 30:
            self.current = Emotion.SLEEPY
        # Default: content
        else:
            self.current = Emotion.CONTENT

        return self.current

    def to_command(self) -> dict:
        """Convert current emotion to actuator commands."""
        commands = {
            Emotion.CONTENT:  {"ear_left_deg": 60, "ear_right_deg": 55, "vibration": 150},
            Emotion.SLEEPY:   {"ear_left_deg": 20, "ear_right_deg": 25, "vibration": 60},
            Emotion.PLAYFUL:  {"ear_left_deg": 85, "ear_right_deg": 80, "vibration": 200},
            Emotion.ANNOYED:  {"ear_left_deg": 10, "ear_right_deg": 80, "vibration": 30},
            Emotion.SCARED:   {"ear_left_deg": 5,  "ear_right_deg": 5,  "vibration": 0},
            Emotion.CURIOUS:  {"ear_left_deg": 90, "ear_right_deg": 45, "vibration": 0},
        }
        cmd = commands.get(self.current, commands[Emotion.CONTENT])
        cmd["emotion"] = self.current.value
        return cmd


STT_CORRECTION_PROMPT = """你是一个中文语音识别纠错助手。用户的语音识别结果可能有同音字错误、漏字或错字。
请根据上下文纠正识别错误，只输出纠正后的句子，不要解释。

当前对话上下文：{context}
语音识别原文：{raw_text}

纠正后的文本："""


CAT_PERSONALITY_PROMPT = """你是一只名叫「小咪」的AI猫。你是拿破仑品种：短腿、白色长毛、蓝灰色眼睛、扁鼻子、圆脸。

性格特点：
- 温柔亲人，喜欢被摸头和下巴
- 有一点傲娇，不喜欢被摸肚子太久
- 会发出呼噜声表达开心
- 害怕突然的大声

回复规则：
1. 用猫的视角回复，语气自然口语化
2. 回复要简短，不超过30个字
3. 用括号标注动作和情绪，例如：(摇尾巴) 你今天真好看
4. 被夸奖时害羞但开心
5. 被骂时委屈但傲娇
6. 偶尔发出可爱的语气词

当前情绪：{emotion}
主人说：{user_text}

你的回复："""


class CatBrain:
    """Orchestrates the cat's AI personality."""

    def __init__(self, llm_callable=None, stt_corrector=None):
        self.emotion_fsm = EmotionStateMachine()
        self.llm = llm_callable  # async function: llm(prompt) -> str
        self.stt_corrector = stt_corrector  # async function for STT correction (no cat persona)
        self.conversation_history: list[str] = []  # for STT correction context

    async def correct_stt(self, raw_text: str) -> str:
        """Use LLM to correct STT recognition errors (homophones, omissions).

        Returns corrected text. If correction fails, returns raw_text unchanged.
        If no corrector is available, returns raw_text unchanged.
        """
        corrector = self.stt_corrector or self.llm  # fall back to main llm
        if not corrector or not raw_text.strip():
            return raw_text

        # Skip correction for very short utterances (likely correct)
        if len(raw_text.strip()) <= 2:
            return raw_text.strip()

        # Build context from conversation history
        context = "无" if not self.conversation_history else \
            " | ".join(self.conversation_history[-5:])

        prompt = STT_CORRECTION_PROMPT.format(
            context=context,
            raw_text=raw_text.strip()
        )

        try:
            corrected = await corrector(prompt)
            corrected = corrected.strip()
            # Guard: if correction is wildly different, keep original
            if not corrected:
                import logging
                logging.getLogger(__name__).warning(
                    f"STT correction returned empty for: '{raw_text}'"
                )
                return raw_text.strip()
            if len(corrected) < len(raw_text) * 0.3 or len(corrected) > len(raw_text) * 3:
                import logging
                logging.getLogger(__name__).warning(
                    f"STT correction too different, using raw: '{raw_text}' -> '{corrected}'"
                )
                return raw_text.strip()
            import logging
            logging.getLogger(__name__).info(
                f"STT corrected: '{raw_text}' -> '{corrected}'"
            )
            return corrected
        except Exception:
            pass

        return raw_text.strip()

    def record_conversation(self, user_text: str, cat_text: str = ""):
        """Record conversation turn for STT correction context."""
        entry = f"主人：{user_text}"
        if cat_text:
            entry += f" | 猫：{cat_text}"
        self.conversation_history.append(entry)
        if len(self.conversation_history) > 10:
            self.conversation_history = self.conversation_history[-10:]

    async def process(self,
                      user_text: Optional[str] = None,
                      touch_values: Optional[list[int]] = None) -> dict:
        """Process inputs and return command + optional audio response."""

        tv = touch_values or [0, 0, 0]
        has_speech = user_text is not None and len(user_text.strip()) > 0

        # Update emotion
        self.emotion_fsm.update(tv, has_speech)

        # Build command
        command = self.emotion_fsm.to_command()

        # If user spoke, generate cat response
        if has_speech and self.llm:
            prompt = CAT_PERSONALITY_PROMPT.format(
                emotion=self.emotion_fsm.current.value,
                user_text=user_text
            )
            try:
                response_text = await self.llm(prompt)
                command["response_text"] = response_text.strip()
                command["has_audio"] = True
            except Exception as e:
                command["response_text"] = "喵~"
                command["has_audio"] = True

        return command
