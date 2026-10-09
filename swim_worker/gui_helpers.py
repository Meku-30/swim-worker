"""GUI (gui.py) から使う tkinter 非依存のヘルパー。単体テスト可能にするため分離。"""
import logging
import shlex
from pathlib import Path

ROLLBACK_DISABLE_THRESHOLD = 2  # 同一バージョンでこの回数ロールバックしたら自動更新を止める


def next_rollback_state(gui_settings: dict, from_version: str) -> tuple[dict, bool]:
    """ロールバック検知時の gui_settings 更新を計算する (純粋関数)。

    同一バージョンのロールバック回数を数え、閾値に達したら auto_update を False にする。
    戻り値: (新しい settings dict, 自動更新を停止したか)
    """
    new = dict(gui_settings)
    counts = dict(new.get("rollback_count") or {})
    counts[from_version] = counts.get(from_version, 0) + 1
    new["rollback_count"] = counts
    disable = counts[from_version] >= ROLLBACK_DISABLE_THRESHOLD and new.get("auto_update", False)
    if disable:
        new["auto_update"] = False
    return new, disable


def write_startup_marker() -> None:
    """GUI が起動して生存したことをヘルパースクリプトに伝える (`data/.startup_ok`)。

    v1.1.2 以前は Worker が Redis に接続したときしか書かれず、「起動時に自動接続」OFF の
    利用者では更新のたびに 120 秒後ロールバックされていた。
    """
    from swim_worker import __version__
    from swim_worker.consumer import _startup_marker_path
    try:
        marker = _startup_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(__version__, encoding="utf-8")
    except Exception as e:
        logging.debug("startup marker 作成失敗 (無視): %s", e)


_PYI_ENV_KEYS = ("_PYI_ARCHIVE_FILE", "_PYI_APPLICATION_HOME_DIR",
                 "_PYI_PARENT_PROCESS_LEVEL", "_MEIPASS2")


def pyinstaller_clean_env(base_env: dict) -> dict:
    """PyInstaller onefile の子プロセス判定用環境変数を除去した環境を返す。

    ヘルパー経由で再起動する新 exe がこれらを継承すると、既に消えた _MEI ディレクトリから
    Python を読もうとして起動に失敗する (bootloader はプラットフォーム非依存で判定する)。
    """
    env = {k: v for k, v in base_env.items() if k not in _PYI_ENV_KEYS}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def bat_escape(text: str) -> str:
    """バッチファイルに埋め込む文字列の % を %% にする (変数展開されないように)"""
    return text.replace("%", "%%")


def encode_bat(text: str, ansi_codec: str = "mbcs") -> bytes:
    """.bat の中身をバイト列にする。

    cmd.exe はバッチをシステムの ANSI コードページ (日本語 Windows は CP932) で読むので、
    書ければそれで書く。書けない文字 (コードページ外のユーザー名など) があれば、
    2 行目に `chcp 65001` を入れて UTF-8 で書く (それ以降の行は UTF-8 として読まれる)。
    """
    try:
        return text.encode(ansi_codec)
    except UnicodeEncodeError:
        first, sep, rest = text.partition("\r\n")
        return (first + sep + "chcp 65001 > nul\r\n" + rest).encode("utf-8")


