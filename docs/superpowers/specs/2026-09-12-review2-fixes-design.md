# 第 2 回コードレビュー指摘の修正 (Worker) 設計書

**作成**: 2026-09-12
**対象リリース**: v1.1.3
**レビュー報告書**: 非公開リポジトリ (swim-coordinator) の `docs/superpowers/reviews/2026-09-12-swim-worker-review.md`。本書の番号 (C-1 / I-n / M-n) はその報告書に対応する

## 背景

v1.1.2 までの修正後に実施した全体レビューで、GUI 自動更新・停止処理・更新ヘルパー・ログ・Docker 経路に Critical 1 件、Important 10 件、Minor 14 件が見つかった。本書はそのうち設計判断を伴わないものをまとめて修正する。**Coordinator 側に設計判断が必要な項目 (capability テストの結果解釈、タイムアウト再配布、バックアップ) は対象外**で、別途扱う。

## 決定事項

| 論点 | 決定 |
|------|------|
| 対象 | C-1、I-1〜I-10、M-1、M-2、M-6、M-8 |
| 対象外 | I-3 の「未権限 job_type を Coordinator がどう扱うか」(Worker 側の再ログイン抑止のみ実施)、M-3〜M-5、M-7、M-9〜M-14 |
| macOS (I-6) | 現在利用者はいないが README が案内しているため修正する。Intel Mac は非対応と明記 |
| Docker (I-10) | ビルド・起動が通る最小修正のみ (CA は埋め込みを使用) |
| Worker 名 (I-1) | `Settings` と GUI の両方で `^[A-Za-z0-9._-]{1,32}$` を強制。既存の 6 台はすべて適合 |
| リリース | v1.1.3。Windows 利用者にはロールバック無限ループ回避のため、更新前に「起動時に自動接続」の状態に依存しない起動確認 (C-1) を入れる |

## 設計

### C-1 + I-5: 更新後の起動確認とロールバック抑止

- `gui.py` `WorkerGUI.run()`: `mainloop()` の直前に `self._root.after(2000, _write_startup_marker)` を仕込み、GUI が 2 秒生存した時点で `data/.startup_ok` にバージョン文字列を書く (`consumer._startup_marker_path()` を再利用)。ロールバックの目的は「新 exe が即死しないか」の検知なので GUI 表示到達で十分。Worker 接続時の上書きは従来どおり
- `_check_rollback_marker_after_ready()`: マーカーを読んだら `_set_snooze(from_version)` を呼び、同一バージョンで 2 回連続ロールバック (`gui_settings.json` の `rollback_count[version]`) したら `auto_update=false` を保存して通知文に「自動更新を停止しました」を追加
- ヘルパー (.bat / .sh) の待機秒数は据え置き (120 秒)

### I-1: Worker 名の検証

- `config.py` `Settings.worker_name` に `field_validator` (`^[A-Za-z0-9._-]{1,32}$`)。不一致は `ValueError("Worker 名は半角英数字・ピリオド・アンダースコア・ハイフン (1〜32 文字) のみ")`
- `gui.py` `_on_start`: 同じ正規表現 (`WORKER_NAME_RE` を `config.py` から import) で検証し、不一致なら `messagebox.showerror` で同文言を表示して起動しない
- `redis_client.py`: `client_name` は検証済みの名前をそのまま使う (正規化は不要になる)
- README: Worker 名の制約を明記

### I-2 + I-9 + M-3: GUI の停止経路

- `_run_worker` の `_main()` を `loop.create_task()` で生成し `self._worker_task` に保持。`_on_stop` は `loop.call_soon_threadsafe(task.cancel)` で consumer 生成前 (Redis ping リトライ中・lock 待ち中) でも中断する
- `_main()` は `try/finally` で `await swim_client.close()` と `await redis_client.aclose()` を実行 (生成済みの場合のみ)。`CancelledError` は正常終了として扱う
- `_on_start`: 前回スレッド `self._worker_thread` が `is_alive()` なら「停止処理中です。しばらく待ってから再度押してください」を表示して起動しない
- `_run_worker` の finally: `if self._worker_loop is loop: self._worker_loop = None`
- `_on_stop` の UI 復帰 (「起動」ボタン有効化) は、スレッド終了を `after(500)` ポーリングで確認してから行う

### I-3 (Worker 側のみ): capability テストでの再ログイン抑止

- `auth.py` `execute_api(url, body, *, retry_on_auth_error: bool = True)`。`False` のとき 401/403 は再ログインせず即 `SwimAuthError` (Cookie も削除しない)
- `consumer.py` `_run_capability_test`: `execute_api(url, body, retry_on_auth_error=False)`
- capability テストの結果解釈 (403 = 未権限、それ以外 = 一時障害) を Coordinator に伝える方式は対象外 (設計判断)

### I-4 + M-5 一部: Windows ヘルパーの `:fail` 経路

- `.bat` の `:fail`: `{rollback_marker}` に `{"rolled_back_from": "v{new_version}", "reason": "move_failed"}` を書き、`start "" /D "{base}" "{current_exe}"` で旧 exe を起動してから `exit /b 1`。`.new.exe` は残す (次回のクリーンアップで削除)
- `_check_rollback_marker_after_ready` は `reason` を通知文に含める

