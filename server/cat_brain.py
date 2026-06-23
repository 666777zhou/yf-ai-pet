"""Cat personality engine: emotion state machine + LLM prompt."""
from enum import Enum
from typing import Optional
import random
import re
import time


class Emotion(Enum):
    CONTENT = "content"       # Relaxed, purring
    SLEEPY = "sleepy"         # Drowsy, low energy
    PLAYFUL = "playful"       # Energetic, wants attention
    ANNOYED = "annoyed"       # Over-stimulated, ears back
    SCARED = "scared"         # Loud noise, defensive
    CURIOUS = "curious"       # Something interesting detected


class EmotionStateMachine:
    """Dynamic emotion transitions driven by touch, speech, LLM output, and time."""

    def __init__(self):
        self.current = Emotion.CONTENT
        self.last_touch_time = time.time()
        self.last_speech_time = time.time()
        self.last_emotion_change = time.time()
        self.touch_count = 0
        self.conversation_turns = 0

    def update(self, touch_values: list[int], has_speech: bool = False,
               llm_response_text: str = "") -> Emotion:
        """Update emotion based on inputs.

        Args:
            touch_values: [head, back, belly] touch sensor readings (0-100)
            has_speech: True if user just spoke
            llm_response_text: the cat's generated response (used to extract mood cues)
        """
        now = time.time()
        max_touch = max(touch_values) if touch_values else 0
        touch_active = max_touch > 50

        # ---- Track touch patterns ----
        if touch_active:
            self.last_touch_time = now
            self.touch_count += 1
        else:
            if now - self.last_touch_time > 3.0:
                self.touch_count = max(0, self.touch_count - 1)

        if has_speech:
            self.last_speech_time = now
            self.conversation_turns += 1

        # ---- Try to read emotion from LLM response ----
        llm_emotion = None
        if llm_response_text:
            llm_emotion = self._extract_emotion_from_text(llm_response_text)

        # ---- Emotion selection ----
        new_emotion = self.current  # default: stay unchanged

        if self.touch_count > 15:
            # Over-stimulated → annoyed
            new_emotion = Emotion.ANNOYED
        elif touch_active and has_speech:
            # Active petting + talking → vary between playful/curious/content
            r = random.random()
            if r < 0.40:
                new_emotion = Emotion.PLAYFUL
            elif r < 0.70:
                new_emotion = Emotion.CURIOUS
            else:
                new_emotion = Emotion.CONTENT
        elif llm_emotion:
            # LLM response contains emotion cues → follow them
            new_emotion = llm_emotion
        elif has_speech:
            # Speech without touch → engaged but varied
            r = random.random()
            if r < 0.25:
                new_emotion = Emotion.CURIOUS
            elif r < 0.45:
                new_emotion = Emotion.PLAYFUL
            else:
                new_emotion = Emotion.CONTENT
        elif touch_active:
            # Gentle petting without speech → content
            new_emotion = Emotion.CONTENT
        elif now - self.last_touch_time > 60 and now - self.last_speech_time > 60:
            # Long idle → sleepy
            new_emotion = Emotion.SLEEPY
        elif (now - self.last_emotion_change > 20 and
              now - self.last_speech_time > 10 and
              random.random() < 0.12):
            # Occasional spontaneous mood swing when idle
            new_emotion = random.choice([Emotion.CONTENT, Emotion.CURIOUS, Emotion.PLAYFUL])
        else:
            # Default: relaxed content
            new_emotion = Emotion.CONTENT

        if new_emotion != self.current:
            self.current = new_emotion
            self.last_emotion_change = now

        return self.current

    def _extract_emotion_from_text(self, text: str) -> Emotion | None:
        """Parse cat's response for parenthetical emotion cues like '(开心地摇尾巴)'."""
        match = re.search(r'[（(]([^）)]*)[）)]', text)
        if not match:
            return None
        hint = match.group(1)

        if any(w in hint for w in ['开心', '高兴', '摇尾巴', '呼噜', '蹭', '眯眼笑']):
            return Emotion.CONTENT
        if any(w in hint for w in ['兴奋', '玩', '跳', '追', '扑', '跑']):
            return Emotion.PLAYFUL
        if any(w in hint for w in ['好奇', '歪头', '盯', '闻', '嗅', '竖耳朵']):
            return Emotion.CURIOUS
        if any(w in hint for w in ['困', '哈欠', '眯眼', '懒', '趴']):
            return Emotion.SLEEPY
        if any(w in hint for w in ['生气', '炸毛', '哼', '不开心', '扭头']):
            return Emotion.ANNOYED
        if any(w in hint for w in ['怕', '吓', '躲', '缩', '发抖']):
            return Emotion.SCARED
        return None

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
        self.stt_corrector = stt_corrector  # async function for STT correction
        self.conversation_history: list[str] = []

    async def correct_stt(self, raw_text: str) -> str:
        """Use LLM to correct STT recognition errors (homophones, omissions).

        Returns corrected text. If correction fails, returns raw_text unchanged.
        If no corrector is available, returns raw_text unchanged.
        """
        corrector = self.stt_corrector or self.llm
        if not corrector or not raw_text.strip():
            return raw_text

        if len(raw_text.strip()) <= 2:
            return raw_text.strip()

        context = "无" if not self.conversation_history else \
            " | ".join(self.conversation_history[-5:])

        prompt = STT_CORRECTION_PROMPT.format(
            context=context,
            raw_text=raw_text.strip()
        )

        try:
            corrected = await corrector(prompt)
            corrected = corrected.strip()
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

        # Generate LLM response first if user spoke
        llm_response = ""
        if has_speech and self.llm:
            prompt = CAT_PERSONALITY_PROMPT.format(
                emotion=self.emotion_fsm.current.value,
                user_text=user_text
            )
            try:
                llm_response = (await self.llm(prompt)).strip()
            except Exception:
                llm_response = "喵~"

        # Update emotion: considers touch, speech, AND the LLM's response text
        # This way the cat's own words influence its mood
        self.emotion_fsm.update(tv, has_speech, llm_response)

        # Build command
        command = self.emotion_fsm.to_command()

        if llm_response:
            command["response_text"] = llm_response
            command["has_audio"] = True

        return command
