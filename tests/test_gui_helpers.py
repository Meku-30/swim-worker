"""gui_helpers (tkinter 非依存) のテスト"""
from pathlib import Path

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


class TestPyinstallerCleanEnv:
    def test_removes_pyi_vars_and_sets_reset(self):
        env = {"_PYI_ARCHIVE_FILE": "x", "_PYI_APPLICATION_HOME_DIR": "y",
               "_PYI_PARENT_PROCESS_LEVEL": "1", "_MEIPASS2": "z", "PATH": "/bin"}
        out = gui.pyinstaller_clean_env(env)
        assert "PATH" in out
        assert not any(k.startswith("_PYI_") for k in out)
        assert "_MEIPASS2" not in out
        assert out["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        assert env.get("_PYI_ARCHIVE_FILE") == "x"  # 入力は変更しない


class TestWindowsUpdateScript:
    def _script(self):
        base = Path(r"C:\Users\test\swim")
        return gui.build_windows_update_script(
            base=base, current_exe=base / "swim-worker-gui.exe",
            new_exe=base / "swim-worker-gui.new.exe", old_exe=base / "swim-worker-gui.exe.old",
            startup_ok=base / "data" / ".startup_ok",
            rollback_marker=base / "data" / ".update_rollback.json",
            log_path=base / "swim-worker-update.log", new_version="1.1.3")

    def test_fail_path_restarts_old_exe_and_writes_marker(self):
        s = self._script()
        fail = s[s.index(":fail"):]
        assert 'start "" /D' in fail and "swim-worker-gui.exe" in fail
        assert '"reason":"move_failed"' in fail
        assert ".update_rollback.json" in fail

    def test_rollback_path_still_restores_old_exe(self):
        s = self._script()
        rb = s[s.index(":rollback"):s.index(":fail")]
        assert "move /Y" in rb and ".exe.old" in rb


class TestMacosUpdateScript:
    def _script(self):
        base = Path("/Applications/swim")
        return gui.build_macos_update_script(
            current_exe=base / "swim-worker", new_exe=base / "swim-worker.new",
            old_exe=base / "swim-worker.old", startup_ok=base / "data" / ".startup_ok",
            rollback_marker=base / "data" / ".update_rollback.json",
            log_path=base / "swim-worker-update.log", new_version="1.1.3")

    def test_uses_pid_instead_of_pkill(self):
        s = self._script()
        assert "pkill" not in s
        assert "NEW_PID=$!" in s
        assert 'kill "$NEW_PID"' in s
