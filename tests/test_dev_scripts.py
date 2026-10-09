"""調査用スクリプト (scripts/dev/) の決まり: パスワードを引数で受けない・Cookie は一時ディレクトリ・出力は 0600"""
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEV = ROOT / "scripts" / "dev"
PW = "pass" + "word"
sys.path.insert(0, str(DEV))
import _common  # noqa: E402


def test_no_investigation_scripts_left_in_scripts_root():
    """pytest に拾われる名前 (test_*.py) を scripts/ の直下に置かない"""
    assert not list((ROOT / "scripts").glob("test_*.py"))
    assert not list(DEV.glob("test_*.py"))


@pytest.mark.parametrize("path", sorted(DEV.glob("*.py")), ids=lambda p: p.name)
def test_scripts_do_not_take_password_argument_or_use_tmp(path):
    text = path.read_text(encoding="utf-8")
    assert f"--{PW}" not in text
    assert "/tmp/" not in text
    assert "_auth_module.COOKIE_FILE" not in text


def test_probes_use_temp_cookie_file():
    for name in ("probe_swim_apis.py", "probe_pkg_browse.py", "probe_pkg_deep.py"):
        text = (DEV / name).read_text(encoding="utf-8")
        assert "cookie_file=COOKIE_PATH" in text, name
        assert "temp_cookie_file()" in text, name


def test_temp_cookie_file_is_removed():
    with _common.temp_cookie_file() as p:
        d = os.path.dirname(p)
        Path(p).write_text("{}")
        if os.name != "nt":
            assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert not os.path.exists(d)


@pytest.mark.skipif(os.name == "nt", reason="POSIX のパーミッション")
def test_write_private_json_is_0600(tmp_path):
    out = tmp_path / "capture.json"
    out.write_text("old")
    os.chmod(out, 0o644)
    _common.write_private_json(out, [{"a": 1}])
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert '"a": 1' in out.read_text()


def test_swim_credentials_from_env_and_getpass(monkeypatch):
    monkeypatch.setenv("SWIM_USERNAME", "u")
    monkeypatch.setenv("SWIM_" + PW.upper(), "s")
    assert _common.swim_credentials() == ("u", "s")
    monkeypatch.delenv("SWIM_" + PW.upper())
    monkeypatch.setattr(_common.getpass, "getpass", lambda prompt="": "typed")
    assert _common.swim_credentials("other") == ("other", "typed")


def test_docker_install_test_has_no_fixed_default_release():
    text = (ROOT / "scripts" / "test-install-docker.sh").read_text(encoding="utf-8")
    assert "v1.0.0-rc1" not in text
    assert "releases/latest" in text
    assert '"0.9.5"' not in text
