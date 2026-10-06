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
    monkeypatch.setattr(g, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(g, "GUI_SETTINGS_PATH", tmp_path / "data" / "gui_settings.json")
    monkeypatch.setattr(g, "UPDATE_SNOOZE_PATH", tmp_path / "data" / "update_snooze.json")
    return g


def _write_env(g, username: str) -> None:
    g.ENV_PATH.write_text(
        "REDIS_HOST=redis.example\n"
        f"REDIS_USERNAME={username}\n"
        "REDIS_PASSWORD=pw\n"
        "SWIM_USERNAME=u\nSWIM_PASSWORD=p\nWORKER_NAME=tester\n"
        "REDIS_PORT=6380\nAUTO_CONNECT=false\n",
        encoding="utf-8",
    )


class _NoThread:
    """Worker スレッドを起動しない代わり"""
    def __init__(self, *a, **kw):
        pass

    def start(self):
        pass

    def is_alive(self):
        return False


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
    monkeypatch.setattr(gui.threading, "Thread", _NoThread)
    app = gui.WorkerGUI()
    try:
        app._on_start()
        assert errors == []
        assert app._worker_settings["redis_username"] == username
    finally:
        app._root.destroy()
