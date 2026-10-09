#!/usr/bin/env bash
# swim-worker の自動更新 (固定の更新スクリプト)
#
# install.sh が /usr/local/libexec/swim-worker/update.sh (root:root 0755) に置き、
# swim-worker-update.service (timer から 6h ごと) が root で実行する。
# リリースの公開鍵を埋め込んであり、GitHub から取ったものは署名を確かめてからしか実行しない。
#
# 流れ (どれかで止まれば何もしない):
#   1. ローカルの opt-out (/opt/swim-worker/.no-auto-update)
#   2. 最新 stable のタグを取り、今の版 (.version) と比べる (同じ・ダウングレード・前回
#      ロールバックした版なら終わり)
#   3. Coordinator の一時停止 (kill switch)・段階配布 (whitelist) を Redis に聞く
#      (埋め込みの CA でサーバー証明書を検証する)。認証に失敗したら GitHub の最新版で続ける
#   4. メジャー版の変更は手動 (sudo bash install.sh) に任せる
#   5. そのタグの SHA256SUMS と SHA256SUMS.sig を取り、埋め込みの公開鍵のどれかで署名を確かめ、
#      署名された先頭行 `# swim-worker-release <タグ>` がそのタグと一致することを確かめる
#   6. install.sh を取り、ハッシュを SHA256SUMS で確かめる
#   7. SWIM_UPDATE_TAG=<タグ> bash install.sh --auto (バイナリ・この更新スクリプト・unit の
#      差し替え、再起動、起動確認、ロールバック)
#
# ダウンロードの前に 1〜4 を確かめるので、止めている間は GitHub にも取りに行かない。
#
# `update.sh --guard-only`: 3 (一時停止・段階配布) だけを確かめ、通れば 0、止めるなら 3 で終わる。
# 旧方式から移る install.sh が、署名を確かめたこのスクリプトを一時ファイルに置いて使う
# (判定を install.sh に二重に持たない)。opt-out は見ない (移行は opt-out の機でも行う)。
#
# 手で走らせる: sudo systemctl start swim-worker-update.service
#              sudo journalctl -u swim-worker-update.service -n 30

set -euo pipefail
umask 022

REPO="Meku-30/swim-worker"
INSTALL_DIR="/opt/swim-worker"
VERSION_FILE="${INSTALL_DIR}/.version"
FAILED_VERSION_FILE="${INSTALL_DIR}/.failed-version"
LATEST_API_URL="https://api.github.com/repos/${REPO}/releases/latest"
DOWNLOAD_BASE="https://github.com/${REPO}/releases/download"

