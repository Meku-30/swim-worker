"""single_instance (同一マシン上の多重起動防止) のテスト"""
import os
import stat

import pytest

from swim_worker.single_instance import LocalInstanceLock, AlreadyRunning


def test_frozen_lock_path_is_always_under_data_dir(tmp_path, monkeypatch):
    import sys
    from swim_worker import single_instance
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "swim-worker.exe"))
    first = single_instance.get_lock_path()
    assert first == tmp_path / "data" / "swim-worker.lock"
    assert single_instance.get_lock_path() == first

    lock = LocalInstanceLock(first)
    lock.acquire()
    try:
        assert (tmp_path / "data").is_dir()  # acquire() 時に作られる
    finally:
        lock.release()


def test_acquire_raises_oserror_when_dir_not_writable(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root では書込不可ディレクトリを作れない")
    locked_dir = tmp_path / "ro"
    locked_dir.mkdir()
    locked_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        lock = LocalInstanceLock(locked_dir / "swim-worker.lock")
        with pytest.raises(OSError) as ei:
            lock.acquire()
        assert not isinstance(ei.value, AlreadyRunning)
    finally:
        locked_dir.chmod(stat.S_IRWXU)
