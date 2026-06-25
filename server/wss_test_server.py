"""Minimal WSS echo server — test if TLS bypasses router nginx on port 8080."""
import ssl
import asyncio
import logging
from websockets.asyncio.server import serve

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wss-test")

CERT_FILE = "certs/cert.pem"
KEY_FILE = "certs/key.pem"


async def handler(websocket):
    """Echo handler — just log and echo back."""
    remote = websocket.remote_address
    logger.info(f"Connection from {remote}")
    try:
        async for message in websocket:
            logger.info(f"Received: {message[:100] if isinstance(message, str) else f'binary {len(message)}B'}")
            await websocket.send(f"ECHO: {message}" if isinstance(message, str) else message)
    except Exception as e:
        logger.info(f"Connection closed: {e}")


async def main():
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(CERT_FILE, KEY_FILE)

    logger.info("WSS test server starting on wss://0.0.0.0:8080/ws")
    logger.info("Test from external: wss://yfcat.x3322.net:8080/ws")
    logger.info("Test from LAN:     wss://192.168.2.138:8080/ws")

    async with serve(handler, "0.0.0.0", 8080, ssl=ssl_context):
        logger.info("Server ready!")
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    asyncio.run(main())
