"""Redis タスクコンシューマー

自分専用のキュー (tasks:{worker_name}) を監視し、
タスクを取得してSWIM APIを実行、結果をRedisに返す。
"""
import asyncio
import zstandard as zstd
import json
import logging
import math
import random
import uuid
from datetime import datetime, timezone

import redis.exceptions

from swim_worker import __version__, parsers
from swim_worker.auth import SwimClient

logger = logging.getLogger(__name__)

RESULT_TTL = 3600  # 結果の有効期限（秒）
HEARTBEAT_TTL_MULTIPLIER = 3

# Coordinator への結果送信を zstd で圧縮 (gzip より小さく速い)。
# Coordinator 側は zstd/gzip/生JSON のいずれも解凍可能 (後方互換)。
# level=6: gzip level 9 デフォルトに対して -5% 程度。
# L3 だと実測で gzip L9 より僅かに悪化、L6 で逆転 (実測サンプル48件、pkg/pirep)。
_zstd_compressor = zstd.ZstdCompressor(level=6)


def _encode_result(result: dict) -> bytes:
    """result を最小サイズの JSON にして zstd 圧縮する"""
    payload = json.dumps(result, separators=(",", ":")).encode()
    return _zstd_compressor.compress(payload)


class DuplicateWorkerError(RuntimeError):
    """同一の worker_name で別プロセスが既に稼働していることを示す例外"""
    pass


