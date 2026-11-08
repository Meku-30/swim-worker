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
- TLS: 最初の接続は JA4 `t13d1516h2_…`、TLS のセッション再開のときは pre_shared_key が増えて `t13d1517h2_…`。どの要求が再開になるかも Python 版と同じであること
- 待ち時間は 0 にして取っている (分布は別の見本)
