"""Python 版の platform の文字列 (consumer.report_version) を見本にする。

使い方: python tools/golden/dump_platform.py --out rust/tests/golden/platform.json
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from swim_worker import consumer  # noqa: E402

CASES = [
    ("Windows", "10", "10.0.22631", "AMD64"),
    ("Windows", "10", "10.0.22000", "AMD64"),
    ("Windows", "10", "10.0.21999", "AMD64"),
    ("Windows", "10", "10.0.19045", "ARM64"),
    ("Windows", "10", "bad", "x64"),
    ("Linux", "6.1.0-rpi7-rpi-v8", "#1 SMP", "aarch64"),
    ("Linux", "6.8.0-45-generic", "#1 SMP", "x86_64"),
    ("Linux", "6.1.21+", "#1642", "armv6l"),
]


def run(system, release, version, machine) -> str:
    r = MagicMock()
    r.hset = AsyncMock()
    c = consumer.TaskConsumer.__new__(consumer.TaskConsumer)
    c._redis = r
    c._worker_name = "w1"
    with patch("platform.system", return_value=system), patch("platform.release", return_value=release), \
            patch("platform.version", return_value=version), patch("platform.machine", return_value=machine):
        asyncio.run(c.report_version())
    return [a.args[2] for a in r.hset.await_args_list if a.args[0] == "worker_platforms"][0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = [{"system": s, "release": r, "version": v, "machine": m, "expected": run(s, r, v, m)} for s, r, v, m in CASES]
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
