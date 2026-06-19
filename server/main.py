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
import sys

from websockets.asyncio.server import serve

from websocket_handler import CatConnection
from stt_engine import STTEngine
from tts_engine import TTSEngine
from cat_brain import CatBrain

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger("ai-cat-server")

# Configuration
HOST = "0.0.0.0"
PORT = 8080
WHISPER_MODEL = "small"  # "tiny", "small", "medium", "large"

# DeepSeek API config — set DEEPSEEK_API_KEY env var or replace the default
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"  # or "deepseek-reasoner" for R1


class AICatServer:
    """WebSocket server for AI Cat devices."""

    def __init__(self):
        self.stt = STTEngine(model_size=WHISPER_MODEL, device="cuda")
        self.tts = TTSEngine()
        self.active_connection: CatConnection | None = None

        self.llm = self._create_deepseek_llm()
        self.brain = CatBrain(llm_callable=self.llm)

    def _create_deepseek_llm(self):
        """Create an LLM function that calls DeepSeek API."""
        import httpx

        client = httpx.AsyncClient(timeout=30.0)

        async def deepseek_llm(prompt: str) -> str:
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
                            {"role": "system", "content": "你是一只猫。用猫的视角回复，简短可爱，不超过40字，用'喵~'开头或结尾。"},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 120,
                        "temperature": 0.9,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    result = data["choices"][0]["message"]["content"].strip()
                    logger.info(f"DeepSeek raw response: {result}")
                    return result
                else:
                    logger.error(f"DeepSeek API error {resp.status_code}: {resp.text[:200]}")
                    return f"喵~（{resp.status_code}错误喵）"
            except Exception as e:
                logger.error(f"DeepSeek API call failed: {e}")
                return f"喵~（出错了：{str(e)[:20]}）"

        return deepseek_llm

    async def handle_connection(self, websocket):
        """Handle a new ESP32 connection."""
        conn = CatConnection(websocket, self.stt, self.tts, self.brain)
        self.active_connection = conn
        await conn.handle()
        self.active_connection = None

    async def _console_reader(self):
        """Read user input from console and feed to cat brain."""
        loop = asyncio.get_running_loop()
        logger.info("Console input ready — type something to talk to the cat!")
        while True:
            try:
                text = await loop.run_in_executor(None, input, "👤 You: ")
                text = text.strip()
                if not text:
                    continue
                if text.lower() in ("quit", "exit", "q"):
                    logger.info("Exiting...")
                    import sys; sys.exit(0)
                if self.active_connection:
                    await self.active_connection.feed_text(text)
                else:
                    logger.warning("No cat connected yet — please wait for ESP32 to connect")
            except (EOFError, KeyboardInterrupt):
                break
            except Exception as e:
                logger.error(f"Console read error: {e}")

    async def start(self):
        """Start the WebSocket server."""
        logger.info(f"AI Cat Server starting on {HOST}:{PORT}")
        logger.info("STT: Faster-Whisper small on CUDA")
        logger.info("TTS: Edge-TTS (zh-CN-XiaoxiaoNeural)")
        logger.info(f"LLM: DeepSeek API ({DEEPSEEK_MODEL})")

        console_task = asyncio.create_task(self._console_reader())

        async with serve(self.handle_connection, HOST, PORT):
            logger.info(f"Server ready — waiting for AI Cat connections...")
            await asyncio.get_running_loop().create_future()  # run forever


if __name__ == "__main__":
    server = AICatServer()
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("Server stopped")