# Redis TLS 用 CA 証明書 (swim_worker/certs.py の CA_CERT_PEM と同一。公開鍵なので秘匿不要)。
# kill switch の確認で Redis に AUTH (パスワード送信) するため、
# 証明書検証なし (CERT_NONE) では MITM でパスワードを窃取されうる。必ず検証する。
# tests/test_update_script.py が certs.py との一致を検証している。
read -r -d '' REDIS_CA_PEM <<'CAEOF' || true
-----BEGIN CERTIFICATE-----
MIIFETCCAvmgAwIBAgIUa37Dpr6cCRedc6aFqB0Lxita18UwDQYJKoZIhvcNAQEL
BQAwGDEWMBQGA1UEAwwNc3dpbS1yZWRpcy1jYTAeFw0yNjAzMzEyMTA2MzdaFw0z
NjAzMjgyMTA2MzdaMBgxFjAUBgNVBAMMDXN3aW0tcmVkaXMtY2EwggIiMA0GCSqG
SIb3DQEBAQUAA4ICDwAwggIKAoICAQCRhkoOXWg0ewc/HFxp59EO1nws/g6x+czH
Vbclrwiu5rty1AYcZs7OggqDAi+Uju7eJTvQxhWE2uOk3yYYWT3VcJsD3nblZAuA
i6gi6rIOM47fweVyUAyuFRdGibCTqqvvRye5SQxG6QJa4PZTl/GeAz90MqThES50
jkhSe2esA5TRGNTJI8yshD/JVjCRdu6sPuzK1X9LwDcAJqKCTrPtnAxU0j53ub/r
8gORWwgxFhiY8eRK5TMmENeqcplntx69DC4RenxqnxA8vaF3R40Vsqmufpfvvxph
KlEtzWXCeXznnOTkTnVejVir0gvzQjETcnXp4oQJyEgvBv6DGGJojlbWhlcwMpnY
aLsE74Uq+nS27vlvH1UZlyc++TACqbCvYm9bwVJkUeVcMJRqp3zzXHXmTDqWB7YY
CbPQNuXIwjEiTpEm3SykqaFQhlxEFjpB0u6rQGfWEwB8pF5SiYOruam0rz8x2M6i
jQ9KcOOeo8eKuV1UDwM7P9bCt0EMr3Vd51ttancdWk+GG4YmSf1gHZwmbJuJpBCB
YAiHFptliSRT0IvQ0haILvCz7Fc06g5YSFYGtcFP4UBdbh2yTnsrw78qHnjeb9vi
BszxexdElFAk5xaG1WKl0VYs1FrWdVcngi2BRkS/zSCjILOeBfSxLBGgEGgFv8Gd
GUzuycDUnwIDAQABo1MwUTAdBgNVHQ4EFgQUngQHZ7JSi0eCv0gsvpKwu9p332Qw
HwYDVR0jBBgwFoAUngQHZ7JSi0eCv0gsvpKwu9p332QwDwYDVR0TAQH/BAUwAwEB
/zANBgkqhkiG9w0BAQsFAAOCAgEAEFRbB+Pe1CGzR1kNNgpw2j/OOitB5hm03GhH
W6as1nEaizQxGX+GV5N70yvLYef+ig43iSq7ved04/mCQONCnMD3Og0OGExmOOJ/
ffs0m8c5jLo3Zlvesk2O5iyQPqvYUYT2DnZvZTKc0MW+ab4vsIonpe2GlWZm2kOq
7ryXA+xjuZNXJVeEj9XWnQ6ZxFdv1U2S7c44mGETk571At6qasa24DONNwC/9omB
6cvdm1b28+sxVVZgFC4oZYQIKX0k9emGONcE47R7NKi3ku63vpzqV+uh6+94yzSt
a8QzXEsp2W4bVdEJc6asAI6ATLVn2ULTWdcuJHURLeHj+hcR7N240Z0uMSKqriGx
nnDgs6iUFEq3EGsxl90HqYO0KfabH8mFXVd2sXLBKJxb8Bq6+OcA9cFfgRaFxmrP
y0sZG+mz7jURCrpoijb5qqMGKMwL5b82A9A8BNbuysoo5bICY50PVG4zwOvB6kBT
Wzt7/195TDXVypH9M7DDnMD0XPrsrxQ1ce9Eg7jWdhMe7dPzz0lm9kJefFPyLgiO
HY7SsUQX4NBMjof3S6Cg7+bzJtjiQazJNuJULHslGWd9gbwd9X3k0UGjgeMOMESj
bmKpFYjCVWnobdviueeior9ma52p387KUSydPkArU3gY0UTVBNG/yk/1x1351Ql7
U2NY60E=
-----END CERTIFICATE-----
CAEOF
# リリース (SHA256SUMS) の署名を確かめる Ed25519 公開鍵 (ふだんは 1 本、鍵の入れ替え中だけ 2 本まで)。
# scripts/set-release-pubkeys.sh が scripts/release_pubkeys/*.pub.pem から書く。手で編集しない。
# install.sh・swim_worker/release_keys.py と同じであることを tests/test_release_pubkeys.py が確かめる
# BEGIN RELEASE PUBKEYS
read -r -d '' RELEASE_PUBKEYS_PEM <<'PUBKEYEOF' || true
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAZ+6EShmEUwozcwVIWHNfkMdw4JvziE15STc5ZJ7yuLk=
-----END PUBLIC KEY-----
PUBKEYEOF
# END RELEASE PUBKEYS

log()  { echo "==> $*"; }
warn() { echo "!!  $*"; }
die()  { echo "ERR $*" >&2; exit 1; }

# --- ヘルパー関数 (install.sh と同じ。tests/test_update_script.py が一致を確かめる) ---

# GitHub API から tag_name を取得 (python3 の json.load で確実にパース)
# 使い方: tag=$(fetch_latest_tag "$API_URL")
fetch_latest_tag() {
    local url="$1"
    curl -fsSL --proto '=https' --tlsv1.2 "$url" \
        | python3 -c 'import json, sys; d = json.load(sys.stdin); print(d.get("tag_name", ""))' \
        2>/dev/null || true
}

