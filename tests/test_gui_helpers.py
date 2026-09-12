"""gui_helpers (tkinter 非依存) のテスト"""
from swim_worker import gui_helpers as gui


class TestNextRollbackState:
    def test_first_rollback_counts_but_keeps_auto_update(self):
        new, disable = gui.next_rollback_state({"auto_update": True}, "1.1.3")
        assert new["rollback_count"] == {"1.1.3": 1}
        assert new["auto_update"] is True
        assert disable is False

    def test_second_rollback_of_same_version_disables_auto_update(self):
        new, disable = gui.next_rollback_state(
            {"auto_update": True, "rollback_count": {"1.1.3": 1}}, "1.1.3")
        assert new["rollback_count"] == {"1.1.3": 2}
        assert new["auto_update"] is False
        assert disable is True

    def test_other_version_rollback_does_not_accumulate(self):
        new, disable = gui.next_rollback_state(
            {"auto_update": True, "rollback_count": {"1.1.3": 1}}, "1.1.4")
        assert new["rollback_count"] == {"1.1.3": 1, "1.1.4": 1}
        assert disable is False


class TestStartupMarker:
    def test_write_startup_marker_writes_version(self, tmp_path, monkeypatch):
        from swim_worker import consumer, __version__
        monkeypatch.setattr(consumer, "_startup_marker_path", lambda: tmp_path / "data" / ".startup_ok")
        gui.write_startup_marker()
        assert (tmp_path / "data" / ".startup_ok").read_text(encoding="utf-8") == __version__
