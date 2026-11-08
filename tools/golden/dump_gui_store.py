"""Python 版の GUI の設定の読み書き (settings_store) を見本にする。値は作ったものだけ。

使い方: python tools/golden/dump_gui_store.py --out rust/tests/golden/gui_store
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from swim_worker import settings_store as ss  # noqa: E402

READ_CASES = [
    "REDIS_HOST='redis.example.org'\nWORKER_NAME=tanaka\n",
    "# コメント\nexport SWIM_USERNAME=\"u\\\"ser\"\nA='it\\'s'\nB='back\\\\slash'\n",
    "X=v1 # not comment\nX=v2\n  Y = spaced \nZ=\"a\\nb\"\nbad line\n",
    "REDIS_HOST='日本語.example'\nAUTO_CONNECT='true'\n",
]

UPDATE_CASES = [
    ("", {"REDIS_HOST": "'h'", "AUTO_CONNECT": "'false'"}),
    ("# keep\nREDIS_HOST=old\nOTHER=1\nREDIS_HOST=dup\n", {"REDIS_HOST": "'new'", "REDIS_PASSWORD": None}),
    ("REDIS_PASSWORD='x'\nSWIM_PASSWORD='y'\nKEEP='z'\n", {"REDIS_PASSWORD": None, "SWIM_PASSWORD": None}),
]

QUOTE_CASES = ["plain", "it's", "back\\slash", "日本語", "", "a b#c"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    out = Path(ap.parse_args().out)
    reads = []
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        for text in READ_CASES:
            p.write_text(text, encoding="utf-8")
            reads.append({"text": text, "values": ss.read_env_values(p)})
        updates = []
        for before, upd in UPDATE_CASES:
            if before:
                p.write_text(before, encoding="utf-8")
            elif p.exists():
                p.unlink()
            ss.update_env_file(p, upd)
            updates.append({"before": before, "updates": upd, "after": p.read_text(encoding="utf-8")})
    data = {"read": reads, "update": updates, "quote": [{"in": q, "out": ss.quote_value(q)} for q in QUOTE_CASES]}
    (out / "env.json").write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
