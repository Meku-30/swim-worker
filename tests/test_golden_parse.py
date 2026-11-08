"""解析の見本 (rust/tests/golden/parse/) が今の Python の解析の結果と同じか。
Python の解析を変えたら tools/golden/dump_parse_cases.py で作り直し、Rust 側も合わせる"""
import copy
import importlib.util
import json
from pathlib import Path

from swim_worker import parsers

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "rust" / "tests" / "golden" / "parse"
_spec = importlib.util.spec_from_file_location("dump_parse_cases", ROOT / "tools" / "golden" / "dump_parse_cases.py")
dump = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dump)


def test_parse_golden_is_current():
    for job_type, cases in dump.CASES.items():
        rows = json.loads((GOLDEN / f"{job_type}.json").read_text(encoding="utf-8"))
        assert len(rows) == len(cases), job_type
        for row, (data, params) in zip(rows, cases):
            try:
                got = {"parsed": parsers.parse_for_job_type(job_type, copy.deepcopy(data), task_params=params)}
            except Exception as e:  # noqa: BLE001
                got = {"error": type(e).__name__}
            want = {k: row[k] for k in ("parsed", "error") if k in row}
            assert json.loads(json.dumps(got)) == want, job_type
