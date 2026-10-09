"""GUI の起動確認 (画面がある環境だけ。CI では Windows で走る)

GUI は tkinter の画面が要るため、ヘッドレスの Linux では skip する。
Redis・SWIM には接続しない (Worker スレッドの起動は差し替える)。
"""
import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture
def gui(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
        root.destroy()
    except tk.TclError:
        pytest.skip("画面が無い環境")
    import swim_worker.gui as g
    # リポジトリ直下 (古い .old の掃除・data/ のマーカー・ログ) を触らないよう、
    # 置き場所とカレントディレクトリを一時ディレクトリにする
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(g, "_get_base_dir", lambda: tmp_path)
    monkeypatch.setattr(g, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(g, "GUI_SETTINGS_PATH", tmp_path / "data" / "gui_settings.json")
    monkeypatch.setattr(g, "UPDATE_SNOOZE_PATH", tmp_path / "data" / "update_snooze.json")
    import logging

    def _drop_text_handlers():
        # WorkerGUI はログの出力先 (TextHandler) をルートのロガーに足す。前のテストの閉じた
        # 画面に書こうとすると例外になるので外す (実際の GUI はプロセスに 1 つだけ)
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, g.TextHandler):
                root.removeHandler(h)

    _drop_text_handlers()
    yield g
    _drop_text_handlers()


def _write_env(g, username: str) -> None:
    g.ENV_PATH.write_text(
        "REDIS_HOST=redis.example\n"
        f"REDIS_USERNAME={username}\n"
        "REDIS_PASSWORD=pw\n"
        "SWIM_USERNAME=u\nSWIM_PASSWORD=p\nWORKER_NAME=tester\n"
        "REDIS_PORT=6380\nAUTO_CONNECT=false\n",
        encoding="utf-8",
    )


def test_gui_loads_and_saves_redis_username(gui):
    _write_env(gui, "worker-tester")
    app = gui.WorkerGUI()
    try:
        assert app._entries["redis_username"].get() == "worker-tester"
        app._save_env()
        text = gui.ENV_PATH.read_text(encoding="utf-8")
        assert "REDIS_USERNAME=worker-tester" in text
        assert "REDIS_PASSWORD=pw" in text
    finally:
        app._root.destroy()


@pytest.mark.parametrize("username", ["", "worker-tester"])
def test_gui_start_accepts_empty_or_set_username(gui, monkeypatch, username):
    """ユーザー名は空でも起動できる (移行前の .env と互換)。値は Worker の設定に渡る"""
    _write_env(gui, username)
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **kw: errors.append(a))
    monkeypatch.setattr(gui.WorkerRunner, "start", lambda self: None)
    app = gui.WorkerGUI()
    try:
        app._on_start()
        assert errors == []
        assert app._worker_settings["redis_username"] == username
    finally:
        app._root.destroy()


def test_gui_checks_github_when_redis_auth_fails(gui, monkeypatch):
    """Redis の認証に失敗したら GitHub で最新版を確かめる (自動更新を止めない)"""
    import threading
    import redis.exceptions
    _write_env(gui, "worker-tester")
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **kw: errors.append(a))

    class _AuthFailRedis:
        async def ping(self):
            raise redis.exceptions.AuthenticationError("WRONGPASS invalid username-password pair")

        async def aclose(self):
            pass

    import swim_worker.redis_client as rc
    import swim_worker.update_check as uc
    monkeypatch.setattr(rc, "create_redis_client", lambda settings: _AuthFailRedis())
    calls = []
    monkeypatch.setattr(uc, "check_update_without_redis",
                        lambda current, notify: calls.append((current, notify)))
    app = gui.WorkerGUI()
    try:
        app._entries["worker_name"].insert(0, "tester")
        # 実際の GUI と同じく、メインループを回しながら Worker を別スレッドで動かす
        # (Worker スレッドからの画面更新はメインループが処理する)
        import time
        app._on_start()
        runner = app._runner
        deadline = time.monotonic() + 20

        def _poll():
            if not runner.is_alive() or time.monotonic() > deadline:
                app._root.quit()
            else:
                app._root.after(100, _poll)

        app._root.after(100, _poll)
        app._root.mainloop()
        assert not runner.is_alive(), "認証エラーの後に Worker が終わらない"
        assert errors == []
        assert len(calls) == 1
        assert calls[0][0] == gui.__version__
        assert calls[0][1] == app._on_update_detected
    finally:
        app._root.destroy()