# タグの形式を確認する
#   strict: vX.Y.Z だけ (最新 stable・自動更新)
#   manual: RELEASE_TAG 用。vX.Y.Z-rc1 のような prerelease も可
# 使い方: validate_tag <tag> <strict|manual>
validate_tag() {
    local tag="$1" mode="${2:-strict}"
    if [[ "$mode" == "strict" ]]; then
        [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]
    else
        [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]]
    fi
}

# `openssl version` の出力が OpenSSL 3.0 以上か (Ed25519 で SHA256SUMS をそのまま確かめる
# `pkeyutl -rawin` は 3.0 から。1.1.1 には無い)
# 使い方: openssl_version_ok "OpenSSL 3.0.13 30 Jan 2024"
openssl_version_ok() {
    local v="$1"
    [[ "$v" =~ ^OpenSSL\ ([0-9]+)\.([0-9]+)\.([0-9]+) ]] || return 1
    (( BASH_REMATCH[1] >= 3 ))
}

require_openssl() {
    command -v openssl >/dev/null || die "openssl が必要です (apt install -y openssl)。更新の署名を確かめるのに使います"
    local v
    v=$(openssl version 2>/dev/null || true)
    openssl_version_ok "$v" \
        || die "OpenSSL 3.0 以上が必要です (更新の署名の検証に openssl pkeyutl -rawin を使います)。今: ${v:-不明}。Debian 12・Ubuntu 22.04・Raspberry Pi OS Bookworm 以降なら標準で入っています。古い OS は OS の更新が必要です"
}

# SHA256SUMS の署名を、埋め込みの公開鍵 (RELEASE_PUBKEYS_PEM) のどれかで確かめる。
# 署名が無い・長さが違う・公開鍵が未設定・どの鍵でも通らない、はすべて失敗 (1)
# 使い方: verify_sums_signature <SHA256SUMS> <SHA256SUMS.sig>
verify_sums_signature() {
    local sums="$1" sig="$2" keydir key nkeys=0 rc=1
    if [[ ! -s "$sig" ]]; then
        echo "SHA256SUMS.sig がありません (署名のないリリース)" >&2
        return 1
    fi
    if [[ "$(wc -c < "$sig")" -ne 64 ]]; then
        echo "SHA256SUMS.sig の長さが不正です (Ed25519 の署名は 64 バイト)" >&2
        return 1
    fi
    keydir=$(mktemp -d)
    # 埋め込みの PEM を 1 本ずつファイルに分ける
    awk -v dir="$keydir" '
        /^-----BEGIN PUBLIC KEY-----$/ { n++; f = dir "/key" n ".pem" }
        f != "" { print > f }
        /^-----END PUBLIC KEY-----$/ { if (f != "") close(f); f = "" }
    ' <<< "${RELEASE_PUBKEYS_PEM:-}"
    for key in "$keydir"/key*.pem; do
        [[ -f "$key" ]] || continue
        nkeys=$((nkeys + 1))
        if openssl pkeyutl -verify -pubin -inkey "$key" -rawin \
                -in "$sums" -sigfile "$sig" >/dev/null 2>&1; then
            rc=0
            break
        fi
    done
    rm -rf "$keydir"
    if (( nkeys == 0 )); then
        echo "署名を確かめる公開鍵が埋め込まれていません (この版では更新できません)" >&2
        return 1
    fi
    if (( rc != 0 )); then
        echo "SHA256SUMS の署名を確かめられません (改ざん、または知らない鍵の署名)" >&2
    fi
    return "$rc"
}

# 署名した SHA256SUMS の先頭行が `# swim-worker-release <タグ>` と完全に一致するか。
# 署名を版に結び付ける (古い版の署名済みファイルを新しいタグとして出すリプレイを防ぐ)
# 使い方: check_release_line <SHA256SUMS> <tag>
check_release_line() {
    local first=""
    IFS= read -r first < "$1" || true
    if [[ "$first" != "# swim-worker-release $2" ]]; then
        echo "SHA256SUMS の版の行が $2 と一致しません。別の版のファイルの可能性があります" >&2
        return 1
    fi
}

