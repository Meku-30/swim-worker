"""GUI から Worker (TaskConsumer) を別スレッドの asyncio ループで動かす。tkinter 非依存。

GUI への知らせはコールバックで行う (呼ばれるのは Worker のスレッド。GUI 側でメイン
スレッドに渡すこと)。
"""
import asyncio
import logging
import threading

import redis.exceptions

from swim_worker import __version__
from swim_worker import update_check

logger = logging.getLogger(__name__)

# 終わり方 (on_finished の outcome)
STOPPED = "stopped"          # 停止要求で止まった
DUPLICATE = "duplicate"      # 同じ worker_name が別の場所で動いている
AUTH_ERROR = "auth_error"    # Redis の認証・権限エラー
ERROR = "error"              # それ以外のエラー

AUTH_ERRORS = (redis.exceptions.AuthenticationError, redis.exceptions.NoPermissionError)


def _noop(*a, **kw):
    pass


class WorkerRunner:
    """Worker を 1 回動かす (起動 → 停止で終わり。再起動は新しいインスタンスで)"""

    def __init__(self, settings: dict, *, on_status=_noop, on_finished=_noop,
                 on_update_available=_noop, on_task_state=_noop):
        # settings: GUI のメインスレッドでコピーした値 (別スレッドから Tk を触らないため)
        self._settings = dict(settings)
        self._on_status = on_status
        self._on_finished = on_finished
        self._on_update_available = on_update_available
        self._on_task_state = on_task_state
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._stop_requested = False
        self.consumer = None

    # --- 外 (GUI のスレッド) から呼ぶ ---

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: float | None = None) -> bool:
        """終わるまで待つ。終わっていれば True"""
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.is_alive()

    def request_stop(self) -> None:
        """停止要求 (consumer 生成前の Redis リトライ中でも中断できる)"""
        # 先にフラグを立てる。_thread_main 側は create_task 直後にこのフラグを見て、
        # ループ/Task ができる前に停止が押されていた場合は自分で cancel する (両側で競合を閉じる)
        self._stop_requested = True
        loop = self._loop
        if loop is not None and not loop.is_closed():
            # Task.cancel はスレッドセーフでないためループのスレッドで実行する。
            # consumer.run() 内なら CancelledError で heartbeat/consume が止まり finally が走る
            try:
                loop.call_soon_threadsafe(self._cancel_task)
            except RuntimeError:
                pass  # ループ終了済み

    # --- Worker のスレッド ---

    def _cancel_task(self) -> None:
        t = self._task
        if t is not None and not t.done():
            t.cancel()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop  # request_stop から見えるよう create_task より前に公開する
        try:
            self._task = loop.create_task(self._main())
            if self._stop_requested:
                self._task.cancel()  # 起動前に停止が押された場合
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("予期しないエラー: %s", e)
        finally:
            self._task = None
            self._loop = None
            loop.close()

    def _build_settings(self):
        from swim_worker.config import Settings
        ws = self._settings
        # os.environ には書かず直接構築する (.env も読まない)
        return Settings(
            _env_file=None,
            redis_host=ws["redis_host"], redis_port=6380,
            redis_username=ws["redis_username"],
            redis_password=ws["redis_password"], redis_ca_cert="",
            swim_username=ws["swim_username"], swim_password=ws["swim_password"],
            worker_name=ws["worker_name"],
        )

    async def _connect(self, redis_client) -> None:
        """Redis接続を指数バックオフでリトライ (最大10回)"""
        delay = 1.0
        for attempt in range(1, 11):
            try:
                await redis_client.ping()
                logger.info("Redis接続成功 (%d回目)", attempt)
                return
            except Exception as e:
                # 認証の失敗は待っても直らないので再試行しない
                if attempt == 10 or isinstance(e, redis.exceptions.AuthenticationError):
                    raise
                logger.warning("Redis接続失敗 (%d/10)、%.1f秒後にリトライ: %s", attempt, delay, e)
                self._on_status(f"再試行中 ({attempt}/10)")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise RuntimeError("Redis接続失敗")

    async def _main(self) -> None:
        from swim_worker import redis_client as rc
        from swim_worker.auth import SwimClient
        from swim_worker.consumer import TaskConsumer, DuplicateWorkerError

        swim_client = None
        redis_client = None
        try:
            settings = self._build_settings()
            # CLI と同じファクトリを使う (client_name 等の設定漏れ防止)
            redis_client = rc.create_redis_client(settings)
            await self._connect(redis_client)
            self._on_status("● 接続中 (タスク待ち)")

            swim_client = SwimClient(
                username=settings.swim_username,
                password=settings.swim_password,
                cookie_file=settings.cookie_file,
            )
            self.consumer = TaskConsumer(
                redis_client=redis_client,
                swim_client=swim_client,
                worker_name=settings.worker_name,
                heartbeat_interval=settings.heartbeat_interval,
                request_delay_median=settings.request_delay_median,
                request_delay_p99=settings.request_delay_p99,
                request_delay_clip_min=settings.request_delay_clip_min,
                request_delay_clip_max=settings.request_delay_clip_max,
                task_hard_timeout=settings.task_hard_timeout,
                blpop_timeout=settings.redis_blpop_timeout,
                on_update_available=self._on_update_available,
                on_task_state=self._on_task_state,
            )
            await self.consumer.run()
        except DuplicateWorkerError as e:
            logger.error("重複起動検知: %s", e)
            self._on_finished(DUPLICATE, str(e))
        except asyncio.CancelledError:
            logger.info("Worker 停止要求を受け付けました")
        except Exception as e:
            logger.error("エラー: %s", e)
            auth_failed = isinstance(e, AUTH_ERRORS)
            self._on_finished(AUTH_ERROR if auth_failed else ERROR, str(e))
            if auth_failed:
                # Redis に入れないと Coordinator 経由の更新通知が届かないので、GitHub で確かめる
                try:
                    await asyncio.to_thread(
                        update_check.check_update_without_redis, __version__,
                        self._on_update_available)
                except Exception as ue:
                    logger.warning("GitHub での更新確認に失敗: %s", ue)
        finally:
            if swim_client is not None:
                try:
                    await swim_client.close()
                except Exception as e:
                    logger.debug("SwimClient close 失敗 (無視): %s", e)
            if redis_client is not None:
                try:
                    await redis_client.aclose()
                except Exception as e:
                    logger.debug("Redis close 失敗 (無視): %s", e)
            self.consumer = None
