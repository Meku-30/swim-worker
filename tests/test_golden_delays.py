"""Rust 版と照合するための見本 (待ち時間の分布) が今の設定と合っていることを確かめる"""
import json
import math
from pathlib import Path

from swim_worker.config import Settings

GOLDEN = Path(__file__).resolve().parent.parent / "rust" / "tests" / "golden" / "delays.json"


def test_delay_params_match_config():
    d = json.loads(GOLDEN.read_text(encoding="utf-8"))
    f = Settings.model_fields
    pre = d["pre_task"]
    assert pre["median"] == f["request_delay_median"].default == 4.0
    assert pre["p99"] == f["request_delay_p99"].default == 15.0
    assert pre["clip_min"] == f["request_delay_clip_min"].default == 1.5
    assert pre["clip_max"] == f["request_delay_clip_max"].default == 25.0
    assert math.isclose(pre["mu"], math.log(4.0))
    assert math.isclose(pre["sigma"], (math.log(15.0) - math.log(4.0)) / 2.326)


def test_quantiles_are_sorted_and_clipped():
    d = json.loads(GOLDEN.read_text(encoding="utf-8"))
    q = [d["pre_task"]["quantiles"][k] for k in ("5", "25", "50", "75", "95", "99")]
    assert q == sorted(q)
    assert 1.5 <= q[0] and q[-1] <= 25.0
    assert abs(d["pre_task"]["quantiles"]["50"] - 4.0) < 0.1
    assert d["post_response"] == {"rate": 3.0, "offset": 0.05, **{k: d["post_response"][k] for k in ("quantiles",)}}
