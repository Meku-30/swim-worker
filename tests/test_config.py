"""Config テスト"""
import pytest
from pydantic import ValidationError

from swim_worker.config import Settings, WORKER_NAME_RE, WORKER_NAME_RULE_MESSAGE


def _settings_with_name(name: str) -> Settings:
    return Settings(_env_file=None, redis_host="h", redis_password="p",
                    swim_username="u", swim_password="p", worker_name=name)


@pytest.mark.parametrize("name", ["GCP-worker", "hyuga_main", "w.1", "a", "x" * 32])
def test_worker_name_accepts_ascii_names(name):
    assert _settings_with_name(name).worker_name == name


@pytest.mark.parametrize("name", ["Taro Yamada", "田中", "", "x" * 33, "a/b", "name\n"])
def test_worker_name_rejects_invalid_names(name):
    with pytest.raises(ValidationError) as ei:
        _settings_with_name(name)
    assert WORKER_NAME_RULE_MESSAGE in str(ei.value)


def test_worker_name_re_matches_rule_message_examples():
    assert WORKER_NAME_RE.fullmatch("abc-123_x.y")
    assert not WORKER_NAME_RE.fullmatch("a b")


class TestSettings:
    def test_loads_from_env(self, monkeypatch):
        monkeypatch.setenv("REDIS_HOST", "localhost")
        monkeypatch.setenv("REDIS_PORT", "6380")
        monkeypatch.setenv("REDIS_PASSWORD", "testpass")
        monkeypatch.setenv("SWIM_USERNAME", "user1")
        monkeypatch.setenv("SWIM_PASSWORD", "pass1")
        monkeypatch.setenv("WORKER_NAME", "test-worker")

        from swim_worker.config import Settings
        s = Settings()
        assert s.redis_host == "localhost"
        assert s.redis_port == 6380
        assert s.redis_password == "testpass"
        assert s.swim_username == "user1"
        assert s.worker_name == "test-worker"

    def test_defaults(self, monkeypatch):
        monkeypatch.setenv("REDIS_HOST", "localhost")
        monkeypatch.setenv("REDIS_PASSWORD", "p")
        monkeypatch.setenv("SWIM_USERNAME", "u")
        monkeypatch.setenv("SWIM_PASSWORD", "p")
        monkeypatch.setenv("WORKER_NAME", "w")

        from swim_worker.config import Settings
        s = Settings()
        assert s.heartbeat_interval == 30
        assert s.redis_ca_cert == ""
        assert s.task_hard_timeout == 300.0
        assert s.redis_socket_timeout == 30.0
        # blpop 窓は socket_timeout より短くないと、サーバーの nil 応答 (blpop_timeout + RTT)
        # より先にクライアント側 socket_timeout が発火して毎サイクル再接続になる
        assert s.redis_blpop_timeout == 20.0
        assert s.redis_blpop_timeout < s.redis_socket_timeout
