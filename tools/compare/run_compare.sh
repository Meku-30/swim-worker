#!/usr/bin/env bash
# Python 版と Rust 版の結果を比べる (使い捨てのコンテナ。Docker のある Linux の機で、docker を使える利用者が実行)。
# 使い方: run_compare.sh <作業フォルダ> <リポの根> <試運転用の Worker (feature trial、amd64)>
# 中で作るもの (終わったら名前で消す): 使い捨ての CA・Redis のパスワード・イメージ 1 つ・コンテナ 2 つ
# SWIM には繋がない (両方とも偽の SWIM)。結果は <作業>/diff.txt・py.jsonl・rs.jsonl
set -euo pipefail
WD="$(mkdir -p "$1" && cd "$1" && pwd)"
REPO="$(cd "$2" && pwd)"
BIN="$3"
IMG=swim-compare:local
C=swim-compare
RC=swim-compare-redis
DOCKER="${DOCKER:-docker}"
cleanup() {
    $DOCKER rm -f "$C" "$RC" >/dev/null 2>&1 || true
    $DOCKER rmi "$IMG" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cd "$WD"
umask 077
mkdir -p pki bin repo
openssl req -x509 -newkey rsa:2048 -nodes -keyout pki/ca.key -out pki/ca.crt -days 2 -subj "/CN=cmp-ca" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -keyout pki/srv.key -out pki/srv.csr -subj "/CN=127.0.0.1" 2>/dev/null
printf "subjectAltName=IP:127.0.0.1\n" > pki/ext.cnf
openssl x509 -req -in pki/srv.csr -CA pki/ca.crt -CAkey pki/ca.key -CAcreateserial -out pki/srv.crt -days 2 -extfile pki/ext.cnf 2>/dev/null
openssl rand -hex 24 > pki/cmp.pw
# ACL はここでは確かめない (2 つの Worker と Coordinator のふりが同じ利用者)
printf 'user default off\nuser cmp on >%s ~* &* +@all\n' "$(cat pki/cmp.pw)" > pki/users.acl
printf 'port 0\ntls-port 6390\nbind 127.0.0.1\ntls-cert-file /t/pki/srv.crt\ntls-key-file /t/pki/srv.key\ntls-ca-cert-file /t/pki/ca.crt\ntls-auth-clients no\naclfile /t/pki/users.acl\nsave ""\nappendonly no\n' > pki/redis.conf
chmod 711 "$WD" pki
chmod 644 pki/srv.crt pki/srv.key pki/ca.crt pki/users.acl pki/redis.conf pki/cmp.pw
cp -r "$REPO/swim_worker" "$REPO/tools" "$REPO/requirements.txt" repo/
cp "$REPO/tools/compare/inner.sh" "$REPO/tools/compare/seed.py" "$REPO/tools/compare/tasks.json" .
cp "$BIN" bin/swim-worker-trial
# 任意: ARMv6 の Worker を qemu-user で動かす (QEMU_ARM=qemu-arm-static、ARM_SYSROOT=Pi OS の libc6 を展開したところ)
if [[ -n "${QEMU_ARM:-}" ]]; then
    rm -rf qemu && mkdir -p qemu && cp "$QEMU_ARM" qemu/qemu-arm-static && cp -r "$ARM_SYSROOT" qemu/root
    chmod -R a+rX qemu
fi
chmod -R a+rX bin repo inner.sh seed.py tasks.json
$DOCKER build -q -t "$IMG" - >/dev/null <<'DEOF'
FROM python:3.12-slim-bookworm
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends procps ca-certificates && rm -rf /var/lib/apt/lists/*
DEOF
$DOCKER run -d --name "$C" -v "$WD":/t "$IMG" sleep 3600 >/dev/null
$DOCKER exec "$C" pip install -q --root-user-action=ignore -r /t/repo/requirements.txt >/dev/null
$DOCKER run -d --name "$RC" --network "container:$C" -v "$WD":/t:ro redis:7 redis-server /t/pki/redis.conf >/dev/null
sleep 2
set +e
$DOCKER exec "$C" bash /t/inner.sh
rc=$?
$DOCKER exec "$C" chmod -R a+rwX /t >/dev/null 2>&1
exit $rc
