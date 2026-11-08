"""Python 版と Rust 版の結果を比べる。

入力は 1 行 1 件の jsonl ({"name": 場面の名, "payload": 展開後の結果の JSON の文字列})。
比べないもの: task_id・worker_name・completed_at (毎回変わる)。それ以外は
  1. 文字列として: 送られた JSON の文字列 (キーの順・数の書き方・エスケープまで)。3 つの値だけ伏せる
  2. 値として: 型まで (1 と 1.0、true と 1、null と欠けを別に数える)
違いが無ければ 0、あれば 1 件 1 行で出して 1。
使い方: python diff_results.py py.jsonl rs.jsonl
"""
import json
import re
import sys

SKIP = ("task_id", "worker_name", "completed_at")
MASK = re.compile(r'"(%s)":"(?:[^"\\]|\\.)*"' % "|".join(SKIP))


def load(path: str) -> dict[str, str]:
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                out[d["name"]] = d["payload"]
    return out


def walk(a, b, at: str, out: list[str]) -> None:
    if type(a) is not type(b):
        out.append(f"{at}: 型 {type(a).__name__} {a!r:.80} ≠ {type(b).__name__} {b!r:.80}")
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k in SKIP and at == "":
                continue
            if k not in a or k not in b:
                out.append(f"{at}/{k}: {'Python だけ' if k in a else 'Rust だけ'}")
            else:
                walk(a[k], b[k], f"{at}/{k}", out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{at}: 長さ {len(a)} ≠ {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            walk(x, y, f"{at}[{i}]", out)
    elif a != b:
        out.append(f"{at}: {a!r:.120} ≠ {b!r:.120}")


def compare(py: dict[str, str], rs: dict[str, str]) -> list[str]:
    diffs = []
    for name in sorted(set(py) | set(rs)):
        if name not in py or name not in rs:
            diffs.append(f"{name}: 結果が {'Python だけ' if name in py else 'Rust だけ'}")
            continue
        vals: list[str] = []
        walk(json.loads(py[name]), json.loads(rs[name]), "", vals)
        diffs += [f"{name} 値 {v}" for v in vals]
        if not vals and MASK.sub(r'"\1":"*"', py[name]) != MASK.sub(r'"\1":"*"', rs[name]):
            diffs.append(f"{name} 文字列: {py[name][:200]} ≠ {rs[name][:200]}")
    return diffs


def main(a: str, b: str) -> int:
    py, rs = load(a), load(b)
    diffs = compare(py, rs)
    for d in diffs:
        print(d)
    print(f"場面 {len(set(py) | set(rs))} 件、違い {len(diffs)} 件")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
