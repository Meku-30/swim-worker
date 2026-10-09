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
# consumer.run() の途中でこれが出たら Redis に入り直す (認証エラー・重複起動以外の通信の失敗)
RETRYABLE_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError,
                    OSError, asyncio.TimeoutError)
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 300.0  # 5 分


def _noop(*a, **kw):
    pass


class WorkerRunner:
    """Worker を 1 回動かす (起動 → 停止で終わり。再起動は新しいインスタンスで)。

    Redis に入れない間は認証エラー・重複起動以外なら上限 5 分のバックオフで無期限に再試行する。
    終わったら on_finished(outcome, message) を必ず 1 回呼ぶ (Worker のスレッドから)。
    """

    def __init__(self, settings: dict, *, on_status=_noop, on_finished=_noop,
                 on_update_available=_noop, on_task_state=_noop, sleep=None):
        # settings: GUI のメインスレッドでコピーした値 (別スレッドから Tk を触らないため)
        self._settings = dict(settings)
        self._on_status = on_status
        self._on_finished = on_finished
        self._on_update_available = on_update_available
        self._on_task_state = on_task_state
        self._sleep = sleep or asyncio.sleep
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._stop_requested = False
        self._outcome = (STOPPED, "")
        self.consumer = None

    # --- 外 (GUI のスレッドなど) から呼ぶ ---

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, daemon=True,
                                        name="swim-worker-runner")
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: float | None = None) -> bool:
        """終わるまで待つ。終わっていれば True"""
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.is_alive()

    def request_stop(self, graceful: bool = True) -> None:
        """停止要求。graceful なら処理中のタスクを終えてから (consumer.request_stop)、
        そうでなければ即座に止める。consumer を作る前 (Redis の再試行中) なら待ちを中断する。"""
        # 先にフラグを立てる。_thread_main 側は create_task 直後にこのフラグを見て、
        # ループ/Task ができる前に停止が押されていた場合は自分で cancel する (両側で競合を閉じる)
        self._stop_requested = True
        loop = self._loop
        if loop is not None and not loop.is_closed():
            # asyncio のオブジェクトはループのスレッドで触る
            try:
                loop.call_soon_threadsafe(self._stop_in_loop, graceful)
            except RuntimeError:
                pass  # ループ終了済み

    def stop_and_join(self, graceful_timeout: float = 30.0, hard_timeout: float = 10.0) -> bool:
        """止めて終わるまで待つ (GUI のメインスレッドからは呼ばないこと)。

        処理中のタスクを graceful_timeout 秒まで待ち、終わらなければ中断して hard_timeout 秒待つ。
        止まったら True。
        """
        if not self.is_alive():
            return True
        self.request_stop(graceful=True)
        if self.join(graceful_timeout):
            return True
        logger.warning("Worker が %.0f 秒で止まらないため、処理中のタスクを中断します", graceful_timeout)
        self.request_stop(graceful=False)
        return self.join(hard_timeout)

    # --- Worker のスレッド ---

    def _stop_in_loop(self, graceful: bool) -> None:
        consumer = self.consumer
        if graceful and consumer is not None:
            consumer.request_stop()
            return
        if consumer is not None:
            consumer.stop()
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
            self._outcome = (ERROR, str(e))
        finally:
            self._task = None
            self._loop = None
            try:
                loop.close()
            finally:
                outcome, message = self._outcome
                try:
                    self._on_finished(outcome, message)
                except Exception as e:
                    logger.debug("on_finished で例外 (無視): %s", e)

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

    async def _backoff(self, attempt: int, delay: float, error) -> float:
        logger.warning("Redis に接続できません (%d 回目)。%.0f 秒後に再試行: %s", attempt, delay, error)
        self._on_status(f"Redis 再接続待ち ({attempt} 回目、{delay:.0f} 秒後に再試行)")
        await self._sleep(delay)
        return min(delay * 2, MAX_BACKOFF)

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
            delay = INITIAL_BACKOFF
            attempt = 0
            while not self._stop_requested:
                try:
                    await redis_client.ping()
                except AUTH_ERRORS:
                    raise  # 待っても直らない
                except Exception as e:
                    attempt += 1
                    delay = await self._backoff(attempt, delay, e)
                    continue
                logger.info("Redis接続成功")
                self._on_status("● 接続中 (タスク待ち)")
                delay = INITIAL_BACKOFF
                if swim_client is None:
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
                if self._stop_requested:
                    break
                try:
                    await self.consumer.run()
                    break  # 停止要求 (処理中のタスクは終えた)
                except (DuplicateWorkerError, *AUTH_ERRORS):
                    raise
                except RETRYABLE_ERRORS as e:
                    self.consumer = None
                    attempt += 1
                    delay = await self._backoff(attempt, delay, e)
            self._outcome = (STOPPED, "")
        except DuplicateWorkerError as e:
            logger.error("重複起動検知: %s", e)
            self._outcome = (DUPLICATE, str(e))
        except asyncio.CancelledError:
            logger.info("Worker 停止要求を受け付けました")
            self._outcome = (STOPPED, "")
        except Exception as e:
            logger.error("エラー: %s", e)
            auth_failed = isinstance(e, AUTH_ERRORS)
            self._outcome = (AUTH_ERROR if auth_failed else ERROR, str(e))
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
