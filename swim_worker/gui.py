"""SWIM Worker GUI

スレッドの決まり: Tk (ウィジェット・Tk 変数・after) を触るのはメインスレッドだけ。
Worker・トレイ・アップデートのスレッドは self._post(fn, ...) でキューに積み、メインスレッドが
UI_POLL_MS ごとにまとめて取り出して実行する (ログも同じキューでまとめて画面に足す)。
"""
import base64
import io
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from pathlib import Path

from swim_worker import __version__
from swim_worker import autostart, paths, updater
from swim_worker.gui_helpers import (
    next_rollback_state,
    task_state_text,
    update_prompt_kind,
    validate_fields,
    write_startup_marker,
)
from swim_worker.icon import create_icon
from swim_worker.settings_store import (
    SettingsStore, load_json as _load_json, save_json as _save_json,
)
from swim_worker.worker_runner import WorkerRunner, DUPLICATE, AUTH_ERROR, STOPPED

# System tray support (Windows + macOS)
# 実際の描画は swim_worker.icon.create_icon に委譲するため、ここでは pystray の有無だけ判定。
_HAS_TRAY = False
if sys.platform in ("win32", "darwin"):
    try:
        import pystray
        _HAS_TRAY = True
    except ImportError:
        pass


def _get_base_dir() -> Path:
    """.env・data/・ログの置き場所 (exe のフォルダ。開発環境ではカレントディレクトリ)"""
    return paths.base_dir()


ENV_PATH = _get_base_dir() / ".env"
GUI_SETTINGS_PATH = _get_base_dir() / "data" / "gui_settings.json"
UPDATE_SNOOZE_PATH = _get_base_dir() / "data" / "update_snooze.json"
SNOOZE_DURATION_HOURS = updater.SNOOZE_DURATION_HOURS
UI_POLL_MS = 100           # キューを取り出す間隔
LOG_MAX_LINES = 1000       # 画面のログはこれを超えたら古い行を消す
LOG_KEEP_LINES = 800
QUIT_GRACEFUL_SEC = 30.0   # 終了時、処理中のタスクを待つ上限
UPDATE_GRACEFUL_SEC = 60.0  # 更新前、処理中のタスクを待つ上限


class UpdateProgressDialog:
    """アップデート進捗ダイアログ (Toplevel + Progressbar + ステータス表示)"""

    def __init__(self, parent: tk.Tk, title: str = "アップデート中"):
        self._win = tk.Toplevel(parent)
        self._win.title(title)
        self._win.geometry("420x150")
        self._win.resizable(False, False)
        self._win.transient(parent)
        # Xボタン無効化 (進行中はユーザーが閉じられないように)
        self._win.protocol("WM_DELETE_WINDOW", lambda: None)
        try:
            self._win.grab_set()  # モーダル化
        except tk.TclError:
            pass

        self._status_var = tk.StringVar(value="準備中...")
        ttk.Label(self._win, textvariable=self._status_var,
                  font=("", 10, "bold")).pack(pady=(20, 8))
        self._progress = ttk.Progressbar(self._win, mode="determinate", length=380)
        self._progress.pack(pady=8, padx=20)
        self._detail_var = tk.StringVar(value="")
        ttk.Label(self._win, textvariable=self._detail_var,
                  font=("", 9)).pack(pady=(0, 10))
        self._closed = False

    def set_status(self, text: str) -> None:
        if self._closed:
            return
        self._status_var.set(text)

    def set_progress(self, percent: float, detail: str = "") -> None:
        if self._closed:
            return
        self._progress.configure(mode="determinate")
        self._progress["value"] = max(0, min(100, percent))
        if detail:
            self._detail_var.set(detail)

    def set_indeterminate(self, detail: str = "") -> None:
        if self._closed:
            return
        self._progress.configure(mode="indeterminate")
        self._progress.start(20)
        if detail:
            self._detail_var.set(detail)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._progress.stop()
        except Exception:
            pass
        try:
            self._win.grab_release()
        except Exception:
            pass
        try:
            self._win.destroy()
        except Exception:
            pass


class CountdownDialog:
    """自動アップデートの 5 秒カウントダウン (今すぐ / スキップ)。

    Tk 変数を閉包の循環参照に入れない (別スレッドの GC で Tk を触らないように)。
    """

    def __init__(self, parent: tk.Tk, new_version: str, *, on_update, on_skip,
                 seconds: int = 5):
        self._parent = parent
        self._on_update = on_update
        self._on_skip = on_skip
        self._remaining = seconds
        self._done = False
        self._win = tk.Toplevel(parent)
        self._win.title("自動アップデート")
        self._win.geometry("420x160")
        self._win.resizable(False, False)
        self._win.transient(parent)
        try:
            self._win.grab_set()
        except tk.TclError:
            pass
        ttk.Label(self._win, text=f"新しいバージョン v{new_version} が利用可能です",
                  font=("", 10, "bold")).pack(pady=(15, 4))
        ttk.Label(self._win, text=f"現在: v{__version__}", font=("", 9)).pack(pady=(0, 10))
        self._msg_var = tk.StringVar(master=self._win, value=self._message())
        ttk.Label(self._win, textvariable=self._msg_var, font=("", 10)).pack(pady=(0, 8))
        btn_frame = ttk.Frame(self._win)
        btn_frame.pack(pady=(0, 10))
        ttk.Button(btn_frame, text="今すぐ", command=self._update_now).pack(side="left", padx=5)
        ttk.Button(btn_frame, text=f"スキップ ({SNOOZE_DURATION_HOURS}h)",
                   command=self._skip).pack(side="left", padx=5)
        self._win.protocol("WM_DELETE_WINDOW", self._skip)
        parent.after(1000, self._tick)

    def _message(self) -> str:
        return f"{self._remaining} 秒後に自動でアップデートします..."

    def _close(self) -> None:
        self._done = True
        try:
            self._win.grab_release()
        except Exception:
            pass
        try:
            self._win.destroy()
        except Exception:
            pass

    def _update_now(self) -> None:
        if self._done:
            return
        self._close()
        self._on_update()

    def _skip(self) -> None:
        if self._done:
            return
        self._close()
        self._on_skip()

    def _tick(self) -> None:
        if self._done:
            return
        self._remaining -= 1
        if self._remaining <= 0:
            self._update_now()
            return
        self._msg_var.set(self._message())
        self._parent.after(1000, self._tick)