# Coordinator の一時停止 (kill switch)・段階配布 (whitelist) を Redis に聞く。
# 標準の python3 + ssl で Redis に直接問い合わせる (Worker のバイナリには頼らない)。
# install.sh の移行 (旧方式から) も --guard-only でこれを使う (判定を二重に持たない)
# 戻り値: 0 = 通過、2 = Redis の認証に失敗 (GitHub の最新版で続ける)、1 = 止める (理由はログ)
coordinator_guard() {
    local result worker_name=""
    if ! command -v python3 >/dev/null; then
        warn "python3 がないため Coordinator のガードを確認できません、安全側で止めます"
        return 1
    fi
    if [[ -r "${INSTALL_DIR}/.env" ]]; then
        worker_name=$(grep -E "^WORKER_NAME=" "${INSTALL_DIR}/.env" 2>/dev/null \
            | head -1 | sed "s/^WORKER_NAME=//; s/^'//; s/'$//" || true)
    fi
    result=$(INSTALL_DIR="$INSTALL_DIR" SWIM_REDIS_CA_PEM="$REDIS_CA_PEM" python3 - <<'PYEOF' 2>/dev/null || echo "ERROR"
import os, re, socket, ssl, sys

# .env を読み込み (install.sh が書いた単引用符形式)
env = {}
try:
    env_path = os.path.join(os.environ.get("INSTALL_DIR", "/opt/swim-worker"), ".env")
    with open(env_path) as f:
        for line in f:
            m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
            if not m:
                continue
            k, v = m.group(1), m.group(2)
            if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
                v = v[1:-1]
            env[k] = v
except Exception:
    print("ERROR:env-unreadable")
    sys.exit(0)

host = env.get("REDIS_HOST", "")
port = int(env.get("REDIS_PORT", "6380"))
username = env.get("REDIS_USERNAME", "")  # ACL のワーカー用ユーザー (空なら default)
password = env.get("REDIS_PASSWORD", "")
worker_name = env.get("WORKER_NAME", "")
if not host:
    print("ERROR:no-host")
    sys.exit(0)

# Redis TLS 接続。AUTH でパスワードを送るため、埋め込み CA でサーバー証明書を
# 検証する (Worker 本体 redis-py と同じ CA・同じ hostname/IP SAN 照合)。
ca_pem = os.environ.get("SWIM_REDIS_CA_PEM", "")
if not ca_pem.strip():
    print("ERROR:no-ca-pem")
    sys.exit(0)
try:
    ctx = ssl.create_default_context(cadata=ca_pem)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    raw = socket.create_connection((host, port), timeout=5)
    sock = ctx.wrap_socket(raw, server_hostname=host)
    sock.settimeout(5)

    def recv_until(delim):
        buf = b""
        while delim not in buf:
            ch = sock.recv(1)
            if not ch:
                return buf
            buf += ch
        return buf

    class RedisError(Exception):
        """Redis -ERR 応答 (AUTH 失敗、コマンドエラー等)"""

    def read_resp():
        """RESP 単一応答 (simple string / error / bulk string) を読む。"""
        head = recv_until(b"\r\n")
        if not head:
            return None
        kind = head[:1]
        body = head[1:-2]
        if kind == b"+":
            return body.decode("utf-8", "replace")
        if kind == b"-":
            # -ERR / -WRONGPASS / -NOAUTH 等の Redis エラー応答
            raise RedisError(body.decode("utf-8", "replace"))
        if kind == b"$":
            n = int(body)
            if n < 0:
                return None
            remaining = n + 2  # data + \r\n
            data = b""
            while len(data) < remaining:
                chunk = sock.recv(remaining - len(data))
                if not chunk:
                    break
                data += chunk
            return data[:-2].decode("utf-8", "replace")
        # 整数 (":") 等は現状不要。未知の応答は None
        return None

    def cmd(*args):
        buf = f"*{len(args)}\r\n".encode()
        for a in args:
            b = a.encode()
            buf += f"${len(b)}\r\n".encode() + b + b"\r\n"
        sock.sendall(buf)
        return read_resp()

    # 認証の失敗・権限不足 (パスワードの作り直し・入力ミス・古い .env) は AUTH_FALLBACK:
    # Redis の一時停止・段階配布の設定を読めない = Coordinator にもつなげず働けていない Worker
    # なので、自動更新を止めずに GitHub の最新版で更新する (直った版が来れば復帰できる)
    try:
        if username:
            cmd("AUTH", username, password)
        else:
            cmd("AUTH", password)
    except RedisError as e:
        print(f"AUTH_FALLBACK:{e}")
        sys.exit(0)

    try:
        enabled   = cmd("GET", "swim:auto_update_enabled")
        whitelist = cmd("GET", "swim:auto_update_whitelist")
    except RedisError as e:
        if str(e).startswith(("NOPERM", "NOAUTH")):
            print(f"AUTH_FALLBACK:{e}")
            sys.exit(0)
        raise
    sock.close()

    if enabled != "true":
        print("DISABLED")
        sys.exit(0)
    if whitelist and whitelist.strip():
        allowed = [x.strip() for x in whitelist.split(",") if x.strip()]
        if worker_name not in allowed:
            print(f"NOT_IN_WHITELIST:{worker_name}")
            sys.exit(0)
    print("OK")
except Exception as e:
    print(f"ERROR:{type(e).__name__}:{e}")
    sys.exit(0)
PYEOF
    )
    case "$result" in
        "DISABLED")
            log "Coordinator kill switch が OFF (自動更新は一時停止中)、スキップ"
            return 1
            ;;
        NOT_IN_WHITELIST:*)
            log "staged rollout whitelist に含まれていない Worker: ${worker_name}、スキップ"
            return 1
            ;;
        AUTH_FALLBACK:*)
            warn "Redis 認証失敗 (${result#AUTH_FALLBACK:}) — .env の REDIS_USERNAME / REDIS_PASSWORD を確認。"
            warn "  Coordinator の一時停止・段階配布の設定を読めないため、GitHub の最新版 (署名を確認) で続けます"
            return 2
            ;;
        ERROR:*)
            warn "Coordinator 疎通確認失敗 (${result#ERROR:})、安全側でスキップ"
            return 1
            ;;
        "OK")
            log "ガード通過"
            return 0
            ;;
        *)
            warn "予期しないガード応答 (${result})、スキップ"
            return 1
            ;;
    esac
}

