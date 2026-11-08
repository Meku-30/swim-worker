#!/usr/bin/env bash
# 比べる道具の中身 (Worker のコンテナの中で動かす)。/t に道具一式、/t/repo にリポの写し、/t/bin/swim-worker-trial
# Redis (TLS) は同じネットワークの別のコンテナ (127.0.0.1:6390)。パスワードはファイルから読み、外に出さない
set -uo pipefail
cd /t || exit 1
export FAKE_REDIS_HOST=127.0.0.1 FAKE_REDIS_PORT=6390 FAKE_REDIS_USER=cmp FAKE_REDIS_PASSWORD_FILE=/t/pki/cmp.pw FAKE_REDIS_CA=/t/pki/ca.crt
export SWIM_TRIAL_RESPONSES=/t/tasks.json
for n in py rs; do
    mkdir -p /w/$n
    {
        echo "REDIS_HOST=127.0.0.1"; echo "REDIS_PORT=6390"; echo "REDIS_USERNAME=cmp"
        printf 'REDIS_%s=%s\n' PASSWORD "$(cat /t/pki/cmp.pw)"; echo "REDIS_CA_CERT=/t/pki/ca.crt"
        echo "SWIM_USERNAME=dummy"; printf 'SWIM_%s=dummy\n' PASSWORD; echo "WORKER_NAME=cmp-$n"
        # 待ちを短く (両方同じ)
        echo "REQUEST_DELAY_MEDIAN=0.3"; echo "REQUEST_DELAY_P99=0.6"; echo "TASK_HARD_TIMEOUT=45"
    } > /w/$n/.env
    chmod 600 /w/$n/.env
done
python3 /t/seed.py setup cmp-py cmp-rs || exit 1
(cd /w/py && exec python3 /t/repo/tools/compare/fake_swim.py > /t/py.log 2>&1) &
RS=(/t/bin/swim-worker-trial)
[[ -x /t/qemu/qemu-arm-static ]] && RS=(/t/qemu/qemu-arm-static -cpu arm1176 -L /t/qemu/root /t/bin/swim-worker-trial)
(cd /w/rs && exec "${RS[@]}" > /t/rs.log 2>&1) &
sleep 5
python3 /t/seed.py run cmp-py cmp-rs /t/py.jsonl /t/rs.jsonl 900
rc=$?
python3 /t/repo/tools/compare/diff_results.py /t/py.jsonl /t/rs.jsonl > /t/diff.txt
d=$?
cat /t/diff.txt
echo "RSS(KB) py=$(ps -o rss= -C python3 | sort -n | tail -1) rs=$(ps -o rss= -C swim-worker-tri,qemu-arm-static | tail -1)"
exit $(( rc | d ))
