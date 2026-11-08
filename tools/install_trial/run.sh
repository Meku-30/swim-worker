#!/usr/bin/env bash
# install.sh の試運転を、使い捨てのコンテナで流す (Docker のある Linux の機で、docker を使える利用者が実行)。
#
# 使い方: run.sh <作業フォルダ> <試運転用の Worker (feature trial、amd64)>
#   作業フォルダにはこのフォルダの中身・リポの scripts/・tools/fake_coordinator.py を置いておく
#   (scripts/ → <作業>/scripts.orig、fake_coordinator.py → <作業>/fake_coordinator.py)
#
# 中で作るもの (終わったら全部消す): 使い捨ての CA と証明書・Redis のパスワード・Ed25519 の鍵・
# systemd の入ったイメージ・コンテナ 2 つ (systemd と Redis。ネットワークを共有し 127.0.0.1 だけで話す)
# systemd をコンテナの中で動かすため --privileged を使う (使い捨てで外に口を開けない)
set -euo pipefail
WD="$(cd "$1" && pwd)"
BIN="$2"
IMG=swim-install-trial:local
C=swim-install-trial
RC=swim-install-trial-redis
DOCKER="${DOCKER:-docker}"

cleanup() {
    $DOCKER rm -f "$C" "$RC" >/dev/null 2>&1 || true
    $DOCKER rmi "$IMG" >/dev/null 2>&1 || true
    [[ -n "${PULLED_UBUNTU:-}" ]] && $DOCKER rmi ubuntu:22.04 >/dev/null 2>&1 || true
    [[ -n "${WATCH:-}" ]] && kill "$WATCH" 2>/dev/null || true
}
trap cleanup EXIT

cd "$WD"
umask 077
mkdir -p pki www bin ctl
# --- 使い捨ての CA (Redis と置き場の両方に 127.0.0.1 の証明書) ---
openssl req -x509 -newkey rsa:2048 -nodes -keyout pki/ca.key -out pki/ca.crt -days 2 -subj "/CN=trial-ca" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -keyout pki/srv.key -out pki/srv.csr -subj "/CN=127.0.0.1" 2>/dev/null
printf "subjectAltName=IP:127.0.0.1\n" > pki/ext.cnf
openssl x509 -req -in pki/srv.csr -CA pki/ca.crt -CAkey pki/ca.key -CAcreateserial -out pki/srv.crt -days 2 -extfile pki/ext.cnf 2>/dev/null
openssl rand -hex 24 > pki/worker.pw
openssl rand -hex 24 > pki/admin.pw
# Worker の利用者の規則 (本番の ACL と同じ形。名前は試運転用)
echo 'on -@all +ping +client|setname +get +set +setex +del ~heartbeat:w1 (+blpop +del ~tasks:w1) (+setex %W~results:w1:*) (+sadd +sismember ~workers:pending) (+sismember %R~workers:approved) (+hset %W~worker_versions %W~worker_platforms) (+get +sismember +smembers %R~swim:latest_worker_version %R~swim:latest_worker_version_staged %R~swim:parse_enabled %R~swim:parse_disabled_workers:* %R~swim:auto_update_enabled %R~swim:auto_update_whitelist)' > pki/rules.txt
printf 'user default off\nuser admin on >%s ~* &* +@all\nuser worker-w1 %s >%s\n' \
    "$(cat pki/admin.pw)" "$(cat pki/rules.txt)" "$(cat pki/worker.pw)" > pki/users.acl
printf 'port 0\ntls-port 6390\nbind 127.0.0.1\ntls-cert-file /t/pki/srv.crt\ntls-key-file /t/pki/srv.key\ntls-ca-cert-file /t/pki/ca.crt\ntls-auth-clients no\naclfile /t/pki/users.acl\nsave ""\nappendonly no\n' > pki/redis.conf
# Redis のコンテナの利用者が読めるように (使い捨て)
chmod 711 "$WD" pki; chmod 644 pki/*

# --- リリースと同じ形の置き場 (scripts の写しを差し替えてから署名) ---
rm -rf scripts && cp -r scripts.orig scripts
openssl genpkey -algorithm ed25519 -out pki/release.key 2>/dev/null
openssl pkey -in pki/release.key -pubout -out pki/release.pub 2>/dev/null
for f in scripts/install.sh scripts/swim-worker-update.sh; do
    bash ./patch_install.sh "$f" pki/release.pub https://127.0.0.1:8443
done
cp "$BIN" bin/swim-worker-trial
printf '#!/bin/sh\necho "壊れた版 (試運転)" >&2\nexit 1\n' > bin/broken; chmod 755 bin/*
bash ./make_release.sh 1.3.2 bin/broken amd64 scripts www pki/release.key
bash ./make_release.sh 1.3.1 bin/swim-worker-trial amd64 scripts www pki/release.key
bash ./make_release.sh 1.3.0 bin/swim-worker-trial amd64 scripts www pki/release.key   # latest.json は 1.3.0
chmod -R a+rX www scripts bin

# --- systemd の入ったイメージ ---
$DOCKER image inspect ubuntu:22.04 >/dev/null 2>&1 || PULLED_UBUNTU=1
$DOCKER build -q -t "$IMG" - >/dev/null <<'DEOF'
FROM ubuntu:22.04
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      systemd systemd-sysv dbus curl ca-certificates openssl python3 python3-redis python3-pip util-linux passwd \
    && pip3 install -q zstandard==0.23.0 && rm -rf /var/lib/apt/lists/*
CMD ["/sbin/init"]
DEOF
$DOCKER run -d --name "$C" --privileged --cgroupns=private --tmpfs /run --tmpfs /run/lock \
    -v "$WD":/t "$IMG" >/dev/null
$DOCKER run -d --name "$RC" --network "container:$C" -v "$WD":/t:ro redis:7 redis-server /t/pki/redis.conf >/dev/null
for _ in $(seq 1 30); do
    $DOCKER exec "$C" systemctl is-system-running 2>/dev/null | grep -qE "running|degraded" && break; sleep 1
done

# --- 中の手順から Redis を止める・戻すための合図 (ctl/ のファイル) ---
(
    while sleep 1; do
        for a in redis-stop redis-start redis-pause redis-unpause; do
            [[ -f ctl/$a ]] || continue
            rm -f "ctl/$a"
            case $a in
                redis-stop)    $DOCKER stop -t 1 "$RC" >/dev/null ;;
                redis-start)   $DOCKER start "$RC" >/dev/null ;;
                redis-pause)   $DOCKER pause "$RC" >/dev/null ;;
                redis-unpause) $DOCKER unpause "$RC" >/dev/null ;;
            esac
        done
    done
) &
WATCH=$!
chmod 777 ctl

rc=0
$DOCKER exec "$C" bash /t/inner.sh || rc=$?
echo "inner.sh の終了コード: $rc"
exit "$rc"
