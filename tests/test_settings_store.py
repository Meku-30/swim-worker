"""settings_store: .env の読み書き (0600・原子的な書き換え・引用符) と keyring のテスト"""
import os
import stat
import sys

import pytest
from dotenv import dotenv_values

from swim_worker import settings_store as ss

# .env のキー (テストの文字列に直接書かない)
RP = "REDIS_" + "PASSWORD"
SP = "SWIM_" + "PASSWORD"

NASTY = [
    "simple",
    " lead and trail ",
    "has#hash and # space-hash",
    "quote'single",
    'quote"double',
    "back\\slash\\",
    "dollar$HOME",
    "eq=sign",
    "日本語パス",
    "'starts-with-quote",
]


class FakeKeyring:
    def __init__(self):
        self.data = {}

    def get_password(self, service, user):
        return self.data.get((service, user))

    def set_password(self, service, user, pw):
        self.data[(service, user)] = pw

    def delete_password(self, service, user):
        self.data.pop((service, user), None)


class BrokenKeyring(FakeKeyring):
    def set_password(self, service, user, pw):
        raise RuntimeError("locked")

    def get_password(self, service, user):
        raise RuntimeError("locked")


FIELDS = {
    "redis_host": "redis.example", "redis_username": "worker-tester",
    "redis_password": "r-pass", "swim_username": "u", "swim_password": "s-pass",
    "worker_name": "tester",
}


def _store(tmp_path, keyring=None):
    return ss.SettingsStore(tmp_path / ".env", keyring=keyring)