class TaskConsumer:
    def __init__(self, redis_client, swim_client: SwimClient, worker_name: str,
                 heartbeat_interval: int = 30,
                 request_delay_median: float = 4.0,
                 request_delay_p99: float = 15.0,
                 request_delay_clip_min: float = 1.5,
                 request_delay_clip_max: float = 25.0,
                 on_update_available=None,
                 on_task_state=None) -> None:
        self._redis = redis_client
        self._swim = swim_client
        self._worker_name = worker_name
        self._heartbeat_interval = heartbeat_interval
        # 対数正規分布パラメータ（人間のブラウジング間隔を再現）
        self._delay_mu = math.log(request_delay_median)
        self._delay_sigma = (math.log(request_delay_p99) - self._delay_mu) / 2.326
        self._delay_clip_min = request_delay_clip_min
        self._delay_clip_max = request_delay_clip_max
        self._running = False
        self._tasks: list[asyncio.Task] = []
        # 新バージョン検知時に呼ばれるコールバック (GUI連携用)
        # シグネチャ: callback(latest_version: str) -> None
        self._on_update_available = on_update_available
        # タスク実行状態変化コールバック (GUIステータス表示更新用)
        # シグネチャ: callback(state: str, job_type: str = "", total: int = 0, errors: int = 0)
        #   state: "processing" | "idle"
        self._on_task_state = on_task_state
        self._task_total = 0
        self._task_errors = 0
        # 重複起動検知用のインスタンストークン (Redis lock の所有者識別に使用)
        self._instance_token = str(uuid.uuid4())
        # 定期バージョンチェック用（10分間隔 = ハートビート20回に1回）
        self._version_check_counter = 0
        self._VERSION_CHECK_INTERVAL = 20
        self._notified_version: str | None = None

    async def register(self) -> None:
        """Worker を pending リストに登録（承認済みならスキップ）"""
        is_approved = await self._redis.sismember("workers:approved", self._worker_name)
        if is_approved:
            logger.info("Worker '%s' は承認済み（再登録スキップ）", self._worker_name)
            return
        await self._redis.sadd("workers:pending", self._worker_name)
        logger.info("Worker '%s' を登録しました (pending)", self._worker_name)

    async def _run_capability_test(self, task_id: str, params: dict) -> None:
        """capability_test ジョブ: 複数のテストリクエストを実行して結果を返す。

        params: {"tests": [{"job_type": str, "url": str, "body": dict}, ...]}
        返却: {job_type: {"ok": bool, "error": str | None}, ...}
        """
        tests = params.get("tests") or []
        results: dict[str, dict] = {}
        for test in tests:
            jt = test.get("job_type", "")
            url = test.get("url", "")
            body = test.get("body") or {}
            try:
                # 各テスト間に短い遅延 (一気に叩かない)
                await asyncio.sleep(random.uniform(1.0, 3.0))
                await self._swim.execute_api(url, body)
                results[jt] = {"ok": True, "error": None}
                logger.info("capability OK: %s", jt)
            except Exception as e:
                results[jt] = {"ok": False, "error": str(e)[:300]}
                logger.warning("capability NG: %s — %s", jt, e)
        result = {
            "task_id": task_id,
            "worker_name": self._worker_name,
            "status": "success",
            "data": {"capabilities": results},
            "error": None,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._redis.setex(f"results:{task_id}", RESULT_TTL, _encode_result(result))
        logger.info("capability_test 完了: %s (ok=%d, ng=%d)",
            task_id[:8],
            sum(1 for r in results.values() if r["ok"]),
            sum(1 for r in results.values() if not r["ok"]),
        )

    async def report_version(self) -> None:
        """自身のバージョンと OS 情報を Redis に保存する。

        - worker_versions: バージョン文字列のみ
        - worker_platforms: "OS 種別 + バージョン + アーキ" 形式 (ダッシュボード表示用)
        """
        try:
            await self._redis.hset("worker_versions", self._worker_name, __version__)
            import platform as _platform
            osname = _platform.system()
            release = _platform.release()
            machine = _platform.machine()

            # Windows 11 は platform.release() が "10" を返す既知のバグ。
            # ビルド番号 (>= 22000 で Win11) で判別して補正する。
            if osname == "Windows":
                try:
                    build = int(_platform.version().split(".")[2])
                    if build >= 22000:
                        release = "11"
                except (ValueError, IndexError):
                    pass

            # アーキ表記を OS 間で統一:
            #   AMD64 (Windows) / x86_64 (Linux/Mac) は同じ 64bit x86 → "x86_64"
            #   aarch64 (Linux) / ARM64 (Windows) / arm64 (Mac) は同じ ARM 64bit → "arm64"
            machine_map = {
                "AMD64": "x86_64",
                "x64": "x86_64",
                "aarch64": "arm64",
                "ARM64": "arm64",
            }
            machine = machine_map.get(machine, machine)

            # 例: "Linux 6.1.0-rpi7 arm64" / "Windows 11 x86_64" / "Darwin 24.1 arm64"
            platform_str = f"{osname} {release} {machine}".strip()
            await self._redis.hset("worker_platforms", self._worker_name, platform_str)
            logger.info("Workerバージョン: v%s, platform: %s", __version__, platform_str)
        except Exception as e:
            logger.warning("バージョン登録エラー: %s", e)

    async def check_latest_version(self, *, quiet: bool = False) -> None:
        """Coordinatorが記録した最新版 (Redis) と自分を比較し、古ければ警告ログを出す

        quiet=True: 定期チェック用。「最新です」ログを抑制し、同一バージョンの重複通知を防ぐ。
        """
        try:
            raw = await self._redis.get("swim:latest_worker_version")
            if not raw:
                return
            latest_tag = (raw.decode() if isinstance(raw, bytes) else raw).lstrip("v")
            if not latest_tag:
                return
            current = tuple(int(x) for x in __version__.split(".") if x.isdigit())
            latest = tuple(int(x) for x in latest_tag.split(".") if x.isdigit())
            if latest > current:
                # 同じバージョンの重複通知を防ぐ
                if quiet and self._notified_version == latest_tag:
                    return
                self._notified_version = latest_tag
                logger.warning(
                    "新しいバージョンが利用可能です: v%s → v%s  "
                    "https://github.com/Meku-30/swim-worker/releases/latest",
                    __version__, latest_tag,
                )
                if self._on_update_available:
                    try:
                        self._on_update_available(latest_tag)
                    except Exception as e:
                        logger.debug("update callback エラー: %s", e)
            elif not quiet:
                logger.info("バージョン最新 (v%s)", __version__)
        except Exception as e:
            logger.debug("バージョンチェックエラー: %s", e)

    async def _acquire_instance_lock(self) -> None:
        """起動時に heartbeat キーを atomic に取得して重複起動を検知する。

        - SET NX で heartbeat:{name} に自分の instance_token を登録
        - 成功: 自分が唯一のワーカー
        - 失敗: TTL 失効を最大90秒待ってリトライ（アップデート後の再起動に対応）
        - それでも失敗: DuplicateWorkerError を送出

        ※ 他プロセスが graceful shutdown 時に DEL してくれば即再起動可能。
           クラッシュ/アップデート時は TTL (heartbeat_interval × 3) 経過後に再起動可能。
        """
        ttl = self._heartbeat_interval * HEARTBEAT_TTL_MULTIPLIER
        key = f"heartbeat:{self._worker_name}"
        acquired = await self._redis.set(key, self._instance_token, ex=ttl, nx=True)
        if acquired:
            return
        # TTL 失効を待ってリトライ（アップデート後の再起動等で旧プロセスの
        # heartbeat が残っている場合に対応。最大 TTL + マージン 秒待つ）
        max_wait = ttl + 10
        logger.info(
            "heartbeat キーが残存中。前プロセスの TTL 失効を最大%d秒待機します...", max_wait,
        )
        for elapsed in range(0, max_wait, 5):
            await asyncio.sleep(5)
            acquired = await self._redis.set(key, self._instance_token, ex=ttl, nx=True)
            if acquired:
                logger.info("heartbeat キー取得成功（%d秒待機）", elapsed + 5)
                return
        raise DuplicateWorkerError(
            f"worker_name '{self._worker_name}' は既に別プロセスで稼働中です。"
            f" 別の名前を使うか、もう一方を停止してください。"
        )

    async def send_heartbeat(self) -> None:
        """heartbeat を更新する。値は instance_token で所有者を識別する。

        worker_manager 側は EXISTS しか見ないため値の変更は影響しない。
        """
        ttl = self._heartbeat_interval * HEARTBEAT_TTL_MULTIPLIER
        await self._redis.setex(
            f"heartbeat:{self._worker_name}", ttl, self._instance_token,
        )

    async def _release_instance_lock(self) -> None:
        """自分の instance_token を持つ heartbeat キーだけを削除する。

        GET で所有者確認 → DEL。他ワーカーが取って代わっていれば削除しない。
        非原子的だが shutdown 時のベストエフォートクリーンアップなので許容。
        """
        key = f"heartbeat:{self._worker_name}"
        try:
            current = await self._redis.get(key)
        except Exception:
            return
        current_str = current.decode() if isinstance(current, bytes) else current
        if current_str != self._instance_token:
            return
        try:
            await self._redis.delete(key)
        except Exception as e:
            logger.debug("instance lock 解放エラー (無視): %s", e)

    def _notify_state(self, state: str, job_type: str = "") -> None:
        """タスク状態変化をGUIコールバックに通知 (例外は握りつぶす)"""
        if self._on_task_state is None:
            return
        try:
            self._on_task_state(state, job_type=job_type,
                                total=self._task_total, errors=self._task_errors)
        except Exception as e:
            logger.debug("on_task_state callback エラー: %s", e)

    async def execute_task(self, task: dict) -> None:
        """タスクを実行し結果をRedisに書き込む"""
        task_id = task.get("task_id")
        job_type = task.get("job_type")
        if not task_id or not job_type:
            logger.error("不正なタスク（task_id/job_type欠落）: %s", str(task)[:200])
            return
        params = task.get("params") or {}
        logger.info("タスク実行開始: %s (type=%s)", task_id, job_type)
        self._notify_state("processing", job_type=job_type)

        # capability_test: 複数のテストリクエストを順に実行し各結果を返す
        if job_type == "capability_test":
            await self._run_capability_test(task_id, params)
            self._task_total += 1
            self._notify_state("idle")
            return

        success = False
        try:
            raw = random.lognormvariate(self._delay_mu, self._delay_sigma)
            delay = max(self._delay_clip_min, min(self._delay_clip_max, raw))
            logger.debug("リクエスト前遅延: %.1f秒", delay)
            await asyncio.sleep(delay)

            url = params["url"]
            method = params.get("method", "POST")
            if method == "GET":
                data = await self._swim.fetch_public_get(
                    url, params=params.get("params"), headers=params.get("headers"),
                )
            else:
                body = params["body"]
                data = await self._swim.execute_api(url, body)

            # Worker 側で parse まで行い、Coordinator には構造化データを送る
            # (帯域削減: 未使用フィールド/メタデータが落ちる)。
            # 有効化する job_type は Redis whitelist `swim:parse_enabled` で動的制御。
            # 未登録 or Redis 不通時は raw 送信 (現状維持 = 安全側)。
            if await parsers.supports(job_type, self._redis):
                try:
                    parsed = parsers.parse_for_job_type(job_type, data)
                    result = {
                        "task_id": task_id, "worker_name": self._worker_name,
                        "status": "success", "format": "parsed",
                        "data": parsed, "error": None,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }
                except Exception as e:
                    # パース失敗時は raw を送って Coordinator 側のフロー (parse → store) に任せる
                    logger.warning("Worker パース失敗、raw 送信にフォールバック (%s): %s", job_type, e)
                    result = {
                        "task_id": task_id, "worker_name": self._worker_name,
                        "status": "success", "data": data, "error": None,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }
            else:
                result = {
                    "task_id": task_id, "worker_name": self._worker_name,
                    "status": "success", "data": data, "error": None,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
            logger.info("タスク成功: %s", task_id)
            success = True
        except Exception as e:
            result = {
                "task_id": task_id, "worker_name": self._worker_name,
                "status": "error", "data": None, "error": str(e),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            logger.error("タスク失敗: %s — %s", task_id, e)

        await self._redis.setex(f"results:{task_id}", RESULT_TTL, _encode_result(result))
        self._task_total += 1
        if not success:
            self._task_errors += 1
        self._notify_state("idle")

    async def _ensure_registered(self) -> None:
        """approved にも pending にもいなければ再登録する"""
        is_approved = await self._redis.sismember("workers:approved", self._worker_name)
        is_pending = await self._redis.sismember("workers:pending", self._worker_name)
        if not is_approved and not is_pending:
            await self.register()

    async def _heartbeat_loop(self) -> None:
        while self._running:
            try:
                await self.send_heartbeat()
                await self._ensure_registered()
                # 定期バージョンチェック（10分間隔）
                self._version_check_counter += 1
                if self._version_check_counter >= self._VERSION_CHECK_INTERVAL:
                    self._version_check_counter = 0
                    await self.check_latest_version(quiet=True)
                    # Redis 揮発時の自動復旧を兼ねて worker_versions/platforms も再登録
                    # (起動時のみだと Redis が空になった時に Worker 再起動まで復活しない)
                    await self.report_version()
            except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
                logger.warning("Redis接続エラー（ハートビート）、5秒後にリトライ: %s", e)
                await asyncio.sleep(5)
                continue
            except Exception as e:
                logger.warning("ハートビート送信失敗: %s", e)
            await asyncio.sleep(self._heartbeat_interval)

    async def _consume_loop(self) -> None:
        queue_key = f"tasks:{self._worker_name}"
        while self._running:
            try:
                item = await self._redis.blpop(queue_key, timeout=5)
                if item is None:
                    continue
                _, raw = item
                task = json.loads(raw)
                await self.execute_task(task)
            except asyncio.CancelledError:
                break
            except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
                logger.warning("Redis接続エラー（コンシューマー）、5秒後にリトライ: %s", e)
                await asyncio.sleep(5)
            except Exception as e:
                logger.error("コンシューマーエラー: %s", e)
                await asyncio.sleep(1)

    async def run(self) -> None:
        self._running = True
        # 重複起動検知: SET NX で heartbeat キーを atomic に取得する。
        # 同じ worker_name で別プロセス/別マシンが稼働中なら DuplicateWorkerError。
        # これが最初の Redis 操作なので、失敗時は何も副作用を残さず exit できる。
        await self._acquire_instance_lock()
        # 起動時に自分宛キューをクリア (前回停止中にキューに残った
        # スタールタスクの再実行を防止)。コーディネーターの timeout
        # 再配布機構で既に別ワーカーに割り当て済みの可能性があるため、
        # 残存タスクは破棄して良い。instance lock 取得済みなので、
        # この時点で他プロセスが新規タスクを入れる余地はない。
        try:
            queue_key = f"tasks:{self._worker_name}"
            cleared = await self._redis.delete(queue_key)
            if cleared:
                logger.info("起動時に古いタスクキューをクリアしました")
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
            logger.warning("起動時キュークリア失敗（続行）: %s", e)
        try:
            await self._redis.client_setname(self._worker_name)
        except Exception as e:
            logger.warning("CLIENT SETNAME 失敗: %s", e)
        await self.register()
        await self.report_version()
        await self.check_latest_version()
        logger.info("Worker '%s' 起動", self._worker_name)
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        consume_task = asyncio.create_task(self._consume_loop())
        self._tasks = [heartbeat_task, consume_task]
        try:
            await asyncio.gather(heartbeat_task, consume_task)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            # instance lock を自分の所有下にある場合のみ解放する。
            # これにより次回起動時に TTL 待ちなく即再起動できる。
            await self._release_instance_lock()
            logger.info("Worker '%s' 停止", self._worker_name)

    def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
