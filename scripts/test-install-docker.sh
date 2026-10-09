#!/usr/bin/env bash
# install.sh を Docker コンテナで E2E 検証するスクリプト。
#
# やること:
#   1. ubuntu:22.04 ベースの systemd 付きコンテナを起動
#   2. 最新 release (GitHub の releases/latest) のタグから install.sh を DL
#   3. ダミー .env を食わせて install.sh 実行
#   4. 各ステップの成否を確認
#      - 専用ユーザー作成
#      - バイナリ配置 + permission
#      - .env 権限 0600 chown swim-worker
#      - systemd unit / timer 配置 + enable
#      - .version ファイル
#   5. install.sh --auto のバージョン比較早期 exit を検証
#   6. コンテナ破棄
#
# 実 Redis 接続はしない (Worker 起動まではしない)。
# 実バイナリ差し替え/ロールバックのテストは実機で。
#
# 使い方:
#   bash scripts/test-install-docker.sh                   # 最新 release (prerelease 含まず)
#   RELEASE=v1.2.3 bash scripts/test-install-docker.sh      # 特定 release 指定

set -euo pipefail

# 既定は GitHub の最新リリース (releases/latest のリダイレクト先のタグ)。固定の版を書かない
if [[ -z "${RELEASE:-}" ]]; then
    RELEASE=$(curl -fsSI -o /dev/null -w '%{redirect_url}' \
        https://github.com/Meku-30/swim-worker/releases/latest | sed -n 's#.*/releases/tag/##p')
fi
if [[ ! "$RELEASE" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]]; then
    echo "リリースのタグを決められません ('${RELEASE}')。RELEASE=vX.Y.Z で指定してください" >&2
    exit 1
fi
RELEASE_VERSION="${RELEASE#v}"
IMAGE="ubuntu:22.04"
CONTAINER_NAME="swim-worker-install-test"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}OK${NC} $1"; }
fail() { echo -e "  ${RED}NG${NC} $1"; FAILURES=$((FAILURES + 1)); }
step() { echo -e "\n${YELLOW}=== $1 ===${NC}"; }

FAILURES=0

# 既存コンテナクリーンアップ
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

step "1. ubuntu:22.04 コンテナ起動"
# systemd-less で検証。install.sh の systemd 操作部分は docker 上で
# systemctl not-running を返すため、そこは graceful に通るか確認する
docker run -d --name "$CONTAINER_NAME" \
    "$IMAGE" sleep 3600 >/dev/null
ok "コンテナ起動"

step "2. 必要パッケージインストール"
docker exec "$CONTAINER_NAME" bash -c '\
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update -qq >/dev/null 2>&1 && \
    apt-get install -y -qq curl ca-certificates openssl systemd python3 >/dev/null 2>&1 \
' && ok "curl / systemd / python3 導入"

step "3. install.sh を release から DL → 構文チェック"
docker exec "$CONTAINER_NAME" bash -c "\
    curl -fsSL https://github.com/Meku-30/swim-worker/releases/download/${RELEASE}/install.sh \
        -o /tmp/install.sh && \
    bash -n /tmp/install.sh && \
    echo '  DL + 構文 OK' \
" && ok "install.sh DL + syntax OK"

step "4. install.sh を非対話モードで実行"
# systemd がコンテナ内で動かないため、systemctl 呼び出しは
# 「Failed to connect to bus」エラーになる。--auto モードでない
# 通常モードでは systemctl daemon-reload / enable が失敗する可能性あり。
# これは期待動作 (実機にはあり、Docker にはない)。
# 通常 install.sh を実行して、systemctl 失敗までは許容し、
# その手前 (バイナリ配置 / .env 生成 / .version 書き込み) が通ったか確認。
docker exec "$CONTAINER_NAME" bash -c '\
    REDIS_HOST=test.example.com \
    REDIS_PASSWORD=testpass \
    SWIM_USERNAME=testuser \
    SWIM_PASSWORD=testsecret \
    WORKER_NAME=docker-test \
    bash /tmp/install.sh 2>&1 \
' | tail -40 || true  # systemctl 失敗で exit > 0 になる可能性

step "5. インストール結果を検証"

# 5a. 専用ユーザー作成
if docker exec "$CONTAINER_NAME" id swim-worker >/dev/null 2>&1; then
    ok "swim-worker ユーザー作成"
else
    fail "swim-worker ユーザーが存在しない"
fi

