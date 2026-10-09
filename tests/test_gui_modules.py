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
        bat = autostart.build_bat([r"C:\swim\swim-worker-gui.exe"], Path(r"C:\swim"))
        assert bat.startswith("@echo off\r\n")
        assert 'start "" "C:\\swim\\swim-worker-gui.exe"' in bat

    def test_plist_escapes_xml(self, tmp_path):
        import plistlib
        exe = "/Users/a&b/<swim>/swim-worker"
        data = plistlib.loads(autostart.file_bytes("darwin", [exe], Path("/Users/a&b")))
        assert data["ProgramArguments"] == [exe]
        assert data["WorkingDirectory"] == "/Users/a&b"
        assert data["Label"] == "org.swim-worker"
        assert data["RunAtLoad"] is True

    def test_bat_doubles_percent(self):
        bat = autostart.build_bat([r"C:\100%\swim.exe"], Path(r"C:\100%"))
        assert 'cd /d "C:\\100%%"' in bat
        assert 'start "" "C:\\100%%\\swim.exe"' in bat

    def test_bat_uses_ansi_when_possible(self):
        b = autostart.file_bytes("win32", [r"C:\ユーザー\swim.exe"], Path(r"C:\ユーザー"),
                                 ansi_codec="cp932")
        assert b.decode("cp932").startswith("@echo off\r\ncd /d")
        assert "chcp" not in b.decode("cp932")

    def test_bat_falls_back_to_utf8_with_chcp(self):
        """ANSI コードページで書けない文字 (例: 日本語 Windows でのウムラウト) は UTF-8 + chcp 65001"""
        b = autostart.file_bytes("win32", [r"C:\Jürgen\swim.exe"], Path(r"C:\Jürgen"),
                                 ansi_codec="cp932")
        text = b.decode("utf-8")
        assert text.startswith("@echo off\r\nchcp 65001 > nul\r\n")
        assert r'start "" "C:\Jürgen\swim.exe"' in text

    def test_launch_command(self, monkeypatch):
        monkeypatch.setattr(autostart.sys, "frozen", True, raising=False)
        monkeypatch.setattr(autostart.sys, "executable", "/x/swim-worker")
        assert autostart.launch_command() == ["/x/swim-worker"]

    def test_enable_disable(self, tmp_path):
        p = tmp_path / "org.swim-worker.plist"
        autostart.enable(p, "darwin", ["/Apps/swim/swim-worker"], tmp_path)
        assert "<string>/Apps/swim/swim-worker</string>" in p.read_text(encoding="utf-8")
        assert autostart.needs_rewrite(p, "darwin", ["/Apps/swim/swim-worker"], tmp_path) is False
        assert autostart.needs_rewrite(p, "darwin", ["/Other/swim-worker"], tmp_path) is True
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
        r.request_stop(graceful=False)
        assert r.join(5)
        assert finished == [worker_runner.STOPPED]

    def test_graceful_stop_lets_consumer_finish(self, monkeypatch):
        import swim_worker.redis_client as rc
        import swim_worker.consumer as consumer
        monkeypatch.setattr(rc, "create_redis_client", lambda s: _FakeRedis())
        state = {}

        async def _run(self):
            self._running = True
            state["consumer"] = self
            while self._running:
                await asyncio.sleep(0.01)
            state["finished_normally"] = True
        monkeypatch.setattr(consumer.TaskConsumer, "run", _run)
        finished = []
        r = worker_runner.WorkerRunner(SETTINGS, on_finished=lambda o, m: finished.append(o))
        r.start()
        deadline = time.monotonic() + 5
        while "consumer" not in state and time.monotonic() < deadline:
            time.sleep(0.01)
        assert r.stop_and_join(graceful_timeout=5, hard_timeout=1)
        assert state.get("finished_normally") is True
        assert finished == [worker_runner.STOPPED]

    def test_stop_and_join_cancels_when_graceful_times_out(self, monkeypatch):
        import swim_worker.redis_client as rc
        import swim_worker.consumer as consumer
        monkeypatch.setattr(rc, "create_redis_client", lambda s: _FakeRedis())
        started = []

        async def _stuck(self):
            started.append(1)
            await asyncio.sleep(3600)
        monkeypatch.setattr(consumer.TaskConsumer, "run", _stuck)
        r = worker_runner.WorkerRunner(SETTINGS)
        r.start()
        deadline = time.monotonic() + 5
        while not started and time.monotonic() < deadline:
            time.sleep(0.01)
        t0 = time.monotonic()
        assert r.stop_and_join(graceful_timeout=0.3, hard_timeout=5)
        assert time.monotonic() - t0 < 4

    def test_stop_before_loop_starts(self, monkeypatch):
        import swim_worker.redis_client as rc
        monkeypatch.setattr(rc, "create_redis_client", lambda s: _FakeRedis(ConnectionError("x")))
        finished = []
        r = worker_runner.WorkerRunner(SETTINGS, on_finished=lambda o, m: finished.append(o))
        r.request_stop()
        _run_until_done(r)
        assert finished == [worker_runner.STOPPED]


