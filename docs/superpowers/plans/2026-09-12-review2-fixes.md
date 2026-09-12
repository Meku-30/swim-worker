# 第 2 回レビュー指摘の修正 (Worker, v1.1.3) 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GUI 自動更新のロールバック無限ループ (C-1/I-5)、Worker 名の検証 (I-1)、GUI 停止経路 (I-2/I-9/M-3/M-6)、capability テストの再ログイン抑止 (I-3)、更新ヘルパー (I-4/I-6)、ログローテ (I-7)、Worker パースの task params マージ (I-8)、Docker 経路 (I-10)、ロックパス (M-1)、CA 一時ファイル (M-2)、install.sh の失敗版再試行抑止 (M-8) を修正し v1.1.3 としてリリースする。

**Architecture:** 変更はすべて既存モジュール内。GUI のロジックのうちテストしたいものは `gui.py` からモジュール関数に切り出す (`_pyinstaller_clean_env()`、`_build_windows_update_script()`、`_build_macos_update_script()`、`_next_rollback_state()`)。tkinter 依存のない関数だけをテストする。

**Tech Stack:** Python 3.12 / asyncio / tkinter + pystray / redis-py 8 / curl_cffi / pydantic-settings / PyInstaller / pytest + pytest-asyncio / bash (install.sh)

**Spec:** `docs/superpowers/specs/2026-09-12-review2-fixes-design.md`

## Global Constraints

- 作業ディレクトリ `/home/meku/claude/swim-worker` (**Public リポジトリ**)。シークレット・内部 IP・NAS パス・VPS 事業者名をコード/ドキュメントに書かない。コミットメッセージは日本語
- `swim_worker/parsers/*.py` (`__init__.py` を除く) は Coordinator と一致必須。**本計画では触らない** (`parsers/__init__.py` のみ Worker 固有で変更可)
- Windows / macOS の GUI 動作は本環境で実行確認できない。tkinter 非依存に切り出した関数をテストし、生成する `.bat` / `.sh` 文字列をアサートする
- 各タスク末尾で `python3 -m pytest tests/ -q` が全件 PASS (基準 37 passed)。`bash -n scripts/install.sh` と `shellcheck -S warning scripts/install.sh` も通ること
- `__version__` は Task 12 まで `1.1.2` のまま
- コミット末尾:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01HvJvyome4qYgikSoCTLSjT
  ```

---

## File Structure

| ファイル | 責務 | 変更 |
|---------|------|------|
| `swim_worker/config.py` | 設定 | `WORKER_NAME_RE` + validator (T1) |
| `swim_worker/gui.py` | GUI | 起動マーカー・ロールバック抑止 (T2)、停止経路・Settings 直接構築 (T3)、ヘルパー生成関数化 (T5)、ログローテ (T6)、名前検証 (T1) |
| `swim_worker/auth.py` | SWIM クライアント | `retry_on_auth_error` (T4) |
| `swim_worker/consumer.py` | consumer | capability テストの呼び出し (T4) |
| `swim_worker/parsers/__init__.py` | Worker 側 parse 入口 | task params マージ (T7) |
| `swim_worker/single_instance.py` | ロック | パス固定 (T9) |
| `swim_worker/certs.py` / `redis_client.py` | CA / Redis | `ssl_ca_data` + atexit (T9) |
| `swim_worker/__main__.py` | CLI | ロック OSError (T8) |
| `Dockerfile` / `docker-compose.yml` | Docker | T8 |
| `scripts/install.sh` | Linux 自動更新 | `.failed-version` (T10) |
| `README.md` / `docs/architecture.md` | docs | T11 |
| `tests/` | テスト | 各タスク |

---

### Task 1: Worker 名の検証 (I-1)

**Files:**
- Modify: `swim_worker/config.py`
- Modify: `swim_worker/gui.py` (`_on_start` 冒頭)、`README.md` (Worker 名の説明)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `swim_worker.config.WORKER_NAME_RE: re.Pattern` (`^[A-Za-z0-9._-]{1,32}$`)、`WORKER_NAME_RULE_MESSAGE: str`。`Settings(worker_name=...)` は不一致で `pydantic.ValidationError`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_config.py` に追加 (既存の `_settings(monkeypatch)` ヘルパーの形に合わせる。無ければ `Settings(_env_file=None, redis_host="h", redis_password="p", swim_username="u", swim_password="p", worker_name=...)` で直接構築):

```python
import pytest
from pydantic import ValidationError
from swim_worker.config import Settings, WORKER_NAME_RE, WORKER_NAME_RULE_MESSAGE


def _settings_with_name(name: str) -> Settings:
    return Settings(_env_file=None, redis_host="h", redis_password="p",
                    swim_username="u", swim_password="p", worker_name=name)


@pytest.mark.parametrize("name", ["GCP-worker", "hyuga_main", "w.1", "a", "x" * 32])
def test_worker_name_accepts_ascii_names(name):
    assert _settings_with_name(name).worker_name == name


@pytest.mark.parametrize("name", ["Taro Yamada", "田中", "", "x" * 33, "a/b", "name\n"])
def test_worker_name_rejects_invalid_names(name):
    with pytest.raises(ValidationError) as ei:
        _settings_with_name(name)
    assert WORKER_NAME_RULE_MESSAGE in str(ei.value)


def test_worker_name_re_matches_rule_message_examples():
    assert WORKER_NAME_RE.fullmatch("abc-123_x.y")
    assert not WORKER_NAME_RE.fullmatch("a b")
```

- [ ] **Step 2: 失敗確認**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: `ImportError: cannot import name 'WORKER_NAME_RE'`

- [ ] **Step 3: 実装**

`swim_worker/config.py`:

```python
"""Worker設定"""
import re

from pydantic import field_validator
from pydantic_settings import BaseSettings

# Worker 名は Redis CLIENT SETNAME (空白・制御文字不可) とキー名に使うため ASCII に限定する
WORKER_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
WORKER_NAME_RULE_MESSAGE = (
    "Worker 名は半角英数字・ピリオド・アンダースコア・ハイフンのみ (1〜32 文字) です"
)


class Settings(BaseSettings):
    ...(既存フィールドはそのまま)...

    @field_validator("worker_name")
    @classmethod
    def _validate_worker_name(cls, v: str) -> str:
        if not WORKER_NAME_RE.fullmatch(v or ""):
            raise ValueError(WORKER_NAME_RULE_MESSAGE)
        return v
```

