# Python 版と Rust 版の結果を比べる (使い捨てのコンテナ)

同じタスク・同じ SWIM の応答を、今の Python の Worker と Rust の Worker (試運転のビルド `--features trial`) に流し、
Redis に書かれた結果 (展開後の JSON) を比べる道具。SWIM には繋がない (両方とも偽の SWIM)。

| ファイル | 役目 |
|---|---|
| `gen_tasks.py` → `tasks.json` | 場面の一覧と応答の決まり。解析の見本 (`rust/tests/golden/parse/`) と、エラーの場面 (401・403・500・壊れた JSON・SWIM の外への転送・時間切れ・capability など) |
| `fake_swim.py` | Python の Worker の通信 (curl_cffi の AsyncSession) を偽物に差し替えて起こす。応答の選び方は Rust の `src/swim/trial.rs` と同じ |
| `seed.py` | Coordinator のふり。両方の Worker に同じ順でタスクを積み、結果を集める |
| `diff_results.py` | 比べる。`task_id`・`worker_name`・`completed_at` を除き、値 (型まで) と JSON の文字列の両方で |
| `run_compare.sh`・`inner.sh` | コンテナ (Python 3.12 と Redis 7 の TLS) を立てて流し、片付ける |

## 動かし方

docker を使える Linux の機で (sudo が要る機は `DOCKER="sudo docker"`):

```bash
python tools/compare/gen_tasks.py > tools/compare/tasks.json
# 試運転用のビルド (amd64): cargo zigbuild --release -p swim-worker --bin swim-worker --features trial --target x86_64-unknown-linux-gnu.2.31
bash tools/compare/run_compare.sh <作業フォルダ> . <試運転用の Worker の絶対パス>
```

ARMv6 の Worker を qemu-user で動かすときは、`QEMU_ARM=<qemu-arm-static>`・`ARM_SYSROOT=<Pi OS の libc6・libgcc-s1 を展開したところ>` を付ける。

**共有の機では、片付けは名前で** (`swim-compare`・`swim-compare-redis`・イメージ `swim-compare:local`)。一括の消去 (prune) は使わない。
