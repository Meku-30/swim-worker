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
    task_hard_timeout: float = 300.0  # 1タスクの強制タイムアウト（秒、consume_loop自己回復用）
    # redis-py非同期クライアントの累積タイムアウト計算バグ(redis/redis-py#3454, 未修正)への緩和策。
    # 明示的にsocket_timeoutを設定しないと、TLS越し・レイテンシのある経路でblpopの
    # 正常な待機ですら「Timeout reading from ...」として接続切断される頻度が上がる。
    redis_socket_timeout: float = 30.0  # Redisクライアントのsocket_timeout(秒)
    redis_blpop_timeout: float = 30.0   # consume_loopのblpopブロッキング窓(秒)。長くして問題に当たる頻度を下げる

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