`swim_worker/gui.py` `_on_start` の空チェック直後に追加:

```python
        from swim_worker.config import WORKER_NAME_RE, WORKER_NAME_RULE_MESSAGE
        name = self._entries["worker_name"].get().strip()
        if not WORKER_NAME_RE.fullmatch(name):
            messagebox.showerror("エラー", WORKER_NAME_RULE_MESSAGE)
            return
```

`README.md` の Worker 名の説明 (`grep -n "worker_name\|Worker名\|WORKER_NAME" README.md` で位置を確認) に「半角英数字・`.` `_` `-` のみ、1〜32 文字。空白や日本語は使えません」を追記。

- [ ] **Step 4: テスト実行 → コミット**

Run: `python3 -m pytest tests/ -q` → 全件 PASS

```bash
git add swim_worker/config.py swim_worker/gui.py README.md tests/test_config.py
git commit -m "Worker 名を半角英数字等 1〜32 文字に制限 (Redis CLIENT SETNAME 失敗の予防)

v1.1.1 で client_name を接続ごとに送るようにした結果、空白や日本語を含む名前では
全接続が失敗するようになっていた。Settings と GUI の両方で検証する。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HvJvyome4qYgikSoCTLSjT"
```

---

### Task 2: 更新後の起動確認とロールバック抑止 (C-1, I-5)

**Files:**
- Modify: `swim_worker/gui.py` (`run()`、`_check_rollback_marker_after_ready()`、新規モジュール関数 `_next_rollback_state()`)
- Test: `tests/test_gui_helpers.py` (新規、tkinter 非依存の関数のみ)

**Interfaces:**
- Produces: `swim_worker.gui._next_rollback_state(gui_settings: dict, from_version: str) -> tuple[dict, bool]` — `rollback_count[from_version]` を +1 した新しい settings dict と、「自動更新を停止すべきか (2 回目以降)」を返す。純粋関数
- `swim_worker.gui._write_startup_marker() -> None` — `consumer._startup_marker_path()` に `__version__` を書く (失敗は無視)

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_gui_helpers.py` を新規作成。`gui.py` は import 時に tkinter を読むため、`import` を `pytest.importorskip("tkinter")` で保護する:

```python
"""gui.py のうち tkinter 非依存の純粋関数のテスト"""
import pytest

pytest.importorskip("tkinter")
from swim_worker import gui  # noqa: E402


class TestNextRollbackState:
    def test_first_rollback_counts_but_keeps_auto_update(self):
        new, disable = gui._next_rollback_state({"auto_update": True}, "1.1.3")
        assert new["rollback_count"] == {"1.1.3": 1}
        assert new["auto_update"] is True
        assert disable is False

    def test_second_rollback_of_same_version_disables_auto_update(self):
        new, disable = gui._next_rollback_state(
            {"auto_update": True, "rollback_count": {"1.1.3": 1}}, "1.1.3")
        assert new["rollback_count"] == {"1.1.3": 2}
        assert new["auto_update"] is False
        assert disable is True

    def test_other_version_rollback_does_not_accumulate(self):
        new, disable = gui._next_rollback_state(
            {"auto_update": True, "rollback_count": {"1.1.3": 1}}, "1.1.4")
        assert new["rollback_count"] == {"1.1.3": 1, "1.1.4": 1}
        assert disable is False


class TestStartupMarker:
    def test_write_startup_marker_writes_version(self, tmp_path, monkeypatch):
        from swim_worker import consumer, __version__
        monkeypatch.setattr(consumer, "_startup_marker_path", lambda: tmp_path / "data" / ".startup_ok")
        gui._write_startup_marker()
        assert (tmp_path / "data" / ".startup_ok").read_text(encoding="utf-8") == __version__
```

- [ ] **Step 2: 失敗確認**

Run: `python3 -m pytest tests/test_gui_helpers.py -q`
Expected: `AttributeError: module 'swim_worker.gui' has no attribute '_next_rollback_state'` (tkinter が無い環境なら skip される — その場合は `sudo apt install python3-tk` 相当が必要。この環境にあるか `python3 -c "import tkinter"` で確認)

- [ ] **Step 3: 実装**

`swim_worker/gui.py` のモジュールレベル (`_load_json` / `_save_json` の近く) に追加:

```python
ROLLBACK_DISABLE_THRESHOLD = 2  # 同一バージョンでこの回数ロールバックしたら自動更新を止める


def _next_rollback_state(gui_settings: dict, from_version: str) -> tuple[dict, bool]:
    """ロールバック検知時の gui_settings 更新を計算する (純粋関数)。

    同一バージョンのロールバック回数を数え、閾値に達したら auto_update を False にする。
    戻り値: (新しい settings dict, 自動更新を停止したか)
    """
    new = dict(gui_settings)
    counts = dict(new.get("rollback_count") or {})
    counts[from_version] = counts.get(from_version, 0) + 1
    new["rollback_count"] = counts
    disable = counts[from_version] >= ROLLBACK_DISABLE_THRESHOLD and new.get("auto_update", False)
    if disable:
        new["auto_update"] = False
    return new, disable


def _write_startup_marker() -> None:
    """GUI が起動して生存したことをヘルパースクリプトに伝える (`data/.startup_ok`)。

    v1.1.2 以前は Worker が Redis に接続したときしか書かれず、「起動時に自動接続」OFF の
    利用者では更新のたびに 120 秒後ロールバックされていた。
    """
    from swim_worker import __version__
    from swim_worker.consumer import _startup_marker_path
    try:
        marker = _startup_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(__version__, encoding="utf-8")
    except Exception as e:
        logging.debug("startup marker 作成失敗 (無視): %s", e)
```

`WorkerGUI.run()` の `self._root.mainloop()` 直前に:

```python
        # 更新ヘルパーの起動確認: GUI が 2 秒生存したら成功マーカーを書く
        self._root.after(2000, _write_startup_marker)
