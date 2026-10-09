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

    import gc
    _drop_text_handlers()
    gc.collect()
    yield g
    _drop_text_handlers()
    # 壊した画面の Tk 変数 (StringVar 等) をメインスレッドで回収する。後のテストの
    # 別スレッドで GC されると、消えた Tcl インタプリタを触って落ちる (Illegal instruction)
    gc.collect()


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
        from dotenv import dotenv_values
        values = dotenv_values(gui.ENV_PATH, interpolate=False)
        assert values["REDIS_USERNAME"] == "worker-tester"
        assert values["REDIS_" + "PASSWORD"] == "pw"
    finally:
        app._root.destroy()


def test_gui_start_requires_redis_username(gui, monkeypatch):
    """Redis のユーザー名は必須 (default ユーザーはサーバー側で使えなくなった)"""
    _write_env(gui, "")
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **kw: errors.append(a))
    started = []
    monkeypatch.setattr(gui.WorkerRunner, "start", lambda self: started.append(1))
    app = gui.WorkerGUI()
    try:
        app._entries["worker_name"].insert(0, "tester")
        app._on_start()
        assert started == []
        assert len(errors) == 1 and "Redis ユーザー名" in errors[0][1]
    finally:
        app._root.destroy()


def test_gui_start_passes_username(gui, monkeypatch):
    _write_env(gui, "worker-tester")
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **kw: errors.append(a))
    monkeypatch.setattr(gui.WorkerRunner, "start", lambda self: None)
    app = gui.WorkerGUI()
    try:
        app._entries["worker_name"].insert(0, "tester")
        app._on_start()
        assert errors == []
        assert app._worker_settings["redis_username"] == "worker-tester"
        assert app._worker_running is True
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


def test_passwords_are_not_stripped_when_starting(gui, monkeypatch):
    _write_env(gui, "worker-tester")
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **kw: None)
    monkeypatch.setattr(gui.WorkerRunner, "start", lambda self: None)
    app = gui.WorkerGUI()
    try:
        app._entries["worker_name"].insert(0, "tester")
        app._entries["swim_password"].delete(0, "end")
        app._entries["swim_password"].insert(0, " p a ss ")
        app._on_start()
        assert app._worker_settings["swim_password"] == " p a ss "
        app2_fields, _ = app._store.load()
        assert app2_fields["swim_password"] == " p a ss "
    finally:
        app._root.destroy()


# --- スレッド (W2-3)・停止 (W2-4)・終了時の UI (W2-6) ---

def _forbid_tk_from_other_threads(app, monkeypatch):
    """メインスレッド以外から Tk (after・変数の get/set) を触ったら記録する"""
    import threading
    main = threading.get_ident()
    bad = []
    orig_after = app._root.after

    def after(*a, **kw):
        if threading.get_ident() != main:
            bad.append(("after", a))
        return orig_after(*a, **kw)
    monkeypatch.setattr(app._root, "after", after)
    for var in (app._status_var, app._autoconnect_var, getattr(app, "_auto_update_var", None)):
        if var is None:
            continue
        for name in ("get", "set"):
            orig = getattr(var, name)

            def wrapped(*a, _orig=orig, _name=name, **kw):
                if threading.get_ident() != main:
                    bad.append((_name, a))
                return _orig(*a, **kw)
            monkeypatch.setattr(var, name, wrapped)
    return bad


def _in_thread(fn, *args):
    import threading
    t = threading.Thread(target=fn, args=args)
    t.start()
    t.join(5)


def test_worker_callbacks_do_not_touch_tk_from_other_threads(gui, monkeypatch):
    import logging
    import tkinter as tk
    app = gui.WorkerGUI()
    try:
        app._auto_update_var = tk.BooleanVar(master=app._root, value=False)
        prompts = []
        monkeypatch.setattr(app, "_prompt_update", lambda v: prompts.append(v))
        app._worker_running = True
        bad = _forbid_tk_from_other_threads(app, monkeypatch)
        _in_thread(app._on_worker_status, "● 接続中 (タスク待ち)")
        _in_thread(app._on_task_state_changed, "processing", "collect_notams")
        _in_thread(app._on_update_detected, "9.9.9")
        _in_thread(logging.info, "スレッドからのログ")
        assert bad == []
        app._drain_ui_queue()
        assert app._status_var.get() == "● 実行中: NOTAM収集"
        assert prompts == ["9.9.9"]
        assert "スレッドからのログ" in app._log_text.get("1.0", "end")
    finally:
        app._root.destroy()


@pytest.mark.parametrize("outcome,status_part,tray", [
    ("error", "エラー", "red"),
    ("auth_error", "Redis 認証エラー", "red"),
    ("duplicate", "重複起動", "red"),
    ("stopped", "停止中", "gray"),
])
def test_worker_finished_resets_ui(gui, monkeypatch, outcome, status_part, tray):
    _write_env(gui, "worker-tester")
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **kw: None)
    monkeypatch.setattr(gui.WorkerRunner, "start", lambda self: None)
    app = gui.WorkerGUI()
    try:
        app._entries["worker_name"].insert(0, "tester")
        app._on_start()
        assert app._worker_running and app._tray_color == "green"
        _in_thread(app._on_worker_finished, outcome, "msg")
        app._drain_ui_queue()
        assert app._worker_running is False
        assert status_part in app._status_var.get()
        assert app._tray_color == tray
        assert str(app._start_btn.cget("state")) == "normal"
        assert str(app._stop_btn.cget("state")) == "disabled"
        assert all(str(e.cget("state")) == "normal" for e in app._entries.values())
    finally:
        app._root.destroy()


class _FakeRunner:
    def __init__(self):
        import threading
        self.calls = []
        self.alive = True
        self.main = threading.get_ident()

    def is_alive(self):
        return self.alive

    def request_stop(self, graceful=True):
        self.calls.append(("request_stop", graceful))

    def stop_and_join(self, graceful_timeout=30.0, hard_timeout=10.0):
        import threading
        self.calls.append(("stop_and_join", threading.get_ident() != self.main))
        self.alive = False
        return True


def test_force_quit_waits_for_worker_outside_ui_thread(gui):
    import time
    app = gui.WorkerGUI()
    runner = _FakeRunner()
    app._runner = runner
    app._worker_running = True
    app._force_quit()
    # まだ壊していない (Worker の停止を待つ)
    assert app._root.winfo_exists()
    deadline = time.monotonic() + 5
    while not app._closed and time.monotonic() < deadline:
        app._root.update()
        time.sleep(0.01)
    assert app._closed
    assert ("stop_and_join", True) in runner.calls


def test_stop_button_requests_graceful_stop(gui):
    app = gui.WorkerGUI()
    try:
        runner = _FakeRunner()
        app._runner = runner
        app._set_ui_running(True)
        app._on_stop()
        assert runner.calls == [("request_stop", True)]
        assert "停止処理中" in app._status_var.get()
        assert str(app._stop_btn.cget("state")) == "disabled"
    finally:
        app._root.destroy()


def test_update_stops_worker_with_join_outside_ui_thread(gui, monkeypatch):
    app = gui.WorkerGUI()
    try:
        runner = _FakeRunner()
        app._runner = runner
        app._set_ui_running(True)
        _in_thread(app._stop_worker_for_update)
        assert ("stop_and_join", True) in runner.calls
    finally:
        app._root.destroy()
