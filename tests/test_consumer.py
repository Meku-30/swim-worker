"""Consumer テスト"""
import asyncio
import zstandard as zstd
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from swim_worker.consumer import TaskConsumer


@pytest.mark.asyncio
class TestTaskConsumer:
    async def test_send_heartbeat(self):
        mock_redis = AsyncMock()
        consumer = TaskConsumer(redis_client=mock_redis, swim_client=AsyncMock(), worker_name="test-worker", heartbeat_interval=30)
        await consumer.send_heartbeat()
        mock_redis.setex.assert_called_once()
        args = mock_redis.setex.call_args[0]
        assert args[0] == "heartbeat:test-worker"
        assert args[1] == 90
        # value は instance_token (UUID 文字列)
        assert isinstance(args[2], str) and len(args[2]) >= 32

    async def test_register_worker_new(self):
        mock_redis = AsyncMock()
        mock_redis.sismember.return_value = False  # 未承認
        consumer = TaskConsumer(redis_client=mock_redis, swim_client=AsyncMock(), worker_name="test-worker", heartbeat_interval=30)
        await consumer.register()
        mock_redis.sadd.assert_called_once_with("workers:pending", "test-worker")

    async def test_register_worker_already_approved(self):
        mock_redis = AsyncMock()
        mock_redis.sismember.return_value = True  # 承認済み
        consumer = TaskConsumer(redis_client=mock_redis, swim_client=AsyncMock(), worker_name="test-worker", heartbeat_interval=30)
        await consumer.register()
        mock_redis.sadd.assert_not_called()  # pendingに追加しない

    async def test_execute_task_parsed_when_whitelisted(self):
        """Redis whitelist に含まれる job_type は Worker 側でパース (format=parsed)"""
        import swim_worker.parsers as _p
        _p._cache_expires_at = 0.0  # キャッシュリセット
        mock_swim = AsyncMock()
        mock_swim.execute_api.return_value = {"weatherDTO": {}}
        mock_redis = AsyncMock()
        mock_redis.smembers.return_value = {b"collect_pkg_weather"}
        mock_redis.sismember.return_value = False  # per-worker 個別無効化なし
        consumer = TaskConsumer(redis_client=mock_redis, swim_client=mock_swim, worker_name="test-worker", heartbeat_interval=30)
        task = {"task_id": "task-001", "job_type": "collect_pkg_weather", "params": {"url": "https://example.com/api", "body": {"airports": ["RJTT"]}}}
        await consumer.execute_task(task)
        result_calls = [c for c in mock_redis.setex.call_args_list if c[0][0] == "results:task-001"]
        assert len(result_calls) == 1
        result_data = json.loads(zstd.ZstdDecompressor().decompress(result_calls[0][0][2]))
        assert result_data["status"] == "success"
        assert result_data["format"] == "parsed"
        assert isinstance(result_data["data"], list)

    async def test_execute_task_raw_when_worker_individually_disabled(self):
        """global で enable されていても、per-worker 除外に入っていれば raw 送信"""
        import swim_worker.parsers as _p
        _p._cache_expires_at = 0.0
        mock_swim = AsyncMock()
        mock_swim.execute_api.return_value = {"weatherDTO": {}}
        mock_redis = AsyncMock()
        mock_redis.smembers.return_value = {b"collect_pkg_weather"}  # global enable
        mock_redis.sismember.return_value = True  # この Worker は個別 disable
        consumer = TaskConsumer(redis_client=mock_redis, swim_client=mock_swim, worker_name="hyuga", heartbeat_interval=30)
        task = {"task_id": "task-x", "job_type": "collect_pkg_weather", "params": {"url": "https://example.com/api", "body": {}}}
        await consumer.execute_task(task)
        result_calls = [c for c in mock_redis.setex.call_args_list if c[0][0] == "results:task-x"]
        result_data = json.loads(zstd.ZstdDecompressor().decompress(result_calls[0][0][2]))
        assert "format" not in result_data  # 個別 disable → raw 送信
        assert result_data["data"] == {"weatherDTO": {}}

    async def test_execute_task_raw_when_not_whitelisted(self):
        """Redis whitelist にない job_type は raw 送信 (現状維持)"""
        import swim_worker.parsers as _p
        _p._cache_expires_at = 0.0
        mock_swim = AsyncMock()
        mock_swim.execute_api.return_value = {"weatherDTO": {}}
        mock_redis = AsyncMock()
        mock_redis.smembers.return_value = set()  # whitelist 空
        mock_redis.sismember.return_value = False
        consumer = TaskConsumer(redis_client=mock_redis, swim_client=mock_swim, worker_name="test-worker", heartbeat_interval=30)
        task = {"task_id": "task-002", "job_type": "collect_pkg_weather", "params": {"url": "https://example.com/api", "body": {}}}
        await consumer.execute_task(task)
        result_calls = [c for c in mock_redis.setex.call_args_list if c[0][0] == "results:task-002"]
        result_data = json.loads(zstd.ZstdDecompressor().decompress(result_calls[0][0][2]))
        assert "format" not in result_data  # whitelist 外 → raw
        assert result_data["data"] == {"weatherDTO": {}}

    async def test_execute_task_failure(self):
        mock_swim = AsyncMock()
        mock_swim.execute_api.side_effect = Exception("API down")
        mock_redis = AsyncMock()
        consumer = TaskConsumer(redis_client=mock_redis, swim_client=mock_swim, worker_name="test-worker", heartbeat_interval=30)
        task = {"task_id": "task-002", "job_type": "collect_pireps", "params": {"url": "https://example.com/api", "body": {}}}
        await consumer.execute_task(task)
        result_calls = [c for c in mock_redis.setex.call_args_list if c[0][0] == "results:task-002"]
        assert len(result_calls) == 1
        result_data = json.loads(zstd.ZstdDecompressor().decompress(result_calls[0][0][2]))
        assert result_data["status"] == "error"
        assert "API down" in result_data["error"]

    async def test_heartbeat_loop_periodically_reports_version(self):
        """_heartbeat_loop は _VERSION_CHECK_INTERVAL ごとに report_version を呼ぶ
        (Redis 揮発時の worker_versions/platforms 自動復旧用)"""
        mock_redis = AsyncMock()
        mock_redis.sismember.return_value = True
        consumer = TaskConsumer(redis_client=mock_redis, swim_client=AsyncMock(),
                                worker_name="test-worker", heartbeat_interval=30)
        # counter を既に限界直前にして、1回のループで超えるように
        consumer._version_check_counter = consumer._VERSION_CHECK_INTERVAL - 1
        consumer._running = True

        async def stop_after_one_iter():
            await asyncio.sleep(0)
            consumer._running = False

        # _heartbeat_loop は無限ループなので、1イテレーション後に止める。
        # sleep を短縮するため heartbeat_interval を 0 にしておくと即2周目だがキャンセルで抜ける
        consumer._heartbeat_interval = 0
        task = asyncio.create_task(consumer._heartbeat_loop())
        await asyncio.sleep(0.1)
        consumer._running = False
        try:
            await asyncio.wait_for(task, timeout=1)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            task.cancel()

        # report_version が hset するのは worker_versions と worker_platforms の2種
        keys_written = [c[0][0] for c in mock_redis.hset.call_args_list]
        assert "worker_versions" in keys_written
        assert "worker_platforms" in keys_written

    async def test_run_sets_client_name(self):
        mock_redis = AsyncMock()
        mock_redis.sismember.return_value = True
        mock_redis.blpop.side_effect = asyncio.CancelledError()
        consumer = TaskConsumer(mock_redis, AsyncMock(), "test-worker")
        try:
            await asyncio.wait_for(consumer.run(), timeout=3)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        mock_redis.client_setname.assert_called_once_with("test-worker")