class TextHandler(logging.Handler):
    """ログを画面に出すハンドラー。どのスレッドから呼ばれてもキューに積むだけ"""
    def __init__(self, sink):
        super().__init__()
        self._sink = sink  # 文字列を受け取る関数 (WorkerGUI._post_log)

    def emit(self, record):
        try:
            self._sink(self.format(record))
        except Exception:
            self.handleError(record)


class WorkerGUI:
    def __init__(self):
        self._root = tk.Tk()
        self._root.title("SWIM Worker")
        self._root.geometry("520x640")
        self._root.minsize(420, 400)
        self._root.resizable(True, True)

        self._runner: WorkerRunner | None = None
        self._worker_running = False
        # 別スレッドからの UI 操作の受け口 (メインスレッドが after でまとめて取り出す)
        self._ui_queue: queue.Queue = queue.Queue()
        self._closed = False
        self._quitting = False
        self._tray_color = "gray"
        self._log_handler: TextHandler | None = None
        # GUI 設定 (auto_update 等) と snooze 情報を永続化
        self._gui_settings: dict = _load_json(GUI_SETTINGS_PATH)
        self._snooze = updater.SnoozeStore(UPDATE_SNOOZE_PATH)
        # .env とパスワード (Windows・macOS は OS の資格情報ストア)
        self._store = SettingsStore(ENV_PATH)
        self._update_progress_dialog: UpdateProgressDialog | None = None
        # 古いアップデート関連ファイルを掃除 (3 日超過の .old / .new、期限切れ snooze)
        self._cleanup_stale_update_files()
        # Phase 2: 前回アップデートがロールバックされていたら通知 (起動後に表示)
        self._check_rollback_marker_after_ready()

        self._tray_icon = None
        self._build_ui()
        self._set_window_icon()
        self._load_env()
        self._root.after(UI_POLL_MS, self._pump_ui_queue)

    def _build_ui(self):
        root = self._root

        # --- 状態表示 ---
        status_frame = ttk.LabelFrame(root, text="状態", padding=10)
        status_frame.pack(fill="x", padx=10, pady=(10, 5))

        self._status_var = tk.StringVar(value="停止中")
        self._status_label = ttk.Label(status_frame, textvariable=self._status_var, font=("", 12, "bold"))
        self._status_label.pack()

        # --- 設定 (折りたたみ可能。初回設定後はあまり見ないため隠せるようにする) ---
        settings_toggle_frame = ttk.Frame(root)
        settings_toggle_frame.pack(fill="x", padx=10, pady=(5, 0))

        self._settings_collapsed = bool(self._gui_settings.get("settings_collapsed", False))
        self._settings_toggle_btn = ttk.Button(
            settings_toggle_frame, command=self._toggle_settings_section,
        )
        self._settings_toggle_btn.pack(side="left")

        settings_frame = ttk.LabelFrame(root, text="設定", padding=10)
        self._settings_frame = settings_frame

        fields = [
            ("Redis ホスト:", "redis_host", False),
            ("Redis ユーザー名:", "redis_username", False),
            ("Redis パスワード:", "redis_password", True),
            ("SWIM ID:", "swim_username", False),
            ("SWIM パスワード:", "swim_password", True),
            ("Worker 名:", "worker_name", False),
        ]

        self._entries: dict[str, ttk.Entry] = {}
        for i, (label, key, is_secret) in enumerate(fields):
            ttk.Label(settings_frame, text=label).grid(row=i, column=0, sticky="w", pady=2)
            entry = ttk.Entry(settings_frame, width=40, show="*" if is_secret else "")
            entry.grid(row=i, column=1, sticky="ew", padx=(10, 0), pady=2)
            self._entries[key] = entry

        settings_frame.columnconfigure(1, weight=1)

        # --- ボタン ---
        btn_frame = ttk.Frame(root, padding=10)
        btn_frame.pack(fill="x", padx=10)
        self._btn_frame = btn_frame

        # 設定欄の初期表示状態を反映 (トグルボタンのpack/pack_forgeもここで確定)
        self._apply_settings_collapsed_state()

        self._start_btn = ttk.Button(btn_frame, text="▶ 起動", command=self._on_start)
        self._start_btn.pack(side="left", padx=(0, 5))

        self._stop_btn = ttk.Button(btn_frame, text="■ 停止", command=self._on_stop, state="disabled")
        self._stop_btn.pack(side="left", padx=(0, 15))

        self._save_btn = ttk.Button(btn_frame, text="設定保存", command=self._save_env)
        self._save_btn.pack(side="left")

        # アップデートボタン (新バージョン検知時のみ表示)
        self._pending_update_version: str | None = None
        self._update_btn = ttk.Button(
            btn_frame, text="⬆ アップデート", command=self._on_update_click,
        )
        # 初期状態は非表示 (pack_forget 相当 — 初回は pack しない)

        # --- 自動接続 ---
        opt_frame = ttk.Frame(root, padding=(10, 0))
        opt_frame.pack(fill="x", padx=10)

        self._autoconnect_var = tk.BooleanVar(value=False)
        autoconnect_cb = ttk.Checkbutton(
            opt_frame, text="起動時に自動接続",
            variable=self._autoconnect_var, command=self._save_autoconnect,
        )
        autoconnect_cb.pack(side="left")

        # --- OS自動起動 (Windows / macOS) ---
        if sys.platform in ("win32", "darwin"):
            self._autostart_var = tk.BooleanVar(value=self._check_autostart())
            label = "ログイン時に自動起動" if sys.platform == "darwin" else "Windows起動時に自動起動"
            autostart_cb = ttk.Checkbutton(
                opt_frame, text=label,
                variable=self._autostart_var, command=self._toggle_autostart,
            )
            autostart_cb.pack(side="right")

        # --- 自動アップデート (Windows / macOS GUI 版のみ) ---
        # Linux CLI 版は install.sh の systemd timer で完全自動化されているため、
        # GUI 版にだけ「チェックボックスで自動適用を有効化」機能を提供する。
        if sys.platform in ("win32", "darwin"):
            opt_frame2 = ttk.Frame(root, padding=(10, 0))
            opt_frame2.pack(fill="x", padx=10)
            self._auto_update_var = tk.BooleanVar(
                value=bool(self._gui_settings.get("auto_update", False))
            )
            auto_update_cb = ttk.Checkbutton(
                opt_frame2,
                text="アップデートを自動適用 (確認ダイアログなし、5秒カウントダウンのみ)",
                variable=self._auto_update_var,
                command=self._on_auto_update_toggle,
            )
            auto_update_cb.pack(side="left")

        # --- ログ ---
        log_frame = ttk.LabelFrame(root, text="ログ", padding=5)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(5, 10))

        self._log_text = scrolledtext.ScrolledText(log_frame, height=12, state="disabled", font=("Consolas", 9))
        self._log_text.pack(fill="both", expand=True)

        # ログハンドラー設定
        handler = TextHandler(self._post_log)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
        logging.root.addHandler(handler)
        logging.root.setLevel(logging.INFO)
        self._log_handler = handler

        self._setup_tray()

        # 起動時に運用者向けに現在の自動更新設定をログへ (トラブルシュート用)
        if sys.platform in ("win32", "darwin"):
            auto_state = "有効" if self._auto_update_var.get() else "無効"
            logging.info(
                "GUI 起動 (v%s) / アップデート自動適用: %s",
                __version__, auto_state,
            )

    # --- スレッド間 (メインスレッドへの受け渡し) ---
    def _post(self, fn, *args) -> None:
        """どのスレッドからでも呼べる。fn(*args) をメインスレッドで実行する"""
        self._ui_queue.put((fn, args))

    def _post_log(self, line: str) -> None:
        self._ui_queue.put((None, line))

    def _drain_ui_queue(self) -> None:
        """キューに溜まったものをまとめて実行する (メインスレッド)"""
        logs: list[str] = []
        while True:
            try:
                fn, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            if fn is None:
                logs.append(args)
                continue
            if logs:
                self._append_logs(logs)
                logs = []
            if self._closed:
                continue
            try:
                fn(*args)
            except Exception:
                logging.exception("UI の更新に失敗")
        if logs:
            self._append_logs(logs)

    def _pump_ui_queue(self) -> None:
        if self._closed:
            return
        self._drain_ui_queue()
        if not self._closed:
            self._root.after(UI_POLL_MS, self._pump_ui_queue)

    def _append_logs(self, lines: list[str]) -> None:
        if self._closed:
            return
        try:
            self._log_text.configure(state="normal")
            self._log_text.insert(tk.END, "\n".join(lines) + "\n")
            n = int(self._log_text.index("end-1c").split(".")[0])
            if n > LOG_MAX_LINES:
                self._log_text.delete("1.0", f"{n - LOG_KEEP_LINES}.0")
            self._log_text.see(tk.END)
            self._log_text.configure(state="disabled")
        except tk.TclError:
            pass  # 画面を閉じた後

    def _set_tray_color(self, color: str) -> None:
        self._tray_color = color
        if _HAS_TRAY and self._tray_icon:
            try:
                self._tray_icon.icon = create_icon(color=color, size=64)
            except Exception as e:
                logging.debug("トレイアイコン更新失敗 (無視): %s", e)

    def _set_ui_running(self, running: bool, status: str | None = None,
                        tray: str | None = None) -> None:
        """実行中フラグ・ボタン・設定欄・トレイの色をまとめて切り替える (メインスレッド)"""
        self._worker_running = running
        self._start_btn.configure(state="disabled" if running else "normal")
        self._stop_btn.configure(state="normal" if running else "disabled")
        for entry in self._entries.values():
            entry.configure(state="disabled" if running else "normal")
        if status is not None:
            self._status_var.set(status)
        self._set_tray_color(tray or ("green" if running else "gray"))

    # --- System tray ---
    def _set_window_icon(self):
        """ウィンドウタイトルバー (Windows/Linux) と Dock (macOS) のアイコンを設定する。

        tkinter の iconphoto は PhotoImage を要求するため、PIL Image を
        PNG → base64 経由で渡す (ImageTk 不要)。
        """
        try:
            img = create_icon(color="green", size=256)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            data = base64.b64encode(buf.getvalue())
            photo = tk.PhotoImage(data=data)
            self._root.iconphoto(True, photo)
            self._window_icon_photo = photo  # GC 防止のため参照を保持
        except Exception as e:
            logging.debug("ウィンドウアイコン設定失敗 (無視): %s", e)

    def _setup_tray(self):
        """Setup system tray icon — 起動時にバックグラウンドスレッドで常駐開始"""
        if not _HAS_TRAY:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("表示", self._tray_show, default=True),
                pystray.MenuItem("終了", self._tray_quit),
            )
            self._tray_icon = pystray.Icon(
                "swim-worker",
                create_icon(color="gray", size=64),
                "SWIM Worker",
                menu,
            )
            # 起動時にトレイアイコンを常駐開始 (最小化時に即反応できるように)
            if sys.platform == "darwin":
                # AppKit はメインスレッド必須。pystray の tkinter 併用向け API を使う
                self._tray_icon.run_detached()
            else:
                self._tray_thread = threading.Thread(target=self._tray_icon.run, daemon=True)
                self._tray_thread.start()
        except Exception as e:
            logging.warning("システムトレイ初期化失敗（トレイ機能を無効化）: %s", e)
            self._tray_icon = None

    def _tray_show(self, icon=None, item=None):
        """Show the main window from tray (pystray のスレッドから呼ばれる)"""
        def restore():
            self._root.deiconify()
            self._root.state("normal")
            self._root.lift()
            self._root.focus_force()
        self._post(restore)

    def _tray_quit(self, icon=None, item=None):
        """Quit from tray (pystray のスレッドから呼ばれる)"""
        self._post(self._force_quit)

    def _force_quit(self):
        """アプリを終了する。Worker が動いていれば、処理中のタスクを終えて止まるのを
        別スレッドで待ってから画面を閉じる (heartbeat の lock を残さないため)"""
        if self._quitting:
            return
        self._quitting = True
        runner = self._runner
        if runner is None or not runner.is_alive():
            self._destroy()
            return
        self._worker_running = False
        self._status_var.set("終了処理中 (処理中のタスクを終えてから閉じます)...")
        self._start_btn.configure(state="disabled")
        self._stop_btn.configure(state="disabled")

        def wait_then_close():
            if not runner.stop_and_join(QUIT_GRACEFUL_SEC, 10.0):
                logging.warning("Worker が止まらないまま終了します")
            self._post(self._destroy)

        threading.Thread(target=wait_then_close, daemon=True, name="swim-worker-quit").start()

    def _destroy(self):
        """画面を閉じる (メインスレッド)"""
        if self._closed:
            return
        self._closed = True
        if self._log_handler is not None:
            logging.root.removeHandler(self._log_handler)
            self._log_handler = None
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        try:
            self._root.destroy()
        except tk.TclError:
            pass

    def _quit_for_update(self):
        """アップデート用の完全終了: exeファイルのロックを確実に解放するため os._exit を使う
        (Worker は _do_update が止めてある)"""
        try:
            self._destroy()
        finally:
            # daemon スレッドが残っていても強制終了 (ファイルロック即解放)
            os._exit(0)

    def _minimize_to_tray(self):
        """Minimize window to system tray — タスクバーからも消える"""
        if not _HAS_TRAY or not self._tray_icon:
            return
        # 最小化状態を解除してから withdraw (iconic 状態だと withdraw が効かないことがある)
        try:
            self._root.state("normal")
        except tk.TclError:
            pass
        self._root.withdraw()  # ウィンドウ非表示（タスクバーからも消える）
        logging.info("システムトレイに格納しました")

    def _load_env(self):
        """既存の.envと資格情報ストアから設定を読み込む"""
        try:
            fields, auto_connect = self._store.load()
        except Exception as e:
            logging.warning("設定の読み込みに失敗: %s", e)
            return
        if auto_connect is not None:
            self._autoconnect_var.set(auto_connect)
        for field, value in fields.items():
            if field in self._entries:
                self._entries[field].delete(0, tk.END)
                self._entries[field].insert(0, value)

    def _save_env(self) -> bool:
        """設定を保存 (.env は 0600、パスワードは使えれば OS の資格情報ストア)"""
        fields = {k: e.get() for k, e in self._entries.items()}
        try:
            where = self._store.save(fields, bool(self._autoconnect_var.get()))
        except Exception as e:
            logging.error("設定の保存に失敗: %s", e)
            messagebox.showerror("エラー", f"設定を保存できませんでした:\n{e}")
            return False
        if where == "keyring":
            logging.info("設定を保存しました (パスワードは OS の資格情報ストアに保存)")
        else:
            logging.info("設定を保存しました")
        return True

    def _save_autoconnect(self):
        """自動接続チェックボックス変更時に.envを更新"""
        try:
            if not self._store.set_auto_connect(bool(self._autoconnect_var.get())):
                return
        except Exception as e:
            logging.warning("自動接続の設定を保存できません: %s", e)
            return
        state = "有効" if self._autoconnect_var.get() else "無効"
        logging.info("自動接続を%sにしました", state)

    def _apply_settings_collapsed_state(self):
        """self._settings_collapsed の値に合わせて設定欄の表示/非表示とボタン表記を更新する。

        再展開時は before=self._btn_frame でボタン行の直前に挿入し直すことで、
        pack順(状態→設定→ボタン→...→ログ)を維持する。
        """
        if self._settings_collapsed:
            self._settings_frame.pack_forget()
            self._settings_toggle_btn.configure(text="▶ 設定を表示")
        else:
            self._settings_frame.pack(fill="x", padx=10, pady=5, before=self._btn_frame)
            self._settings_toggle_btn.configure(text="▼ 設定を隠す")

    def _toggle_settings_section(self):
        """設定欄の折りたたみボタン押下時: 開閉を切り替えて gui_settings.json に永続化する。"""
        self._settings_collapsed = not self._settings_collapsed
        self._apply_settings_collapsed_state()
        self._gui_settings["settings_collapsed"] = self._settings_collapsed
        try:
            _save_json(GUI_SETTINGS_PATH, self._gui_settings)
        except Exception as e:
            logging.warning("GUI 設定保存失敗: %s", e)

    def _on_start(self):
        """Worker起動"""
        if self._quitting:
            return
        fields = {k: e.get() for k, e in self._entries.items()}
        problem = validate_fields(fields)
        if problem:
            messagebox.showerror("エラー", problem)
            return

        if self._runner is not None and self._runner.is_alive():
            messagebox.showinfo("停止処理中", "前回の停止処理が完了していません。しばらく待ってから再度押してください。")
            return

        # 設定保存してから起動
        if not self._save_env():
            return

        # UIスレッドで値をコピー（別スレッドから Tk を触らない）。パスワードは strip しない
        self._worker_settings = {
            k: (v if k in ("redis_password", "swim_password") else v.strip())
            for k, v in fields.items()
        }

        self._set_ui_running(True, status="● 起動中...")
        self._runner = WorkerRunner(
            self._worker_settings,
            on_status=self._on_worker_status,
            on_finished=self._on_worker_finished,
            on_update_available=self._on_update_detected,
            on_task_state=self._on_task_state_changed,
        )
        self._runner.start()

    def _on_stop(self):
        """Worker停止。処理中のタスクは結果を書いてから止まる (Redis の再試行中ならすぐ止まる)。
        UI を戻すのは Worker が終わったとき (_handle_worker_finished)"""
        self._worker_running = False
        if self._runner is not None:
            self._runner.request_stop(graceful=True)
        self._set_tray_color("gray")
        self._status_var.set("停止処理中 (処理中のタスクを終えてから止まります)...")
        self._stop_btn.configure(state="disabled")

    # --- Worker からの知らせ (Worker のスレッドで呼ばれる → メインスレッドへ) ---
    def _on_worker_status(self, text: str):
        self._post(self._set_status_if_running, text)

    def _set_status_if_running(self, text: str):
        if self._worker_running:
            self._status_var.set(text)

    def _on_worker_finished(self, outcome: str, message: str):
        self._post(self._handle_worker_finished, outcome, message)

    def _handle_worker_finished(self, outcome: str, message: str):
        """Worker が終わった (メインスレッド)。どの終わり方でも UI を起動前に戻す"""
        if self._quitting:
            return
        if outcome == STOPPED:
            self._set_ui_running(False, status="停止中")
            logging.info("Worker停止")
        elif outcome == DUPLICATE:
            self._set_ui_running(False, status="重複起動エラー", tray="red")
            # 同じ worker_name の別プロセス/別マシンが稼働中
            messagebox.showerror(
                "SWIM Worker - 重複起動",
                f"同じ Worker 名 '{self._worker_settings['worker_name']}' で"
                f"別のプロセスが稼働中のため起動できません。\n\n"
                f"考えられる原因:\n"
                f"  • 他の PC や VPS で同名ワーカーが動いている\n"
                f"  • 前回クラッシュ時の古い heartbeat が残っている\n"
                f"    (数分で自動解放されます)\n\n"
                f"別の worker_name を設定するか、もう一方を停止してください。",
            )
        elif outcome == AUTH_ERROR:
            self._set_ui_running(
                False, status="Redis 認証エラー (設定欄のユーザー名・パスワードを確認)", tray="red")
        else:
            self._set_ui_running(False, status="エラーで停止しました (ログを確認)", tray="red")

    # --- 自動起動 (Windows / macOS) ---
    def _get_startup_path(self) -> Path:
        """プラットフォーム別の自動起動ファイルパス"""
        return autostart.startup_path()

    def _check_autostart(self) -> bool:
        return autostart.is_enabled()

    def _sync_autostart_path(self):
        """自動起動ファイル内のexeパスが現在のパスと異なる場合、自動で書き直す。

        exeを別フォルダに移動すると、スタートアップフォルダの.bat/plist内に
        残った古いパスが無効になり、Windows再起動時に自動起動しなくなる問題を解消する。
        """
        if not autostart.supported():
            return
        if not getattr(sys, "frozen", False):
            return  # 開発環境では何もしない
        if not autostart.needs_rewrite(self._get_startup_path(), sys.platform,
                                       autostart.launch_command(), _get_base_dir()):
            return
        logging.info("自動起動ファイルを今の exe に合わせて書き直します: %s", sys.executable)
        try:
            self._autostart_var.set(True)
            self._toggle_autostart()
        except Exception as e:
            logging.warning("自動起動パス更新失敗: %s", e)

    def _toggle_autostart(self):
        path = self._get_startup_path()
        if self._autostart_var.get():
            autostart.enable(path, sys.platform, autostart.launch_command(), _get_base_dir())
        else:
            autostart.disable(path)

    def run(self):
        """GUIメインループ"""
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        # 最小化検出: <Unmap> が発火したとき state が 'iconic' ならトレイに格納
        # (Tk には <Iconify> イベントは存在しない。<Unmap> + state チェックが正攻法)
        self._root.bind("<Unmap>", self._on_unmap)

        # 自動起動パスの整合性チェック (exeを移動した場合に自動修正)
        self._sync_autostart_path()

        # 自動接続: 全フィールドが埋まっていれば起動後に自動開始
        if self._autoconnect_var.get():
            fields = {k: e.get() for k, e in self._entries.items()}
            if validate_fields(fields) is None:
                logging.info("自動接続: Workerを起動します")
                self._root.after(500, self._on_start)
            else:
                logging.warning("自動接続: 設定が未入力・不正のためスキップしました")

        # 更新ヘルパーの起動確認: GUI が 2 秒生存したら成功マーカーを書く
        self._root.after(2000, write_startup_marker)
        # 以前の版が OS の資格情報ストアに置いたパスワードを読めない (macOS はキーチェーンを
        # 使わなくなった) ときは、起動確認のあとで入れ直しを促す
        if getattr(self._store, "missing_secrets", None):
            self._root.after(3000, self._ask_reenter_passwords)
        self._root.mainloop()

    def _ask_reenter_passwords(self):
        messagebox.showwarning(
            "パスワードの入れ直し",
            "保存していたパスワードを読めませんでした。\n"
            "Redis と SWIM のパスワードを入れ直して「保存」を押してください。")

    # --- タスク状態更新 (Consumer から別スレッドで呼ばれる) ---
    def _on_task_state_changed(self, state: str, job_type: str = "",
                                total: int = 0, errors: int = 0):
        """Consumer から別スレッドで呼ばれるタスク状態変化コールバック"""
        self._post(self._set_status_if_running, task_state_text(state, job_type, total, errors))

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

        self._gui_settings, disabled = next_rollback_state(self._gui_settings, from_version)
        try:
            _save_json(GUI_SETTINGS_PATH, self._gui_settings)
        except Exception as e:
            logging.debug("GUI 設定保存失敗 (無視): %s", e)
        if from_version != "?":
            self._set_snooze(from_version)

        def notify():
            try:
                if disabled and hasattr(self, "_auto_update_var"):
                    self._auto_update_var.set(False)
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

    def _cleanup_stale_update_files(self) -> None:
        """古いアップデート関連ファイルを削除 (起動時の軽量ハウスキープ)。

        - 3 日以上古い `*.old` / `*.new` / `*.new.exe` ファイルを削除
        - 期限切れ の snooze ファイルを削除 (_is_snoozed で処理されるが念のため)
        """
        updater.cleanup_stale_update_files(_get_base_dir())
        self._snooze.cleanup_expired()

    # --- 自動アップデート ---
    def _on_auto_update_toggle(self) -> None:
        """auto_update チェックボックスの状態を永続化。"""
        self._gui_settings["auto_update"] = bool(self._auto_update_var.get())
        try:
            _save_json(GUI_SETTINGS_PATH, self._gui_settings)
            state = "有効" if self._auto_update_var.get() else "無効"
            logging.info("アップデート自動適用: %s", state)
        except Exception as e:
            logging.warning("GUI 設定保存失敗: %s", e)

    def _is_snoozed(self, version: str) -> bool:
        """指定バージョンに対して現在 snooze 期間中かを返す。"""
        return self._snooze.is_snoozed(version)

    def _set_snooze(self, version: str) -> None:
        """「後で」選択時、このバージョンを一定時間スキップする。"""
        self._snooze.set(version)

    def _clear_snooze(self) -> None:
        """snooze 情報を消去 (Yes 選択時や別バージョン検知時)。"""
        self._snooze.clear()

    def _is_auto_update_enabled(self) -> bool:
        """auto_update 設定が有効か (メインスレッドで呼ぶ。Linux では常に False)。"""
        var = getattr(self, "_auto_update_var", None)
        return var is not None and bool(var.get())

    def _on_update_detected(self, new_version: str):
        """Consumer・GitHub の確認から呼ばれる (別スレッド)。判断はメインスレッドで行う"""
        self._post(self._handle_update_detected, new_version)

    def _handle_update_detected(self, new_version: str):
        """新版を知った (メインスレッド)。

        - 常にアップデートボタンは表示する
        - auto_update 有効: 5秒カウントダウン → 自動アップデート (キャンセル可)
        - auto_update 無効: 従来通り確認ダイアログ
        - snooze 中のバージョンはポップアップ/カウントダウンをスキップ (ボタンは残す)
        """
        self._show_update_button(new_version)

        # 既にこのバージョンで重複プロンプトを抑制
        if getattr(self, "_update_prompted_version", None) == new_version:
            return
        self._update_prompted_version = new_version

        kind = update_prompt_kind(snoozed=self._is_snoozed(new_version),
                                  auto_update=self._is_auto_update_enabled())
        if kind is None:
            logging.info("アップデート v%s は snooze 期間中のためプロンプトを抑制", new_version)
        elif kind == "countdown":
            self._prompt_auto_update_countdown(new_version)
        else:
            self._prompt_update(new_version)

    def _show_update_button(self, new_version: str):
        """メインGUIにアップデートボタンを表示する"""
        self._pending_update_version = new_version
        self._update_btn.configure(text=f"⬆ アップデート (v{new_version})")
        # すでにpack済みならスキップ
        if not self._update_btn.winfo_ismapped():
            self._update_btn.pack(side="right")

    def _on_update_click(self):
        """アップデートボタンクリック時: 確認ダイアログを出す"""
        if not self._pending_update_version:
            return
        self._prompt_update(self._pending_update_version)

    def _get_download_url(self, version: str) -> str | None:
        """バージョンタグからダウンロードURLを組み立てる (GitHub APIを使わない)"""
        return updater.download_url(version, sys.platform)

    def _prompt_update(self, new_version: str):
        """新バージョン検知時の確認ダイアログ (auto_update=OFF 用)。

        「はい」→ アップデート開始
        「いいえ」→ 24h snooze (ボタンからは後でも実行可能)
        """
        download_url = self._get_download_url(new_version)
        if not download_url:
            logging.warning("このプラットフォームはアップデート非対応")
            return
        answer = messagebox.askyesno(
            "アップデートがあります",
            f"新しいバージョン v{new_version} が利用可能です。\n"
            f"現在のバージョン: v{__version__}\n\n"
            f"今すぐアップデートしますか？\n"
            f"（ダウンロード後、自動で再起動します）\n\n"
            f"「いいえ」を選ぶと {SNOOZE_DURATION_HOURS} 時間、確認ダイアログを表示しません\n"
            f"（右上の「アップデート」ボタンからはいつでも実行できます）",
        )
        if not answer:
            self._set_snooze(new_version)
            return
        self._clear_snooze()
        self._start_update(new_version, download_url)

    def _prompt_auto_update_countdown(self, new_version: str):
        """auto_update=ON 時のカウントダウンダイアログ (5秒で自動実行、キャンセル可)。"""
        download_url = self._get_download_url(new_version)
        if not download_url:
            logging.warning("このプラットフォームはアップデート非対応")
            return

        def start():
            self._clear_snooze()
            self._start_update(new_version, download_url)

        CountdownDialog(self._root, new_version, on_update=start,
                        on_skip=lambda: self._set_snooze(new_version))

    def _start_update(self, new_version: str, download_url: str) -> None:
        """進捗ダイアログを表示してダウンロード開始 (UI スレッドから呼ぶ)。"""
        if self._update_progress_dialog is not None:
            logging.debug("既にアップデート進行中、重複起動をスキップ")
            return
        self._update_progress_dialog = UpdateProgressDialog(self._root)
        self._update_progress_dialog.set_status("ダウンロードを準備中...")
        # 別スレッドで実ダウンロード + 差し替え
        threading.Thread(
            target=self._do_update, args=(new_version, download_url), daemon=True,
        ).start()

    # 進捗ダイアログの更新 (_do_update のスレッドから呼ばれる → メインスレッドへ)
    def _update_dialog_status(self, text: str) -> None:
        self._post(self._with_dialog, "set_status", text)

    def _update_dialog_progress(self, percent: float, detail: str = "") -> None:
        self._post(self._with_dialog, "set_progress", percent, detail)

    def _update_dialog_indeterminate(self, detail: str = "") -> None:
        self._post(self._with_dialog, "set_indeterminate", detail)

    def _with_dialog(self, method: str, *args) -> None:
        dlg = self._update_progress_dialog
        if dlg is not None:
            getattr(dlg, method)(*args)

    def _close_update_dialog(self) -> None:
        self._post(self._close_update_dialog_now)

    def _close_update_dialog_now(self) -> None:
        dlg = self._update_progress_dialog
        self._update_progress_dialog = None
        if dlg is not None:
            dlg.close()

    def _show_update_error(self, message: str) -> None:
        messagebox.showerror("アップデート失敗", f"アップデートに失敗しました:\n{message}")

    def _stop_worker_for_update(self) -> None:
        """更新の前に Worker を止めて終わるまで待つ (メインスレッドの外で呼ぶ)。

        処理中のタスクは結果を書いてから止まる。UPDATE_GRACEFUL_SEC で終わらなければ中断する。
        """
        runner = self._runner
        if runner is None or not runner.is_alive():
            return
        self._update_dialog_status("Worker を停止中 (処理中のタスクを終えています)...")
        self._post(self._on_stop)
        if not runner.stop_and_join(UPDATE_GRACEFUL_SEC, 15.0):
            # 止まらなくても続ける (os._exit で終わる。heartbeat の lock は次の起動が回収する)
            logging.warning("Worker が止まらないまま更新を続けます")

    def _do_update(self, new_version: str, download_url: str):
        """新exeをダウンロードし、ヘルパースクリプト経由で置き換え → 再起動 (別スレッド)"""
        try:
            if not getattr(sys, 'frozen', False):
                logging.warning("開発環境ではアップデート不可")
                self._close_update_dialog()
                return
            current_exe = Path(sys.executable)
            logging.info("アップデート v%s をダウンロード中...", new_version)
            self._update_dialog_status("ダウンロード中...")
            self._update_dialog_indeterminate("新しいバージョンを取得しています")

            base = _get_base_dir()
            new_exe = updater.new_exe_path(base)
            mb = 1024 * 1024

            def _progress(done: int, total: int | None) -> None:
                if total:
                    self._update_dialog_progress(
                        done * 100 / total, f"{done / mb:.1f} / {total / mb:.1f} MB")
                else:
                    self._update_dialog_indeterminate(f"{done / mb:.1f} MB")

            updater.download_and_verify(
                download_url, new_exe, on_progress=_progress, on_phase=self._update_dialog_status,
                current_version=__version__)
            size_mb = new_exe.stat().st_size / mb
            logging.info("ダウンロード完了: %s (%.1f MB)", new_exe, size_mb)
            self._update_dialog_progress(100, f"ダウンロード完了 ({size_mb:.1f} MB)")

            self._stop_worker_for_update()

            self._update_dialog_status("差し替えスクリプトを起動中...")
            updater.launch_update_helper(
                base=base, current_exe=current_exe, new_exe=new_exe, new_version=new_version)

            logging.info("アップデータを起動しました。まもなく再起動します")
            self._update_dialog_status("再起動中...")
            self._update_dialog_progress(100, "ヘルパースクリプトに引き継ぎました")
            # 少し後に強制終了 → exe ファイルロック解放 → bat が move 成功
            time.sleep(1.0)
            self._post(self._quit_for_update)
        except Exception as e:
            logging.exception("アップデート失敗")
            self._close_update_dialog()
            self._post(self._show_update_error, str(e))

    def _on_unmap(self, event=None):
        """<Unmap>イベント発生時、最小化されていればトレイに格納する"""
        # トップレベル以外 (子ウィジェット) の Unmap は無視
        if event is not None and event.widget is not self._root:
            return
        if not _HAS_TRAY or not self._tray_icon:
            return
        try:
            state = self._root.state()
        except tk.TclError:
            return
        # 最小化されたときのみトレイへ (withdraw 済みや normal は無視)
        if state == "iconic":
            self._root.after(10, self._minimize_to_tray)

    def _on_close(self):
        """Xボタンが押された時"""
        if self._worker_running and _HAS_TRAY:
            self._minimize_to_tray()
        else:
            self._force_quit()