# --- updater のダウンロード (ストリーム・検証) ---

import hashlib
import contextlib


class FakeFetcher:
    def __init__(self, files: dict, status: dict | None = None, chunk=1000):
        self.files = files
        self.status = status or {}
        self.chunk = chunk
        self.requested = []

    def get_text(self, url, max_bytes=1 << 20):
        self.requested.append(url)
        name = url.rsplit("/", 1)[-1]
        if self.status.get(name, 200) != 200 or name not in self.files:
            raise RuntimeError(f"{name} 取得失敗: status={self.status.get(name, 404)}")
        return self.files[name].decode()

    @contextlib.contextmanager
    def stream(self, url):
        self.requested.append(url)
        name = url.rsplit("/", 1)[-1]
        data = self.files.get(name, b"")
        chunks = (data[i:i + self.chunk] for i in range(0, len(data), self.chunk))
        yield self.status.get(name, 200 if name in self.files else 404), len(data), chunks


BASE = "https://github.com/Meku-30/swim-worker/releases/download/v9.9.9"
ASSET = "swim-worker-windows.exe"


def _release(content: bytes, digest: str | None = None):
    digest = digest or hashlib.sha256(content).hexdigest()
    return {ASSET: content, "SHA256SUMS": f"{digest}  {ASSET}\n".encode()}


class TestDownloadAndVerify:
    def test_streams_verifies_and_reports_progress(self, tmp_path):
        content = os.urandom(2 * 1024 * 1024 + 7)
        f = FakeFetcher(_release(content), chunk=256 * 1024)
        progress = []
        dest = tmp_path / "swim-worker-gui.new.exe"
        n = updater.download_and_verify(f"{BASE}/{ASSET}", dest, fetcher=f,
                                        on_progress=lambda d, t: progress.append((d, t)))
        assert n == len(content)
        assert dest.read_bytes() == content
        assert progress[-1] == (len(content), len(content))
        assert len(progress) >= 2
        # SHA256SUMS を先に取る (W3 で署名を検証してから本体を落とすため)
        assert f.requested[0].endswith("/SHA256SUMS")
        assert not list(tmp_path.glob("*.part"))

    def test_hash_mismatch_leaves_nothing(self, tmp_path):
        content = os.urandom(2 * 1024 * 1024)
        f = FakeFetcher(_release(content, digest="0" * 64))
        dest = tmp_path / "new.exe"
        with pytest.raises(Exception, match="SHA256"):
            updater.download_and_verify(f"{BASE}/{ASSET}", dest, fetcher=f)
        assert list(tmp_path.iterdir()) == []

    def test_missing_entry_fails_before_download(self, tmp_path):
        f = FakeFetcher({ASSET: b"x", "SHA256SUMS": b"abc  other\n"})
        with pytest.raises(Exception, match="エントリ"):
            updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe", fetcher=f)
        assert len(f.requested) == 1

    def test_http_error(self, tmp_path):
        content = os.urandom(2 * 1024 * 1024)
        f = FakeFetcher(_release(content), status={ASSET: 404})
        with pytest.raises(RuntimeError, match="404"):
            updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe", fetcher=f)
        assert list(tmp_path.iterdir()) == []

    def test_too_small_is_rejected(self, tmp_path):
        f = FakeFetcher(_release(b"tiny"))
        with pytest.raises(RuntimeError, match="サイズ"):
            updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe", fetcher=f)

    def test_too_large_is_cut(self, tmp_path, monkeypatch):
        monkeypatch.setattr(updater, "MAX_ASSET_SIZE", 3 * 1024 * 1024)
        content = os.urandom(4 * 1024 * 1024)
        f = FakeFetcher(_release(content), chunk=512 * 1024)
        with pytest.raises(RuntimeError, match="大きすぎ"):
            updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe", fetcher=f)
        assert list(tmp_path.iterdir()) == []

    def test_wall_clock_limit(self, tmp_path):
        content = os.urandom(2 * 1024 * 1024)
        f = FakeFetcher(_release(content), chunk=64 * 1024)
        t = iter(range(0, 10 ** 6, 100))
        with pytest.raises(RuntimeError, match="時間"):
            updater.download_and_verify(f"{BASE}/{ASSET}", tmp_path / "n.exe", fetcher=f,
                                        clock=lambda: next(t))


