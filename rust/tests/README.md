# Rust 版のテストと、Python 版のテストの対応

Rust 版は Python 版 (`swim_worker/`) と同じ約束で動く。Python の `tests/` のうち「どう動くべきか」を Rust のテストに移した。見本 (`golden/`) の説明は `golden/README.md`。

| Python | Rust | 備考 |
|---|---|---|
| `tests/test_consumer.py` (30)・`test_consumer_robust.py` (16) | `crates/swim-worker/tests/consumer.rs` (43) | シグナルの 2 件 (`test_falls_back_to_signal_signal_on_windows`・`test_uses_add_signal_handler_when_available`) は起動 (`app.rs`) のテストへ。`test_run_does_not_call_client_setname_at_runtime` は接続ごとに名前を付ける作り (`kv.rs`) で不要。`test_consume_loop_uses_configured_blpop_timeout` 系は設定値の受け渡しだけなので移していない |
| `tests/test_auth.py` (24) | `tests/swim_client.rs` (8)・`tests/swim_headers.rs` (6) | 要求の中身は見本 (`golden/requests/`) との一致で確かめる。本物の通信で確かめる道具は `src/bin/client_fingerprint.rs` |
| `tests/test_config.py` (5) | `tests/config.rs` (6) | `.env` の読み方は `golden/dotenv_cases.json` |
| `tests/test_paths.py` (5)・`test_single_instance.py` (2) | `tests/paths.rs` (4) | |
| `tests/test_redis_client.py` (5) | `tests/kv.rs` (4 + 本物の Redis 1 件は ignored) | |
| `tests/test_parsers.py` (7) | `crates/swim-parsers/tests/worker_parse.rs` | 見本 `golden/parse/` (Python の結果) と照合 |

本物の Redis が要るテスト (`#[ignore]`) は `SWIM_TEST_REDIS_HOST`・`SWIM_TEST_REDIS_PORT`・`SWIM_TEST_REDIS_USER`・`SWIM_TEST_REDIS_PASSWORD`・`SWIM_TEST_REDIS_CA`・`SWIM_TEST_WORKER` を渡して `cargo test -- --ignored` で走らせる。
