#!/usr/bin/env bash
# 本物の GitHub のリリース (署名つき) で入れる・入れ替える試運転。run.sh の中で INNER=real_inner.sh として動かす。
# 取るのは GitHub の公開のリリースだけ (SWIM には繋がない。Worker は試運転のビルドでないので SWIM のログインは
# 失敗するが、偽の Coordinator から見えるハートビート・版・結果のエラーまでで確かめる)
# 場面: R1 rc を新しく入れる / R2 今の正式版 (Python) を入れてから、rc を手で入れ直す (自動更新は rc を断ることも確かめる)
set -uo pipefail
cd /t || exit 1
export FAKE_REDIS_HOST=127.0.0.1 FAKE_REDIS_PORT=6390 FAKE_REDIS_USER=admin FAKE_REDIS_PASSWORD_FILE=/t/pki/admin.pw FAKE_REDIS_CA=/t/pki/ca.crt
RC_TAG="${RC_TAG:-v1.3.0-rc1}"
OLD_TAG="${OLD_TAG:-v1.2.2}"
GH=https://github.com/Meku-30/swim-worker/releases/download
W=/opt/swim-worker
R=0
ok() { echo "OK  $*"; }
ng() { echo "NG  $*"; R=1; }
wait_marker() {
    local i; for i in $(seq 1 120); do [[ -f $W/data/.startup_ok ]] && { echo "$i"; return 0; }; sleep 1; done; return 1
}
fresh_install() {  # <タグ> <ログ>
    curl -fsSLo /t/install-$1.sh "$GH/$1/install.sh" || return 1
    local wpw; wpw=$(cat /t/pki/worker.pw)
    REDIS_HOST=127.0.0.1 REDIS_PORT=6390 REDIS_USERNAME=worker-w1 REDIS_PASSWORD="$wpw" \
    SWIM_USERNAME=dummy-user SWIM_PASSWORD=dummy-pass WORKER_NAME=w1 RELEASE_TAG="$1" \
        bash /t/install-$1.sh > "$2" 2>&1
}
use_trial_ca() {
    install -m 0644 /t/pki/ca.crt $W/trial-ca.crt
    grep -q '^REDIS_CA_CERT=' $W/.env || echo "REDIS_CA_CERT=$W/trial-ca.crt" >> $W/.env
}
purge() {
    systemctl stop swim-worker swim-worker-update.timer 2>/dev/null
    rm -rf $W /usr/local/libexec/swim-worker /etc/systemd/system/swim-worker*
    systemctl daemon-reload
}
coord() {  # 偽の Coordinator に 60 秒タスクを積み、版・結果を見る
    python3 /t/fake_coordinator.py w1 60 5 > "$1" 2>&1; cat "$1"
}

echo "== R1 $RC_TAG を新しく入れる (本物の配布物・本物の署名)"
fresh_install "$RC_TAG" /t/R1-install.log && ok "install.sh" || { ng "install.sh"; tail -20 /t/R1-install.log; }
grep -E "署名|SHA256" /t/R1-install.log | head -3
use_trial_ca
systemctl start swim-worker
if s=$(wait_marker); then ok "起動の印 ${s} 秒"; else ng "起動の印が 120 秒で出ない"; fi
[[ "$(cat $W/.version)" == "${RC_TAG#v}" ]] && ok ".version ${RC_TAG#v}" || ng ".version $(cat $W/.version)"
echo "実行ファイル: $(du -h $W/swim-worker | cut -f1)"
coord /t/R1-coord.json
python3 -c 'import json,sys; d=json.load(open("/t/R1-coord.json")); sys.exit(0 if d["heartbeat"] and d["version"]==sys.argv[1] else 1)' "${RC_TAG#v}" \
    && ok "ハートビートと版の報告" || ng "ハートビート・版"
echo "RSS(KB): $(awk '/VmRSS/{print $2}' "/proc/$(systemctl show -p MainPID --value swim-worker)/status")"
purge

echo "== R2 $OLD_TAG (Python) を入れてから $RC_TAG に入れ替える"
fresh_install "$OLD_TAG" /t/R2-old.log && ok "$OLD_TAG の install.sh" || { ng "$OLD_TAG の install.sh"; tail -20 /t/R2-old.log; }
use_trial_ca
systemctl start swim-worker
if s=$(wait_marker); then ok "$OLD_TAG の起動の印 ${s} 秒"; else ng "$OLD_TAG の起動の印"; fi
coord /t/R2-old-coord.json
echo "旧版の RSS(KB) (本体): $(ps -o rss= -u swim-worker | sort -n | tail -1)"
cp $W/.env /t/env-before
# 自動更新 (--auto) は正式版 (vX.Y.Z) だけを受け付けるので (rc は SWIM_UPDATE_TAG の形式で断る。仕組みどおり)、
# rc は手で入れ直す (RELEASE_TAG)。設定はそのまま残る
curl -fsSLo /tmp/rc-install.sh "$GH/$RC_TAG/install.sh"
rm -f $W/data/.startup_ok
SWIM_UPDATE_TAG="$RC_TAG" bash /tmp/rc-install.sh --auto > /t/R2-auto.log 2>&1 && ng "--auto が rc を受け付けた" || ok "--auto は rc を断る ($(tail -1 /t/R2-auto.log))"
RELEASE_TAG="$RC_TAG" bash /tmp/rc-install.sh > /t/R2-manual.log 2>&1 && ok "RELEASE_TAG で入れ直し" || { ng "入れ直し"; tail -25 /t/R2-manual.log; }
systemctl is-active --quiet swim-worker || systemctl start swim-worker
if s=$(wait_marker); then ok "入れ替え後の起動の印 ${s} 秒"; else ng "入れ替え後の起動の印"; fi
[[ "$(cat $W/.version)" == "${RC_TAG#v}" ]] && ok ".version ${RC_TAG#v}" || ng ".version $(cat $W/.version)"
[[ ! -f $W/.failed-version ]] && ok ".failed-version なし" || ng ".failed-version $(cat $W/.failed-version)"
cmp -s /t/env-before $W/.env && ok ".env はそのまま" || ng ".env が変わった"
systemctl show -p Environment --value swim-worker | grep -q MALLOC_ARENA_MAX=2 && ok "unit が新しい版に" || ng "unit が古いまま"
[[ "$(systemctl show -p MemoryDenyWriteExecute --value swim-worker)" == yes ]] && ok "W^X の禁止" || ng "W^X の禁止"
systemctl is-active --quiet swim-worker && ok "動いている" || ng "動いていない"
coord /t/R2-new-coord.json
python3 -c 'import json,sys; d=json.load(open("/t/R2-new-coord.json")); sys.exit(0 if d["heartbeat"] and d["version"]==sys.argv[1] else 1)' "${RC_TAG#v}" \
    && ok "入れ替え後のハートビートと版の報告" || ng "入れ替え後のハートビート・版"
journalctl -u swim-worker --no-pager -o cat | grep -E "SECCOMP|Bad system call|panicked" | head -3
echo "RSS(KB): $(awk '/VmRSS/{print $2}' "/proc/$(systemctl show -p MainPID --value swim-worker)/status")"
echo "== 終わり"
exit $R
