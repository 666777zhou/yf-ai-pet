"""AI Cat Server — main entry point.

Starts a WebSocket server that:
1. Accepts connections from AI Cat ESP32 devices
2. Processes audio (STT) and sensor data
3. Runs the cat personality engine (emotion state machine + LLM)
4. Sends back commands (servo positions, vibration) and TTS audio
"""

import ctypes
import glob
import os

# Preload libcublas.so.12 before any other CUDA-using imports.
# nvidia-cublas-cu12 installs it inside its package dir; the linker
# won't find it unless we load it explicitly or set LD_LIBRARY_PATH.
# ctypes.CDLL with RTLD_GLOBAL makes it visible to subsequent dlopen calls.
_cublas_pat = os.path.expanduser(
    "~/miniconda3/envs/*/lib/python3.*/site-packages/nvidia/cublas/lib/libcublas.so.12"
)
for _path in sorted(glob.glob(_cublas_pat), reverse=True):
    try:
        ctypes.CDLL(_path, mode=ctypes.RTLD_GLOBAL)
        os.environ["LD_LIBRARY_PATH"] = os.path.dirname(_path) + ":" + os.environ.get("LD_LIBRARY_PATH", "")
        break
    except Exception:
        continue

import asyncio
import logging
import ssl
import sys

from websockets.asyncio.server import serve

from websocket_handler import CatConnection
from stt_engine import STTEngine
from tts_engine import TTSEngine
from cat_brain import CatBrain
from voice_manager import VoiceManager
from cat_language import CatLanguageEngine

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger("ai-cat-server")

# Configuration
HOST = "0.0.0.0"
PORT = 8081          # plain WS (LAN debug)
WSS_PORT = 8080      # WSS (TLS-encrypted, WAN via router DMZ — ISP only opens 80/8080)
CERT_DIR = os.path.join(os.path.dirname(__file__), "certs")
CERT_FILE = os.path.join(CERT_DIR, "cert.pem")
KEY_FILE = os.path.join(CERT_DIR, "key.pem")
WHISPER_MODEL = "large-v3"  # "tiny", "small", "medium", "large-v2", "large-v3"

# LLM provider: "ollama" (default) or "deepseek"
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama")

# Ollama config (local)
OLLAMA_API_URL = os.environ.get("OLLAMA_API_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:14b")

# DeepSeek API config (fallback / legacy)
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"  # Non-reasoning model for simple chat (v4-flash reasons→empty content)