# --- 事前チェック ---
[[ $EUID -eq 0 ]] || die "root で実行してください"
command -v curl >/dev/null      || die "curl が必要です"
command -v sha256sum >/dev/null || die "sha256sum が必要です"
require_openssl

[[ -f "$VERSION_FILE" ]] || die ".version がありません。通常の install.sh を先に実行してください"
CURRENT_VERSION=$(cat "$VERSION_FILE")
[[ -n "$CURRENT_VERSION" ]] || die ".version が空です"

# --- 移行用: 一時停止・段階配布だけを確かめる ---
# 旧方式 (v1.2.x の update.service) から移る install.sh が、署名を確かめたこのスクリプトを
# 一時ファイルに置いて `--guard-only` で呼ぶ。通れば 0、止めるなら 3。
# opt-out (.no-auto-update) は見ない: 移行 (このスクリプトと unit の差し替え) は opt-out の機でも
# 行い、バイナリを更新しないのはこのスクリプト自身 (下のガード1) が守る
if [[ "${1:-}" == "--guard-only" ]]; then
    guard_rc=0
    coordinator_guard || guard_rc=$?
    # 2 (認証に失敗している Worker) は本来の流れと同じく続ける
    if (( guard_rc == 1 )); then
        exit 3
    fi
    exit 0
fi

# --- ガード1: ローカル opt-out ファイル ---
if [[ -f "${INSTALL_DIR}/.no-auto-update" ]]; then
    log "自動更新が opt-out されています (${INSTALL_DIR}/.no-auto-update)"
    exit 0
fi

# --- 最新版 (常に最新 stable。prerelease は拾わない)。以後のダウンロードはこのタグに固定 ---
command -v python3 >/dev/null || {
    # python3 がないと最新版も kill switch / whitelist も確認できない。安全側で更新しない
    warn "python3 がないため最新版・Coordinator のガードを確認できません、安全側で更新スキップ"
    exit 0
}
LATEST_TAG=$(fetch_latest_tag "$LATEST_API_URL")
validate_tag "$LATEST_TAG" strict || die "GitHub API から最新のタグを取得できません (${LATEST_TAG:-空})"
LATEST_VERSION="${LATEST_TAG#v}"

