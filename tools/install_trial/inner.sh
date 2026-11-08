#!/usr/bin/env bash
# 使い捨ての systemd のコンテナの中で root で動かす。/t に道具一式、/t/bin に実行ファイルがある前提。
# 場面: A 新規 / B Redis が遅れて起きる / C 更新して成功 / D 更新して失敗→巻き戻し / E Redis が止まって戻る / F 権限の一部の欠け
# Redis (TLS・ACL) は同じネットワークの別のコンテナ (127.0.0.1:6390)。パスワードはここで作り、外に出さない
set -uo pipefail
cd /t || exit 1
export FAKE_REDIS_HOST=127.0.0.1 FAKE_REDIS_PORT=6390 FAKE_REDIS_USER=admin FAKE_REDIS_PASSWORD_FILE=/t/pki/admin.pw FAKE_REDIS_CA=/t/pki/ca.crt
W=/opt/swim-worker
R=0
ok() { echo "OK  $*"; }
ng() { echo "NG  $*"; R=1; }
admin() { python3 /t/redis_admin.py "$@"; }
wait_marker() {  # 120 秒以内に .startup_ok が出るか (install.sh と同じ待ち)
    local i; for i in $(seq 1 120); do [[ -f $W/data/.startup_ok ]] && { echo "$i"; return 0; }; sleep 1; done; return 1
}
journal_bad() { journalctl -u swim-worker --no-pager -o cat | grep -E "SECCOMP|Bad system call|EPERM|Read-only file system|Operation not permitted|panicked" | head -3; }

# --- 置き場 (HTTPS、使い捨ての CA をこのコンテナに信頼させる) ---
python3 /t/serve.py /t/www 8443 /t/pki/srv.crt /t/pki/srv.key >/t/serve.log 2>&1 &
cp /t/pki/ca.crt /usr/local/share/ca-certificates/trial-ca.crt && update-ca-certificates >/dev/null 2>&1
sleep 1

echo "== A 新規に入れる"
WPW=$(cat /t/pki/worker.pw)
REDIS_HOST=127.0.0.1 REDIS_PORT=6390 REDIS_USERNAME=worker-w1 REDIS_PASSWORD="$WPW" \
SWIM_USERNAME=dummy-user SWIM_PASSWORD=dummy-pass WORKER_NAME=w1 \
    bash /t/scripts/install.sh > /t/A-install.log 2>&1 && ok "install.sh" || { ng "install.sh"; tail -20 /t/A-install.log; }
unset WPW
install -m 0644 /t/pki/ca.crt $W/trial-ca.crt
echo "REDIS_CA_CERT=$W/trial-ca.crt" >> $W/.env
systemctl start swim-worker
if s=$(wait_marker); then ok "起動の印 ${s} 秒"; else ng "起動の印が 120 秒で出ない"; fi
[[ "$(systemctl show -p MemoryDenyWriteExecute --value swim-worker)" == yes ]] && ok "W^X の禁止" || ng "W^X の禁止"
systemctl show -p Environment --value swim-worker | grep -q MALLOC_ARENA_MAX=2 && ok "MALLOC_ARENA_MAX=2" || ng "MALLOC_ARENA_MAX"
python3 /t/fake_coordinator.py w1 120 3 > /t/A-coord.json 2>&1
cat /t/A-coord.json
python3 -c 'import json,sys; d=json.load(open("/t/A-coord.json")); sys.exit(0 if d["results"]>0 and all(k.startswith(("ok","success")) or "parsed" in k or "raw" in k for k in d["kinds"]) else 1)' \
    && ok "結果が届く" || ng "結果"
b=$(journal_bad); [[ -z "$b" ]] && ok "制限の止め・panic なし" || ng "制限に止められた: $b"
echo "RSS(KB): $(awk '/VmRSS/{print $2}' "/proc/$(systemctl show -p MainPID --value swim-worker)/status")"

echo "== B Redis が遅れて起きる (20 秒)"
touch /t/ctl/redis-stop; sleep 3
rm -f $W/data/.startup_ok
systemctl restart swim-worker
sleep 20; touch /t/ctl/redis-start
if s=$(wait_marker); then ok "起動の印 ${s} 秒 (120 秒以内)"; else ng "Redis が遅れると 120 秒で印が出ない"; fi

echo "== C 更新して成功 (v1.3.1)"
SWIM_UPDATE_TAG=v1.3.1 bash /t/scripts/install.sh --auto > /t/C.log 2>&1 && ok "--auto" || { ng "--auto"; tail -15 /t/C.log; }
[[ "$(cat $W/.version)" == 1.3.1 ]] && ok ".version 1.3.1" || ng ".version $(cat $W/.version)"
[[ ! -f $W/.failed-version ]] && ok ".failed-version なし" || ng ".failed-version がある"

echo "== D 更新して失敗 → 巻き戻し (v1.3.2 はすぐ終わる)"
cp /etc/systemd/system/swim-worker.service /t/unit-before
echo "# trial-marker" >> /etc/systemd/system/swim-worker.service   # 巻き戻しで unit も戻るかの印
cp /etc/systemd/system/swim-worker.service /t/unit-before
SWIM_UPDATE_TAG=v1.3.2 bash /t/scripts/install.sh --auto > /t/D.log 2>&1; rc=$?
[[ $rc -ne 0 ]] && ok "--auto が失敗を返す ($rc)" || ng "--auto が成功を返した"
[[ "$(cat $W/.version)" == 1.3.1 ]] && ok ".version が 1.3.1 に戻る" || ng ".version $(cat $W/.version)"
[[ "$(cat $W/.failed-version 2>/dev/null)" == 1.3.2 ]] && ok ".failed-version 1.3.2" || ng ".failed-version"
cmp -s /t/unit-before /etc/systemd/system/swim-worker.service && ok "unit も戻る" || ng "unit が戻らない"
systemctl is-active --quiet swim-worker && ok "前の版で動いている" || ng "動いていない"

echo "== E Redis が 60 秒止まって戻る"
python3 /t/fake_coordinator.py w1 20 3 > /dev/null 2>&1
touch /t/ctl/redis-pause; sleep 60; touch /t/ctl/redis-unpause; sleep 5
python3 /t/fake_coordinator.py w1 90 3 > /t/E-coord.json 2>&1; cat /t/E-coord.json
python3 -c 'import json,sys; sys.exit(0 if json.load(open("/t/E-coord.json"))["results"]>0 else 1)' && ok "止まったあとも結果が届く" || ng "張り直せない"
systemctl is-active --quiet swim-worker && ok "Worker は動いたまま" || ng "Worker が止まった"

echo "== F 承認の一覧を読む権限がない"
admin drop_approved
python3 /t/fake_coordinator.py w1 60 3 > /t/F-coord.json 2>&1; cat /t/F-coord.json
journalctl -u swim-worker --no-pager -o cat --since "-70s" | grep -E "WARNING|ERROR" | sed -E 's/^[0-9-]+ [0-9:,]+ //' | sort | uniq -c | head -8
admin restore_approved

echo "== 終わり"
journalctl -u swim-worker --no-pager -o cat > /t/worker.log
exit $R