def _write_crash_log(exc: BaseException) -> None:
    """起動時例外をファイルに書き出す（Windowsの--windowed環境では標準出力が消失するため）"""
    import traceback
    try:
        log_path = _get_base_dir() / "swim-worker-crash.log"
        from datetime import datetime
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n=== {datetime.now().isoformat()} ===\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            f.write("\n")
    except Exception:
        pass  # クラッシュログの書き込みで更にクラッシュしないよう握りつぶす


def main():
    # 早期ファイルログ設定 (exe環境でも起動段階のエラーを追跡可能に)
    try:
        log_path = _get_base_dir() / "swim-worker.log"
        # 5MB × 3 世代でローテーション (無制限に肥大化させない)
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))
        logging.root.addHandler(file_handler)
        logging.root.setLevel(logging.INFO)
        logging.info("swim-worker 起動開始")
    except Exception:
        pass

    # 同一マシン上の多重起動をOSファイルロックで防ぐ。
    # 2個目の exe はここで即座に終了する。
    from swim_worker.single_instance import LocalInstanceLock, AlreadyRunning
    local_lock = LocalInstanceLock()
    try:
        local_lock.acquire()
    except AlreadyRunning as e:
        logging.warning("多重起動検知: %s", e)
        # GUI 環境なのでダイアログで通知してから終了
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "SWIM Worker",
                "SWIM Worker は既に起動しています。\n\n"
                "タスクバーまたはシステムトレイ (レーダー型アイコン) を確認してください。",
            )
            root.destroy()
        except Exception:
            pass
        sys.exit(2)

    try:
        app = WorkerGUI()
        app.run()
    except Exception as e:
        logging.exception("致命的エラーで終了")
        _write_crash_log(e)
        raise
    finally:
        local_lock.release()


if __name__ == "__main__":
    main()