def build_windows_update_script(*, base: Path, current_exe: Path, new_exe: Path, old_exe: Path,
                                startup_ok: Path, rollback_marker: Path, log_path: Path,
                                new_version: str) -> str:
    """Windows 用の差し替え .bat の内容を返す (CRLF)。

    move をリトライする (旧exeが解放されるまで最大30秒待機)。
    Phase 2: .old バックアップ + 起動成功判定 + ロールバック。
    move が最終的に失敗した場合 (:fail) も旧 exe を再起動し、reason=move_failed のマーカーを
    書く (Worker が黙って消えないため)。
    """
    # パスの % はバッチの変数展開にならないよう %% にする (%DATE% などスクリプト自身の変数はそのまま)
    base_s, current_exe_s, new_exe_s, old_exe_s, startup_ok_s, rollback_marker_s, log_path_s = (
        bat_escape(str(x)) for x in (base, current_exe, new_exe, old_exe, startup_ok,
                                     rollback_marker, log_path))
    marker_dir = bat_escape(str(rollback_marker.parent))
    exe_name = bat_escape(current_exe.name)
    return (
        "@echo off\r\n"
        f'echo [%DATE% %TIME%] update start > "{log_path_s}"\r\n'
        "set /a COUNT=0\r\n"
        ":retry\r\n"
        "set /a COUNT+=1\r\n"
        f'echo [%DATE% %TIME%] attempt %COUNT% >> "{log_path_s}"\r\n'
        "if %COUNT% gtr 30 goto fail\r\n"
        "ping 127.0.0.1 -n 2 > nul\r\n"
        # Phase 2: 旧 exe を .old にバックアップ (失敗時のロールバック用)
        f'copy /Y "{current_exe_s}" "{old_exe_s}" >> "{log_path_s}" 2>&1\r\n'
        f'move /Y "{new_exe_s}" "{current_exe_s}" >> "{log_path_s}" 2>&1\r\n'
        "if errorlevel 1 goto retry\r\n"
        f'echo [%DATE% %TIME%] move success >> "{log_path_s}"\r\n'
        # Phase 2: 前回の startup marker を削除 (新 exe の成功判定用)
        f'if exist "{startup_ok_s}" del "{startup_ok_s}" >> "{log_path_s}" 2>&1\r\n'
        # ファイルシステム同期待ち
        "ping 127.0.0.1 -n 2 > nul\r\n"
        # PyInstaller 6.9+ の bootloader が _PYI_ARCHIVE_FILE を
        # 継承していると onefile 展開をスキップして python DLL 読み込み失敗する
        # (親exeが終了して _MEI tmpdir が消えているため)
        # 公式対応: PYINSTALLER_RESET_ENVIRONMENT=1 + 関連envを削除
        'set "_PYI_ARCHIVE_FILE="\r\n'
        'set "_PYI_APPLICATION_HOME_DIR="\r\n'
        'set "_PYI_PARENT_PROCESS_LEVEL="\r\n'
        'set "_MEIPASS2="\r\n'
        'set "PYINSTALLER_RESET_ENVIRONMENT=1"\r\n'
        f'start "" /D "{base_s}" "{current_exe_s}"\r\n'
        f'echo [%DATE% %TIME%] new exe started >> "{log_path_s}"\r\n'
        # Phase 2: 起動成功判定ループ (最大約120秒 startup_ok を待つ)
        # ping -n 2 が約1秒/反復のため、120 反復 ≈ 120 秒。
        # 旧 exe の Redis heartbeat TTL (heartbeat_interval × multiplier=2 の最大 60秒)
        # 失効を待つ余裕として、TTL の倍以上を確保する。
        "set /a WAIT=0\r\n"
        ":waitok\r\n"
        "set /a WAIT+=1\r\n"
        "if %WAIT% gtr 120 goto rollback\r\n"
        "ping 127.0.0.1 -n 2 > nul\r\n"
        f'if exist "{startup_ok_s}" goto success\r\n'
        "goto waitok\r\n"
        ":success\r\n"
        f'echo [%DATE% %TIME%] startup OK after %WAIT%s >> "{log_path_s}"\r\n'
        # 成功: .old を削除
        f'if exist "{old_exe_s}" del "{old_exe_s}" >> "{log_path_s}" 2>&1\r\n'
        'del "%~f0"\r\n'
        "exit /b 0\r\n"
        ":rollback\r\n"
        f'echo [%DATE% %TIME%] ROLLBACK: startup_ok not found in 120s >> "{log_path_s}"\r\n'
        # 新 exe を消して旧 exe を戻す (taskkill で走ってる新 exe を止める)
        f'taskkill /F /IM "{exe_name}" >> "{log_path_s}" 2>&1\r\n'
        "ping 127.0.0.1 -n 3 > nul\r\n"
        f'move /Y "{old_exe_s}" "{current_exe_s}" >> "{log_path_s}" 2>&1\r\n'
        # GUI 起動時にロールバック通知するためのマーカー書き込み (JSON)
        f'mkdir "{marker_dir}" 2>nul\r\n'
        f'echo {{"rolled_back_from":"v{new_version}"}} > "{rollback_marker_s}"\r\n'
        f'start "" /D "{base_s}" "{current_exe_s}"\r\n'
        'del "%~f0"\r\n'
        "exit /b 0\r\n"
        ":fail\r\n"
        f'echo [%DATE% %TIME%] FAILED after %COUNT% attempts, restarting current exe >> "{log_path_s}"\r\n'
        f'mkdir "{marker_dir}" 2>nul\r\n'
        f'echo {{"rolled_back_from":"v{new_version}","reason":"move_failed"}} > "{rollback_marker_s}"\r\n'
        f'start "" /D "{base_s}" "{current_exe_s}"\r\n'
        'del "%~f0"\r\n'
        "exit /b 1\r\n"
    )


