"""試運転用: Coordinator のふりをする小さな道具 (SWIM には繋がない)。

Worker の宛先 (tasks:<名>) に作ったタスクを積み、results:<名>:* を読んで展開して数える。
接続先は環境変数 (FAKE_REDIS_HOST・FAKE_REDIS_PORT・FAKE_REDIS_USER・FAKE_REDIS_PASSWORD_FILE・FAKE_REDIS_CA)。
使い方: python tools/fake_coordinator.py <worker 名> <秒数> <間隔の秒数>
"""
import json
import os
import sys
import time
import uuid

import redis
import zstandard


def main(name: str, seconds: int, every: float) -> None:
    with open(os.environ["FAKE_REDIS_PASSWORD_FILE"], encoding="utf-8") as f:
        password = f.read().strip()
    r = redis.Redis(host=os.environ["FAKE_REDIS_HOST"], port=int(os.environ["FAKE_REDIS_PORT"]),
                    username=os.environ.get("FAKE_REDIS_USER") or None, password=password,
                    ssl=True, ssl_ca_certs=os.environ["FAKE_REDIS_CA"])
    r.sadd("workers:approved", name)
    r.sadd("swim:parse_enabled", "collect_notams")
    url = "https://web.swim.mlit.go.jp/f2dnrq/web/FUV201/USV001"
    sent, got, kinds = 0, 0, {}
    end = time.monotonic() + seconds
    i = 0
    while time.monotonic() < end:
        tid = uuid.uuid4().hex
        if i % 10 == 9:
            task = {"task_id": tid, "job_type": "capability_test",
                    "params": {"tests": [{"job_type": "collect_notams", "url": url, "body": {}}]}}
        else:
            task = {"task_id": tid, "job_type": "collect_notams", "params": {"url": url, "body": {"x": i}}}
        r.rpush(f"tasks:{name}", json.dumps(task))
        sent += 1
        i += 1
        time.sleep(every)
        for k in r.scan_iter(f"results:{name}:*"):
            raw = r.get(k)
            r.delete(k)
            res = json.loads(zstandard.ZstdDecompressor().decompress(raw, max_output_size=32 << 20))
            key = f'{res["status"]}/{res.get("format", "raw")}'
            kinds[key] = kinds.get(key, 0) + 1
            got += 1
    print(json.dumps({"sent": sent, "results": got, "kinds": kinds,
                      "heartbeat": bool(r.exists(f"heartbeat:{name}")),
                      "version": (r.hget("worker_versions", name) or b"").decode(),
                      "platform": (r.hget("worker_platforms", name) or b"").decode()}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), float(sys.argv[3]))
