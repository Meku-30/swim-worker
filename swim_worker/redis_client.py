"""Redis 非同期クライアント生成 (CLI / GUI 共通)"""
import redis.asyncio as aioredis

from swim_worker.certs import get_ca_cert_path
from swim_worker.config import Settings


def create_redis_client(settings: Settings) -> aioredis.Redis:
    """Redis 非同期クライアントを生成する（埋め込みCA証明書を使用）。

    client_name を渡すことで redis-py が接続確立のたびに CLIENT SETNAME を送る。
    実行時に 1 回 client_setname() する方式だとプール内の 1 本にしか名前が付かず、
    タイムアウト等で再接続した時点で名前が消え、Coordinator が CLIENT LIST から
    Worker の接続元 IP を取れなくなる (ダッシュボードの IP/状態テーブルから消える)。

    CLI (__main__) と GUI (gui.py) の両方がここを使うこと。個別に aioredis.Redis を
    組み立てると設定漏れが再発する。
    """
    ca_cert = settings.redis_ca_cert if settings.redis_ca_cert else get_ca_cert_path()
    return aioredis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        ssl=True,
        ssl_ca_certs=ca_cert,
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout,
        client_name=settings.worker_name,
    )
