"""GUI (gui.py) から使う tkinter 非依存のヘルパー。単体テスト可能にするため分離。"""
import logging

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
