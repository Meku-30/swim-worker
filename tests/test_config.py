"""Config テスト"""
import pytest


class TestSettings:
    def test_loads_from_env(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "rediss://localhost:6380")
        monkeypatch.setenv("REDIS_PASSWORD", "testpass")
        monkeypatch.setenv("SWIM_USERNAME", "user1")
        monkeypatch.setenv("SWIM_PASSWORD", "pass1")
        monkeypatch.setenv("WORKER_NAME", "test-worker")

        from swim_worker.config import Settings
        s = Settings()
        assert s.redis_url == "rediss://localhost:6380"
        assert s.redis_password == "testpass"
        assert s.swim_username == "user1"
        assert s.worker_name == "test-worker"

    def test_defaults(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "rediss://localhost:6380")
        monkeypatch.setenv("REDIS_PASSWORD", "p")
        monkeypatch.setenv("SWIM_USERNAME", "u")
        monkeypatch.setenv("SWIM_PASSWORD", "p")
        monkeypatch.setenv("WORKER_NAME", "w")

        from swim_worker.config import Settings
        s = Settings()
        assert s.heartbeat_interval == 30
        assert s.redis_ca_cert == ""
