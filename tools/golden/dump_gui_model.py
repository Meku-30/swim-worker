"""Python の GUI の文言・判定 (gui_helpers の validate_fields・task_state_text) を見本にする。

使い方: python tools/golden/dump_gui_model.py --out rust/tests/golden/gui_model.json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from swim_worker import gui_helpers as gh  # noqa: E402

FULL = {"redis_host": "h", "redis_username": "u", "redis_password": "p", "swim_username": "s",
        "swim_password": "q", "worker_name": "w1"}
VALIDATE = [
    FULL,
    {**FULL, "redis_host": "  "},
    {**FULL, "redis_username": ""},
    {**FULL, "redis_password": ""},
    {**FULL, "redis_password": " "},
    {**FULL, "swim_password": ""},
    {**FULL, "worker_name": "山田"},
    {**FULL, "worker_name": " w.1_-a "},
    {**FULL, "worker_name": "a" * 33},
    {k: "" for k in FULL},
]
STATES = [("processing", "collect_notams", 0, 0), ("processing", "unknown_job", 3, 1), ("idle", "", 5, 0),
          ("idle", "", 7, 2)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    out = {"validate": [{"fields": f, "message": gh.validate_fields(f)} for f in VALIDATE],
           "task_state": [{"args": list(s), "text": gh.task_state_text(*s)} for s in STATES]}
    Path(ap.parse_args().out).write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