def build_macos_update_script(*, current_exe: Path, new_exe: Path, old_exe: Path,
                              startup_ok: Path, rollback_marker: Path, log_path: Path,
                              new_version: str) -> str:
    """macOS 用の差し替え .sh の内容を返す。

    Phase 2: .old バックアップ + 起動成功判定 + ロールバック。
    ロールバック時は pkill -f (スクリプト自身も対象になる) ではなく、起動した新 exe の PID を kill する。
    """
    # パスはシェルの単引用符で囲む ($ や ` や " を含むフォルダ名でも壊れないように)
    current_exe_q, new_exe_q, old_exe_q, startup_ok_q, rollback_marker_q, log_path_q = (
        shlex.quote(str(x)) for x in (current_exe, new_exe, old_exe, startup_ok,
                                      rollback_marker, log_path))
    marker_dir_q = shlex.quote(str(rollback_marker.parent))
    return f"""#!/bin/bash
set -u
LOG={log_path_q}
echo "[$(date)] update start" > "$LOG"
sleep 3
# 旧 exe をバックアップ
cp {current_exe_q} {old_exe_q} >> "$LOG" 2>&1 || true
mv {new_exe_q} {current_exe_q} >> "$LOG" 2>&1
chmod +x {current_exe_q}
# 前回の startup marker 削除
rm -f {startup_ok_q}
# 新 exe 起動
{current_exe_q} &
NEW_PID=$!
echo "[$(date)] new exe started" >> "$LOG"
# 起動成功判定 (最大 120 秒)
# 旧 exe の Redis heartbeat TTL (heartbeat_interval × multiplier=2 の最大 60秒)
# 失効を待つ余裕として TTL の倍以上を確保する。
for i in $(seq 1 120); do
    sleep 1
    if [ -f {startup_ok_q} ]; then
        echo "[$(date)] startup OK after ${{i}}s" >> "$LOG"
        rm -f {old_exe_q}
        rm -- "$0"
        exit 0
    fi
done
# ロールバック
echo "[$(date)] ROLLBACK: startup_ok not found in 120s" >> "$LOG"
kill "$NEW_PID" >> "$LOG" 2>&1 || true
sleep 2
mv {old_exe_q} {current_exe_q} >> "$LOG" 2>&1
mkdir -p {marker_dir_q}
echo '{{"rolled_back_from":"v{new_version}"}}' > {rollback_marker_q}
{current_exe_q} &
rm -- "$0"
exit 0
"""


# --- 画面の表示・判定 (tkinter 非依存) ---

# SWIM API ジョブタイプの日本語ラベル
JOB_LABELS = {
    "collect_notams": "NOTAM収集",
    "collect_pireps": "PIREP収集",
    "collect_pkg_weather": "PKG気象収集",
    "collect_airports": "空港一覧取得",
    "collect_airport_profiles": "空港詳細取得",
    "collect_airspace_data": "空域データ取得",
    "collect_flight_foids": "フライト一覧取得",
    "collect_flight_details": "フライト詳細取得",
    "fetch_maintenance_info": "メンテ情報取得",
    "capability_test": "権限テスト",
}


def task_state_text(state: str, job_type: str = "", total: int = 0, errors: int = 0) -> str:
    """Consumer のタスク状態を状態欄の文字列にする"""
    if state == "processing":
        return f"● 実行中: {JOB_LABELS.get(job_type, job_type)}"
    if errors > 0:
        return f"● 接続中 (処理済 {total} 件, エラー {errors})"
    return f"● 接続中 (処理済 {total} 件)"


def update_prompt_kind(*, snoozed: bool, auto_update: bool) -> str | None:
    """新版を知ったときに出すもの: "countdown" (自動適用) / "prompt" (確認) / None (snooze 中)"""
    if snoozed:
        return None
    return "countdown" if auto_update else "prompt"


# 設定欄 (表示名)。この順に確かめる
FIELD_LABELS = {
    "redis_host": "Redis ホスト",
    "redis_username": "Redis ユーザー名",
    "redis_password": "Redis パスワード",
    "swim_username": "SWIM ID",
    "swim_password": "SWIM パスワード",
    "worker_name": "Worker 名",
}
SECRET_FIELDS = ("redis_password", "swim_password")


def validate_fields(fields: dict[str, str]) -> str | None:
    """起動前の確認。問題があればユーザーに見せる文、無ければ None。

    全部必須 (Redis のユーザー名も。サーバー側で default ユーザーは使えない)。
    パスワードは空白も値の一部なので strip しない。
    """
    from swim_worker.config import WORKER_NAME_RE, WORKER_NAME_RULE_MESSAGE
    for key, label in FIELD_LABELS.items():
        value = fields.get(key, "")
        empty = (value == "") if key in SECRET_FIELDS else (value.strip() == "")
        if empty:
            hint = "管理者から教えてもらったユーザー名を入力してください。" \
                if key == "redis_username" else "設定を記入してください。"
            return f"{label} が空です。{hint}"
    if not WORKER_NAME_RE.fullmatch(fields.get("worker_name", "").strip()):
        return WORKER_NAME_RULE_MESSAGE
    return None
