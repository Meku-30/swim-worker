#!/usr/bin/env bash
# Coordinator と Worker の parsers ファイルが一致しているかを確認する。
# CI で Worker のリリース前に走らせて、同期忘れを検知する。
set -uo pipefail

coord="${1:-../swim-coordinator}"
src="$coord/coordinator/parsers"
dst="swim_worker/parsers"

if [[ ! -d "$src" ]]; then
    echo "ERROR: $src が見つからない (coordinator のリポジトリパス指定: bash $0 <path>)" >&2
    exit 2
fi

mismatch=0
for f in notam.py pirep.py weather.py airspace.py airport.py flight.py; do
    if ! diff -q "$src/$f" "$dst/$f" > /dev/null; then
        echo "DIFF: $f"
        diff -u "$src/$f" "$dst/$f" | head -40
        mismatch=$((mismatch + 1))
    fi
done

if [[ $mismatch -gt 0 ]]; then
    echo
    echo "ERROR: $mismatch 個の parser ファイルが Coordinator と不一致。"
    echo "       bash scripts/sync_parsers.sh $coord で同期してください。"
    exit 1
fi

echo "OK: 全 parser が Coordinator と一致"
