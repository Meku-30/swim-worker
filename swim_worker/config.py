"""Worker設定"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Redis
    redis_url: str
    redis_password: str
    redis_ca_cert: str = ""

    # SWIM認証
    swim_username: str
    swim_password: str

    # Worker
    worker_name: str
    heartbeat_interval: int = 30  # 秒

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