if [[ "$CURRENT_VERSION" == "$LATEST_VERSION" ]]; then
    log "最新版です (v${CURRENT_VERSION})"
    exit 0
fi

# ダウングレード防止: 現行 > latest なら skip (prerelease 検証中等の保護)
NEWER=$(printf '%s\n%s\n' "$CURRENT_VERSION" "$LATEST_VERSION" | sort -V | tail -1)
if [[ "$NEWER" == "$CURRENT_VERSION" ]]; then
    log "現行 (v${CURRENT_VERSION}) が latest (v${LATEST_VERSION}) より新しい、skip (prerelease 検証中等)"
    exit 0
fi

# --- ガード0: 前回この版でロールバックしていれば再試行しない (手動 install.sh で解除) ---
if [[ -f "$FAILED_VERSION_FILE" ]]; then
    FAILED_VERSION=$(cat "$FAILED_VERSION_FILE")
    if [[ "$FAILED_VERSION" == "$LATEST_VERSION" ]]; then
        log "v${LATEST_VERSION} は前回ロールバックした版のため skip (手動で install.sh を実行すると解除)"
        exit 0
    fi
    rm -f "$FAILED_VERSION_FILE"   # より新しい版が出たので解除
fi

# --- ガード2: Redis kill switch + whitelist (staged rollout) ---
AUTH_BROKEN=0
guard_rc=0
coordinator_guard || guard_rc=$?
case "$guard_rc" in
    0) ;;
    2) AUTH_BROKEN=1 ;;
    *) exit 0 ;;
esac

# --- ガード3: メジャーバージョン変更は手動必須 ---
CUR_MAJOR="${CURRENT_VERSION%%.*}"
NEW_MAJOR="${LATEST_VERSION%%.*}"
if [[ "$CUR_MAJOR" != "$NEW_MAJOR" ]]; then
    warn "メジャーバージョン変更 (${CUR_MAJOR}.x → ${NEW_MAJOR}.x)、自動更新をスキップ"
    warn "手動で sudo bash install.sh を実行してください"
    exit 0
fi

log "自動更新: v${CURRENT_VERSION} → v${LATEST_VERSION}"

# --- 署名を確かめた install.sh を取る (調べたタグに固定) ---
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
BASE="${DOWNLOAD_BASE}/${LATEST_TAG}"
for f in SHA256SUMS SHA256SUMS.sig; do
    curl -fsSL --proto '=https' --tlsv1.2 --max-filesize 65536 -o "${WORK}/${f}" "${BASE}/${f}" \
        || die "ダウンロード失敗: ${f} (署名のないリリースには更新しません)"
done
verify_sums_signature "${WORK}/SHA256SUMS" "${WORK}/SHA256SUMS.sig" \
    || die "v${LATEST_VERSION} の署名を確かめられないため更新しません"
# 署名された版の行が取りに行ったタグと一致すること (今の版より新しいことは上で確かめた)
check_release_line "${WORK}/SHA256SUMS" "$LATEST_TAG" \
    || die "v${LATEST_VERSION} の SHA256SUMS が別の版のものなので更新しません"
log "SHA256SUMS の署名 OK (${LATEST_TAG})"

curl -fsSL --proto '=https' --tlsv1.2 -o "${WORK}/install.sh" "${BASE}/install.sh" \
    || die "ダウンロード失敗: install.sh"
expected=$(awk '$2 == "install.sh" || $2 == "*install.sh" { print $1; exit }' "${WORK}/SHA256SUMS")
[[ -n "$expected" ]] || die "SHA256SUMS に install.sh のエントリがありません"
actual=$(sha256sum "${WORK}/install.sh" | awk '{print $1}')
[[ "$expected" == "$actual" ]] || die "SHA256 不一致: install.sh (expected=${expected}, actual=${actual})"

# install.sh がバイナリ・この更新スクリプト・unit を (同じタグ・同じ検証で) 差し替え、
# 再起動して起動を確かめる。起動しなければロールバック (exit 1)
rc=0
SWIM_UPDATE_TAG="$LATEST_TAG" SWIM_UPDATE_AUTH_BROKEN="$AUTH_BROKEN" \
    bash "${WORK}/install.sh" --auto || rc=$?
exit "$rc"
