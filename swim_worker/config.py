"""Worker設定"""
import re

from pydantic import field_validator
from pydantic_settings import BaseSettings

# Worker 名は Redis CLIENT SETNAME (空白・制御文字不可) とキー名に使うため ASCII に限定する
WORKER_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
WORKER_NAME_RULE_MESSAGE = (
    "Worker 名は半角英数字・ピリオド・アンダースコア・ハイフンのみ (1〜32 文字) です"
)


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
    # blpop 窓は socket_timeout より短くすること。同値だとサーバーの nil 応答
    # (blpop_timeout + RTT) より先にクライアント側 socket_timeout が発火し、
    # 毎サイクル TimeoutError → 再接続になる (redis-py>=6 は 3 回まで無言でリトライ
    # するため警告には稀にしか出ないが、接続は約 30 秒ごとに張り直されていた)。
    redis_blpop_timeout: float = 20.0   # consume_loopのblpopブロッキング窓(秒)

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @field_validator("worker_name")
    @classmethod
    def _validate_worker_name(cls, v: str) -> str:
        if not WORKER_NAME_RE.fullmatch(v or ""):
            raise ValueError(WORKER_NAME_RULE_MESSAGE)
        return v
