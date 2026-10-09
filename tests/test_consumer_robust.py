"""consumer の結果の書き込み・heartbeat・停止まわりのテスト (W1-5・W1-7)"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis.exceptions
import zstandard as zstd

from swim_worker import consumer as consumer_mod
from swim_worker.consumer import DuplicateWorkerError, TaskConsumer
from tests.conftest import make_redis

URL = "https://web.swim.mlit.go.jp/f2aspr/web/FLV920/LGV358"


def _result(call):
    return json.loads(zstd.ZstdDecompressor().decompress(call.args[2]))


def _results(redis_mock, key):
    return [_result(c) for c in redis_mock.setex.call_args_list if c.args[0] == key]


def _consumer(redis_mock, swim=None, **kw):
    states = []
    c = TaskConsumer(redis_mock, swim or AsyncMock(), "w1",
                     request_delay_median=0.01, request_delay_p99=0.02,
                     request_delay_clip_min=0.0, request_delay_clip_max=0.01,
                     on_task_state=lambda state, **k: states.append((state, k)), **kw)
    return c, states


@pytest.fixture(autouse=True)
def _no_parse(monkeypatch):
    async def no(*a, **k):
        return False
    monkeypatch.setattr(consumer_mod.parsers, "supports", no)


@pytest.fixture
def fast_sleep(monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake(sec, *a, **k):
        sleeps.append(sec)
        await real_sleep(0)
    monkeypatch.setattr(consumer_mod.asyncio, "sleep", fake)
    return sleeps


@pytest.mark.asyncio
class TestResultWrite:
    async def test_result_write_is_retried(self, fast_sleep):
        r = make_redis()
        r.setex.side_effect = [redis.exceptions.ConnectionError("x"), redis.exceptions.TimeoutError("y"), True]
        swim = AsyncMock()
        swim.execute_api.return_value = {"ok": 1}
        c, states = _consumer(r, swim)
        await c.execute_task({"task_id": "t1", "job_type": "collect_pireps", "params": {"url": URL, "body": {}}})
        assert r.setex.await_count == 3
        assert _result(r.setex.call_args_list[-1])["status"] == "success"
        assert states[-1] == ("idle", {"job_type": "", "total": 1, "errors": 0})

    async def test_result_write_failure_still_counts_and_notifies(self, fast_sleep):
        r = make_redis()
        r.setex.side_effect = redis.exceptions.ConnectionError("down")
        swim = AsyncMock()
        swim.execute_api.return_value = {"ok": 1}
        c, states = _consumer(r, swim)
        await c.execute_task({"task_id": "t1", "job_type": "collect_pireps", "params": {"url": URL, "body": {}}})
        assert states[-1][0] == "idle"
        assert states[-1][1]["total"] == 1 and states[-1][1]["errors"] == 1

    async def test_capability_result_write_failure_still_notifies(self, fast_sleep):
        r = make_redis()
        r.setex.side_effect = redis.exceptions.ConnectionError("down")
        c, states = _consumer(r)
        await c.execute_task({"task_id": "t1", "job_type": "capability_test", "params": {"tests": []}})
        assert states[-1][0] == "idle" and states[-1][1]["total"] == 1

    async def test_result_dict_shape(self, fast_sleep):
        r = make_redis()
        swim = AsyncMock()
        swim.execute_api.side_effect = RuntimeError("boom")
        c, _ = _consumer(r, swim)
        await c.execute_task({"task_id": "t9", "job_type": "collect_pireps", "params": {"url": URL, "body": {}}})
        res = _results(r, "results:w1:t9")[0]
        assert set(res) == {"task_id", "worker_name", "status", "data", "error", "completed_at"}
        assert res["status"] == "error" and "boom" in res["error"] and res["worker_name"] == "w1"


@pytest.mark.asyncio
class TestHardTimeout:
    async def test_hard_timeout_writes_error_result(self):
        r = make_redis()
        task = {"task_id": "slow", "job_type": "collect_pireps", "params": {"url": URL, "body": {}}}
        n = {"i": 0}

        async def blpop(*a, **k):
            n["i"] += 1
            if n["i"] == 1:
                return ("tasks:w1", json.dumps(task))
            raise asyncio.CancelledError()
        r.blpop.side_effect = blpop
        swim = AsyncMock()

        async def hang(*a, **k):
            await asyncio.sleep(10)
        swim.execute_api.side_effect = hang
        c, states = _consumer(r, swim, task_hard_timeout=0.2)
        c._running = True
        await asyncio.wait_for(c._consume_loop(), timeout=3)
        res = _results(r, "results:w1:slow")
        assert len(res) == 1 and res[0]["status"] == "error"
        assert "タイムアウト" in res[0]["error"]
        assert states[-1][0] == "idle" and states[-1][1]["errors"] == 1


@pytest.mark.asyncio
class TestRedisErrors:
    async def test_consume_loop_waits_longer_on_auth_error(self):
        r = make_redis()
        r.blpop.side_effect = redis.exceptions.AuthenticationError("WRONGPASS")
        c, _ = _consumer(r)
        c._running = True
        sleeps = []

        async def fake_sleep(sec):
            sleeps.append(sec)
            c._running = False
        with patch("swim_worker.consumer.asyncio.sleep", new=fake_sleep):
            await c._consume_loop()
        assert sleeps == [consumer_mod.AUTH_RETRY_DELAY]
        assert consumer_mod.AUTH_RETRY_DELAY >= 60

    async def test_heartbeat_loop_waits_longer_on_auth_error(self):
        r = make_redis()
        r.eval.side_effect = redis.exceptions.AuthenticationError("WRONGPASS")
        c, _ = _consumer(r)
        c._running = True
        sleeps = []

        async def fake_sleep(sec):
            sleeps.append(sec)
            c._running = False
        with patch("swim_worker.consumer.asyncio.sleep", new=fake_sleep):
            await c._heartbeat_loop()
        assert sleeps == [consumer_mod.AUTH_RETRY_DELAY]


@pytest.mark.asyncio
class TestHeartbeatOwnership:
    async def test_other_owner_raises_duplicate(self):
        r = make_redis()
        r.eval.return_value = 0
        c, _ = _consumer(r)
        with pytest.raises(DuplicateWorkerError):
            await c.send_heartbeat()
        r.setex.assert_not_called()

    async def test_fallback_without_eval_permission(self):
        """Redis の ACL に EVAL が無い間は GET で確かめてから SETEX (非原子だが所有確認はする)"""
        r = make_redis()
        r.eval.side_effect = redis.exceptions.NoPermissionError("NOPERM eval")
        c, _ = _consumer(r)
        r.get.return_value = c._instance_token
        await c.send_heartbeat()
        r.setex.assert_awaited_once_with("heartbeat:w1", 60, c._instance_token)
        # 2 回目以降は EVAL を試さない
        await c.send_heartbeat()
        assert r.eval.await_count == 1
        assert r.setex.await_count == 2
        # 期限切れ (None) なら取り直す
        r.get.return_value = None
        await c.send_heartbeat()
        assert r.setex.await_count == 3
        # 他人の token なら延長しない
        r.get.return_value = "someone-else"
        with pytest.raises(DuplicateWorkerError):
            await c.send_heartbeat()
        assert r.setex.await_count == 3

    async def test_run_stops_with_duplicate_when_heartbeat_taken(self, tmp_path, monkeypatch):
        monkeypatch.setattr(consumer_mod, "_last_instance_token_path", lambda: tmp_path / "tok")
        monkeypatch.setattr(consumer_mod, "_startup_marker_path", lambda: tmp_path / "ok")
        r = make_redis()
        r.set.return_value = True
        r.sismember.return_value = True
        r.get.return_value = None
        r.eval.return_value = 0

        async def blpop(*a, **k):
            await asyncio.sleep(10)
        r.blpop.side_effect = blpop
        c, _ = _consumer(r)
        with pytest.raises(DuplicateWorkerError):
            await asyncio.wait_for(c.run(), timeout=3)

    async def test_registration_check_uses_pipeline(self):
        r = make_redis()
        r.sismember.return_value = False
        c, _ = _consumer(r)
        await c._ensure_registered()
        r.pipeline.assert_called()
        r.sadd.assert_awaited_once_with("workers:pending", "w1")


@pytest.mark.asyncio
class TestParseCachePipeline:
    async def test_refresh_cache_uses_pipeline(self, monkeypatch):
        from swim_worker import parsers
        monkeypatch.setattr(parsers, "_cache_expires_at", 0.0)
        r = make_redis()
        r.smembers.return_value = {"collect_pireps", "collect_notams"}

        async def sismember(key, member):
            return key.endswith("collect_notams")
        r.sismember.side_effect = sismember
        await parsers._refresh_cache(r, worker_name="w1")
        r.pipeline.assert_called_once()
        assert parsers._cache_enabled == {"collect_pireps"}


@pytest.mark.asyncio
class TestGracefulStop:
    async def test_request_stop_finishes_running_task(self, tmp_path, monkeypatch):
        """W1-7: 停止要求 (SIGTERM) では処理中のタスクを終えて結果を書いてから止まる"""
        monkeypatch.setattr(consumer_mod, "_last_instance_token_path", lambda: tmp_path / "tok")
        monkeypatch.setattr(consumer_mod, "_startup_marker_path", lambda: tmp_path / "ok")
        r = make_redis()
        r.set.return_value = True
        r.sismember.return_value = True
        r.get.return_value = None
        r.eval.return_value = 1
        task = {"task_id": "t1", "job_type": "collect_pireps", "params": {"url": URL, "body": {}}}
        n = {"i": 0}

        async def blpop(*a, **k):
            n["i"] += 1
            if n["i"] == 1:
                return ("tasks:w1", json.dumps(task))
            await asyncio.sleep(10)
        r.blpop.side_effect = blpop
        started = asyncio.Event()
        swim = AsyncMock()

        async def slow_api(*a, **k):
            started.set()
            await asyncio.sleep(0.3)
            return {"ok": 1}
        swim.execute_api.side_effect = slow_api
        c, _ = _consumer(r, swim)
        run = asyncio.create_task(c.run())
        await asyncio.wait_for(started.wait(), timeout=3)
        c.request_stop()
        await asyncio.wait_for(run, timeout=3)
        res = _results(r, "results:w1:t1")
        assert len(res) == 1 and res[0]["status"] == "success"
        assert n["i"] == 1  # 停止要求後は次のタスクを取らない

    async def test_request_stop_when_idle_returns_quickly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(consumer_mod, "_last_instance_token_path", lambda: tmp_path / "tok")
        monkeypatch.setattr(consumer_mod, "_startup_marker_path", lambda: tmp_path / "ok")
        r = make_redis()
        r.set.return_value = True
        r.sismember.return_value = True
        r.get.return_value = None
        r.eval.return_value = 1
        entered = asyncio.Event()

        async def blpop(*a, **k):
            entered.set()
            await asyncio.sleep(10)
        r.blpop.side_effect = blpop
        c, _ = _consumer(r)
        run = asyncio.create_task(c.run())
        await asyncio.wait_for(entered.wait(), timeout=3)
        c.request_stop()
        await asyncio.wait_for(run, timeout=1)


class TestSignalHandlers:
    def test_falls_back_to_signal_signal_on_windows(self, monkeypatch):
        """W1-7: add_signal_handler が無い (Windows) ときは signal.signal で受けて停止要求を出す"""
        from swim_worker import __main__ as main_mod
        loop = MagicMock()
        loop.add_signal_handler.side_effect = NotImplementedError
        consumer = MagicMock()
        installed = {}
        monkeypatch.setattr(main_mod.signal, "signal", lambda sig, h: installed.__setitem__(sig, h))
        main_mod.install_signal_handlers(loop, consumer)
        assert main_mod.signal.SIGINT in installed and main_mod.signal.SIGTERM in installed
        handler = installed[main_mod.signal.SIGTERM]
        handler(main_mod.signal.SIGTERM, None)
        loop.call_soon_threadsafe.assert_called_with(consumer.request_stop)
        # 2 回目は即停止
        handler(main_mod.signal.SIGTERM, None)
        loop.call_soon_threadsafe.assert_called_with(consumer.stop)

    def test_uses_add_signal_handler_when_available(self, monkeypatch):
        from swim_worker import __main__ as main_mod
        loop = MagicMock()
        consumer = MagicMock()
        called = []
        monkeypatch.setattr(main_mod.signal, "signal", lambda *a: called.append(a))
        main_mod.install_signal_handlers(loop, consumer)
        assert loop.add_signal_handler.call_count >= 2
        assert called == []
        # 登録したハンドラ: 1 回目は停止要求、2 回目は即停止
        cb = loop.add_signal_handler.call_args_list[0].args[1]
        cb()
        consumer.request_stop.assert_called_once()
        cb()
        consumer.stop.assert_called_once()
