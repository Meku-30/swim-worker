#!/usr/bin/env python3
"""Worker↔Redis 通信テスト

Workerを起動した状態で実行すると、ダミータスクを送信して
Workerが正常に結果を返すか確認する。SWIMには接続しない。

使い方:
  # Worker起動後、別ターミナルで:
  python scripts/test_worker_redis.py <worker_name>

  # 例:
  python scripts/test_worker_redis.py meku
"""
import json
import os
import sys
import time
import uuid

import redis

# --- 設定（.envから読む） ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

REDIS_HOST = os.environ.get("REDIS_HOST", "")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6380"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_worker_redis.py <worker_name>")
        sys.exit(1)

    worker_name = sys.argv[1]

    if not REDIS_HOST or not REDIS_PASSWORD:
        print("エラー: REDIS_HOST, REDIS_PASSWORD を .env に設定してください")
        sys.exit(1)

    # 埋め込みCA証明書を使用
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from swim_worker.certs import get_ca_cert_path
    ca_cert = get_ca_cert_path()

    r = redis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD,
        ssl=True, ssl_ca_certs=ca_cert, decode_responses=True,
    )

    # 1. Redis接続テスト
    print(f"[1/5] Redis接続テスト... ", end="")
    assert r.ping(), "PING失敗"
    print("OK")

    # 2. Workerのハートビート確認
    print(f"[2/5] Worker '{worker_name}' のハートビート確認... ", end="")
    hb = r.exists(f"heartbeat:{worker_name}")
    if hb:
        print("OK (alive)")
    else:
        print("NG (ハートビートなし — Workerが起動していますか？)")
        sys.exit(1)

    # 3. Worker承認（まだなら）
    print(f"[3/5] Worker承認状態確認... ", end="")
    if r.sismember("workers:approved", worker_name):
        print("承認済み")
    elif r.sismember("workers:pending", worker_name):
        print("pending → 承認します")
        r.smove("workers:pending", "workers:approved", worker_name)
    else:
        print("未登録 — Workerを起動してください")
        sys.exit(1)

    # 4. ダミータスクを送信（SWIMに接続しないURL）
    task_id = str(uuid.uuid4())
    task = {
        "task_id": task_id,
        "job_type": "test_ping",
        "params": {
            "url": "https://httpbin.org/post",
            "body": {"test": True, "worker": worker_name},
        },
        "timeout_seconds": 30,
    }
    print(f"[4/5] テストタスク送信: {task_id[:8]}... ", end="")
    r.rpush(f"tasks:{worker_name}", json.dumps(task))
    print("OK")

    # 5. 結果待ち
    print(f"[5/5] 結果待ち (最大30秒)... ", end="", flush=True)
    for i in range(15):
        result_raw = r.get(f"results:{task_id}")
        if result_raw:
            result = json.loads(result_raw)
            r.delete(f"results:{task_id}")
            print()
            print()
            if result["status"] == "success":
                print("=== テスト成功 ===")
                print(f"  Worker: {result['worker_name']}")
                print(f"  Status: {result['status']}")
                print(f"  Completed: {result['completed_at']}")
                print(f"  Data keys: {list(result['data'].keys()) if result['data'] else 'None'}")
            else:
                print("=== タスク実行エラー（通信自体は成功）===")
                print(f"  Worker: {result['worker_name']}")
                print(f"  Error: {result['error']}")
            return
        print(".", end="", flush=True)
        time.sleep(2)

    print("\nタイムアウト — Workerが応答しませんでした")
    sys.exit(1)


if __name__ == "__main__":
    main()
