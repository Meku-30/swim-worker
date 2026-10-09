"""paths (ファイルの置き場所の決め方) のテスト"""
import sys

from swim_worker import paths


def test_frozen_uses_exe_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "swim-worker.exe"))
    assert paths.base_dir() == tmp_path
    assert paths.data_dir() == tmp_path / "data"
    assert paths.startup_marker_path() == tmp_path / "data" / ".startup_ok"
    assert paths.last_instance_token_path() == tmp_path / "data" / ".last_instance_token"
    assert paths.cookie_file_path() == tmp_path / "data" / ".swim_cookies.json"
    assert paths.lock_path() == tmp_path / "data" / "swim-worker.lock"


def test_not_frozen_uses_cwd(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.chdir(tmp_path)
    assert paths.base_dir() == tmp_path
    assert paths.data_dir() == tmp_path / "data"
    assert paths.cookie_file_path() == tmp_path / "data" / ".swim_cookies.json"
    # CLI/Docker/開発時のロックは従来どおり cwd 直下
    assert paths.lock_path() == tmp_path / "swim-worker.lock"


def test_cookie_override_wins(tmp_path):
    assert paths.cookie_file_path(str(tmp_path / "c.json")) == tmp_path / "c.json"


def test_paths_do_not_create_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "swim-worker"))
    paths.startup_marker_path()
    paths.lock_path()
    assert not (tmp_path / "data").exists()


def test_consumer_and_auth_use_paths(tmp_path, monkeypatch):
    """パスの決め方が 1 か所 (paths.py) に集約されている"""
    from swim_worker import auth, consumer, single_instance
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "swim-worker"))
    assert consumer._startup_marker_path() == paths.startup_marker_path()
    assert consumer._last_instance_token_path() == paths.last_instance_token_path()
    assert single_instance.get_lock_path() == paths.lock_path()
    client = auth.SwimClient(username="u", password="p")
    assert client._cookie_file == str(paths.cookie_file_path())