```

`_check_rollback_marker_after_ready()` を以下に置き換える:

```python
    def _check_rollback_marker_after_ready(self) -> None:
        """前回アップデートがロールバックされた場合、ユーザーに通知し再試行を抑止する。

        - 同一バージョンを snooze (SNOOZE_DURATION_HOURS)
        - 同一バージョンで ROLLBACK_DISABLE_THRESHOLD 回目なら auto_update を OFF にする
        autoconnect=True の場合は Worker 起動処理と重ならないよう 3 秒待つ。
        """
        marker = _get_base_dir() / "data" / ".update_rollback.json"
        if not marker.exists():
            return
        try:
            info = _load_json(marker)
        except Exception:
            info = {}
        from_version = str(info.get("rolled_back_from", "?")).lstrip("v")
        reason = info.get("reason", "")

        self._gui_settings, disabled = _next_rollback_state(self._gui_settings, from_version)
        try:
            _save_json(GUI_SETTINGS_PATH, self._gui_settings)
        except Exception as e:
            logging.debug("GUI 設定保存失敗 (無視): %s", e)
        if from_version != "?":
            self._set_snooze(from_version)

        def notify():
            try:
                text = (f"v{from_version} へのアップデートが起動確認に失敗したため、"
                        f"自動的に前バージョンにロールバックされました。\n\n")
                if reason == "move_failed":
                    text += "原因: 新しい exe への置き換えに失敗しました (ウイルス対策ソフトのスキャン等)。\n\n"
                if disabled:
                    text += ("同じバージョンで 2 回失敗したため、自動更新を停止しました。\n"
                             "設定で再度有効にするか、手動で更新してください。\n\n")
                text += "詳細は swim-worker-update.log を確認してください。"
                messagebox.showwarning("前回のアップデートは失敗しました", text)
            finally:
                try:
                    marker.unlink()
                except Exception:
                    pass

        self._root.after(3000, notify)
```

`_on_auto_update_toggle` / 初期化で `self._auto_update_var` を `self._gui_settings["auto_update"]` から作っている箇所 (`grep -n "auto_update" swim_worker/gui.py`) を確認し、`_next_rollback_state` が `auto_update=False` にしたときに UI のチェックボックスも反映されるよう、`notify()` 内で `if disabled and hasattr(self, "_auto_update_var"): self._auto_update_var.set(False)` を追加する。

- [ ] **Step 4: テスト実行 → コミット**

```bash
python3 -m pytest tests/ -q
git add swim_worker/gui.py tests/test_gui_helpers.py
git commit -m "GUI 更新: 起動確認マーカーを GUI 表示時点で書き、同一版のロールバックが続いたら自動更新を停止

- .startup_ok が Redis 接続後にしか書かれず、自動接続 OFF の利用者は更新のたびに
  120 秒後ロールバックされていた → GUI が 2 秒生存した時点で書く
- ロールバック後に snooze し、同一バージョンで 2 回目なら auto_update を OFF
- .bat の :fail 由来 (reason=move_failed) を通知文に反映

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HvJvyome4qYgikSoCTLSjT"
```

---

### Task 3: GUI 停止経路と Settings 直接構築 (I-2, I-9, M-3, M-6)

**Files:**
- Modify: `swim_worker/gui.py` (`__init__` 属性、`_on_start`、`_on_stop`、`_run_worker`)

**Interfaces:**
- Produces: `WorkerGUI._worker_task: asyncio.Task | None`。`_on_stop` は `loop.call_soon_threadsafe(self._worker_task.cancel)` で中断

この Task は tkinter 依存のため単体テストは書かない。代わりに `_main()` の finally 構造を静的に確認する (Step 3 の grep)。

- [ ] **Step 1: `_run_worker` を書き換える**

`_main()` 冒頭の `os.environ[...]` 7 行と `settings = Settings()` を以下に置き換える (M-6):

```python
                ws = self._worker_settings
                settings = Settings(
                    _env_file=None,
                    redis_host=ws["redis_host"], redis_port=6380,
                    redis_password=ws["redis_password"], redis_ca_cert="",
                    swim_username=ws["swim_username"], swim_password=ws["swim_password"],
                    worker_name=ws["worker_name"],
                )
```

`_main()` 全体を `swim_client = None; redis_client = None` の初期化 → `try: ... except DuplicateWorkerError ... except asyncio.CancelledError: logging.info("Worker 停止要求を受け付けました") ... except Exception ... finally:` の形にし、finally で:

```python
            finally:
                if swim_client is not None:
                    try:
                        await swim_client.close()
                    except Exception as e:
                        logging.debug("SwimClient close 失敗 (無視): %s", e)
                if redis_client is not None:
                    try:
                        await redis_client.aclose()
                    except Exception as e:
                        logging.debug("Redis close 失敗 (無視): %s", e)
                self._consumer = None
