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

from swim_worker import __version__, parsers, paths
from swim_worker.auth import SwimClient, SwimUnauthorizedError
from swim_worker.update_check import parse_version

logger = logging.getLogger(__name__)

RESULT_TTL = 3600  # 結果の有効期限（秒）
# Redis ACL の権限不足 (設定の誤り) の時の待ち時間 (秒)。直るまでログを溢れさせない
NOPERM_RETRY_DELAY = 60
# Redis の認証失敗 (パスワードの作り直し・ユーザー削除等) の時の待ち時間 (秒)。
# 待っても直らないことが多いので、接続エラー (5 秒) より長く待つ
AUTH_RETRY_DELAY = 60
HEARTBEAT_TTL_MULTIPLIER = 2
# 結果の書き込みに失敗したときの再試行の間隔 (秒)。3 回試す
RESULT_WRITE_RETRY_DELAYS = (1.0, 2.0)

# heartbeat の延長は「自分の token のとき (または切れているとき)」だけ。
# 別プロセスが同じ名前で lock を取っていたら上書きしない (戻り値 0)
HEARTBEAT_CAS_SCRIPT = """
local v = redis.call('GET', KEYS[1])
if (not v) or v == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
  return 1
end
return 0
"""

# Coordinator が置く最新版と、自動更新の制御 (install.sh --auto と同じキー)
LATEST_VERSION_KEY = "swim:latest_worker_version"
AUTO_UPDATE_ENABLED_KEY = "swim:auto_update_enabled"
AUTO_UPDATE_WHITELIST_KEY = "swim:auto_update_whitelist"
_GATE_REASON_JA = {
    "major": "メジャーバージョンの変更は手動で更新してください",
    "disabled": "管理者が自動更新を止めています",
    "not_in_whitelist": "段階配布の対象外です",
    "error": "自動更新の設定を確認できません",
}


def _as_str(v) -> str | None:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


