"""GUI から分けた tkinter 非依存のモジュールのテスト (画面のない環境でも走る)"""
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import redis.exceptions

from swim_worker import autostart, gui_helpers, settings_store, updater, worker_runner


# --- gui_helpers ---

class TestTaskStateText:
    def test_processing_uses_japanese_label(self):
        assert gui_helpers.task_state_text("processing", "collect_notams") == "● 実行中: NOTAM収集"

    def test_unknown_job_type_shown_as_is(self):
        assert gui_helpers.task_state_text("processing", "x_job") == "● 実行中: x_job"

    def test_idle_with_and_without_errors(self):
        assert gui_helpers.task_state_text("idle", total=3) == "● 接続中 (処理済 3 件)"
        assert gui_helpers.task_state_text("idle", total=3, errors=1) == "● 接続中 (処理済 3 件, エラー 1)"


@pytest.mark.parametrize("snoozed,auto,expected", [
    (False, False, "prompt"), (False, True, "countdown"), (True, False, None), (True, True, None)])
def test_update_prompt_kind(snoozed, auto, expected):
    assert gui_helpers.update_prompt_kind(snoozed=snoozed, auto_update=auto) == expected


# --- updater ---

class TestDownloadUrl:
    def test_windows(self):
        assert updater.download_url("1.2.3", "win32") == \
            "https://github.com/Meku-30/swim-worker/releases/download/v1.2.3/swim-worker-windows.exe"

    def test_macos(self):
        assert updater.download_url("1.2.3", "darwin").endswith("/v1.2.3/swim-worker-macos")

    def test_linux_has_no_gui_update(self):
        assert updater.download_url("1.2.3", "linux") is None

    def test_new_exe_path(self, tmp_path):
        assert updater.new_exe_path(tmp_path, "win32") == tmp_path / "swim-worker-gui.new.exe"
        assert updater.new_exe_path(tmp_path, "darwin") == tmp_path / "swim-worker.new"


class TestSnoozeStore:
    def test_set_then_snoozed_until_expiry(self, tmp_path):
        s = updater.SnoozeStore(tmp_path / "data" / "snooze.json", hours=24)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert s.is_snoozed("1.2.3", now) is False
        s.set("1.2.3", now)
        assert s.is_snoozed("1.2.3", now + timedelta(hours=23)) is True
        assert s.is_snoozed("1.2.3", now + timedelta(hours=24)) is False
        assert not s.path.exists()

    def test_other_version_clears(self, tmp_path):
        s = updater.SnoozeStore(tmp_path / "snooze.json")
        s.set("1.2.3")
        assert s.is_snoozed("1.2.4") is False
        assert not s.path.exists()

    @pytest.mark.parametrize("content", ['{"version": "1.2.3"}', '{"version": "1.2.3", "until": "x"}',
                                         "not json", "[]"])
    def test_broken_file_is_not_snoozed(self, tmp_path, content):
        s = updater.SnoozeStore(tmp_path / "snooze.json")
        s.path.write_text(content, encoding="utf-8")
        assert s.is_snoozed("1.2.3") is False

    def test_cleanup_expired(self, tmp_path):
        s = updater.SnoozeStore(tmp_path / "snooze.json")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        s.set("1.2.3", now)
        s.cleanup_expired(now + timedelta(hours=1))
        assert s.path.exists()
        s.cleanup_expired(now + timedelta(days=2))
        assert not s.path.exists()


def test_cleanup_stale_update_files(tmp_path):
    old = tmp_path / "swim-worker-gui.exe.old"
    fresh = tmp_path / "swim-worker.new"
    keep = tmp_path / "swim-worker-gui.exe"
    for f in (old, fresh, keep):
        f.write_bytes(b"x")
    four_days_ago = time.time() - 4 * 24 * 3600
    os.utime(old, (four_days_ago, four_days_ago))
    os.utime(keep, (four_days_ago, four_days_ago))
    updater.cleanup_stale_update_files(tmp_path)
    assert not old.exists()
    assert fresh.exists() and keep.exists()


# --- settings_store ---

class TestJson:
    def test_roundtrip_and_broken(self, tmp_path):
        p = tmp_path / "data" / "s.json"
        assert settings_store.load_json(p) == {}
        settings_store.save_json(p, {"auto_update": True, "名前": "x"})
        assert settings_store.load_json(p) == {"auto_update": True, "名前": "x"}
        p.write_text("{", encoding="utf-8")
        assert settings_store.load_json(p) == {}


