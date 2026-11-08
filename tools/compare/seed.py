"""比べる道具: Coordinator のふり。tasks.json の場面を 2 つの Worker に同じ順で積み、結果を展開して jsonl に書く。
使い方: seed.py setup <名1> <名2> / seed.py run <名1> <名2> <出力1> <出力2> <待つ秒数>
"""
import json
import os
import sys
import time

import redis
import zstandard


def client() -> redis.Redis:
    with open(os.environ["FAKE_REDIS_PASSWORD_FILE"], encoding="utf-8") as f:
        pw = f.read().strip()
    return redis.Redis(host=os.environ["FAKE_REDIS_HOST"], port=int(os.environ["FAKE_REDIS_PORT"]),
                       username=os.environ["FAKE_REDIS_USER"], password=pw, ssl=True,
                       ssl_ca_certs=os.environ["FAKE_REDIS_CA"])


def main() -> int:
    with open(os.environ["SWIM_TRIAL_RESPONSES"], encoding="utf-8") as f:
        spec = json.load(f)
    r = client()
    names = sys.argv[2:4]
    if sys.argv[1] == "setup":
        jobs = {c["task"]["job_type"] for c in spec["cases"]} - {"collect_unknown", "capability_test"}
        r.sadd("workers:approved", *names)
        r.sadd("swim:parse_enabled", *sorted(jobs))
        for n in names:
            r.sadd(f"swim:parse_disabled_workers:{spec['raw_job']}", n)
        return 0
    out = dict(zip(names, sys.argv[4:6]))
    wait = float(sys.argv[6])
    ids = {}
    for i, c in enumerate(spec["cases"]):
        tid = f"{i:03d}-{c['name']}"
        ids[tid] = c["name"]
        for n in names:
            r.rpush(f"tasks:{n}", json.dumps({"task_id": tid, **c["task"]}))
    got: dict[str, dict[str, str]] = {n: {} for n in names}
    end = time.monotonic() + wait
    while time.monotonic() < end and any(len(g) < len(ids) for g in got.values()):
        time.sleep(2)
        for n in names:
            for k in r.scan_iter(f"results:{n}:*"):
                tid = k.decode().split(":", 2)[2]
                raw = zstandard.ZstdDecompressor().decompress(r.get(k), max_output_size=32 << 20)
                got[n][ids.get(tid, tid)] = raw.decode()
                r.delete(k)
    for n in names:
        with open(out[n], "w", encoding="utf-8") as f:
            for name, payload in sorted(got[n].items()):
                f.write(json.dumps({"name": name, "payload": payload}, ensure_ascii=False) + "\n")
        print(f"{n}: 結果 {len(got[n])}/{len(ids)}")
    return 0 if all(len(g) == len(ids) for g in got.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
