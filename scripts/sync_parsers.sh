#!/usr/bin/env bash
# Coordinator の parsers/ を Worker にコピーする
#
# 使い方: bash scripts/sync_parsers.sh [<coordinator_repo_path>]
# デフォルトでは ../swim-coordinator を参照。
set -euo pipefail

coord="${1:-../swim-coordinator}"
src="$coord/coordinator/parsers"
dst="swim_worker/parsers"

if [[ ! -d "$src" ]]; then
    echo "ERROR: $src が見つからない (coordinator のリポジトリパス指定: bash $0 <path>)" >&2
    exit 1
fi

# parse 関数群が DB 非依存になっているのが前提 (関数内 import 化)
for f in notam.py pirep.py weather.py airspace.py airport.py flight.py; do
    cp "$src/$f" "$dst/$f"
    echo "synced: $f"
done

echo
echo "完了。差分確認: bash scripts/check_parsers_synced.sh $coord"
