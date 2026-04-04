"""Redis タスクコンシューマー

自分専用のキュー (tasks:{worker_name}) を監視し、
タスクを取得してSWIM APIを実行、結果をRedisに返す。
"""
import asyncio
import gzip
import json
import logging
import math
import random
from datetime import datetime, timezone

import redis.exceptions

from swim_worker import __version__
from swim_worker.auth import SwimClient

logger = logging.getLogger(__name__)

RESULT_TTL = 3600  # 結果の有効期限（秒）
HEARTBEAT_TTL_MULTIPLIER = 3


class TaskConsumer:
    def __init__(self, redis_client, swim_client: SwimClient, worker_name: str,
                 heartbeat_interval: int = 30,
                 request_delay_median: float = 4.0,
                 request_delay_p99: float = 15.0,
                 request_delay_clip_min: float = 1.5,
                 request_delay_clip_max: float = 25.0,
                 on_update_available=None) -> None:
        self._redis = redis_client
        self._swim = swim_client
        self._worker_name = worker_name
        self._heartbeat_interval = heartbeat_interval
        # 対数正規分布パラメータ（人間のブラウジング間隔を再現）
        self._delay_mu = math.log(request_delay_median)
        self._delay_sigma = (math.log(request_delay_p99) - self._delay_mu) / 2.326
        self._delay_clip_min = request_delay_clip_min
        self._delay_clip_max = request_delay_clip_max
        self._running = False
        self._tasks: list[asyncio.Task] = []
        # 新バージョン検知時に呼ばれるコールバック (GUI連携用)
        # シグネチャ: callback(latest_version: str) -> None
        self._on_update_available = on_update_available

    async def register(self) -> None:
        """Worker を pending リストに登録（承認済みならスキップ）"""
        is_approved = await self._redis.sismember("workers:approved", self._worker_name)
        if is_approved:
            logger.info("Worker '%s' は承認済み（再登録スキップ）", self._worker_name)
            return
        await self._redis.sadd("workers:pending", self._worker_name)
        logger.info("Worker '%s' を登録しました (pending)", self._worker_name)

    async def _run_capability_test(self, task_id: str, params: dict) -> None:
        """capability_test ジョブ: 複数のテストリクエストを実行して結果を返す。

        params: {"tests": [{"job_type": str, "url": str, "body": dict}, ...]}
        返却: {job_type: {"ok": bool, "error": str | None}, ...}
        """
        tests = params.get("tests") or []
        results: dict[str, dict] = {}
        for test in tests:
            jt = test.get("job_type", "")
            url = test.get("url", "")
            body = test.get("body") or {}
            try:
                # 各テスト間に短い遅延 (一気に叩かない)
                await asyncio.sleep(random.uniform(1.0, 3.0))
                await self._swim.execute_api(url, body)
                results[jt] = {"ok": True, "error": None}
                logger.info("capability OK: %s", jt)
            except Exception as e:
                results[jt] = {"ok": False, "error": str(e)[:300]}
                logger.warning("capability NG: %s — %s", jt, e)
        result = {
            "task_id": task_id,
            "worker_name": self._worker_name,
            "status": "success",
            "data": {"capabilities": results},
            "error": None,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        compressed = gzip.compress(json.dumps(result).encode())
        await self._redis.setex(f"results:{task_id}", RESULT_TTL, compressed)
        logger.info("capability_test 完了: %s (ok=%d, ng=%d)",
            task_id[:8],
            sum(1 for r in results.values() if r["ok"]),
            sum(1 for r in results.values() if not r["ok"]),
        )

    async def report_version(self) -> None:
        """自身のバージョンをRedis hash worker_versions に保存する"""
        try:
            await self._redis.hset("worker_versions", self._worker_name, __version__)
            logger.info("Workerバージョン: v%s", __version__)
        except Exception as e:
            logger.warning("バージョン登録エラー: %s", e)

    async def check_latest_version(self) -> None:
        """Coordinatorが記録した最新版 (Redis) と自分を比較し、古ければ警告ログを出す"""
        try:
            raw = await self._redis.get("swim:latest_worker_version")
            if not raw:
                return
            latest_tag = (raw.decode() if isinstance(raw, bytes) else raw).lstrip("v")
            if not latest_tag:
                return
            current = tuple(int(x) for x in __version__.split(".") if x.isdigit())
            latest = tuple(int(x) for x in latest_tag.split(".") if x.isdigit())
            if latest > current:
                logger.warning(
                    "新しいバージョンが利用可能です: v%s → v%s  "
                    "https://github.com/Meku-30/swim-worker/releases/latest",
                    __version__, latest_tag,
                )
                # GUIに通知 (設定されていれば)
                if self._on_update_available:
                    try:
                        self._on_update_available(latest_tag)
                    except Exception as e:
                        logger.debug("update callback エラー: %s", e)
            else:
                logger.info("バージョン最新 (v%s)", __version__)
        except Exception as e:
            logger.debug("バージョンチェックエラー: %s", e)

    async def send_heartbeat(self) -> None:
        ttl = self._heartbeat_interval * HEARTBEAT_TTL_MULTIPLIER
        await self._redis.setex(f"heartbeat:{self._worker_name}", ttl, "alive")

    async def execute_task(self, task: dict) -> None:
        """タスクを実行し結果をRedisに書き込む"""
        task_id = task.get("task_id")
        job_type = task.get("job_type")
        if not task_id or not job_type:
            logger.error("不正なタスク（task_id/job_type欠落）: %s", str(task)[:200])
            return
        params = task.get("params") or {}
        logger.info("タスク実行開始: %s (type=%s)", task_id, job_type)

        # capability_test: 複数のテストリクエストを順に実行し各結果を返す
        if job_type == "capability_test":
            await self._run_capability_test(task_id, params)
            return

        try:
            raw = random.lognormvariate(self._delay_mu, self._delay_sigma)
            delay = max(self._delay_clip_min, min(self._delay_clip_max, raw))
            logger.debug("リクエスト前遅延: %.1f秒", delay)
            await asyncio.sleep(delay)

            url = params["url"]
            method = params.get("method", "POST")
            if method == "GET":
                data = await self._swim.fetch_public_get(
                    url, params=params.get("params"), headers=params.get("headers"),
                )
            else:
                body = params["body"]
                data = await self._swim.execute_api(url, body)
            result = {
                "task_id": task_id, "worker_name": self._worker_name,
                "status": "success", "data": data, "error": None,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            logger.info("タスク成功: %s", task_id)
        except Exception as e:
            result = {
                "task_id": task_id, "worker_name": self._worker_name,
                "status": "error", "data": None, "error": str(e),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            logger.error("タスク失敗: %s — %s", task_id, e)

        compressed = gzip.compress(json.dumps(result).encode())
        await self._redis.setex(f"results:{task_id}", RESULT_TTL, compressed)

    async def _ensure_registered(self) -> None:
        """approved にも pending にもいなければ再登録する"""
        is_approved = await self._redis.sismember("workers:approved", self._worker_name)
        is_pending = await self._redis.sismember("workers:pending", self._worker_name)
        if not is_approved and not is_pending:
            await self.register()

    async def _heartbeat_loop(self) -> None:
        while self._running:
            try:
                await self.send_heartbeat()
                await self._ensure_registered()
            except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
                logger.warning("Redis接続エラー（ハートビート）、5秒後にリトライ: %s", e)
                await asyncio.sleep(5)
                continue
            except Exception as e:
                logger.warning("ハートビート送信失敗: %s", e)
            await asyncio.sleep(self._heartbeat_interval)

    async def _consume_loop(self) -> None:
        queue_key = f"tasks:{self._worker_name}"
        while self._running:
            try:
                item = await self._redis.blpop(queue_key, timeout=5)
                if item is None:
                    continue
                _, raw = item
                task = json.loads(raw)
                await self.execute_task(task)
            except asyncio.CancelledError:
                break
            except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
                logger.warning("Redis接続エラー（コンシューマー）、5秒後にリトライ: %s", e)
                await asyncio.sleep(5)
            except Exception as e:
                logger.error("コンシューマーエラー: %s", e)
                await asyncio.sleep(1)

    async def run(self) -> None:
        self._running = True
        try:
            await self._redis.client_setname(self._worker_name)
        except Exception as e:
            logger.warning("CLIENT SETNAME 失敗: %s", e)
        await self.register()
        await self.send_heartbeat()
        await self.report_version()
        await self.check_latest_version()
        logger.info("Worker '%s' 起動", self._worker_name)
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        consume_task = asyncio.create_task(self._consume_loop())
        self._tasks = [heartbeat_task, consume_task]
        try:
            await asyncio.gather(heartbeat_task, consume_task)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            logger.info("Worker '%s' 停止", self._worker_name)

    def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