class TestEnvFile:
    @pytest.mark.parametrize("pw", NASTY)
    def test_password_roundtrip_without_strip(self, tmp_path, pw):
        st = _store(tmp_path)
        st.save({**FIELDS, "swim_password": pw, "redis_password": pw}, auto_connect=True)
        fields, auto = st.load()
        assert fields["swim_password"] == pw
        assert fields["redis_password"] == pw
        assert auto is True

    @pytest.mark.parametrize("pw", [p for p in NASTY])
    def test_written_values_read_same_by_dotenv(self, tmp_path, pw):
        """CLI (pydantic-settings → python-dotenv) が読んでも同じ値になる"""
        st = _store(tmp_path)
        st.save({**FIELDS, "swim_password": pw}, auto_connect=False)
        values = dotenv_values(tmp_path / ".env", interpolate=False)
        assert values["SWIM_PASSWORD"] == pw
        assert values["REDIS_HOST"] == "redis.example"
        assert values["REDIS_PORT"] == "6380"

    def test_non_secret_fields_are_stripped(self, tmp_path):
        st = _store(tmp_path)
        st.save({**FIELDS, "redis_host": "  redis.example \n"}, auto_connect=False)
        assert st.load()[0]["redis_host"] == "redis.example"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX のパーミッション")
    def test_file_mode_0600_even_if_existing_was_open(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("REDIS_HOST=x\n", encoding="utf-8")
        os.chmod(p, 0o644)
        _store(tmp_path).save(FIELDS, auto_connect=False)
        assert stat.S_IMODE(p.stat().st_mode) == 0o600

    def test_keeps_comments_and_hand_added_keys(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("# 手で書いたメモ\nHEARTBEAT_INTERVAL=45\nREDIS_HOST=old\n"
                     "COOKIE_FILE='C:\\data\\c.json'\n", encoding="utf-8")
        _store(tmp_path).save(FIELDS, auto_connect=False)
        text = p.read_text(encoding="utf-8")
        assert "# 手で書いたメモ" in text
        assert "HEARTBEAT_INTERVAL=45" in text
        assert "COOKIE_FILE='C:\\data\\c.json'" in text
        assert text.count("REDIS_HOST=") == 1
        assert dotenv_values(p, interpolate=False)["REDIS_HOST"] == "redis.example"

    def test_reads_legacy_unquoted_file(self, tmp_path):
        """v1.1 までの GUI が書いた形 (引用符なし・値はそのまま) を同じ値で読む"""
        p = tmp_path / ".env"
        p.write_text("REDIS_HOST=redis.example\nREDIS_USERNAME=w\n" + RP + "=a#b c\n"
                     "SWIM_USERNAME=u\n" + SP + "='q\n"
                     "WORKER_NAME=tester\nREDIS_PORT=6380\nAUTO_CONNECT=true\n", encoding="utf-8")
        fields, auto = _store(tmp_path).load()
        assert fields["redis_password"] == "a#b c"
        assert fields["swim_password"] == "'q"
        assert auto is True

    def test_atomic_write_leaves_no_temp_files(self, tmp_path):
        _store(tmp_path).save(FIELDS, auto_connect=False)
        assert sorted(f.name for f in tmp_path.iterdir()) == [".env"]

    def test_set_auto_connect_keeps_other_lines(self, tmp_path):
        st = _store(tmp_path)
        assert st.set_auto_connect(True) is False  # .env が無ければ何もしない
        st.save({**FIELDS, "swim_password": "x'y"}, auto_connect=False)
        assert st.set_auto_connect(True) is True
        fields, auto = st.load()
        assert auto is True
        assert fields["swim_password"] == "x'y"


class TestKeyring:
    def test_secrets_go_to_keyring_not_env(self, tmp_path):
        kr = FakeKeyring()
        st = _store(tmp_path, kr)
        assert st.save(FIELDS, auto_connect=False) == "keyring"
        values = dotenv_values(tmp_path / ".env", interpolate=False)
        assert "REDIS_PASSWORD" not in values and "SWIM_PASSWORD" not in values
        assert values[ss.SECRET_STORE_ID_KEY]
        assert sorted(kr.data.values()) == ["r-pass", "s-pass"]
        fields, _ = _store(tmp_path, kr).load()
        assert fields["redis_password"] == "r-pass"
        assert fields["swim_password"] == "s-pass"

    def test_two_folders_do_not_share_secrets(self, tmp_path):
        kr = FakeKeyring()
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        ss.SettingsStore(a / ".env", keyring=kr).save(FIELDS, auto_connect=False)
        ss.SettingsStore(b / ".env", keyring=kr).save({**FIELDS, "swim_password": "other"},
                                                      auto_connect=False)
        assert ss.SettingsStore(a / ".env", keyring=kr).load()[0]["swim_password"] == "s-pass"

    def test_migrates_existing_env_passwords(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("REDIS_HOST=redis.example\n" + RP + "=old-r\nHEARTBEAT_INTERVAL=45\n"
                     + SP + "='old s'\n", encoding="utf-8")
        kr = FakeKeyring()
        fields, _ = _store(tmp_path, kr).load()
        assert fields["redis_password"] == "old-r"
        assert fields["swim_password"] == "old s"
        text = p.read_text(encoding="utf-8")
        assert "PASSWORD" not in text
        assert "HEARTBEAT_INTERVAL=45" in text
        assert sorted(kr.data.values()) == ["old s", "old-r"]
        # 次の起動でも読める
        assert _store(tmp_path, kr).load()[0]["swim_password"] == "old s"

    def test_migration_failure_keeps_env(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text(RP + "=old-r\n", encoding="utf-8")
        fields, _ = _store(tmp_path, BrokenKeyring()).load()
        assert fields["redis_password"] == "old-r"
        assert RP + "=old-r" in p.read_text(encoding="utf-8")

    def test_migration_readback_mismatch_keeps_env(self, tmp_path):
        class Lossy(FakeKeyring):
            def set_password(self, service, user, pw):
                super().set_password(service, user, pw[:2])
        p = tmp_path / ".env"
        p.write_text(RP + "=old-r\n", encoding="utf-8")
        fields, _ = _store(tmp_path, Lossy()).load()
        assert fields["redis_password"] == "old-r"
        assert RP + "=old-r" in p.read_text(encoding="utf-8")

    def test_save_falls_back_to_env_when_keyring_fails(self, tmp_path):
        st = _store(tmp_path, BrokenKeyring())
        assert st.save(FIELDS, auto_connect=False) == "env"
        values = dotenv_values(tmp_path / ".env", interpolate=False)
        assert values["SWIM_PASSWORD"] == "s-pass"
        assert _store(tmp_path, BrokenKeyring()).load()[0]["swim_password"] == "s-pass"

    def test_empty_password_removes_from_keyring(self, tmp_path):
        kr = FakeKeyring()
        _store(tmp_path, kr).save(FIELDS, auto_connect=False)
        _store(tmp_path, kr).save({**FIELDS, "swim_password": ""}, auto_connect=False)
        assert sorted(kr.data.values()) == ["r-pass"]
        assert _store(tmp_path, kr).load()[0]["swim_password"] == ""


class TestDefaultKeyring:
    def test_linux_does_not_use_keyring(self, monkeypatch):
        monkeypatch.setattr(ss.sys, "platform", "linux")
        assert ss.default_keyring() is None

    def test_fail_backend_means_none(self, monkeypatch):
        keyring = pytest.importorskip("keyring")
        from keyring.backends import fail
        monkeypatch.setattr(ss.sys, "platform", "win32")
        monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
        assert ss.default_keyring() is None