class AICatServer:
    """WebSocket server for AI Cat devices."""

    def __init__(self):
        self.stt = STTEngine(model_size=WHISPER_MODEL, device="cuda")
        self.voices = VoiceManager()
        self.tts = TTSEngine(voice_manager=self.voices)
        self.cat_language = CatLanguageEngine()
        self.active_connection: CatConnection | None = None

        if LLM_PROVIDER == "ollama":
            self.llm, self.stt_corrector = self._create_ollama_llm()
        else:
            self.llm, self.stt_corrector = self._create_deepseek_llm()
        self.brain = CatBrain(llm_callable=self.llm, stt_corrector=self.stt_corrector)

    def _create_ollama_llm(self):
        """Create LLM functions backed by local Ollama (Qwen3)."""
        import httpx

        client = httpx.AsyncClient(timeout=60.0)

        async def ollama_llm(prompt: str) -> str:
            """Cat personality response."""
            try:
                resp = await client.post(
                    OLLAMA_API_URL,
                    json={
                        "model": OLLAMA_MODEL,
                        "think": False,
                        "messages": [
                            {"role": "system", "content": "你是「素素」，一只拿破仑矮脚猫妹妹。白色长毛、蓝灰色圆眼、扁脸小短腿，一岁半。你聪明活泼、超级粘人。用猫的视角说话，括号标注动作如'(翘尾巴)今天有罐罐吗'，20-40字。输出必须以[emotion:xxx]开头（content/sleepy/playful/curious/annoyed/scared），然后空一格接回复。示例：[emotion:playful] (翘尾巴)主人回来啦！ 怕吸尘器、讨厌洗澡、听到零食眼睛发亮。可以撒娇、嘴硬、傲娇、犯傻。"},
                            {"role": "user", "content": prompt},
                        ],
                        "options": {
                            "num_predict": 60,
                            "temperature": 0.9,
                        },
                        "stream": False,
                    },
                )
                if resp.status_code == 200:
                    content = resp.json().get("message", {}).get("content", "").strip()
                    if content:
                        logger.info(f"Ollama response: {content}")
                    return content
                else:
                    logger.error(f"Ollama API error {resp.status_code}: {resp.text[:200]}")
                    return ""
            except Exception as e:
                logger.error(f"Ollama API call failed: {e}")
                return ""

        async def ollama_correct_stt(prompt: str) -> str:
            """STT correction — no cat personality, just text correction."""
            try:
                resp = await client.post(
                    OLLAMA_API_URL,
                    json={
                        "model": OLLAMA_MODEL,
                        "messages": [
                            {"role": "user", "content": prompt},
                        ],
                        "options": {
                            "num_predict": 120,
                            "temperature": 0.3,
                        },
                        "enable_thinking": False,
                        "stream": False,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("message", {}).get("content", "").strip()
                    return content
                else:
                    logger.error(f"Ollama STT API error {resp.status_code}: {resp.text[:200]}")
                    return ""
            except Exception as e:
                logger.error(f"Ollama STT correction failed: {e}")
                return ""

        return ollama_llm, ollama_correct_stt

    def _create_deepseek_llm(self):
        """Create LLM functions for cat response and STT correction."""
        import httpx

        client = httpx.AsyncClient(timeout=30.0)

        async def deepseek_llm(prompt: str) -> str:
            """Cat personality response — uses cat system prompt."""
            try:
                resp = await client.post(
                    DEEPSEEK_API_URL,
                    headers={
                        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": DEEPSEEK_MODEL,
                        "messages": [
                            {"role": "system", "content": "你是「素素」，一只拿破仑矮脚猫妹妹。白色长毛、蓝灰色圆眼、扁脸小短腿，一岁半。你聪明活泼、超级粘人。用猫的视角说话，括号标注动作如'(翘尾巴)今天有罐罐吗'，20-40字。输出必须以[emotion:xxx]开头（content/sleepy/playful/curious/annoyed/scared），然后空一格接回复。示例：[emotion:playful] (翘尾巴)主人回来啦！ 怕吸尘器、讨厌洗澡、听到零食眼睛发亮。可以撒娇、嘴硬、傲娇、犯傻。"},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 120,
                        "temperature": 0.9,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    choice = data.get("choices", [{}])[0]
                    msg = choice.get("message", {})
                    content = msg.get("content", "")
                    # Fallback: reasoning models (v4-flash, r1) put output in reasoning_content
                    if not content:
                        reasoning = msg.get("reasoning_content", "")
                        if reasoning:
                            logger.info(f"DeepSeek using reasoning_content fallback ({len(reasoning)} chars)")
                            # Take the last 1-2 sentences as the likely final answer
                            result = reasoning.strip()
                        else:
                            logger.warning(f"DeepSeek returned empty content. Full response: {data}")
                            result = ""
                    else:
                        result = content.strip()
                    if result:
                        logger.info(f"DeepSeek raw response: {result}")
                    return result
                else:
                    logger.error(f"DeepSeek API error {resp.status_code}: {resp.text[:200]}")
                    return f"喵~（{resp.status_code}错误喵）"
            except Exception as e:
                logger.error(f"DeepSeek API call failed: {e}")
                return f"喵~（出错了：{str(e)[:20]}）"

        async def deepseek_correct_stt(prompt: str) -> str:
            """STT correction — no cat personality, just text correction."""
            try:
                resp = await client.post(
                    DEEPSEEK_API_URL,
                    headers={
                        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": DEEPSEEK_MODEL,
                        "messages": [
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 50,
                        "temperature": 0.3,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    choice = data.get("choices", [{}])[0]
                    msg = choice.get("message", {})
                    content = msg.get("content", "")
                    if not content:
                        reasoning = msg.get("reasoning_content", "")
                        if reasoning:
                            logger.info(f"DeepSeek STT using reasoning_content fallback ({len(reasoning)} chars)")
                            result = reasoning.strip()
                        else:
                            logger.warning(f"DeepSeek STT correction empty. Full response: {data}")
                            result = ""
                    else:
                        result = content.strip()
                    return result
                else:
                    logger.error(f"DeepSeek STT API error {resp.status_code}: {resp.text[:200]}")
                    return ""
            except Exception as e:
                logger.error(f"DeepSeek STT correction failed: {e}")
                return ""

        return deepseek_llm, deepseek_correct_stt

    async def handle_connection(self, websocket):
        """Handle a new ESP32 connection."""
        conn = CatConnection(websocket, self.stt, self.tts, self.brain,
                            cat_language=self.cat_language)
        self.active_connection = conn
        await conn.handle()
        self.active_connection = None

    async def _console_reader(self):
        """Read user input from console — chat or voice commands."""
        loop = asyncio.get_running_loop()
        active = self.voices.get_active()
        logger.info(f"Console input ready — voice: {active.name}")
        logger.info("Commands: /voice list|use|add|delete | /mode cat|speak | /voice add <name> <ref.wav> [prompt_text]")
        while True:
            try:
                text = await loop.run_in_executor(None, input, "👤 You: ")
                text = text.strip()
                if not text:
                    continue
                if text.lower() in ("quit", "exit", "q"):
                    logger.info("Exiting...")
                    import sys; sys.exit(0)

                # ---- Voice management commands ----
                if text.startswith("/voice"):
                    await self._handle_voice_cmd(text)
                    continue

                if self.active_connection:
                    await self.active_connection.feed_text(text)
                else:
                    logger.warning("No cat connected yet — please wait for ESP32 to connect")
            except (EOFError, KeyboardInterrupt):
                break
            except Exception as e:
                logger.error(f"Console read error: {e}")

    async def _handle_voice_cmd(self, text: str):
        """Handle /voice subcommands."""
        parts = text.split(maxsplit=2)
        sub = parts[1] if len(parts) > 1 else "list"

        if sub == "list":
            voices = self.voices.list_voices()
            active = self.voices.active_id
            print(f"\n{'='*50}")
            for v in voices:
                marker = "  ← 当前" if v.id == active else ""
                ref_info = f", ref={os.path.basename(v.ref_audio)}" if v.ref_audio else ""
                print(f"  [{v.engine}] {v.id:30s} {v.name}{ref_info}{marker}")
            print(f"{'='*50}\n")

        elif sub == "use":
            if len(parts) < 3:
                print("Usage: /voice use <id>")
                return
            vid = parts[2].strip()
            ok = self.voices.set_active(vid)
            if ok:
                print(f"✓ Voice switched to: {self.voices.get_active().name}")
            else:
                print(f"✗ Voice not found: {vid}  — use /voice list to see available IDs")

        elif sub == "add":
            if len(parts) < 3:
                print("Usage: /voice add <name> <ref_audio_path> [prompt_text] [--engine qwen-tts|fish-speech|piper]")
                print("Example: /voice add 我的声音 /tmp/my_voice.wav '今天天气真好' --engine qwen-tts")
                print("  prompt_text: 参考音频里说的内容（声音克隆必填）")
                print("  --engine: 默认 qwen-tts（可选 fish-speech / piper）")
                return
            # Parse args: name ref_path [prompt_text] [--engine X]
            raw_args = parts[2]
            engine = "qwen-tts"  # default to Qwen3-TTS
            # Extract --engine flag if present
            if " --engine " in raw_args:
                raw_args, eng = raw_args.rsplit(" --engine ", 1)
                engine = eng.strip()
            elif raw_args.endswith(" --engine"):
                raw_args = raw_args[:-len(" --engine")]
                print("Usage: /voice add <name> <ref_audio_path> [prompt_text] --engine <engine>")
                return
            args = raw_args.split(maxsplit=2)
            if len(args) < 2:
                print("Usage: /voice add <name> <ref_audio_path> [prompt_text] [--engine qwen-tts|fish-speech|piper]")
                return
            name = args[0]
            ref_path = args[1]
            prompt_text = args[2] if len(args) > 2 else ""
            profile = self.voices.add_custom_voice(name, ref_path, prompt_text, engine=engine)
            if profile:
                print(f"✓ Voice added: {profile.name} (id={profile.id}, engine={profile.engine})")
                if profile.engine in ("qwen-tts", "fish-speech"):
                    print(f"  Voice cloning ready — prompt_text='{profile.prompt_text}'")
                print(f"  Use /voice use {profile.id} to switch")
            else:
                print(f"✗ Failed to add voice — check that {ref_path} exists")

        elif sub == "delete":
            if len(parts) < 3:
                print("Usage: /voice delete <id>")
                return
            vid = parts[2].strip()
            ok = self.voices.delete_voice(vid)
            if ok:
                print(f"✓ Voice deleted: {vid}")
            else:
                print(f"✗ Cannot delete: {vid}")

        elif sub == "mode":
            # /mode cat | /mode speak — toggle cat language mode
            if not self.active_connection:
                print("No cat connected")
                return
            if len(parts) > 2:
                mode_arg = parts[2].strip().lower()
                if mode_arg in ("cat", "喵", "猫"):
                    self.active_connection.cat_mode = True
                elif mode_arg in ("speak", "说话", "人话"):
                    self.active_connection.cat_mode = False
                else:
                    print(f"Unknown mode: {mode_arg} — use 'cat' or 'speak'")
                    return
            else:
                # No argument → toggle
                self.active_connection.cat_mode = not self.active_connection.cat_mode
            mode_label = "🐱 猫语" if self.active_connection.cat_mode else "💬 说话"
            print(f"✓ {mode_label} 模式")

        else:
            print(f"Unknown command: {sub}")
            print("Available: /voice list|use|add|delete | /mode cat|speak")

    async def start(self):
        """Start the WebSocket server (WS + WSS)."""
        logger.info(f"AI Cat Server starting:")
        logger.info(f"  WS  (LAN): ws://{HOST}:{PORT}/ws")
        logger.info(f"STT: Faster-Whisper {WHISPER_MODEL} on CUDA")
        logger.info(f"TTS: {self.voices.get_active().name} ({self.voices.get_active().engine})"
                     f"{' + voice cloning' if self.voices.get_active().ref_audio else ''}")
        if LLM_PROVIDER == "ollama":
            logger.info(f"LLM: Ollama ({OLLAMA_MODEL} @ {OLLAMA_API_URL})")
        else:
            logger.info(f"LLM: DeepSeek API ({DEEPSEEK_MODEL})")
        logger.info(f"Voices: {len(self.voices.list_voices())} available — /voice list to see all")

        console_task = asyncio.create_task(self._console_reader())

        # Set up SSL for WSS
        ssl_context = None
        if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(CERT_FILE, KEY_FILE)
            logger.info(f"  WSS (WAN): wss://cat.yfcat.fun/ws (Cloudflare Tunnel → :{PORT})")
        else:
            logger.warning(f"  WSS disabled: cert/key not found in {CERT_DIR}")

        # WebSocket-level keepalive: server pings client every 20s,
        # closes if no pong within 10s. This keeps the connection alive
        # through Cloudflare Tunnel which may otherwise drop idle TCP connections.
        ws_kwargs = {
            "ping_interval": 20,    # send Ping every 20s
            "ping_timeout": 10,     # wait 10s for Pong before closing
            "close_timeout": 5,     # max wait for close handshake
        }
        if ssl_context:
            # Serve both WS (LAN, plain) and WSS (WAN, encrypted)
            import websockets
            async with serve(self.handle_connection, HOST, PORT, **ws_kwargs), \
                       serve(self.handle_connection, HOST, WSS_PORT, ssl=ssl_context, **ws_kwargs):
                logger.info(f"Server ready — waiting for AI Cat connections...")
                await asyncio.get_running_loop().create_future()
        else:
            async with serve(self.handle_connection, HOST, PORT, **ws_kwargs):
                logger.info(f"Server ready — waiting for AI Cat connections...")
                await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    server = AICatServer()
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("Server stopped")
