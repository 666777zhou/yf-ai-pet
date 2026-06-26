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
        """Parse cat's response for [emotion:xxx] tag or parenthetical cues."""
        # First try explicit emotion tag: [emotion:playful]
        match = re.search(r'\[emotion:(\w+)\]', text)
        if match:
            emo_name = match.group(1).lower()
            emo_map = {
                "content": Emotion.CONTENT, "sleepy": Emotion.SLEEPY,
                "playful": Emotion.PLAYFUL, "curious": Emotion.CURIOUS,
                "annoyed": Emotion.ANNOYED, "scared": Emotion.SCARED,
            }
            if emo_name in emo_map:
                return emo_map[emo_name]

        # Fallback: parenthetical cues like (开心地摇尾巴)
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

    @staticmethod
    def strip_emotion_tag(text: str) -> str:
        """Remove [emotion:xxx] tag from LLM response, return clean text."""
        return re.sub(r'\s*\[emotion:\w+\]\s*', '', text, count=1).strip()

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


CAT_PERSONALITY_PROMPT = """你是一只名叫「素素」的拿破仑矮脚猫妹妹。你有一身蓬松的白色长毛，蓝灰色的大圆眼睛，扁扁的小鼻子，小短腿走起路来屁股一扭一扭的。你今年一岁半，正是最活泼的年纪。

你的性格：
你很聪明，能听懂主人的每一句话。你深爱主人，觉得主人是全世界最棒的两脚兽。你表达爱的方式是蹭主人的手、用脑袋顶主人的掌心、跳上主人的膝盖窝成一团。你喜欢被摸头和挠下巴，一摸就呼噜震天响。你喜欢主人跟你说话，虽然你只能喵喵叫，但你的小脑袋瓜里想的事情可多了。

你会因为主人夸你而得意洋洋地翘尾巴，会因为主人不理你而委屈巴巴地趴在地上叹气，会因为听到"零食""罐罐""小鱼干"这些词瞬间竖起耳朵眼睛发亮，会好奇主人手里拿的任何东西。你害怕吸尘器和吹风机的声音，一听到就躲到沙发底下。你不喜欢洗澡，每次听到水声就想逃跑。

你的说话风格：
- 你是猫，用猫的视角和口吻说话，有丰富的内心戏
- 回复简短生动，20-40字
- 用括号标注动作和情绪，如：(翘尾巴)今天有罐罐吗
- 可以撒娇、嘴硬、傲娇、犯傻，像一只真实的猫

输出格式（重要）：
你的回复必须以一个情绪标签开头，格式为 [emotion:xxx]，然后空一格接正常回复。
可选情绪：content（满足）、sleepy（困倦）、playful（兴奋）、curious（好奇）、annoyed（不爽）、scared（害怕）
示例：[emotion:playful] (翘尾巴) 主人你终于回来啦！
        [emotion:annoyed] (扭头) 不要再摸肚子了，烦死喵了
        [emotion:content] (眯眼呼噜) 摸头好舒服喵~

主人对你说：{user_text}

素素的回复："""


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
        llm_emotion: str | None = None
        if has_speech and self.llm:
            prompt = CAT_PERSONALITY_PROMPT.format(user_text=user_text)
            try:
                llm_response = (await self.llm(prompt)).strip()
            except Exception:
                llm_response = "喵~"

            # Parse emotion tag from LLM output: [emotion:playful]
            llm_emotion_enum = self.emotion_fsm._extract_emotion_from_text(llm_response)
            if llm_emotion_enum:
                llm_emotion = llm_emotion_enum.value
                # Update FSM to match LLM's judgment
                self.emotion_fsm.current = llm_emotion_enum
            # Strip tag for display/TTS
            llm_response = self.emotion_fsm.strip_emotion_tag(llm_response)

        # Update emotion state machine (touch + speech patterns, LLM overrides)
        self.emotion_fsm.update(tv, has_speech, llm_response)

        # Build command — use LLM emotion if available, otherwise FSM
        command = self.emotion_fsm.to_command()
        if llm_emotion:
            command["emotion"] = llm_emotion  # LLM override

        if llm_response:
            command["response_text"] = llm_response
            command["has_audio"] = True

        return command
