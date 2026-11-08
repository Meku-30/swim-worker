"""試運転用: Worker の利用者の権限を変える (管理者で接続)。使い方: redis_admin.py drop_approved|restore_approved"""
import os, sys

import redis

RULES = os.environ.get("TRIAL_RULES_FILE", "/t/pki/rules.txt")


def main(cmd: str) -> None:
    r = redis.Redis(host="127.0.0.1", port=6390, username="admin",
                    password=open("/t/pki/admin.pw").read().strip(), ssl=True, ssl_ca_certs="/t/pki/ca.crt")
    rules = open(RULES).read().split()
    if cmd == "drop_approved":
        # 承認の一覧 (workers:approved) を読む規則だけを外す
        line = open(RULES).read().replace("(+sismember %R~workers:approved) ", "")
        rules = line.split()
    elif cmd != "restore_approved":
        raise SystemExit(f"不明: {cmd}")
    pw = open("/t/pki/worker.pw").read().strip()
    r.execute_command("ACL", "SETUSER", "worker-w1", "reset", *rules_with_selectors(rules), ">" + pw)


def rules_with_selectors(tokens):
    """'(+a +b ~k)' のように空白で割れたセレクタを 1 つの引数に戻す"""
    out, cur = [], None
    for t in tokens:
        if cur is not None:
            cur.append(t)
            if t.endswith(")"):
                out.append(" ".join(cur)); cur = None
        elif t.startswith("(") and not t.endswith(")"):
            cur = [t]
        else:
            out.append(t)
    return out


if __name__ == "__main__":
    main(sys.argv[1])
