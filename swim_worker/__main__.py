"""swim-worker エントリポイント"""
import asyncio
import logging
import signal
import sys

from swim_worker.config import Settings
from swim_worker.redis_client import create_redis_client
from swim_worker.auth import SwimClient
from swim_worker.consumer import TaskConsumer, DuplicateWorkerError
from swim_worker.single_instance import LocalInstanceLock, AlreadyRunning
from swim_worker import dns_check

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def install_signal_handlers(loop, consumer) -> None:
    """SIGINT / SIGTERM で停止する。1 回目は処理中のタスクを終えてから、2 回目は即時。

    Windows の asyncio には add_signal_handler が無い (NotImplementedError) ので、
    signal.signal で受けてイベントループのスレッドに渡す (Ctrl+C・Ctrl+Break・SIGTERM)。
    """
    state = {"count": 0}

    def on_signal() -> None:
        state["count"] += 1
        if state["count"] == 1:
            consumer.request_stop()
        else:
            consumer.stop()

    sigs = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):  # Windows の Ctrl+Break
        sigs.append(signal.SIGBREAK)
    for sig in sigs:
        try:
            loop.add_signal_handler(sig, on_signal)
            continue
        except (NotImplementedError, RuntimeError, AttributeError):
            pass

        def handler(signum, frame) -> None:
            state["count"] += 1
            target = consumer.request_stop if state["count"] == 1 else consumer.stop
            loop.call_soon_threadsafe(target)
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError) as e:  # メインスレッド以外など
            logger.debug("シグナル %s を登録できません: %s", sig, e)


async def main() -> None:
    settings = Settings()
    # systemd の通信許可 (install.sh の drop-in) が今の DNS サーバーを通すか (塞がれていれば警告)
    dns_check.check_at_startup()
    redis_client = create_redis_client(settings)

    # Redis接続を指数バックオフでリトライ (最大10回)
    delay = 1.0
    for attempt in range(1, 11):
        try:
            await redis_client.ping()
            logger.info("Redis接続成功 (%d回目)", attempt)
            break
        except Exception as e:
            if sys.platform.startswith("linux") and dns_check.is_name_resolution_error(e):
                logger.error("Redis のホスト名を名前解決できません。%s", dns_check.FIX_HINT)
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
        task_hard_timeout=settings.task_hard_timeout,
        blpop_timeout=settings.redis_blpop_timeout,
    )

    install_signal_handlers(asyncio.get_running_loop(), consumer)

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
    except OSError as e:
        logger.error("ロックファイルを作成できません: %s (%s)。書き込み可能なディレクトリで実行してください", _local_lock.path, e)
        sys.exit(3)
    try:
        asyncio.run(main())
    finally:
        _local_lock.release()
