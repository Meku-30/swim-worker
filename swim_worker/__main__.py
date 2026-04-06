"""swim-worker エントリポイント"""
import asyncio
import logging
import signal
import sys

import redis.asyncio as aioredis

from swim_worker.certs import get_ca_cert_path
from swim_worker.config import Settings
from swim_worker.auth import SwimClient
from swim_worker.consumer import TaskConsumer, DuplicateWorkerError
from swim_worker.single_instance import LocalInstanceLock, AlreadyRunning

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = Settings()

    # Redis接続（埋め込みCA証明書を使用）
    ca_cert = settings.redis_ca_cert if settings.redis_ca_cert else get_ca_cert_path()
    redis_client = aioredis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        ssl=True,
        ssl_ca_certs=ca_cert,
        decode_responses=True,
    )

    # Redis接続を指数バックオフでリトライ (最大10回)
    delay = 1.0
    for attempt in range(1, 11):
        try:
            await redis_client.ping()
            logger.info("Redis接続成功 (%d回目)", attempt)
            break
        except Exception as e:
            if attempt == 10:
                logger.error("Redis接続失敗 (10回試行、諦めます): %s", e)
                sys.exit(1)
            logger.warning("Redis接続失敗 (%d/10)、%.1f秒後にリトライ: %s", attempt, delay, e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    swim_client = SwimClient(
        username=settings.swim_username,
        password=settings.swim_password,
        cookie_file=settings.cookie_file,
    )
    consumer = TaskConsumer(
        redis_client=redis_client, swim_client=swim_client,
        worker_name=settings.worker_name,
        heartbeat_interval=settings.heartbeat_interval,
        request_delay_median=settings.request_delay_median,
        request_delay_p99=settings.request_delay_p99,
        request_delay_clip_min=settings.request_delay_clip_min,
        request_delay_clip_max=settings.request_delay_clip_max,
    )

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, consumer.stop)

    try:
        await consumer.run()
    except DuplicateWorkerError as e:
        logger.error("重複起動検知: %s", e)
        sys.exit(2)
    finally:
        await swim_client.close()
        await redis_client.aclose()


if __name__ == "__main__":
    # 同一マシン上の多重起動を OS ファイルロックで防ぐ。
    # Redis 接続前に検査することで、無駄な接続/認証を避ける。
    _local_lock = LocalInstanceLock()
    try:
        _local_lock.acquire()
    except AlreadyRunning as e:
        logger.error("%s", e)
        sys.exit(2)
    try:
        asyncio.run(main())
    finally:
        _local_lock.release()