# --- autostart ---

class TestAutostart:
    def test_startup_paths(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APPDATA", str(tmp_path))
        assert autostart.startup_path("win32") == (
            tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            / "SWIM Worker.bat")
        assert autostart.startup_path("darwin").name == "org.swim-worker.plist"

    def test_bat_contents(self):
        bat = autostart.build_bat(r"C:\swim\swim-worker-gui.exe", Path(r"C:\swim"))
        assert bat.startswith("@echo off\r\n")
        assert 'start "" "C:\\swim\\swim-worker-gui.exe"' in bat

    def test_enable_disable(self, tmp_path):
        p = tmp_path / "org.swim-worker.plist"
        autostart.enable(p, "darwin", "/Apps/swim/swim-worker", tmp_path)
        assert "<string>/Apps/swim/swim-worker</string>" in p.read_text(encoding="utf-8")
        assert autostart.needs_path_update(p, "darwin", "/Apps/swim/swim-worker") is False
        assert autostart.needs_path_update(p, "darwin", "/Other/swim-worker") is True
        autostart.disable(p)
        assert not p.exists()


# --- worker_runner ---

class _FakeRedis:
    def __init__(self, exc=None):
        self.exc = exc
        self.closed = False

    async def ping(self):
        if self.exc:
            raise self.exc
        return True

    async def aclose(self):
        self.closed = True


SETTINGS = {
    "redis_host": "redis.example", "redis_username": "worker-tester", "redis_password": "pw",
    "swim_username": "u", "swim_password": "p", "worker_name": "tester",
}


def _run_until_done(runner, timeout=10):
    runner.start()
    assert runner.join(timeout), "WorkerRunner が終わらない"


class TestWorkerRunner:
    def test_auth_error_reports_and_checks_github(self, monkeypatch):
        import swim_worker.redis_client as rc
        import swim_worker.update_check as uc
        fake = _FakeRedis(redis.exceptions.AuthenticationError("WRONGPASS"))
        monkeypatch.setattr(rc, "create_redis_client", lambda s: fake)
        checked = []
        monkeypatch.setattr(uc, "check_update_without_redis", lambda cur, notify: checked.append(cur))
        finished = []
        r = worker_runner.WorkerRunner(SETTINGS, on_finished=lambda o, m: finished.append(o))
        _run_until_done(r)
        assert finished == [worker_runner.AUTH_ERROR]
        assert len(checked) == 1
        assert fake.closed

    def test_duplicate_reports_duplicate(self, monkeypatch):
        import swim_worker.redis_client as rc
        import swim_worker.consumer as consumer
        monkeypatch.setattr(rc, "create_redis_client", lambda s: _FakeRedis())

        async def _dup(self):
            raise consumer.DuplicateWorkerError("dup")
        monkeypatch.setattr(consumer.TaskConsumer, "run", _dup)
        finished = []
        r = worker_runner.WorkerRunner(SETTINGS, on_finished=lambda o, m: finished.append(o))
        _run_until_done(r)
        assert finished == [worker_runner.DUPLICATE]

    def test_stop_while_running_ends_without_finished_error(self, monkeypatch):
        import swim_worker.redis_client as rc
        import swim_worker.consumer as consumer
        monkeypatch.setattr(rc, "create_redis_client", lambda s: _FakeRedis())
        started = []

        async def _forever(self):
            started.append(1)
            await asyncio.sleep(3600)
        monkeypatch.setattr(consumer.TaskConsumer, "run", _forever)
        finished = []
        r = worker_runner.WorkerRunner(SETTINGS, on_finished=lambda o, m: finished.append(o))
        r.start()
        deadline = time.monotonic() + 5
        while not started and time.monotonic() < deadline:
            time.sleep(0.01)
        r.request_stop()
        assert r.join(5)
        assert finished in ([], [worker_runner.STOPPED])

    def test_stop_before_loop_starts(self, monkeypatch):
        import swim_worker.redis_client as rc
        monkeypatch.setattr(rc, "create_redis_client", lambda s: _FakeRedis(ConnectionError("x")))
        r = worker_runner.WorkerRunner(SETTINGS)
        r.request_stop()
        _run_until_done(r)
