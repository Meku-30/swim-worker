"""single_instance (同一マシン上の多重起動防止) のテスト"""


def test_frozen_lock_path_is_always_under_data_dir(tmp_path, monkeypatch):
    import sys
    from swim_worker import single_instance
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "swim-worker.exe"))
    first = single_instance.get_lock_path()
    assert first == tmp_path / "data" / "swim-worker.lock"
    assert (tmp_path / "data").is_dir()  # 無ければ作る
    assert single_instance.get_lock_path() == first
