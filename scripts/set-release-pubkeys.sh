#!/usr/bin/env bash
# リリースの署名を確かめる公開鍵を配布物に埋め込む
#
# 使い方:
#   scripts/set-release-pubkeys.sh <公開鍵.pem> [<2 本目の公開鍵.pem>]
#
#   例: scripts/set-release-pubkeys.sh ~/.config/swim-release/signing-key.pub.pem
#
# 公開鍵 (openssl pkey -pubout の出力、Ed25519) を scripts/release_pubkeys/ に置き直し、
# swim_worker/release_keys.py・scripts/install.sh・scripts/swim-worker-update.sh に埋め込む。
# ふつうは 1 本。2 本目は鍵を入れ替えるときだけ (旧鍵で署名した版で新旧 2 本を配り、
# 次の版から新しい鍵で署名し、その後の版で旧鍵を外す。docs/release-signing.md)。
# 秘密鍵は渡さない (渡すと止まる)。終わったら git diff で確認してコミットする。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_DIR="${ROOT}/scripts/release_pubkeys"

die() { echo "ERR $*" >&2; exit 1; }

(( $# >= 1 && $# <= 2 )) || die "使い方: $0 <公開鍵.pem> [<2 本目の公開鍵.pem>]"
command -v openssl >/dev/null || die "openssl が必要です"

for f in "$@"; do
    [[ -f "$f" ]] || die "ファイルがありません: $f"
    if grep -q "PRIVATE KEY" "$f"; then
        die "$f は秘密鍵です。公開鍵 (openssl pkey -in <秘密鍵> -pubout) を渡してください"
    fi
    openssl pkey -pubin -in "$f" -noout -text 2>/dev/null | head -1 | grep -q '^ED25519 Public-Key' \
        || die "$f は Ed25519 の公開鍵ではありません"
done

mkdir -p "$KEY_DIR"
rm -f "$KEY_DIR"/*.pub.pem
n=0
for f in "$@"; do
    n=$((n + 1))
    # 先頭・末尾の余計な行を落として PEM の部分だけ置く
    sed -n '/^-----BEGIN PUBLIC KEY-----$/,/^-----END PUBLIC KEY-----$/p' "$f" > "${KEY_DIR}/key${n}.pub.pem"
    chmod 0644 "${KEY_DIR}/key${n}.pub.pem"
done

python3 "${ROOT}/scripts/release_pubkeys.py" sync --root "$ROOT"
python3 "${ROOT}/scripts/release_pubkeys.py" check --require --root "$ROOT"

echo
echo "埋め込んだ公開鍵 (DER の SHA-256。ノート PC の鍵と同じか確かめる):"
for f in "$KEY_DIR"/*.pub.pem; do
    printf '  %s  %s\n' "$(openssl pkey -pubin -in "$f" -outform DER | sha256sum | awk '{print $1}')" \
        "${f#"${ROOT}"/}"
done
echo
echo "次: git diff で確認し、scripts/release_pubkeys/ と埋め込み先 3 ファイルをコミットする"