# 起動成功マーカー・前回プロセスの token の置き場所は paths.py に集約。
# gui_helpers とテストがこの名前で参照するので、モジュール属性として残す。
_startup_marker_path = paths.startup_marker_path
_last_instance_token_path = paths.last_instance_token_path

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
                 task_hard_timeout: float = 300.0,
                 blpop_timeout: float = 20.0,
                 on_update_available=None,
                 on_task_state=None) -> None:
        self._redis = redis_client
        self._swim = swim_client
        self._worker_name = worker_name
        self._heartbeat_interval = heartbeat_interval
        # execute_task がハングし続けても _consume_loop 自体は回復できるようにする
        # 強制タイムアウト (2026-07-20 障害: heartbeatは生きたままタスク消費だけ
        # 完全停止し、キューに230件超が溜まった事象の再発防止)
        self._task_hard_timeout = task_hard_timeout
        # blpopのブロッキング窓。Redis クライアントの socket_timeout (既定 30 秒) より
        # 短くすること。同値だとサーバーの nil 応答より先にクライアント側が切断し、
        # 毎サイクル再接続になる (config.py 参照)
        self._blpop_timeout = blpop_timeout
        # 対数正規分布パラメータ（リクエスト間隔の待機に使用）
        self._delay_mu = math.log(request_delay_median)
        self._delay_sigma = (math.log(request_delay_p99) - self._delay_mu) / 2.326
        self._delay_clip_min = request_delay_clip_min
        self._delay_clip_max = request_delay_clip_max
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._consume_task: asyncio.Task | None = None
        # タスクを取り出してから結果を書き終えるまで True (停止要求はこの間は待つ)
        self._executing = False
        # heartbeat の所有確認を EVAL で行えるか (Redis の ACL に EVAL が無ければ GET→SETEX)
        self._heartbeat_cas = True
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
        self._gate_logged: tuple[str, str] | None = None
        self._invalid_version_logged: str | None = None

    async def register(self) -> None:
        """Worker を pending リストに登録（承認済みならスキップ）"""
        is_approved = await self._redis.sismember("workers:approved", self._worker_name)
        if is_approved:
            logger.info("Worker '%s' は承認済み（再登録スキップ）", self._worker_name)
            return
        await self._redis.sadd("workers:pending", self._worker_name)
        logger.info("Worker '%s' を登録しました (pending)", self._worker_name)

    async def _run_capability_test(self, task_id: str, params: dict) -> dict:
        """capability_test ジョブ: 複数のテストリクエストを実行して結果 dict を返す。

        params: {"tests": [{"job_type": str, "url": str, "body": dict}, ...]}
        data: {"capabilities": {job_type: {"ok": bool, "error": str | None,
                                           "reason": None | "unauthorized" | "transient"}, ...}}
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
                # 403 は「未権限」として即返す (再ログイン・Cookie 破棄をしない)
                await self._swim.execute_api(url, body, retry_on_auth_error=False)
                results[jt] = {"ok": True, "error": None, "reason": None}
                logger.info("capability OK: %s", jt)
            except SwimUnauthorizedError as e:
                # 未権限 (401/403) — Coordinator はこの job_type を非対応として記録する
                results[jt] = {"ok": False, "error": str(e)[:300], "reason": "unauthorized"}
                logger.info("capability NG (未権限): %s — %s", jt, e)
            except Exception as e:
                # 一時障害 (5xx/タイムアウト/接続/セッション失効等) — Coordinator は前回結果を維持する
                results[jt] = {"ok": False, "error": str(e)[:300], "reason": "transient"}
                logger.warning("capability 一時障害: %s — %s", jt, e)
        logger.info("capability_test 完了: %s (ok=%d, ng=%d)",
            task_id[:8],
            sum(1 for r in results.values() if r["ok"]),
            sum(1 for r in results.values() if not r["ok"]),
        )
        return self._make_result(task_id, "success", data={"capabilities": results})

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

    async def _update_gate(self, current: tuple, latest: tuple) -> str | None:
        """新版を案内してよいか。よければ None、だめなら理由。

        判定は install.sh --auto のガードと同じ:
          - 自動更新の kill switch (AUTO_UPDATE_ENABLED_KEY) が文字列 'true' でなければ不可
          - 段階配布の whitelist (AUTO_UPDATE_WHITELIST_KEY、カンマ区切り) が空白以外を含み、
            自分の名前が入っていなければ不可
          - メジャー版が変わる更新は不可 (手動で更新する)
        Redis から読めないときも不可 (安全側。install.sh の ERROR と同じ)。
        """
        if latest[0] != current[0]:
            return "major"
        try:
            enabled = _as_str(await self._redis.get(AUTO_UPDATE_ENABLED_KEY))
            whitelist = _as_str(await self._redis.get(AUTO_UPDATE_WHITELIST_KEY))
        except Exception as e:
            logger.debug("自動更新の設定を読めない: %s", e)
            return "error"
        if enabled != "true":
            return "disabled"
        if whitelist and whitelist.strip():
            allowed = [x.strip() for x in whitelist.split(",") if x.strip()]
            if self._worker_name not in allowed:
                return "not_in_whitelist"
        return None

    async def check_latest_version(self, *, quiet: bool = False) -> None:
        """Coordinatorが記録した最新版 (Redis) と自分を比較し、古ければログを出す。

        新版の案内 (on_update_available) は管理者の kill switch・段階配布の whitelist・
        メジャー版スキップを通ったときだけ呼ぶ (GUI の自動更新もこれで止められる)。
        quiet=True: 定期チェック用。「最新です」ログを抑制し、同一バージョンの重複通知を防ぐ。
        """
        try:
            raw = _as_str(await self._redis.get(LATEST_VERSION_KEY))
            if not raw:
                return
            latest = parse_version(raw)
            if latest is None:
                if self._invalid_version_logged != raw:
                    self._invalid_version_logged = raw
                    logger.warning("Coordinator の最新版の値が不正なため無視: %r", raw[:40])
                return
            latest_tag = "%d.%d.%d" % latest
            current = parse_version(__version__)
            if current is None or latest <= current:
                if not quiet:
                    logger.info("バージョン最新 (v%s)", __version__)
                return
            # 同じバージョンの重複通知を防ぐ
            if quiet and self._notified_version == latest_tag:
                return
            reason = await self._update_gate(current, latest)
            if reason is not None:
                if self._gate_logged != (latest_tag, reason):
                    self._gate_logged = (latest_tag, reason)
                    logger.info(
                        "新しいバージョン v%s があります (自動更新は保留: %s)",
                        latest_tag, _GATE_REASON_JA.get(reason, reason),
                    )
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
        except Exception as e:
            logger.debug("バージョンチェックエラー: %s", e)

    async def _acquire_instance_lock(self) -> None:
        """起動時に heartbeat キーを atomic に取得して重複起動を検知する。

        - 自分の前世代 (= ファイルに保存された instance_token) が残した lock なら
          即 DEL してから取り直す (Windows GUI 自動アップデートで graceful shutdown を
          経ずに再起動した場合の救済)。token 一致確認なので別マシンの同名 Worker の
          lock を奪うことは無い。
        - SET NX で heartbeat:{name} に自分の instance_token を登録
        - 失敗: TTL 失効を最大 TTL+10 秒 (既定 70 秒) 待ってリトライ
        - それでも失敗: DuplicateWorkerError を送出
        """
        ttl = self._heartbeat_interval * HEARTBEAT_TTL_MULTIPLIER
        key = f"heartbeat:{self._worker_name}"

        # 前世代が残した lock なら奪う (token 一致時のみ)
        await self._claim_orphaned_lock(key)

        acquired = await self._redis.set(key, self._instance_token, ex=ttl, nx=True)
        if acquired:
            self._persist_instance_token()
            return
        # TTL 失効を待ってリトライ（claim でも回収できなかった = 別マシンで稼働中の
        # 可能性、または token ファイル不在の旧バージョンからの更新等）
        max_wait = ttl + 10
        logger.info(
            "heartbeat キーが残存中。前プロセスの TTL 失効を最大%d秒待機します...", max_wait,
        )
        for elapsed in range(0, max_wait, 5):
            await asyncio.sleep(5)
            acquired = await self._redis.set(key, self._instance_token, ex=ttl, nx=True)
            if acquired:
                self._persist_instance_token()
                logger.info("heartbeat キー取得成功（%d秒待機）", elapsed + 5)
                return
        raise DuplicateWorkerError(
            f"worker_name '{self._worker_name}' は既に別プロセスで稼働中です。"
            f" 別の名前を使うか、もう一方を停止してください。"
        )

    async def _claim_orphaned_lock(self, key: str) -> None:
        """前回プロセスが残した orphan lock を奪う (token 一致時のみ DEL)。

        ファイルに記録された前世代の instance_token と Redis 上の値が一致した時だけ
        「自分の前世代が異常終了 (taskkill 等で graceful shutdown を経なかった)」と
        判定して lock を解放する。一致しない場合は別マシンで稼働中なので何もしない。
        """
        path = _last_instance_token_path()
        if not path.exists():
            return
        try:
            last_token = path.read_text(encoding="utf-8").strip()
        except Exception:
            return
        if not last_token:
            return
        try:
            current_value = await self._redis.get(key)
        except Exception:
            return
        if current_value is None:
            return
        current_str = (current_value.decode()
                       if isinstance(current_value, bytes) else current_value)
        if current_str != last_token:
            return  # 別 Worker が今 lock を保持 → 触らない
        try:
            await self._redis.delete(key)
            logger.info("前回プロセスの instance lock を解放 (token 一致)")
        except Exception as e:
            logger.debug("orphan lock 解放失敗 (無視): %s", e)

    def _persist_instance_token(self) -> None:
        """自分の instance_token をファイルに書き込み、次世代起動時の claim に備える"""
        path = _last_instance_token_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self._instance_token, encoding="utf-8")
        except Exception as e:
            logger.debug("instance token 永続化失敗 (無視): %s", e)

    async def send_heartbeat(self) -> None:
        """heartbeat を延長する。値は instance_token で所有者を識別する。

        自分の token のとき (または切れているとき) だけ延長する。別プロセスが同じ名前で
        lock を持っていたら上書きせず DuplicateWorkerError (同名の 2 台目が動いている)。
        Redis の ACL に EVAL が無い間は GET で確かめてから SETEX する (非原子だが所有確認はする)。
        worker_manager 側は EXISTS しか見ないため値の変更は影響しない。
        """
        ttl = self._heartbeat_interval * HEARTBEAT_TTL_MULTIPLIER
        key = f"heartbeat:{self._worker_name}"
        if self._heartbeat_cas:
            try:
                ok = await self._redis.eval(
                    HEARTBEAT_CAS_SCRIPT, 1, key, self._instance_token, str(ttl))
            except redis.exceptions.NoPermissionError:
                self._heartbeat_cas = False
                logger.info("Redis の ACL に EVAL がないため、heartbeat の所有確認は GET→SETEX で行います")
            else:
                if ok in (0, "0", b"0"):
                    raise DuplicateWorkerError(
                        f"heartbeat:{self._worker_name} を別のプロセスが持っています"
                        f" (同じ Worker 名で別の Worker が動いています)")
                return
        current = _as_str(await self._redis.get(key))
        if current is not None and current != self._instance_token:
            raise DuplicateWorkerError(
                f"heartbeat:{self._worker_name} を別のプロセスが持っています"
                f" (同じ Worker 名で別の Worker が動いています)")
        await self._redis.setex(key, ttl, self._instance_token)

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

    def _make_result(self, task_id: str, status: str, *, data=None, error: str | None = None,
                     fmt: str | None = None) -> dict:
        """Coordinator に返す結果 dict (形はここだけで決める)"""
        result = {
            "task_id": task_id, "worker_name": self._worker_name,
            "status": status, "data": data, "error": error,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        if fmt:
            result["format"] = fmt
        return result

    async def _write_result(self, task_id: str, result: dict) -> bool:
        """結果を書く。接続の一時的な失敗は短い間隔で再試行する。書けたら True"""
        payload = _encode_result(result)
        delays = (*RESULT_WRITE_RETRY_DELAYS, None)
        for attempt, delay in enumerate(delays, start=1):
            try:
                await self._redis.setex(self._result_key(task_id), RESULT_TTL, payload)
                return True
            except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
                if delay is None:
                    logger.error("結果の書き込みに失敗 (%d 回試行): %s — %s", attempt, task_id, e)
                    return False
                logger.warning("結果の書き込みに失敗、%.0f秒後に再試行: %s — %s", delay, task_id, e)
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error("結果の書き込みに失敗: %s — %s", task_id, e)
                return False
        return False

    async def _finish_task(self, task_id: str, result: dict, success: bool) -> None:
        """結果を書き、集計して GUI に idle を伝える (書き込みに失敗しても集計・通知はする)"""
        written = False
        try:
            written = await self._write_result(task_id, result)
        finally:
            self._task_total += 1
            if not (success and written):
                self._task_errors += 1
            self._notify_state("idle")

    async def _run_api_task(self, task_id: str, job_type: str, params: dict) -> tuple[dict, bool]:
        """SWIM API を呼んで結果 dict を作る。戻り値は (結果, 成功したか)"""
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
        except Exception as e:
            logger.error("タスク失敗: %s — %s", task_id, e)
            return self._make_result(task_id, "error", error=str(e)), False

        # Worker 側で parse まで行い、Coordinator には構造化データを送る
        # (帯域削減: 未使用フィールド/メタデータが落ちる)。
        # 有効化する job_type は Redis whitelist `swim:parse_enabled` で動的制御、
        # さらに per-worker 除外 `swim:parse_disabled_workers:<job_type>` も考慮。
        # 未登録 or Redis 不通時は raw 送信 (現状維持 = 安全側)。
        result = self._make_result(task_id, "success", data=data)
        try:
            if await parsers.supports(job_type, self._redis, worker_name=self._worker_name):
                try:
                    parsed = parsers.parse_for_job_type(job_type, data, task_params=params)
                    result = self._make_result(task_id, "success", data=parsed, fmt="parsed")
                except Exception as e:
                    # パース失敗時は raw を送って Coordinator 側のフロー (parse → store) に任せる
                    logger.warning("Worker パース失敗、raw 送信にフォールバック (%s): %s", job_type, e)
        except Exception as e:
            logger.debug("パース可否の確認に失敗、raw 送信: %s", e)
        logger.info("タスク成功: %s", task_id)
        return result, True

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

        try:
            if job_type == "capability_test":
                # capability_test: 複数のテストリクエストを順に実行し各結果を返す
                result, success = await self._run_capability_test(task_id, params), True
            else:
                result, success = await self._run_api_task(task_id, job_type, params)
        except asyncio.CancelledError:
            # 強制タイムアウト・即時停止。強制タイムアウトの結果は _consume_loop が書く
            raise
        except Exception as e:
            logger.error("タスク失敗: %s — %s", task_id, e)
            result, success = self._make_result(task_id, "error", error=str(e)), False
        await self._finish_task(task_id, result, success)

    def _result_key(self, task_id: str) -> str:
        """結果のキー。Redis ACL で各 Worker が自分の名前の下にしか書けないよう、名前を入れる"""
        return f"results:{self._worker_name}:{task_id}"

    async def _ensure_registered(self) -> None:
        """approved にも pending にもいなければ再登録する (確認は 1 往復の pipeline)"""
        pipe = self._redis.pipeline(transaction=False)
        pipe.sismember("workers:approved", self._worker_name)
        pipe.sismember("workers:pending", self._worker_name)
        is_approved, is_pending = await pipe.execute()
        if not is_approved and not is_pending:
            await self.register()

    async def _heartbeat_loop(self) -> None:
        # 停止要求のあとも、処理中のタスクが終わるまでは heartbeat を続ける
        while self._running or self._executing:
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
            except DuplicateWorkerError:
                raise
            except redis.exceptions.AuthenticationError as e:
                logger.error(
                    "Redis の認証に失敗（ハートビート、REDIS_USERNAME / REDIS_PASSWORD を確認）、"
                    "%d秒後にリトライ: %s", AUTH_RETRY_DELAY, e,
                )
                await asyncio.sleep(AUTH_RETRY_DELAY)
                continue
            except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
                logger.warning("Redis接続エラー（ハートビート）、5秒後にリトライ: %s", e)
                await asyncio.sleep(5)
                continue
            except Exception as e:
                logger.warning("ハートビート送信失敗: %s", e)
            await asyncio.sleep(self._heartbeat_interval)

    async def _run_one(self, raw) -> None:
        """取り出した 1 件を実行する。強制タイムアウトでも error の結果を書く"""
        task = json.loads(raw)
        try:
            await asyncio.wait_for(self.execute_task(task), timeout=self._task_hard_timeout)
        except asyncio.TimeoutError:
            task_id = task.get("task_id") if isinstance(task, dict) else None
            logger.error(
                "タスク強制タイムアウト (%.0f秒超過、consume_loop継続): %s",
                self._task_hard_timeout, task_id,
            )
            if task_id:
                # Coordinator が配布のタイムアウトまで待たずに失敗と分かるよう error を返す
                await self._finish_task(task_id, self._make_result(
                    task_id, "error",
                    error=f"Worker 側の強制タイムアウト ({self._task_hard_timeout:.0f}秒)"), False)
            else:
                self._task_total += 1
                self._task_errors += 1
                self._notify_state("idle")

    async def _consume_loop(self) -> None:
        queue_key = f"tasks:{self._worker_name}"
        while self._running:
            try:
                item = await self._redis.blpop(queue_key, timeout=self._blpop_timeout)
                if item is None:
                    continue
                # ここから結果を書き終えるまでは停止要求でも中断しない
                self._executing = True
                try:
                    _, raw = item
                    await self._run_one(raw)
                finally:
                    self._executing = False
            except asyncio.CancelledError:
                break
            except redis.exceptions.NoPermissionError as e:
                logger.error(
                    "Redis の権限がありません (REDIS_USERNAME と管理者の ACL 設定を確認)、"
                    "%d秒後にリトライ: %s", NOPERM_RETRY_DELAY, e,
                )
                await asyncio.sleep(NOPERM_RETRY_DELAY)
            except redis.exceptions.AuthenticationError as e:
                logger.error(
                    "Redis の認証に失敗（コンシューマー、REDIS_USERNAME / REDIS_PASSWORD を確認）、"
                    "%d秒後にリトライ: %s", AUTH_RETRY_DELAY, e,
                )
                await asyncio.sleep(AUTH_RETRY_DELAY)
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
        # CLIENT SETNAME は接続単位で redis-py が送る (__main__ の client_name= 指定)。
        # ここで 1 回だけ呼ぶと再接続後に名前が消えるため実行時には行わない。
        await self.register()
        await self.report_version()
        await self.check_latest_version()
        logger.info("Worker '%s' 起動", self._worker_name)
        # 起動成功マーカー: GUI 版のアップデートヘルパーがこのファイルの
        # 存在で「新 exe が正常起動したか」を判定する (Phase 2 ロールバック)。
        # register + report_version が通過した = Redis 疎通確認済のタイミングで作成する。
        try:
            marker = _startup_marker_path()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(__version__, encoding="utf-8")
        except Exception as e:
            logger.debug("startup marker 作成失敗 (無視): %s", e)
        if not self._running:
            # 起動処理中に停止要求が来た
            await self._release_instance_lock()
            logger.info("Worker '%s' 停止", self._worker_name)
            return
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        consume_task = asyncio.create_task(self._consume_loop())
        self._consume_task = consume_task
        self._tasks = [heartbeat_task, consume_task]
        try:
            # どちらかが終わったら止める。heartbeat が DuplicateWorkerError (同名の別プロセスが
            # lock を取った) で終わったときは呼び出し元へ伝える
            done, _ = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if not t.cancelled() and t.exception() is not None:
                    raise t.exception()
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            for t in self._tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            # instance lock を自分の所有下にある場合のみ解放する。
            # これにより次回起動時に TTL 待ちなく即再起動できる。
            await self._release_instance_lock()
            logger.info("Worker '%s' 停止", self._worker_name)

    def request_stop(self) -> None:
        """停止要求 (SIGTERM 等): 新しいタスクは取らず、処理中のタスクは結果を書いてから止まる"""
        if not self._running:
            return
        self._running = False
        if self._executing:
            logger.info("停止要求: 処理中のタスクを終えてから停止します")
        else:
            logger.info("停止要求: 停止します")
            if self._consume_task is not None and not self._consume_task.done():
                self._consume_task.cancel()  # BLPOP の待ちを中断

    def stop(self) -> None:
        """即時停止 (処理中のタスクも中断する)"""
        self._running = False
        for task in self._tasks:
            task.cancel()
