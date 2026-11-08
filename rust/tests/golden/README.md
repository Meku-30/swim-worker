# 見本 (golden)

Rust 版が Python 版と同じ動きをすることを確かめるための見本。どれも作った値だけで作っており、本物の認証情報・本番のデータは入っていない。

## requests/ — Python 版が送る要求

`tools/golden/capture_requests.py` で作る。Python 版の `SwimClient` (`swim_worker/auth.py`) が出す要求を、送り先だけ `https://tls.peet.ws/api/all` に替えて送り、確認先が受け取った内容を記録する。SWIM には送らない。SwimClient には作った応答を返す。

```sh
# リリースと同じ版の依存 (requirements-dev.lock) で
python tools/golden/capture_requests.py --out rust/tests/golden/requests
pytest tests/test_golden_requests.py
```

- 場面: `login`・`cookie_restore`・`browse_f2aspr`・`browse_f2dnrq`・`execute_api`・`fetch_public_get`
- `headers` は HTTP/2 の HEADERS フレームに実際に並んだ順 (コードの辞書の順ではない)
- `:authority` は `<authority>`、`cookie` は `<cookie>` に置き換えてある。`:path` は確認先のパスで、元の SWIM のパスは `path` にある。照合では `:authority`・`:path`・`cookie`・`content-length` の値は比べない (名前と順番は比べる)
- TLS: 最初の接続は JA4 `t13d1516h2_…`、TLS のセッション再開のときは pre_shared_key が増えて `t13d1517h2_…`。照合の道具 (`fingerprint_check`) は、見本が新規接続の step でクライアントを作り直し、見本に cookie があれば Cookie を仕込む。つまり照合が確かめるのは「同じヘッダー列・同じ接続の区切りを渡せば、Rust が同じ並び・同じ指紋で送る」こと。Rust 版の本体が Python 版と同じ位置で新しいセッションを作り、同じヘッダー列を組み立てることは M2 で別に確かめる
- 待ち時間は 0 にして取っている (分布は別の見本)

## delays.json — 待ち時間の分布

`tools/golden/sample_delays.py` で作る。要求の前の待ち時間 (対数正規分布を 1.5〜25 秒に切り詰め、中央値 4 秒・p99 15 秒) と、応答のあとの待ち時間 (指数分布 rate 3.0 + 0.05 秒) を、種を固定して 100,000 回引いた分位点。auth.py の一様分布の待ち時間の上下と、応答が遅いときの追加の待ち時間の決まりも書いてある。Rust 版は同じ分位点に近いことを確かめる。

- 応答のあとの待ち時間の中央値は約 0.28 秒 (auth.py のコメントの「約 0.38 秒」は平均に近い値)

## results/・dotenv_cases.json — 結果の形と .env の読み方

`tools/golden/dump_results.py` で作る (作った値だけ)。

- `results/*.json`: `_make_result` が作る結果 (`result`) と、`_encode_result` で最小の JSON にして zstd level 6 で圧縮したバイト列 (`zstd_b64`)。`completed_at` は `2026-11-07T12:34:56.789012+00:00` に固定してある (Python の `datetime.isoformat()` の形: マイクロ秒 6 桁、`+00:00`)。Rust は展開した JSON が同じになること (圧縮のバイト列そのものは一致しなくてよい)
- `dotenv_cases.json`: `.env` の 1 行 → python-dotenv (`dotenv_values`) が読んだ値。引用符・空・行末のコメント・`#` を含む値・`export`・`=` を含む値・前後の空白・エスケープ
- 注意: Linux の CLI 版は systemd の `EnvironmentFile=` でも同じ `.env` を読む (systemd の読み方は python-dotenv と細部が違う)。Rust 版はどちらで渡されても同じ値になることを M2 で確かめる

## keyring_windows.json — Windows の資格情報マネージャーの項目

`tools/golden/dump_keyring.py` を GitHub Actions の Windows (`.github/workflows/rust-m1-windows.yml`) で走らせて取った (作った値だけ)。Python の keyring (WinVaultKeyring) の書き方:

- 種類は Generic (`type: 1`)、`persist: 3` (Enterprise)、値は UTF-16LE
- TargetName は、空いていればサービス名 `swim-worker` そのもの。すでに別のユーザー名で使われていれば `<ユーザー名>@swim-worker`。**どちらになるかは書いた順番で決まる** (この見本では SWIM_PASSWORD が `swim-worker`、REDIS_PASSWORD が `<id>:REDIS_PASSWORD@swim-worker`)
- Rust 版は、UserName (`<store_id>:<ENV_KEY>`) で探す。TargetName だけで決め打ちしない
