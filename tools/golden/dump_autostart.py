"""Python 版の自動起動の .bat と、更新の差し替えの .bat を見本にする (パスは作ったもの)。

使い方: python tools/golden/dump_autostart.py --out rust/tests/golden/bat.json
"""
import argparse
import json
import sys
from pathlib import PureWindowsPath as P
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from swim_worker import autostart, gui_helpers  # noqa: E402

BASES = [r"C:\Users\tanaka\swim-worker", r"C:\Users\山田%太郎\swim worker", r"D:\ツール\☃"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    out = []
    for b in BASES:
        base = P(b)
        exe = base / "swim-worker-gui.exe"
        startup = autostart.build_bat([str(exe)], base)
        helper = gui_helpers.build_windows_update_script(
            base=base, current_exe=exe, new_exe=base / "swim-worker-gui.new.exe",
            old_exe=base / "swim-worker-gui.exe.old", startup_ok=base / "data" / ".startup_ok",
            rollback_marker=base / "data" / ".update_rollback.json",
            log_path=base / "swim-worker-update.log", new_version="1.3.1")
        try:
            cp932 = startup.encode("cp932").hex()
        except UnicodeEncodeError:
            cp932 = None
        utf8_fallback = gui_helpers.encode_bat(startup, "ascii").decode("utf-8") if not startup.isascii() else None
        out.append({"base": b, "exe": str(exe), "startup_bat": startup, "startup_cp932_hex": cp932,
                    "startup_utf8_fallback": utf8_fallback, "helper_bat": helper})
    Path(ap.parse_args().out).write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
