#!/usr/bin/env bash
# Linux の配布物を作る (リリースと CI で同じもの)。glibc 2.31 を下限にし、feature は付けない
# 使い方: bash rust/scripts/build-linux.sh amd64|arm64|armv6
# 出力: rust/target/dist/swim-worker-linux-<arch>
set -euo pipefail
arch="${1:-}"
case "$arch" in
    amd64) triple=x86_64-unknown-linux-gnu ;;
    arm64) triple=aarch64-unknown-linux-gnu ;;
    armv6) triple=arm-unknown-linux-gnueabihf ;;
    *) echo "使い方: $0 amd64|arm64|armv6" >&2; exit 2 ;;
esac
GLIBC_FLOOR="${GLIBC_FLOOR:-2.31}"
cd "$(dirname "$0")/.."
cargo zigbuild --release --locked -p swim-worker --bin swim-worker --target "${triple}.${GLIBC_FLOOR}"
mkdir -p target/dist
out="target/dist/swim-worker-linux-${arch}"
cp "target/${triple}/release/swim-worker" "$out"

# 要求する glibc の最大が 2.31 以下であること
max=$(objdump -T "$out" | grep -o 'GLIBC_[0-9.]*' | sed 's/GLIBC_//' | sort -V | tail -1)
if [[ "$(printf '%s\n%s\n' "$max" 2.31 | sort -V | tail -1)" != "2.31" ]]; then
    echo "glibc ${max} を要求しています (2.31 以下であること)" >&2; exit 1
fi
if [[ "$arch" == armv6 ]]; then
    readelf -A "$out" | grep -q 'Tag_CPU_arch: v6$' || { echo "ARMv6 の実行ファイルではありません" >&2; exit 1; }
fi
echo "${out} ($(stat -c %s "$out") バイト, glibc ${max})"
