"""Redis タスクコンシューマー

自分専用のキュー (tasks:{worker_name}) を監視し、
タスクを取得してSWIM APIを実行、結果をRedisに返す。
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from swim_worker.auth import SwimClient

logger = logging.getLogger(__name__)

RESULT_TTL = 3600  # 結果の有効期限（秒）
HEARTBEAT_TTL_MULTIPLIER = 3


class TaskConsumer:
    def __init__(self, redis_client, swim_client: SwimClient, worker_name: str, heartbeat_interval: int = 30) -> None:
        self._redis = redis_client
        self._swim = swim_client
        self._worker_name = worker_name
        self._heartbeat_interval = heartbeat_interval
        self._running = False
        self._tasks: list[asyncio.Task] = []

    async def register(self) -> None:
        """Worker を pending リストに登録"""
        await self._redis.sadd("workers:pending", self._worker_name)
        logger.info("Worker '%s' を登録しました (pending)", self._worker_name)

    async def send_heartbeat(self) -> None:
        ttl = self._heartbeat_interval * HEARTBEAT_TTL_MULTIPLIER
        await self._redis.setex(f"heartbeat:{self._worker_name}", ttl, "alive")

    async def execute_task(self, task: dict) -> None:
        """タスクを実行し結果をRedisに書き込む"""
        task_id = task["task_id"]
        job_type = task["job_type"]
        params = task.get("params", {})
        logger.info("タスク実行開始: %s (type=%s)", task_id, job_type)

        try:
            url = params["url"]
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

        await self._redis.setex(f"results:{task_id}", RESULT_TTL, json.dumps(result))

    async def _heartbeat_loop(self) -> None:
        while self._running:
            try:
                await self.send_heartbeat()
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
            except Exception as e:
                logger.error("コンシューマーエラー: %s", e)
                await asyncio.sleep(1)

    async def run(self) -> None:
        self._running = True
        await self.register()
        await self.send_heartbeat()
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