# --- 分割 (W2-9) の前に今の振る舞いを押さえるテスト ---

def test_snooze_roundtrip(gui):
    app = gui.WorkerGUI()
    try:
        assert app._is_snoozed("9.9.9") is False
        app._set_snooze("9.9.9")
        assert app._is_snoozed("9.9.9") is True
        # 別の版を聞かれたら古い snooze は消える
        assert app._is_snoozed("9.9.10") is False
        assert not gui.UPDATE_SNOOZE_PATH.exists()
    finally:
        app._root.destroy()


def test_snooze_expired_is_cleared(gui):
    import json
    from datetime import datetime, timedelta, timezone
    gui.UPDATE_SNOOZE_PATH.parent.mkdir(parents=True, exist_ok=True)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    gui.UPDATE_SNOOZE_PATH.write_text(json.dumps({"version": "9.9.9", "until": past}))
    app = gui.WorkerGUI()
    try:
        assert app._is_snoozed("9.9.9") is False
        assert not gui.UPDATE_SNOOZE_PATH.exists()
    finally:
        app._root.destroy()


@pytest.mark.parametrize("platform,asset", [
    ("win32", "swim-worker-windows.exe"), ("darwin", "swim-worker-macos"), ("linux", None)])
def test_download_url(gui, monkeypatch, platform, asset):
    app = gui.WorkerGUI()
    try:
        monkeypatch.setattr(gui.sys, "platform", platform)
        url = app._get_download_url("1.2.3")
        if asset is None:
            assert url is None
        else:
            assert url == f"https://github.com/Meku-30/swim-worker/releases/download/v1.2.3/{asset}"
    finally:
        app._root.destroy()


def _pump(app):
    """メインループの代わりに、溜まった after を処理する"""
    import time
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        app._root.update()
        time.sleep(0.01)


@pytest.mark.parametrize("auto,snoozed,expected", [
    (False, False, "prompt"),
    (True, False, "countdown"),
    (False, True, None),
    (True, True, None),
])
def test_update_detected_branches(gui, monkeypatch, auto, snoozed, expected):
    import tkinter as tk
    app = gui.WorkerGUI()
    try:
        calls = []
        monkeypatch.setattr(app, "_prompt_update", lambda v: calls.append(("prompt", v)))
        monkeypatch.setattr(app, "_prompt_auto_update_countdown",
                            lambda v: calls.append(("countdown", v)))
        app._auto_update_var = tk.BooleanVar(master=app._root, value=auto)
        if snoozed:
            app._set_snooze("9.9.9")
        app._on_update_detected("9.9.9")
        _pump(app)
        assert app._pending_update_version == "9.9.9"
        assert calls == ([] if expected is None else [(expected, "9.9.9")])
        # 同じ版の 2 回目は確認を出さない
        app._on_update_detected("9.9.9")
        _pump(app)
        assert len(calls) == (0 if expected is None else 1)
    finally:
        app._root.destroy()


def test_env_roundtrip_keeps_values(gui):
    _write_env(gui, "worker-tester")
    app = gui.WorkerGUI()
    try:
        app._save_env()
    finally:
        app._root.destroy()
    app2 = gui.WorkerGUI()
    try:
        assert app2._entries["redis_host"].get() == "redis.example"
        assert app2._entries["redis_username"].get() == "worker-tester"
        assert app2._entries["swim_username"].get() == "u"
    finally:
        app2._root.destroy()


def test_autostart_plist_contents(gui, monkeypatch, tmp_path):
    import tkinter as tk
    app = gui.WorkerGUI()
    try:
        monkeypatch.setattr(gui.sys, "platform", "darwin")
        plist = tmp_path / "LaunchAgents" / "org.swim-worker.plist"
        plist.parent.mkdir()
        monkeypatch.setattr(app, "_get_startup_path", lambda: plist)
        app._autostart_var = tk.BooleanVar(master=app._root, value=True)
        app._toggle_autostart()
        text = plist.read_text(encoding="utf-8")
        assert "<string>org.swim-worker</string>" in text
        assert f"<string>{tmp_path}</string>" in text
        app._autostart_var.set(False)
        app._toggle_autostart()
        assert not plist.exists()
    finally:
        app._root.destroy()
