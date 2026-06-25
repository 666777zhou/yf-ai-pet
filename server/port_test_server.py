#!/usr/bin/env python3
"""Multi-port test server: plain WS on several ports for external testing."""
import asyncio
import sys
import logging
from websockets.asyncio.server import serve

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
logger = logging.getLogger("port-test")


async def handler(websocket):
    """Return which port was hit."""
    host, port = websocket.local_address[:2]
    logger.info(f"Connection on port {port} from {websocket.remote_address}")
    await websocket.send(f"OK from port {port}")
    logger.info(f"Response sent on port {port}")


async def main():
    ports = [int(p) for p in sys.argv[1:]] if len(sys.argv) > 1 else [8088, 8765, 9000]
    servers = [serve(handler, "0.0.0.0", p) for p in ports]

    logger.info(f"Test servers on ports: {ports}")
    logger.info(f"Test from phone: http://183.129.98.242:PORT/")
    logger.info("(It'll fail as plain HTTP, but the SYN should get through if port is open)")

    async with servers[0] as s0:
        async with servers[1] if len(servers) > 1 else asyncio.nullcontext() as s1:
            async with servers[2] if len(servers) > 2 else asyncio.nullcontext() as s2:
                logger.info("All servers ready!")
                await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    asyncio.run(main())