### I-6: macOS ヘルパー

1. `pkill -f` をやめ、`"{current_exe}" & NEW_PID=$!` で PID を保持し、ロールバック時は `kill "$NEW_PID"`
2. `subprocess.Popen(["bash", script], env=clean_env, ...)` — Windows と同じ `_PYI_*` 除去 + `PYINSTALLER_RESET_ENVIRONMENT=1` を渡す。`clean_env` の組み立てを `_pyinstaller_clean_env()` に共通化
3. トレイ: `sys.platform == "darwin"` では `pystray.Icon.run_detached()` を使う (Windows は従来どおりスレッドで `run()`)
4. `docs/architecture.md` / README の macOS 対応表記を「Apple Silicon のみ」に修正

### I-7: GUI ログのローテーション

- `gui.py` `main()`: `logging.FileHandler` → `logging.handlers.RotatingFileHandler(log_path, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")`

### I-8 (Worker 側): Worker パースへの task params マージ

- `parsers/__init__.py` `parse_for_job_type`: `ret` unwrap 後に `task_params` の `_` 始まりキーを `data` にマージ (`data.setdefault(k, v)`)。Coordinator の raw 経路と同じ挙動になる
- parsers ディレクトリは Coordinator と一致必須だが `__init__.py` は同期対象外 (Worker 固有) なので Worker 側だけの変更で完結する。Coordinator 側 `result_handler` の `_icao_code` setdefault 修正は Coordinator の spec で扱う

### I-10: Docker 経路

- `Dockerfile`: `COPY ca.crt` を削除。`RUN mkdir -p /app/data && chown -R appuser:appuser /app`。HEALTHCHECK が `ca.crt` を参照していれば埋め込み CA (`certs.get_ca_cert_path()`) に変更
- `docker-compose.yml`: `./data:/app/data` を追加
- `__main__.py`: `LocalInstanceLock.acquire()` の `OSError` (Permission 等) を捕捉し「ロックファイルを作成できません: {path}: {e}」で exit 3

### M-1: ロックファイルの場所

- `single_instance.get_lock_path()`: frozen 時は常に `{exe.parent}/data/swim-worker.lock` とし、`data/` が無ければ作成する (初回起動と 2 回目で場所が変わらない)

### M-2: 埋め込み CA の一時ファイル

- `certs.py`: `mkstemp` 後に `atexit.register(_cleanup)` で削除。加えて `redis_client.create_redis_client` は `ssl_ca_certs` の代わりに `ssl_ca_data=CA_CERT_PEM` を使い (redis-py 8 対応)、外部 `ca.crt` 指定 (`settings.redis_ca_cert`) があるときだけファイルパスを使う。これにより通常運用では一時ファイルを作らない

### M-6: パスワードを環境変数に書かない

- `gui.py` `_run_worker`: `os.environ[...] = ...` をやめ、`Settings(_env_file=None, redis_host=..., redis_password=..., ...)` で直接構築する

### M-8: install.sh のロールバック後再試行抑止

- `install.sh --auto`: ロールバック時に `${INSTALL_DIR}/.failed-version` に失敗したバージョンを書く。ガード評価の先頭で `LATEST_VERSION` が `.failed-version` と一致すれば `skip (前回この版でロールバック)` で exit 0。手動 `install.sh` (非 --auto) 実行時と、より新しい版が出た時にファイルを消す

### ドキュメント

- `README.md`: Worker 名の制約、「起動時に自動接続」の説明、macOS は Apple Silicon のみ、exe は自分専用フォルダに置く推奨
- `docs/architecture.md`: 更新の起動確認方式 (GUI 表示到達で `.startup_ok`)、ロールバック 2 回で自動更新停止、ログローテーション、Docker 経路の修正

### テスト

- `config`: Worker 名の検証 (適合 / 不適合)
- `auth`: `retry_on_auth_error=False` で 403 時に `_relogin` が呼ばれず Cookie も残る
- `parsers/__init__`: `_icao_code` が data にマージされる
- `single_instance`: frozen 相当の呼び出しで `data/` が作られロックパスが固定される
- `certs` / `redis_client`: 一時ファイルを作らず `ssl_ca_data` が渡る、外部 CA 指定時はパスが渡る
- `gui` はロジックを関数に切り出して単体テスト: `_startup_marker_write()`、rollback_count の判定、`_pyinstaller_clean_env()`、`.bat` / `.sh` 生成文字列に `:fail` の `start` と `NEW_PID` が含まれる
- `install.sh`: `.failed-version` の分岐を bash で直接テスト (関数を `source` できる形に切り出す、または `bash -n` + 文字列検査)

### デプロイと確認

1. `__version__` = 1.1.3、タグ push → CI
2. whitelist で Windows 1 台 (自動接続 OFF に切り替えて) に先行配布し、更新後にロールバックされないことを確認 → 全台
3. VPS は timer で更新。`journalctl` に診断/ロック関連のエラーが無いこと
