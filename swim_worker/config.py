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
    cookie_file: str = ""  # Cookie保存先（未設定時は環境に応じて自動決定）
    request_delay_median: float = 4.0  # リクエスト前遅延の中央値（秒、対数正規分布）
    request_delay_p99: float = 15.0   # リクエスト前遅延の99パーセンタイル（秒）
    request_delay_clip_min: float = 1.5  # リクエスト前遅延の下限（秒）
    request_delay_clip_max: float = 25.0  # リクエスト前遅延の上限（秒）

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
