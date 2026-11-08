"""Rust 版と照合するための見本 (結果の形・.env の読み方) が今の Python と合っていることを確かめる"""
import base64
import json
from pathlib import Path

import zstandard
from dotenv import dotenv_values

GOLDEN = Path(__file__).resolve().parent.parent / "rust" / "tests" / "golden"
CASES = ["success_raw", "success_parsed", "error", "error_redirect_outside", "capability"]


def test_results_roundtrip():
    for name in CASES:
        d = json.loads((GOLDEN / "results" / f"{name}.json").read_text(encoding="utf-8"))
        raw = zstandard.ZstdDecompressor().decompress(base64.b64decode(d["zstd_b64"]))
        assert json.loads(raw) == d["result"], name
        # Worker は最小の JSON (区切りに空白なし) を送る
        assert raw == json.dumps(d["result"], separators=(",", ":")).encode(), name


def test_dotenv_cases_match_python(tmp_path):
    cases = json.loads((GOLDEN / "dotenv_cases.json").read_text(encoding="utf-8"))
    assert len(cases) >= 8
    for c in cases:
        p = tmp_path / ".env"
        p.write_text(c["line"] + "\n", encoding="utf-8")
        assert dict(dotenv_values(p)) == c["values"], c["line"]
