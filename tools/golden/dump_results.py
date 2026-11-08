"""Python 版の結果の形 (_make_result → _encode_result) と .env の読み方を見本にする。

入力は作った値だけ。本番の raw は入れない。
使い方: python tools/golden/dump_results.py --out rust/tests/golden
"""
import argparse
import base64
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import dotenv_values  # noqa: E402

from swim_worker import consumer  # noqa: E402

FIXED = dt.datetime(2026, 11, 7, 12, 34, 56, 789012, tzinfo=dt.timezone.utc)

DOTENV_LINES = [
    "A=plain",
    'A="double quoted"',
    "A='single quoted'",
    "A=",
    'A=""',
    "A=value # trailing comment",
    "A=has#hash",
    'A="quoted # not comment"',
    "export A=exported",
    "A=x=y=z",
    "  A = spaced  ",
    'A="esc\\"aped\\nline"',
    "A='raw\\n'",
    "# A=comment",
]


class _FrozenDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED


def results():
    c = consumer.TaskConsumer(MagicMock(), MagicMock(), "golden-worker")
    with patch.object(consumer, "datetime", _FrozenDatetime):
        return {
            "success_raw": c._make_result("t-raw", "success", data={"statusCode": 0, "datas": [{"k": "日本語"}]}),
            "success_parsed": c._make_result("t-parsed", "success", data=[{"id": 1, "v": 1.5}], fmt="parsed"),
            "error": c._make_result("t-err", "error", error="APIエラー (status=500)"),
            "error_redirect_outside": c._make_result(
                "t-redir", "error", error="SWIM の外へリダイレクトされました", error_kind="redirect_outside"),
            "capability": c._make_result("t-cap", "success", data={"capabilities": {
                "collect_notams": {"ok": True, "error": None, "reason": None},
                "collect_pireps": {"ok": False, "error": "API 403エラー", "reason": "unauthorized"},
                "collect_flight_details": {"ok": False, "error": "timeout", "reason": "transient"},
            }}),
        }


def main(out: Path):
    (out / "results").mkdir(parents=True, exist_ok=True)
    for name, r in results().items():
        z = consumer._encode_result(r)
        (out / "results" / f"{name}.json").write_text(json.dumps(
            {"result": r, "zstd_b64": base64.b64encode(z).decode(), "zstd_level": 6},
            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    cases = []
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        for line in DOTENV_LINES:
            p.write_text(line + "\n", encoding="utf-8")
            cases.append({"line": line, "values": dict(dotenv_values(p))})
    (out / "dotenv_cases.json").write_text(
        json.dumps(cases, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    main(ap.parse_args().out)