# 5b. バイナリ配置 + permission
BIN_STAT=$(docker exec "$CONTAINER_NAME" stat -c "%a %U %G" /opt/swim-worker/swim-worker 2>/dev/null || echo "")
if [[ "$BIN_STAT" == "755 root root" ]]; then
    ok "バイナリ permission: $BIN_STAT"
else
    fail "バイナリ permission が不正: '$BIN_STAT' (期待: 755 root root)"
fi

# 5b'. /opt/swim-worker は root、data/ だけサービスユーザー
DIR_STAT=$(docker exec "$CONTAINER_NAME" stat -c "%U" /opt/swim-worker 2>/dev/null || echo "")
DATA_STAT=$(docker exec "$CONTAINER_NAME" stat -c "%a %U" /opt/swim-worker/data 2>/dev/null || echo "")
if [[ "$DIR_STAT" == "root" && "$DATA_STAT" == "750 swim-worker" ]]; then
    ok "所有者: /opt/swim-worker=$DIR_STAT, data=$DATA_STAT"
else
    fail "所有者が不正: /opt/swim-worker='$DIR_STAT' data='$DATA_STAT' (期待: root / 750 swim-worker)"
fi

# 5c. .env 権限 640 root:swim-worker (Worker は読むだけ)
ENV_STAT=$(docker exec "$CONTAINER_NAME" stat -c "%a %U %G" /opt/swim-worker/.env 2>/dev/null || echo "")
if [[ "$ENV_STAT" == "640 root swim-worker" ]]; then
    ok ".env permission: $ENV_STAT"
else
    fail ".env permission が不正: '$ENV_STAT' (期待: 640 root swim-worker)"
fi

# 5d. .env の中身が期待通り (単引用符で囲まれている)
if docker exec "$CONTAINER_NAME" grep -q "^SWIM_PASSWORD='testsecret'$" /opt/swim-worker/.env; then
    ok ".env 値が単引用符で囲まれている"
else
    fail ".env の値が単引用符で囲まれていない"
    docker exec "$CONTAINER_NAME" cat /opt/swim-worker/.env
fi

# 5e. .version ファイル
VERSION=$(docker exec "$CONTAINER_NAME" cat /opt/swim-worker/.version 2>/dev/null || echo "")
if [[ -n "$VERSION" ]]; then
    ok ".version = $VERSION"
else
    fail ".version が書き込まれていない"
fi

# 5f. systemd unit ファイルが配置されている (enable は失敗してもファイル自体は配置されるはず)
for unit in swim-worker.service swim-worker-update.service swim-worker-update.timer; do
    if docker exec "$CONTAINER_NAME" test -f "/etc/systemd/system/$unit"; then
        ok "$unit 配置済み"
    else
        fail "$unit が /etc/systemd/system/ にない"
    fi
done

# 5g. 名前解決・Redis への通信を許可する drop-in
if docker exec "$CONTAINER_NAME" grep -q '^\[Service\]' /etc/systemd/system/swim-worker.service.d/10-ip-allow.conf; then
    ok "drop-in 10-ip-allow.conf 配置済み"
else
    fail "drop-in 10-ip-allow.conf がない"
fi

step "6. 固定の更新スクリプトのバージョン比較早期 exit 検証"
# install.sh が置いた /usr/local/libexec/swim-worker/update.sh (署名を確かめる更新スクリプト)。
# .version と /releases/latest のタグが同じ → 早期 exit (何も起きない)
# (RELEASE が最新でないときは「最新版です」にならないので、既定の最新版で走らせること)
OUT=$(docker exec -e V="$RELEASE_VERSION" "$CONTAINER_NAME" bash -c '\
    echo "$V" > /opt/swim-worker/.version; \
    /usr/local/libexec/swim-worker/update.sh 2>&1 | tail -3 \
')
echo "$OUT"
if echo "$OUT" | grep -q "最新版です"; then
    ok "更新スクリプトが現行==最新で早期 exit"
else
    fail "更新スクリプトの早期 exit が期待通りでない (署名付きリリース v1.2.2 以降で走らせること)"
    echo "$OUT"
fi

step "7. クリーンアップ"
docker rm -f "$CONTAINER_NAME" >/dev/null
ok "コンテナ削除"

echo
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${GREEN}全テストパス${NC}"
    exit 0
else
    echo -e "${RED}$FAILURES 件失敗${NC}"
    exit 1
fi
