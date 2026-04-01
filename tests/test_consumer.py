"""Consumer テスト"""
import gzip
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
        mock_redis.setex.assert_called_once_with("heartbeat:test-worker", 90, "alive")

    async def test_register_worker(self):
        mock_redis = AsyncMock()
        consumer = TaskConsumer(redis_client=mock_redis, swim_client=AsyncMock(), worker_name="test-worker", heartbeat_interval=30)
        await consumer.register()
        mock_redis.sadd.assert_called_once_with("workers:pending", "test-worker")

    async def test_execute_task_success(self):
        mock_swim = AsyncMock()
        mock_swim.execute_api.return_value = {"data": "metar-result"}
        mock_redis = AsyncMock()
        consumer = TaskConsumer(redis_client=mock_redis, swim_client=mock_swim, worker_name="test-worker", heartbeat_interval=30)
        task = {"task_id": "task-001", "job_type": "collect_pkg_weather", "params": {"url": "https://example.com/api", "body": {"airports": ["RJTT"]}}}
        await consumer.execute_task(task)
        result_calls = [c for c in mock_redis.setex.call_args_list if c[0][0] == "results:task-001"]
        assert len(result_calls) == 1
        result_data = json.loads(gzip.decompress(result_calls[0][0][2]))
        assert result_data["status"] == "success"
        assert result_data["data"] == {"data": "metar-result"}

    async def test_execute_task_failure(self):
        mock_swim = AsyncMock()
        mock_swim.execute_api.side_effect = Exception("API down")
        mock_redis = AsyncMock()
        consumer = TaskConsumer(redis_client=mock_redis, swim_client=mock_swim, worker_name="test-worker", heartbeat_interval=30)
        task = {"task_id": "task-002", "job_type": "collect_pireps", "params": {"url": "https://example.com/api", "body": {}}}
        await consumer.execute_task(task)
        result_calls = [c for c in mock_redis.setex.call_args_list if c[0][0] == "results:task-002"]
        assert len(result_calls) == 1
        result_data = json.loads(gzip.decompress(result_calls[0][0][2]))
        assert result_data["status"] == "error"
        assert "API down" in result_data["error"]
