"""Worker設定"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Redis
    redis_host: str
    redis_port: int = 6380
    redis_password: str
    redis_ca_cert: str = ""

    # SWIM認証
    swim_username: str
    swim_password: str

    # Worker
    worker_name: str
    heartbeat_interval: int = 30  # 秒
    request_delay_min: float = 2.0  # SWIM APIリクエスト前の最小遅延（秒）
    request_delay_max: float = 8.0  # SWIM APIリクエスト前の最大遅延（秒）

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