```

(`redis_client = create_redis_client(settings)` と `swim_client = SwimClient(...)` は try 内でそれぞれ代入する。`SwimClient` 生成後は `swim_client` ローカル変数を `TaskConsumer(... swim_client=swim_client ...)` に渡す。)

ループ部分:

```python
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._worker_loop = loop
        try:
            self._worker_task = loop.create_task(_main())
            loop.run_until_complete(self._worker_task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error("予期しないエラー: %s", e)
        finally:
            self._worker_task = None
            if self._worker_loop is loop:
                self._worker_loop = None
            loop.close()
```

`__init__` に `self._worker_task: asyncio.Task | None = None` を追加。

- [ ] **Step 2: `_on_stop` / `_on_start` を書き換える**

`_on_stop`:

```python
    def _on_stop(self):
        """Worker停止 (consumer 生成前の Redis リトライ中でも中断できる)"""
        self._worker_running = False
        loop = self._worker_loop
        task = self._worker_task
        if loop is not None and task is not None and loop.is_running():
            # Task.cancel はスレッドセーフでないためループのスレッドで実行する。
            # consumer.run() 内なら CancelledError で heartbeat/consume が止まり finally が走る
            loop.call_soon_threadsafe(task.cancel)
        if _HAS_TRAY and self._tray_icon:
            self._tray_icon.icon = create_icon(color="gray", size=64)
        self._status_var.set("停止処理中...")
        self._stop_btn.configure(state="disabled")
        self._root.after(500, self._poll_worker_stopped)

    def _poll_worker_stopped(self):
        """ワーカースレッドの終了を待ってから UI を起動可能に戻す"""
        thread = self._worker_thread
        if thread is not None and thread.is_alive():
            self._root.after(500, self._poll_worker_stopped)
            return
        self._status_var.set("停止中")
        self._start_btn.configure(state="normal")
        for entry in self._entries.values():
            entry.configure(state="normal")
        logging.info("Worker停止")
```

`_on_start` のバリデーション直後 (Task 1 の名前検証の後) に:

```python
        thread = self._worker_thread
        if thread is not None and thread.is_alive():
            messagebox.showinfo("停止処理中", "前回の停止処理が完了していません。しばらく待ってから再度押してください。")
            return
```

- [ ] **Step 3: 静的確認とテスト**

```bash
grep -n "os.environ\[" swim_worker/gui.py        # _run_worker 内に残っていないこと (更新ヘルパーの clean_env は別)
grep -n "_worker_task\|call_soon_threadsafe\|_poll_worker_stopped" swim_worker/gui.py
python3 -c "import ast; ast.parse(open('swim_worker/gui.py').read()); print('syntax OK')"
python3 -m pytest tests/ -q
```

- [ ] **Step 4: コミット**

```bash
git add swim_worker/gui.py
git commit -m "GUI 停止経路の修正: Redis リトライ中でも中断可能にし、停止時にクライアントを解放

- _main() を Task 化し、停止は call_soon_threadsafe(task.cancel) で行う
- 停止完了 (スレッド終了) を待ってから「起動」を有効化 (同名 Worker の並走防止)
- finally で SwimClient / Redis client を close (起動停止ごとのリーク解消)
- _worker_loop は自分のループのときだけ None に戻す (競合解消)
- パスワードを os.environ に書かず Settings を直接構築

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HvJvyome4qYgikSoCTLSjT"
```

---

### Task 4: capability テストでの再ログイン抑止 (I-3)

**Files:**
- Modify: `swim_worker/auth.py` (`execute_api`)、`swim_worker/consumer.py` (`_run_capability_test`)
- Test: `tests/test_auth.py`、`tests/test_consumer.py`

**Interfaces:**
- Produces: `SwimClient.execute_api(url, body, *, retry_on_auth_error: bool = True, _retried: bool = False) -> dict`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_auth.py` に追加 (既存 `test_execute_api_retries_on_403` の書き方に倣う):

```python
    async def test_execute_api_no_relogin_on_403_when_retry_disabled(self):
        client = SwimClient(username="user", password="pass")
        client._is_ready = True
        session = AsyncMock()
        session.post.return_value = MagicMock(status_code=403, text="forbidden")
        client._session = session
        client._relogin = AsyncMock()
        with patch("swim_worker.auth.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(SwimAuthError, match="403"):
                await client.execute_api("https://example/api", {}, retry_on_auth_error=False)
        client._relogin.assert_not_called()
        assert session.post.await_count == 1
```

`tests/test_consumer.py` に追加:

```python
    async def test_capability_test_calls_execute_api_without_auth_retry(self):
        mock_redis = AsyncMock()
        swim = AsyncMock()
        consumer = TaskConsumer(mock_redis, swim, "test-worker")
        with patch("swim_worker.consumer.asyncio.sleep", new=AsyncMock()):
            await consumer._run_capability_test("t1", {"tests": [{"job_type": "collect_notams", "url": "u", "body": {}}]})
        swim.execute_api.assert_awaited_once_with("u", {}, retry_on_auth_error=False)
```

- [ ] **Step 2: 失敗確認** — `python3 -m pytest tests/test_auth.py tests/test_consumer.py -q -k "auth_retry or without_auth"` → 2 件 FAIL

- [ ] **Step 3: 実装**

`auth.py` `execute_api` のシグネチャを `async def execute_api(self, url: str, body: dict, *, retry_on_auth_error: bool = True, _retried: bool = False) -> dict:` にし、401/403 分岐を:

```python
        if resp.status_code in (401, 403):
            if retry_on_auth_error and not _retried:
                delay = random.uniform(5, 15)
                logger.warning("API %dエラー、%.0f秒待機後に再ログイン+リトライ", resp.status_code, delay)
                await asyncio.sleep(delay)
                await self._relogin(force=True)
                return await self.execute_api(url, body, retry_on_auth_error=retry_on_auth_error, _retried=True)
            raise SwimAuthError(f"API {resp.status_code}エラー (body={resp.text[:500]})")
```

HTTP 例外側の再帰呼び出しにも `retry_on_auth_error=retry_on_auth_error` を渡す。docstring に「capability テストは 403 を『未権限』として扱うため `retry_on_auth_error=False` で呼ぶ (再ログインと Cookie 破棄を避ける)」を追記。

`consumer.py` `_run_capability_test`: `await self._swim.execute_api(url, body, retry_on_auth_error=False)`。

- [ ] **Step 4: テスト実行 → コミット**

```bash
python3 -m pytest tests/ -q
git add swim_worker/auth.py swim_worker/consumer.py tests/test_auth.py tests/test_consumer.py
git commit -m "capability テストの 403 で再ログイン・Cookie 破棄をしない

未権限 job_type ごとに強制再ログインが走り、Coordinator のタイムアウト超過と
30 分ごとのログイン連打を招いていた。403 は未権限として即返す。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HvJvyome4qYgikSoCTLSjT"
```

---

### Task 5: 更新ヘルパーの修正 (I-4, I-6)

**Files:**
- Modify: `swim_worker/gui.py` (`_do_update` のスクリプト生成をモジュール関数へ切り出し、`_setup_tray`)
- Test: `tests/test_gui_helpers.py`

**Interfaces:**
- Produces (モジュール関数、tkinter 非依存):
  - `_pyinstaller_clean_env(base_env: dict) -> dict` — `_PYI_ARCHIVE_FILE` / `_PYI_APPLICATION_HOME_DIR` / `_PYI_PARENT_PROCESS_LEVEL` / `_MEIPASS2` を除去し `PYINSTALLER_RESET_ENVIRONMENT=1` を設定
  - `_build_windows_update_script(*, base, current_exe, new_exe, old_exe, startup_ok, rollback_marker, log_path, new_version) -> str`
  - `_build_macos_update_script(*, current_exe, new_exe, old_exe, startup_ok, rollback_marker, log_path, new_version) -> str`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_gui_helpers.py` に追加:

```python
from pathlib import Path


class TestPyinstallerCleanEnv:
    def test_removes_pyi_vars_and_sets_reset(self):
        env = {"_PYI_ARCHIVE_FILE": "x", "_PYI_APPLICATION_HOME_DIR": "y",
               "_PYI_PARENT_PROCESS_LEVEL": "1", "_MEIPASS2": "z", "PATH": "/bin"}
        out = gui._pyinstaller_clean_env(env)
        assert "PATH" in out
        assert not any(k.startswith("_PYI_") for k in out)
        assert "_MEIPASS2" not in out
        assert out["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        assert env.get("_PYI_ARCHIVE_FILE") == "x"  # 入力は変更しない


class TestWindowsUpdateScript:
    def _script(self):
        base = Path(r"C:\Users\test\swim")
        return gui._build_windows_update_script(
            base=base, current_exe=base / "swim-worker-gui.exe",
            new_exe=base / "swim-worker-gui.new.exe", old_exe=base / "swim-worker-gui.exe.old",
            startup_ok=base / "data" / ".startup_ok",
            rollback_marker=base / "data" / ".update_rollback.json",
            log_path=base / "swim-worker-update.log", new_version="1.1.3")

    def test_fail_path_restarts_old_exe_and_writes_marker(self):
        s = self._script()
        fail = s[s.index(":fail"):]
        assert 'start "" /D' in fail and "swim-worker-gui.exe" in fail
        assert '"reason":"move_failed"' in fail
        assert ".update_rollback.json" in fail

    def test_rollback_path_still_restores_old_exe(self):
        s = self._script()
        rb = s[s.index(":rollback"):s.index(":fail")]
        assert "move /Y" in rb and ".exe.old" in rb


class TestMacosUpdateScript:
    def _script(self):
        base = Path("/Applications/swim")
        return gui._build_macos_update_script(
            current_exe=base / "swim-worker", new_exe=base / "swim-worker.new",
            old_exe=base / "swim-worker.old", startup_ok=base / "data" / ".startup_ok",
            rollback_marker=base / "data" / ".update_rollback.json",
            log_path=base / "swim-worker-update.log", new_version="1.1.3")

    def test_uses_pid_instead_of_pkill(self):
        s = self._script()
        assert "pkill" not in s
        assert "NEW_PID=$!" in s
        assert 'kill "$NEW_PID"' in s
```

- [ ] **Step 2: 失敗確認** — `python3 -m pytest tests/test_gui_helpers.py -q` → `AttributeError`

- [ ] **Step 3: 実装**

`gui.py` モジュールレベルに 3 関数を追加。`_do_update` の Windows 分岐にある `script = ("@echo off\r\n" ... )` の全文を `_build_windows_update_script(...)` の本体に移し (引数を f-string で使う)、`:fail` ブロックを以下に置き換える:

```python
            ":fail\r\n"
            f'echo [%DATE% %TIME%] FAILED after %COUNT% attempts, restarting current exe >> "{log_path}"\r\n'
            f'mkdir "{rollback_marker.parent}" 2>nul\r\n'
            f'echo {{"rolled_back_from":"v{new_version}","reason":"move_failed"}} > "{rollback_marker}"\r\n'
            f'start "" /D "{base}" "{current_exe}"\r\n'
            'del "%~f0"\r\n'
            "exit /b 1\r\n"
```

macOS 分岐の `script = f"""#!/bin/bash ..."""` を `_build_macos_update_script(...)` に移し、`"{current_exe}" &` → `"{current_exe}" &\nNEW_PID=$!`、`pkill -f "{current_exe.name}" >> "$LOG" 2>&1 || true` → `kill "$NEW_PID" >> "$LOG" 2>&1 || true`。

`_pyinstaller_clean_env`:

```python
_PYI_ENV_KEYS = ("_PYI_ARCHIVE_FILE", "_PYI_APPLICATION_HOME_DIR",
                 "_PYI_PARENT_PROCESS_LEVEL", "_MEIPASS2")


def _pyinstaller_clean_env(base_env: dict) -> dict:
    """PyInstaller onefile の子プロセス判定用環境変数を除去した環境を返す。

    ヘルパー経由で再起動する新 exe がこれらを継承すると、既に消えた _MEI ディレクトリから
    Python を読もうとして起動に失敗する (bootloader はプラットフォーム非依存で判定する)。
    """
    env = {k: v for k, v in base_env.items() if k not in _PYI_ENV_KEYS}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env
```

`_do_update` の Windows 分岐の `clean_env = os.environ.copy(); for k in (...): clean_env.pop(k, None); clean_env[...] = "1"` を `clean_env = _pyinstaller_clean_env(os.environ)` に。macOS 分岐の `subprocess.Popen(["bash", str(script_path)], start_new_session=True, close_fds=True)` に `env=_pyinstaller_clean_env(os.environ)` を追加。

`_setup_tray`: スレッド起動部分を

```python
            if sys.platform == "darwin":
                # AppKit はメインスレッド必須。pystray の tkinter 併用向け API を使う
                self._tray_icon.run_detached()
            else:
                self._tray_thread = threading.Thread(target=self._tray_icon.run, daemon=True)
                self._tray_thread.start()
```

- [ ] **Step 4: テスト実行 → コミット**

```bash
python3 -m pytest tests/ -q
git add swim_worker/gui.py tests/test_gui_helpers.py
git commit -m "更新ヘルパー: Windows の置換失敗時に旧 exe を再起動、macOS は PID で停止・環境変数リセット・トレイをメインスレッドで実行

- .bat の :fail で reason=move_failed のマーカーを書いて旧 exe を起動 (Worker が黙って消えない)
- macOS .sh: pkill -f (自分自身も殺していた) → NEW_PID を kill
- macOS でも _PYI_* を除去した環境でヘルパーを起動
- macOS は pystray.Icon.run_detached()
- スクリプト生成と環境変数整形を関数化しテスト可能に

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HvJvyome4qYgikSoCTLSjT"
```

---

### Task 6: GUI ログのローテーション (I-7)

**Files:**
- Modify: `swim_worker/gui.py` `main()`

- [ ] **Step 1: 実装**

`logging.FileHandler(log_path, encoding="utf-8")` を:

```python
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
```

- [ ] **Step 2: 確認 → コミット**

```bash
python3 -c "import ast; ast.parse(open('swim_worker/gui.py').read())" && python3 -m pytest tests/ -q
git add swim_worker/gui.py
git commit -m "GUI のログファイルを 5MB × 3 世代でローテーション

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HvJvyome4qYgikSoCTLSjT"
```

---

### Task 7: Worker パースへの task params マージ (I-8)

**Files:**
- Modify: `swim_worker/parsers/__init__.py` (`parse_for_job_type`)
- Test: `tests/test_parsers.py`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_parsers.py` に追加 (既存の import に合わせる):

```python
def test_parse_for_job_type_merges_underscore_task_params_into_data():
    """Coordinator の raw 経路と同様、task params の _ 始まりキー (例 _icao_code) を data に加える"""
    from swim_worker import parsers
    captured = {}

    def fake_parser(data):
        captured.update(data)
        return []

    original = parsers._PARSERS["collect_airport_profiles"]
    parsers._PARSERS["collect_airport_profiles"] = fake_parser
    try:
        parsers.parse_for_job_type("collect_airport_profiles", {"ret": {"x": 1}},
                                   task_params={"url": "u", "_icao_code": "RJTT"})
    finally:
        parsers._PARSERS["collect_airport_profiles"] = original
    assert captured == {"x": 1, "_icao_code": "RJTT"}


def test_parse_for_job_type_does_not_override_existing_keys():
    from swim_worker import parsers
    captured = {}
    original = parsers._PARSERS["collect_notams"]
    parsers._PARSERS["collect_notams"] = lambda d: captured.update(d) or []
    try:
        parsers.parse_for_job_type("collect_notams", {"_icao_code": "KEEP"},
                                   task_params={"_icao_code": "OTHER"})
    finally:
        parsers._PARSERS["collect_notams"] = original
    assert captured["_icao_code"] == "KEEP"
```

- [ ] **Step 2: 失敗確認** — 1 件目 FAIL (`captured == {"x": 1}`)

- [ ] **Step 3: 実装**

`parse_for_job_type` の `ret` unwrap 直後に:

```python
    # Coordinator の raw 経路 (result_handler) と同じく、task params の "_" 始まりキーを
    # data にマージする (例: collect_airport_profiles の _icao_code)。既存キーは上書きしない
    if task_params and isinstance(data, dict):
        for k, v in task_params.items():
            if k.startswith("_"):
                data.setdefault(k, v)
```

docstring にもその旨を追記。

- [ ] **Step 4: テスト実行 → コミット**

```bash
python3 -m pytest tests/ -q && bash scripts/check_parsers_synced.sh | tail -1
git add swim_worker/parsers/__init__.py tests/test_parsers.py
git commit -m "Worker 側パースで task params の _ キー (_icao_code 等) を data にマージ

Coordinator の raw 経路と挙動を揃える。collect_airport_profiles を Worker パース対象に
した場合に _icao_code が落ちて保存 0 件になる潜在バグの修正。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HvJvyome4qYgikSoCTLSjT"
```

---

### Task 8: Docker 経路と CLI のロック失敗表示 (I-10)

**Files:**
- Modify: `Dockerfile`、`docker-compose.yml`、`swim_worker/__main__.py`
- Test: `tests/test_single_instance.py` (新規、`LocalInstanceLock` の OSError 伝播)

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_single_instance.py`:

```python
"""single_instance のロック取得失敗の扱い"""
import os
import stat
import pytest

from swim_worker.single_instance import LocalInstanceLock, AlreadyRunning


def test_acquire_raises_oserror_when_dir_not_writable(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root では書込不可ディレクトリを作れない")
    locked_dir = tmp_path / "ro"
    locked_dir.mkdir()
    locked_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        lock = LocalInstanceLock(locked_dir / "swim-worker.lock")
        with pytest.raises(OSError) as ei:
            lock.acquire()
        assert not isinstance(ei.value, AlreadyRunning)
    finally:
        locked_dir.chmod(stat.S_IRWXU)
```

(現状でも OSError は伝播するので、このテストは実装前から PASS する見込み。`__main__` 側のメッセージ化は手動確認とする。)

- [ ] **Step 2: 実装**

`Dockerfile`:

```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY swim_worker/ swim_worker/
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser
# CA 証明書はコードに埋め込み済み (swim_worker/certs.py) のため外部ファイル不要
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import redis, os; from swim_worker.certs import get_ca_cert_path; r=redis.Redis(host=os.environ.get('REDIS_HOST',''), port=int(os.environ.get('REDIS_PORT',6380)), password=os.environ.get('REDIS_PASSWORD',''), ssl=True, ssl_ca_certs=get_ca_cert_path()); r.ping()" || exit 1
CMD ["python", "-m", "swim_worker"]
```

`docker-compose.yml`:

```yaml
services:
  swim-worker:
    build: .
    env_file: .env
    restart: unless-stopped
    volumes:
      - ./data:/app/data
```

`swim_worker/__main__.py` の `_local_lock.acquire()` を:

```python
    try:
        _local_lock.acquire()
    except AlreadyRunning as e:
        logger.error("%s", e)
        sys.exit(2)
    except OSError as e:
        logger.error("ロックファイルを作成できません: %s (%s)。書き込み可能なディレクトリで実行してください", _local_lock.path, e)
        sys.exit(3)
```

- [ ] **Step 3: 確認 → コミット**

```bash
python3 -m pytest tests/ -q
docker build -t swim-worker-test . >/dev/null 2>&1 && echo "docker build OK" || echo "docker build FAILED (docker が無い環境ならスキップし報告に記載)"
git add Dockerfile docker-compose.yml swim_worker/__main__.py tests/test_single_instance.py
git commit -m "Docker 経路の修正: 存在しない ca.crt の COPY を削除し /app/data を書き込み可能に、ロック作成失敗を明示

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HvJvyome4qYgikSoCTLSjT"
```

---

### Task 9: ロックパス固定と CA 一時ファイル廃止 (M-1, M-2)

**Files:**
- Modify: `swim_worker/single_instance.py` (`get_lock_path`)、`swim_worker/certs.py`、`swim_worker/redis_client.py`
- Test: `tests/test_single_instance.py`、`tests/test_redis_client.py`

**Interfaces:**
- Produces: `create_redis_client(settings)` は `settings.redis_ca_cert` が空なら `ssl_ca_data=CA_CERT_PEM` を、指定があれば `ssl_ca_certs=<path>` を渡す

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_redis_client.py` の既存テストを更新・追加:

```python
    def test_uses_embedded_ca_data_when_no_external_cert(self, monkeypatch):
        from swim_worker.redis_client import create_redis_client
        from swim_worker.certs import CA_CERT_PEM
        settings = _settings(monkeypatch)
        settings = settings.model_copy(update={"redis_ca_cert": ""})
        with patch("swim_worker.redis_client.aioredis.Redis") as mock_cls:
            create_redis_client(settings)
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["ssl_ca_data"] == CA_CERT_PEM
        assert "ssl_ca_certs" not in kwargs

    def test_uses_external_cert_path_when_configured(self, monkeypatch):
        from swim_worker.redis_client import create_redis_client
        settings = _settings(monkeypatch)  # REDIS_CA_CERT=/tmp/ca.crt を設定している
        with patch("swim_worker.redis_client.aioredis.Redis") as mock_cls:
            create_redis_client(settings)
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["ssl_ca_certs"] == "/tmp/ca.crt"
        assert "ssl_ca_data" not in kwargs
```

既存の `test_passes_worker_name_as_client_name` の `assert kwargs["ssl_ca_certs"] == "/tmp/ca.crt"` はそのまま残る (外部指定あり)。

`tests/test_single_instance.py` に追加:

```python
def test_frozen_lock_path_is_always_under_data_dir(tmp_path, monkeypatch):
    import sys
    from swim_worker import single_instance
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "swim-worker.exe"))
    first = single_instance.get_lock_path()
    assert first == tmp_path / "data" / "swim-worker.lock"
    assert (tmp_path / "data").is_dir()  # 無ければ作る
    assert single_instance.get_lock_path() == first
```

- [ ] **Step 2: 失敗確認** — 3 件 FAIL

- [ ] **Step 3: 実装**

`redis_client.py`:

```python
from swim_worker.certs import CA_CERT_PEM
...
    kwargs = dict(
        host=settings.redis_host, port=settings.redis_port, password=settings.redis_password,
        ssl=True, decode_responses=True, socket_timeout=settings.redis_socket_timeout,
        client_name=settings.worker_name,
    )
    if settings.redis_ca_cert:
        kwargs["ssl_ca_certs"] = settings.redis_ca_cert
    else:
        # 一時ファイルを作らず埋め込み CA をそのまま渡す (redis-py >= 5 の ssl_ca_data)
        kwargs["ssl_ca_data"] = CA_CERT_PEM
    return aioredis.Redis(**kwargs)
```

(`get_ca_cert_path` の import は不要になれば削除。)

`certs.py` `get_ca_cert_path()`: `mkstemp` 後に

```python
    import atexit
    atexit.register(_remove_temp_cert)
```

と、モジュールレベルに

```python
def _remove_temp_cert() -> None:
    global _temp_cert_path
    if _temp_cert_path:
        try:
            os.remove(_temp_cert_path)
        except OSError:
            pass
        _temp_cert_path = None
```

(Dockerfile の HEALTHCHECK が `get_ca_cert_path()` を使うので関数自体は残す。)

`single_instance.py` `get_lock_path()` の frozen 分岐:

```python
    if getattr(sys, "frozen", False):
        data_dir = Path(sys.executable).parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "swim-worker.lock"
```

docstring の優先順の説明を更新 (「data/ が無ければ作る。初回起動と 2 回目で場所が変わらないようにするため」)。

- [ ] **Step 4: テスト実行 → コミット**

```bash
python3 -m pytest tests/ -q
git add swim_worker/single_instance.py swim_worker/certs.py swim_worker/redis_client.py tests/test_single_instance.py tests/test_redis_client.py
git commit -m "ロックファイルの場所を data/ に固定し、埋め込み CA は ssl_ca_data で渡して一時ファイルを作らない

- 初回起動 (data/ 無し) と 2 回目でロックパスが変わり多重起動を防げなかった
- 起動ごとに %TEMP% に CA の一時ファイルが溜まっていた (作った場合も atexit で削除)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HvJvyome4qYgikSoCTLSjT"
```

---

### Task 10: install.sh のロールバック後再試行抑止 (M-8)

**Files:**
- Modify: `scripts/install.sh`
- Test: `tests/test_install_sh.py`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_install_sh.py` に追加:

```python
def test_install_sh_records_failed_version_and_skips_it():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert ".failed-version" in text
    # ロールバック時に記録
    rollback = text[text.index("# ロールバック"):]
    assert 'echo "$LATEST_VERSION" > "${INSTALL_DIR}/.failed-version"' in rollback
    # ガード評価の前でスキップ
    guard = text[text.index("--- ガード1"):text.index("--- ガード2")]
    assert ".failed-version" in text[:text.index("--- ガード1")]
```

- [ ] **Step 2: 失敗確認** — FAIL

- [ ] **Step 3: 実装**

`install.sh` の `--auto` モードで、ダウングレード防止チェックの直後・`--- ガード1` の前に:

```bash
    # --- ガード0: 前回この版でロールバックしていれば再試行しない (手動 install.sh で解除) ---
    FAILED_VERSION_FILE="${INSTALL_DIR}/.failed-version"
    if [[ -f "$FAILED_VERSION_FILE" ]]; then
        FAILED_VERSION=$(cat "$FAILED_VERSION_FILE")
        if [[ "$FAILED_VERSION" == "$LATEST_VERSION" ]]; then
            log "v${LATEST_VERSION} は前回ロールバックした版のため skip (手動で install.sh を実行すると解除)"
            exit 0
        fi
        rm -f "$FAILED_VERSION_FILE"   # より新しい版が出たので解除
    fi
```

ロールバック処理 (`# ロールバック` コメント以下、`rm -f "${INSTALL_DIR}/swim-worker.old"` の前) に:

```bash
    echo "$LATEST_VERSION" > "${INSTALL_DIR}/.failed-version"
```

通常モード (非 --auto) のインストール完了時 (`.version` 書き込み付近) に `rm -f "${INSTALL_DIR}/.failed-version"` を追加。

- [ ] **Step 4: 確認 → コミット**

```bash
bash -n scripts/install.sh && shellcheck -S warning scripts/install.sh && python3 -m pytest tests/ -q
git add scripts/install.sh tests/test_install_sh.py
git commit -m "install.sh --auto: ロールバックした版を記録し、同じ版を 6 時間ごとに再試行しない

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HvJvyome4qYgikSoCTLSjT"
```

---

### Task 11: ドキュメント

**Files:**
- Modify: `README.md`、`docs/architecture.md`

- [ ] **Step 1: README**

`grep -n "自動接続\|macOS\|Apple\|Worker名\|worker_name" README.md` で位置を確認し:
- 「起動時に自動接続」チェックの説明 (ON にすると起動時に自動で Worker が接続する。更新後の再起動でも接続まで自動)
- macOS: 「Apple Silicon (arm64) のみ。Intel Mac は非対応」
- 「exe は自分専用のフォルダ (例: `C:\Users\<name>\swim-worker\`) に置く」推奨 (設定ファイルと Cookie が同階層に保存されるため)
- Worker 名の制約 (Task 1 で追記済みなら重複しないよう確認)

- [ ] **Step 2: architecture.md**

「既知の障害」節の末尾に v1.1.3 の項を追加:

```markdown
### v1.1.3 での修正 (2026-09-12 レビュー)

- 更新後の起動確認: `.startup_ok` を GUI が表示された時点 (2 秒生存) で書く。以前は Worker が Redis に接続した時にしか書かれず、「起動時に自動接続」OFF の利用者は更新のたびに 120 秒後にロールバックされていた
- ロールバック後は同一バージョンを snooze し、2 回連続なら自動更新を OFF にして通知する
- Windows ヘルパー: 新 exe への置き換えに失敗した場合も旧 exe を再起動する (`reason=move_failed`)
- macOS ヘルパー: 停止は PID 指定 (`pkill -f` は自分自身を止めていた)、PyInstaller 環境変数のリセット、トレイはメインスレッドで実行。Apple Silicon のみ対応
- Worker 名は `[A-Za-z0-9._-]{1,32}` に制限 (`CLIENT SETNAME` 失敗の予防)
- GUI の停止は Redis 再試行中でも中断でき、停止完了までは再起動できない。停止時に SWIM/Redis クライアントを解放する
- capability テストの 403 では再ログイン・Cookie 破棄をしない
- GUI ログは 5MB × 3 世代でローテーション
- ロックファイルは常に `data/swim-worker.lock`。埋め込み CA は一時ファイルを作らず渡す
- Docker 経路 (上級者向け) がビルド・起動できるよう修正
- `install.sh --auto` はロールバックした版を `.failed-version` に記録し再試行しない
```

- [ ] **Step 3: コミット**

```bash
git add README.md docs/architecture.md
git commit -m "docs: v1.1.3 の修正内容と Worker 名・自動接続・macOS 対応範囲を記載

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HvJvyome4qYgikSoCTLSjT"
```

---

### Task 12: リリース v1.1.3 と段階配布

**Files:** `swim_worker/__init__.py`

- [ ] **Step 1: バージョン更新・タグ**

```bash
sed -i 's/^__version__ = "1.1.2"$/__version__ = "1.1.3"/' swim_worker/__init__.py
python3 -m pytest tests/ -q
git add swim_worker/__init__.py
git commit -m "v1.1.3

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HvJvyome4qYgikSoCTLSjT"
git tag -a v1.1.3 -m "v1.1.3: GUI 更新のロールバック無限ループ修正、Worker 名検証、停止経路、ヘルパー、ログローテ、Docker 経路"
git push origin master && git push origin v1.1.3
```

- [ ] **Step 2: CI 完了確認**

`gh run watch <id> --exit-status` → success、`gh release view v1.1.3 --json assets -q '.assets|length'` → 10

- [ ] **Step 3: 段階配布** (この Step は人が判断する: どの Windows 機で先行検証するか)

1. `swim-admin update stage <windows-worker>` で 1 台に絞る
2. その利用者に「起動時に自動接続」を **OFF** にしてもらい、ポップアップから更新 → 120 秒待ってもロールバックされないことを確認 (`swim-admin status` で v1.1.3・alive)
3. 問題なければ `swim-admin update full`。VPS は timer で自動更新
4. `journalctl -u swim-worker` (VPS) と GUI ログに新しい ERROR が無いこと

---

## Self-Review

**Spec coverage:** C-1/I-5 → T2 ✅、I-1 → T1 ✅、I-2/I-9/M-3/M-6 → T3 ✅、I-3 → T4 ✅、I-4/I-6 → T5 ✅、I-7 → T6 ✅、I-8 → T7 ✅、I-10 → T8 ✅、M-1/M-2 → T9 ✅、M-8 → T10 ✅、docs → T11 ✅、リリース → T12 ✅。対象外 (I-3 の Coordinator 側解釈、M-3〜5, 7, 9〜14) は触れない ✅

**Placeholder scan:** T3 は tkinter 依存でテストなし (静的確認で代替) と明記。T8 Step 1 のテストは既存挙動でも PASS することを明記 (メッセージ化は手動確認)。T12 Step 3 は人の判断ステップ。TBD/TODO なし。

**Type consistency:** `_next_rollback_state(dict, str) -> (dict, bool)` を T2 の実装・テストで一致。`_build_windows_update_script` / `_build_macos_update_script` のキーワード引数名を T5 のテストと実装で一致。`create_redis_client` の `ssl_ca_data` / `ssl_ca_certs` 分岐を T9 のテストと一致。`execute_api(..., retry_on_auth_error=False)` を T4 の consumer 呼び出しとテストで一致。
