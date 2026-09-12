"""GUI (gui.py) から使う tkinter 非依存のヘルパー。単体テスト可能にするため分離。"""
import logging
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


def build_windows_update_script(*, base: Path, current_exe: Path, new_exe: Path, old_exe: Path,
                                startup_ok: Path, rollback_marker: Path, log_path: Path,
                                new_version: str) -> str:
    """Windows 用の差し替え .bat の内容を返す (CRLF)。

    move をリトライする (旧exeが解放されるまで最大30秒待機)。
    Phase 2: .old バックアップ + 起動成功判定 + ロールバック。
    move が最終的に失敗した場合 (:fail) も旧 exe を再起動し、reason=move_failed のマーカーを
    書く (Worker が黙って消えないため)。
    """
    return (
        "@echo off\r\n"
        f'echo [%DATE% %TIME%] update start > "{log_path}"\r\n'
        "set /a COUNT=0\r\n"
        ":retry\r\n"
        "set /a COUNT+=1\r\n"
        f'echo [%DATE% %TIME%] attempt %COUNT% >> "{log_path}"\r\n'
        "if %COUNT% gtr 30 goto fail\r\n"
        "ping 127.0.0.1 -n 2 > nul\r\n"
        # Phase 2: 旧 exe を .old にバックアップ (失敗時のロールバック用)
        f'copy /Y "{current_exe}" "{old_exe}" >> "{log_path}" 2>&1\r\n'
        f'move /Y "{new_exe}" "{current_exe}" >> "{log_path}" 2>&1\r\n'
        "if errorlevel 1 goto retry\r\n"
        f'echo [%DATE% %TIME%] move success >> "{log_path}"\r\n'
        # Phase 2: 前回の startup marker を削除 (新 exe の成功判定用)
        f'if exist "{startup_ok}" del "{startup_ok}" >> "{log_path}" 2>&1\r\n'
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
        f'start "" /D "{base}" "{current_exe}"\r\n'
        f'echo [%DATE% %TIME%] new exe started >> "{log_path}"\r\n'
        # Phase 2: 起動成功判定ループ (最大約120秒 startup_ok を待つ)
        # ping -n 2 が約1秒/反復のため、120 反復 ≈ 120 秒。
        # 旧 exe の Redis heartbeat TTL (heartbeat_interval × multiplier=2 の最大 60秒)
        # 失効を待つ余裕として、TTL の倍以上を確保する。
        "set /a WAIT=0\r\n"
        ":waitok\r\n"
        "set /a WAIT+=1\r\n"
        "if %WAIT% gtr 120 goto rollback\r\n"
        "ping 127.0.0.1 -n 2 > nul\r\n"
        f'if exist "{startup_ok}" goto success\r\n'
        "goto waitok\r\n"
        ":success\r\n"
        f'echo [%DATE% %TIME%] startup OK after %WAIT%s >> "{log_path}"\r\n'
        # 成功: .old を削除
        f'if exist "{old_exe}" del "{old_exe}" >> "{log_path}" 2>&1\r\n'
        'del "%~f0"\r\n'
        "exit /b 0\r\n"
        ":rollback\r\n"
        f'echo [%DATE% %TIME%] ROLLBACK: startup_ok not found in 120s >> "{log_path}"\r\n'
        # 新 exe を消して旧 exe を戻す (taskkill で走ってる新 exe を止める)
        f'taskkill /F /IM "{current_exe.name}" >> "{log_path}" 2>&1\r\n'
        "ping 127.0.0.1 -n 3 > nul\r\n"
        f'move /Y "{old_exe}" "{current_exe}" >> "{log_path}" 2>&1\r\n'
        # GUI 起動時にロールバック通知するためのマーカー書き込み (JSON)
        f'mkdir "{rollback_marker.parent}" 2>nul\r\n'
        f'echo {{"rolled_back_from":"v{new_version}"}} > "{rollback_marker}"\r\n'
        f'start "" /D "{base}" "{current_exe}"\r\n'
        'del "%~f0"\r\n'
        "exit /b 0\r\n"
        ":fail\r\n"
        f'echo [%DATE% %TIME%] FAILED after %COUNT% attempts, restarting current exe >> "{log_path}"\r\n'
        f'mkdir "{rollback_marker.parent}" 2>nul\r\n'
        f'echo {{"rolled_back_from":"v{new_version}","reason":"move_failed"}} > "{rollback_marker}"\r\n'
        f'start "" /D "{base}" "{current_exe}"\r\n'
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
    return f"""#!/bin/bash
set -u
LOG="{log_path}"
echo "[$(date)] update start" > "$LOG"
sleep 3
# 旧 exe をバックアップ
cp "{current_exe}" "{old_exe}" >> "$LOG" 2>&1 || true
mv "{new_exe}" "{current_exe}" >> "$LOG" 2>&1
chmod +x "{current_exe}"
# 前回の startup marker 削除
rm -f "{startup_ok}"
# 新 exe 起動
"{current_exe}" &
NEW_PID=$!
echo "[$(date)] new exe started" >> "$LOG"
# 起動成功判定 (最大 120 秒)
# 旧 exe の Redis heartbeat TTL (heartbeat_interval × multiplier=2 の最大 60秒)
# 失効を待つ余裕として TTL の倍以上を確保する。
for i in $(seq 1 120); do
    sleep 1
    if [ -f "{startup_ok}" ]; then
        echo "[$(date)] startup OK after ${{i}}s" >> "$LOG"
        rm -f "{old_exe}"
        rm -- "$0"
        exit 0
    fi
done
# ロールバック
echo "[$(date)] ROLLBACK: startup_ok not found in 120s" >> "$LOG"
kill "$NEW_PID" >> "$LOG" 2>&1 || true
sleep 2
mv "{old_exe}" "{current_exe}" >> "$LOG" 2>&1
mkdir -p "{rollback_marker.parent}"
echo '{{"rolled_back_from":"v{new_version}"}}' > "{rollback_marker}"
"{current_exe}" &
rm -- "$0"
exit 0
"""
