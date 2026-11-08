"""Python 版の待ち時間の分布を見本にする (Rust 版の分布を分位点で照合するため)。

要求の前の待ち時間は TaskConsumer が持つ mu・sigma をそのまま使い (式を写さない)、
consumer.py の _run_api_task と同じ random.lognormvariate + 上下の切り詰めで引く。
使い方: python tools/golden/sample_delays.py --out rust/tests/golden/delays.json
"""
import argparse
import json
import random
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from swim_worker.config import Settings  # noqa: E402
from swim_worker.consumer import TaskConsumer  # noqa: E402

N = 100_000
QS = (5, 25, 50, 75, 95, 99)


def quantiles(xs):
    xs = sorted(xs)
    return {str(q): round(xs[min(len(xs) - 1, int(len(xs) * q / 100))], 4) for q in QS}


def main(out: Path):
    f = Settings.model_fields
    c = TaskConsumer(MagicMock(), MagicMock(), "golden",
                     request_delay_median=f["request_delay_median"].default,
                     request_delay_p99=f["request_delay_p99"].default,
                     request_delay_clip_min=f["request_delay_clip_min"].default,
                     request_delay_clip_max=f["request_delay_clip_max"].default)
    rng = random.Random(20261107)
    pre = [max(c._delay_clip_min, min(c._delay_clip_max, rng.lognormvariate(c._delay_mu, c._delay_sigma)))
           for _ in range(N)]
    post = [rng.expovariate(3.0) + 0.05 for _ in range(N)]  # auth.py execute_api の応答のあと
    data = {
        "pre_task": {
            "source": "consumer.py _run_api_task (lognormvariate を clip_min〜clip_max に切り詰め)",
            "median": f["request_delay_median"].default, "p99": f["request_delay_p99"].default,
            "clip_min": c._delay_clip_min, "clip_max": c._delay_clip_max,
            "mu": c._delay_mu, "sigma": c._delay_sigma, "quantiles": quantiles(pre),
        },
        "post_response": {"rate": 3.0, "offset": 0.05, "quantiles": quantiles(post)},
        "slow_response": {
            "source": "auth.py execute_api",
            "threshold_secs": 10.0, "step_up": 2.0, "max": 15.0, "step_down": 0.5,
        },
        "uniform_secs": {
            "source": "auth.py (random.uniform の上下)",
            "login_after_top_get": [1.0, 3.0],
            "login_after_post": [0.5, 1.5],
            "login_after_redirect_get": [0.5, 1.5],
            "cookie_restore_after_nav": [0.5, 1.0],
            "browse_after_first_get": [0.3, 0.8],
            "browse_phase_break": [0.3, 1.0],
            "browse_between_requests": [0.02, 0.15],
            "browse_extra_get": [0.02, 0.1],
            "browse_after_all": [1.0, 3.0],
            "api_retry_wait": [5.0, 15.0],
        },
        "samples": N, "seed": 20261107,
    }
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    main(ap.parse_args().out)
