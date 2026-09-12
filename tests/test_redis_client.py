"""Redis クライアント生成 (CLI / GUI 共通) のテスト"""
from unittest.mock import patch, MagicMock

from swim_worker.config import Settings


def _settings(monkeypatch) -> Settings:
    monkeypatch.setenv("REDIS_HOST", "redis.example")
    monkeypatch.setenv("REDIS_PASSWORD", "x")
    monkeypatch.setenv("SWIM_USERNAME", "u")
    monkeypatch.setenv("SWIM_PASSWORD", "p")
    monkeypatch.setenv("WORKER_NAME", "test-worker")
    monkeypatch.setenv("REDIS_CA_CERT", "/tmp/ca.crt")
    return Settings(_env_file=None)


class TestCreateRedisClient:
    def test_passes_worker_name_as_client_name(self, monkeypatch):
        """client_name= を渡すと redis-py が接続 (再接続含む) のたびに CLIENT SETNAME を送る。

        Coordinator は Redis CLIENT LIST の name= で Worker の接続元 IP を取得する。
        実行時に 1 回 client_setname() する方式だと再接続後に名前が消えるため、
        接続レベルで名前を付ける。
        """
        from swim_worker.redis_client import create_redis_client
        settings = _settings(monkeypatch)
        with patch("swim_worker.redis_client.aioredis.Redis") as mock_cls:
            mock_cls.return_value = MagicMock()
            create_redis_client(settings)
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["client_name"] == "test-worker"
        assert kwargs["host"] == "redis.example"
        assert kwargs["socket_timeout"] == settings.redis_socket_timeout
        assert kwargs["ssl"] is True
        assert kwargs["ssl_ca_certs"] == "/tmp/ca.crt"

    def test_uses_embedded_ca_data_when_no_external_cert(self, monkeypatch):
        from swim_worker.redis_client import create_redis_client
        from swim_worker.certs import CA_CERT_PEM
        settings = _settings(monkeypatch)
        settings = settings.model_copy(update={"redis_ca_cert": ""})
        with patch("swim_worker.redis_client.aioredis.Redis") as mock_cls:
            create_redis_client(settings)
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["ssl_ca_data"] == CA_CERT_PEM
        assert "ssl_ca_certs" not in kwargs

    def test_uses_external_cert_path_when_configured(self, monkeypatch):
        from swim_worker.redis_client import create_redis_client
        settings = _settings(monkeypatch)  # REDIS_CA_CERT=/tmp/ca.crt を設定している
        with patch("swim_worker.redis_client.aioredis.Redis") as mock_cls:
            create_redis_client(settings)
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["ssl_ca_certs"] == "/tmp/ca.crt"
        assert "ssl_ca_data" not in kwargs
