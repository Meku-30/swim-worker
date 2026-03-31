"""swim-worker エントリポイント"""
import asyncio
import logging
import signal
import sys

import redis.asyncio as aioredis

from swim_worker.config import Settings
from swim_worker.auth import SwimClient
from swim_worker.consumer import TaskConsumer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = Settings()

    # Redis接続
    redis_client = aioredis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        ssl=bool(settings.redis_ca_cert),
        ssl_ca_certs=settings.redis_ca_cert if settings.redis_ca_cert else None,
        decode_responses=True,
    )

    try:
        await redis_client.ping()
        logger.info("Redis接続成功")
    except Exception as e:
        logger.error("Redis接続失敗: %s", e)
        sys.exit(1)

    swim_client = SwimClient(username=settings.swim_username, password=settings.swim_password)
    consumer = TaskConsumer(redis_client=redis_client, swim_client=swim_client, worker_name=settings.worker_name, heartbeat_interval=settings.heartbeat_interval)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, consumer.stop)

    try:
        await consumer.run()
    finally:
        await swim_client.close()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