def test_curl_fetcher_streams_from_local_server(tmp_path):
    """curl_cffi の stream=True で実際に落とせる (ローカルの HTTP サーバー)"""
    pytest.importorskip("curl_cffi")
    import http.server
    import threading
    import functools
    content = os.urandom(2 * 1024 * 1024 + 3)
    srv_dir = tmp_path / "srv"
    srv_dir.mkdir()
    for name, data in _release(content).items():
        (srv_dir / name).write_bytes(data)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(srv_dir))
    handler.log_message = lambda *a, **k: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/{ASSET}"
        dest = tmp_path / "n.exe"
        progress = []
        updater.download_and_verify(url, dest, fetcher=updater.CurlFetcher(impersonate=None),
                                    on_progress=lambda d, t: progress.append((d, t)))
        assert dest.read_bytes() == content
        assert progress[-1] == (len(content), len(content))
    finally:
        httpd.shutdown()


class _FlakyRedis(_FakeRedis):
    def __init__(self, failures: int, exc=ConnectionError("down")):
        super().__init__()
        self.failures = failures
        self.fail_exc = exc
        self.pings = 0

    async def ping(self):
        self.pings += 1
        if self.pings <= self.failures:
            raise self.fail_exc
        return True


class TestWorkerRunnerRetry:
    def _runner(self, monkeypatch, fake, run=None, **kw):
        import swim_worker.redis_client as rc
        import swim_worker.consumer as consumer
        monkeypatch.setattr(rc, "create_redis_client", lambda s: fake)
        ran = []

        async def _run(self):
            ran.append(1)
            if run:
                await run(len(ran))
        monkeypatch.setattr(consumer.TaskConsumer, "run", _run)
        sleeps = []

        async def _sleep(d):
            sleeps.append(d)
        finished = []
        r = worker_runner.WorkerRunner(SETTINGS, sleep=_sleep,
                                       on_finished=lambda o, m: finished.append(o), **kw)
        return r, ran, sleeps, finished

    def test_retries_forever_with_backoff_capped_at_5_minutes(self, monkeypatch):
        fake = _FlakyRedis(failures=20)
        statuses = []
        r, ran, sleeps, finished = self._runner(monkeypatch, fake, on_status=statuses.append)
        _run_until_done(r)
        assert fake.pings == 21
        assert ran == [1]
        assert sleeps[:4] == [1, 2, 4, 8]
        assert max(sleeps) == 300
        assert all(b >= a for a, b in zip(sleeps, sleeps[1:]))
        assert any("再接続" in s or "再試行" in s for s in statuses)
        assert finished == [worker_runner.STOPPED]

    @pytest.mark.parametrize("exc", [redis.exceptions.AuthenticationError("WRONGPASS"),
                                     redis.exceptions.NoPermissionError("NOPERM")])
    def test_auth_errors_are_not_retried(self, monkeypatch, exc):
        import swim_worker.update_check as uc
        monkeypatch.setattr(uc, "check_update_without_redis", lambda cur, notify: None)
        fake = _FlakyRedis(failures=1, exc=exc)
        r, ran, sleeps, finished = self._runner(monkeypatch, fake)
        _run_until_done(r)
        assert sleeps == [] and ran == []
        assert finished == [worker_runner.AUTH_ERROR]

    def test_connection_lost_during_run_reconnects(self, monkeypatch):
        async def run(n):
            if n == 1:
                raise redis.exceptions.ConnectionError("lost")
        r, ran, sleeps, finished = self._runner(monkeypatch, _FlakyRedis(failures=0), run=run)
        _run_until_done(r)
        assert ran == [1, 1]
        assert sleeps == [1]
        assert finished == [worker_runner.STOPPED]

    def test_other_errors_stop(self, monkeypatch):
        async def run(n):
            raise ValueError("bug")
        r, ran, sleeps, finished = self._runner(monkeypatch, _FlakyRedis(failures=0), run=run)
        _run_until_done(r)
        assert ran == [1]
        assert finished == [worker_runner.ERROR]

    def test_stop_during_backoff(self, monkeypatch):
        import swim_worker.redis_client as rc
        monkeypatch.setattr(rc, "create_redis_client", lambda s: _FlakyRedis(failures=10 ** 6))
        finished = []
        r = worker_runner.WorkerRunner(SETTINGS, on_finished=lambda o, m: finished.append(o))
        r.start()
        time.sleep(0.3)
        r.request_stop()
        assert r.join(5)
        assert finished == [worker_runner.STOPPED]
