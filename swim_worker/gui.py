"""SWIM Worker GUI"""
import asyncio
import base64
import io
import json
import logging
import os
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import ttk, scrolledtext, messagebox
from pathlib import Path

from swim_worker import __version__
from swim_worker.icon import create_icon

# System tray support (Windows + macOS)
# 実際の描画は swim_worker.icon.create_icon に委譲するため、ここでは pystray の有無だけ判定。
_HAS_TRAY = False
if sys.platform in ("win32", "darwin"):
    try:
        import pystray
        _HAS_TRAY = True
    except ImportError:
        pass


# .env のデフォルトパス（exeと同じフォルダ）
def _get_base_dir() -> Path:
    """実行ファイルのあるディレクトリを返す"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


ENV_PATH = _get_base_dir() / ".env"
CA_CERT_PATH = _get_base_dir() / "ca.crt"
GUI_SETTINGS_PATH = _get_base_dir() / "data" / "gui_settings.json"
UPDATE_SNOOZE_PATH = _get_base_dir() / "data" / "update_snooze.json"
SNOOZE_DURATION_HOURS = 24


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


def _load_json(path: Path) -> dict:
    """JSON ファイル読み込み (存在しない/壊れていれば空 dict)"""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.debug("設定ファイル読み込み失敗 %s: %s", path, e)
        return {}


def _save_json(path: Path, data: dict) -> None:
    """JSON ファイル書き込み (親ディレクトリ作成込み)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class TextHandler(logging.Handler):
    """ログをtkinter Textウィジェットに表示するハンドラー"""
    def __init__(self, text_widget: scrolledtext.ScrolledText):
        super().__init__()
        self._text = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self._text.configure(state="normal")
            self._text.insert(tk.END, msg + "\n")
            self._text.see(tk.END)
            # 1000行超えたら古い行を削除
            lines = int(self._text.index("end-1c").split(".")[0])
            if lines > 1000:
                self._text.delete("1.0", f"{lines - 800}.0")
            self._text.configure(state="disabled")
        self._text.after(0, append)


class WorkerGUI:
    def __init__(self):
        self._root = tk.Tk()
        self._root.title("SWIM Worker")
        self._root.geometry("520x640")
        self._root.resizable(False, False)

        self._worker_thread: threading.Thread | None = None
        self._worker_running = False
        self._consumer = None
        # GUI 設定 (auto_update 等) と snooze 情報を永続化
        self._gui_settings: dict = _load_json(GUI_SETTINGS_PATH)
        self._update_progress_dialog: UpdateProgressDialog | None = None
        # Phase 2: 前回アップデートがロールバックされていたら通知 (起動後に表示)
        self._check_rollback_marker_after_ready()

        self._tray_icon = None
        self._build_ui()
        self._set_window_icon()
        self._load_env()

    def _build_ui(self):
        root = self._root

        # --- 状態表示 ---
        status_frame = ttk.LabelFrame(root, text="状態", padding=10)
        status_frame.pack(fill="x", padx=10, pady=(10, 5))

        self._status_var = tk.StringVar(value="停止中")
        self._status_label = ttk.Label(status_frame, textvariable=self._status_var, font=("", 12, "bold"))
        self._status_label.pack()

        # --- 設定 ---
        settings_frame = ttk.LabelFrame(root, text="設定", padding=10)
        settings_frame.pack(fill="x", padx=10, pady=5)

        fields = [
            ("Redis ホスト:", "redis_host", False),
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
        handler = TextHandler(self._log_text)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
        logging.root.addHandler(handler)
        logging.root.setLevel(logging.INFO)

        self._setup_tray()

    # --- System tray ---
    def _create_tray_icon(self, color="green"):
        """トレイアイコン用レーダー画像を生成 (共通モジュール経由)"""
        return create_icon(color=color, size=64)

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
                self._create_tray_icon("gray"),
                "SWIM Worker",
                menu,
            )
            # 起動時にトレイアイコンを常駐開始 (最小化時に即反応できるように)
            self._tray_thread = threading.Thread(
                target=self._tray_icon.run, daemon=True,
            )
            self._tray_thread.start()
        except Exception as e:
            logging.warning("システムトレイ初期化失敗（トレイ機能を無効化）: %s", e)
            self._tray_icon = None

    def _tray_show(self, icon=None, item=None):
        """Show the main window from tray"""
        def restore():
            self._root.deiconify()
            self._root.state("normal")
            self._root.lift()
            self._root.focus_force()
        self._root.after(0, restore)

    def _tray_quit(self, icon=None, item=None):
        """Quit from tray"""
        if self._tray_icon:
            self._tray_icon.stop()
        self._root.after(0, self._force_quit)

    def _force_quit(self):
        """Force quit the application"""
        if self._worker_running:
            self._on_stop()
        self._root.destroy()

    def _quit_for_update(self):
        """アップデート用の完全終了: exeファイルのロックを確実に解放するため os._exit を使う"""
        try:
            if self._worker_running:
                self._on_stop()
            if self._tray_icon:
                try:
                    self._tray_icon.stop()
                except Exception:
                    pass
            self._root.destroy()
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
        """既存の.envから設定を読み込む"""
        env_map = {
            "REDIS_HOST": "redis_host",
            "REDIS_PASSWORD": "redis_password",
            "SWIM_USERNAME": "swim_username",
            "SWIM_PASSWORD": "swim_password",
            "WORKER_NAME": "worker_name",
        }
        if not ENV_PATH.exists():
            return
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key == "AUTO_CONNECT":
                self._autoconnect_var.set(value.lower() == "true")
                continue
            field = env_map.get(key)
            if field and field in self._entries:
                self._entries[field].delete(0, tk.END)
                self._entries[field].insert(0, value)

    def _save_env(self):
        """設定を.envに保存"""
        env_map = {
            "redis_host": "REDIS_HOST",
            "redis_password": "REDIS_PASSWORD",
            "swim_username": "SWIM_USERNAME",
            "swim_password": "SWIM_PASSWORD",
            "worker_name": "WORKER_NAME",
        }
        lines = []
        for field, env_key in env_map.items():
            value = self._entries[field].get().strip()
            lines.append(f"{env_key}={value}")

        # 固定値
        lines.append("REDIS_PORT=6380")

        # 自動接続設定
        lines.append(f"AUTO_CONNECT={'true' if self._autoconnect_var.get() else 'false'}")

        ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logging.info("設定を保存しました")

    def _save_autoconnect(self):
        """自動接続チェックボックス変更時に.envを更新"""
        if not ENV_PATH.exists():
            return
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
        new_lines = [l for l in lines if not l.strip().startswith("AUTO_CONNECT=")]
        new_lines.append(f"AUTO_CONNECT={'true' if self._autoconnect_var.get() else 'false'}")
        ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        state = "有効" if self._autoconnect_var.get() else "無効"
        logging.info("自動接続を%sにしました", state)

    def _on_start(self):
        """Worker起動"""
        # バリデーション
        for key, entry in self._entries.items():
            if not entry.get().strip():
                messagebox.showerror("エラー", f"{key} が空です。設定を記入してください。")
                return

        # 設定保存してから起動
        self._save_env()

        # UIスレッドで値をコピー（別スレッドからのアクセスを避ける）
        self._worker_settings = {
            "redis_host": self._entries["redis_host"].get().strip(),
            "redis_password": self._entries["redis_password"].get().strip(),
            "swim_username": self._entries["swim_username"].get().strip(),
            "swim_password": self._entries["swim_password"].get().strip(),
            "worker_name": self._entries["worker_name"].get().strip(),
        }

        self._start_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        for entry in self._entries.values():
            entry.configure(state="disabled")
        self._status_var.set("● 起動中...")

        self._worker_running = True
        if _HAS_TRAY and self._tray_icon:
            self._tray_icon.icon = self._create_tray_icon("green")
        self._worker_thread = threading.Thread(target=self._run_worker, daemon=True)
        self._worker_thread.start()

    def _on_stop(self):
        """Worker停止"""
        self._worker_running = False
        if self._consumer:
            self._consumer.stop()
        if _HAS_TRAY and self._tray_icon:
            self._tray_icon.icon = self._create_tray_icon("gray")
        self._status_var.set("停止中")
        self._start_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")
        for entry in self._entries.values():
            entry.configure(state="normal")
        logging.info("Worker停止")

    def _run_worker(self):
        """ワーカーを別スレッドで実行"""
        import redis.asyncio as aioredis
        from swim_worker.certs import get_ca_cert_path
        from swim_worker.config import Settings
        from swim_worker.auth import SwimClient
        from swim_worker.consumer import TaskConsumer, DuplicateWorkerError

        async def _main():
            try:
                # UIスレッドでコピー済みの値を使用
                ws = self._worker_settings
                os.environ["REDIS_HOST"] = ws["redis_host"]
                os.environ["REDIS_PORT"] = "6380"
                os.environ["REDIS_PASSWORD"] = ws["redis_password"]
                os.environ["REDIS_CA_CERT"] = ""
                os.environ["SWIM_USERNAME"] = ws["swim_username"]
                os.environ["SWIM_PASSWORD"] = ws["swim_password"]
                os.environ["WORKER_NAME"] = ws["worker_name"]

                settings = Settings()

                ca_cert = get_ca_cert_path()
                redis_client = aioredis.Redis(
                    host=settings.redis_host,
                    port=settings.redis_port,
                    password=settings.redis_password,
                    ssl=True,
                    ssl_ca_certs=ca_cert,
                    decode_responses=True,
                )

                # Redis接続を指数バックオフでリトライ (最大10回)
                delay = 1.0
                connected = False
                for attempt in range(1, 11):
                    try:
                        await redis_client.ping()
                        logging.info("Redis接続成功 (%d回目)", attempt)
                        connected = True
                        break
                    except Exception as e:
                        if attempt == 10:
                            raise
                        logging.warning("Redis接続失敗 (%d/10)、%.1f秒後にリトライ: %s",
                            attempt, delay, e)
                        self._root.after(0, lambda a=attempt: self._status_var.set(
                            f"再試行中 ({a}/10)"))
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 30.0)
                if not connected:
                    raise RuntimeError("Redis接続失敗")
                self._root.after(0, lambda: self._status_var.set("● 接続中 (タスク待ち)"))

                swim_client = SwimClient(
                    username=settings.swim_username,
                    password=settings.swim_password,
                    cookie_file=settings.cookie_file,
                )

                self._consumer = TaskConsumer(
                    redis_client=redis_client,
                    swim_client=swim_client,
                    worker_name=settings.worker_name,
                    heartbeat_interval=settings.heartbeat_interval,
                    request_delay_median=settings.request_delay_median,
                    request_delay_p99=settings.request_delay_p99,
                    request_delay_clip_min=settings.request_delay_clip_min,
                    request_delay_clip_max=settings.request_delay_clip_max,
                    on_update_available=self._on_update_detected,
                    on_task_state=self._on_task_state_changed,
                )

                await self._consumer.run()

            except DuplicateWorkerError as e:
                # 同じ worker_name の別プロセス/別マシンが稼働中
                logging.error("重複起動検知: %s", e)
                msg = str(e)
                self._root.after(0, lambda m=msg: messagebox.showerror(
                    "SWIM Worker - 重複起動",
                    f"同じ Worker 名 '{self._worker_settings['worker_name']}' で"
                    f"別のプロセスが稼働中のため起動できません。\n\n"
                    f"考えられる原因:\n"
                    f"  • 他の PC や VPS で同名ワーカーが動いている\n"
                    f"  • 前回クラッシュ時の古い heartbeat が残っている\n"
                    f"    (数分で自動解放されます)\n\n"
                    f"別の worker_name を設定するか、もう一方を停止してください。",
                ))
                self._root.after(0, lambda: self._status_var.set("重複起動エラー"))
                self._root.after(0, lambda: self._start_btn.configure(state="normal"))
                self._root.after(0, lambda: self._stop_btn.configure(state="disabled"))
                self._worker_running = False
                if _HAS_TRAY and self._tray_icon:
                    self._tray_icon.icon = self._create_tray_icon("red")
                for entry in self._entries.values():
                    self._root.after(0, lambda e=entry: e.configure(state="normal"))
            except Exception as e:
                logging.error("エラー: %s", e)
                self._root.after(0, lambda: self._status_var.set("エラー"))
                self._root.after(0, lambda: self._start_btn.configure(state="normal"))
                self._root.after(0, lambda: self._stop_btn.configure(state="disabled"))
                for entry in self._entries.values():
                    self._root.after(0, lambda e=entry: e.configure(state="normal"))

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_main())
        except Exception as e:
            logging.error("予期しないエラー: %s", e)
        finally:
            loop.close()

    # --- 自動起動 (Windows / macOS) ---
    def _get_startup_path(self) -> Path:
        """プラットフォーム別の自動起動ファイルパス"""
        if sys.platform == "darwin":
            return Path.home() / "Library" / "LaunchAgents" / "org.swim-worker.plist"
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        return startup / "SWIM Worker.bat"

    def _check_autostart(self) -> bool:
        if sys.platform not in ("win32", "darwin"):
            return False
        return self._get_startup_path().exists()

    def _sync_autostart_path(self):
        """自動起動ファイル内のexeパスが現在のパスと異なる場合、自動で書き直す。

        exeを別フォルダに移動すると、スタートアップフォルダの.bat/plist内に
        残った古いパスが無効になり、Windows再起動時に自動起動しなくなる問題を解消する。
        """
        if sys.platform not in ("win32", "darwin"):
            return
        if not getattr(sys, "frozen", False):
            return  # 開発環境では何もしない
        path = self._get_startup_path()
        if not path.exists():
            return  # 自動起動未設定ならスキップ
        # .bat はシステムコードページ (mbcs)、plist は UTF-8
        read_encoding = "utf-8" if sys.platform == "darwin" else "mbcs"
        try:
            content = path.read_text(encoding=read_encoding, errors="replace")
        except Exception:
            return
        current_exe = sys.executable
        if current_exe in content:
            return  # パス一致、更新不要
        # パスが変わっている → 書き直し
        logging.info("exeの場所が変わったため自動起動パスを更新: %s", current_exe)
        try:
            self._autostart_var.set(True)
            self._toggle_autostart()
        except Exception as e:
            logging.warning("自動起動パス更新失敗: %s", e)

    def _toggle_autostart(self):
        path = self._get_startup_path()
        if self._autostart_var.get():
            if getattr(sys, 'frozen', False):
                exe_path = sys.executable
            else:
                exe_path = f'python -m swim_worker'

            if sys.platform == "darwin":
                plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>org.swim-worker</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{_get_base_dir()}</string>
</dict>
</plist>
"""
                path.write_text(plist_content, encoding="utf-8")
            else:
                # Windows .bat はシステムの ANSI コードページで読まれる
                # パスに日本語が含まれる場合UTF-8だと文字化けするため mbcs (日本語Windows=CP932) で書き込む
                bat_content = f'@echo off\r\ncd /d "{_get_base_dir()}"\r\nstart "" "{exe_path}"\r\n'
                path.write_bytes(bat_content.encode("mbcs", errors="replace"))
            logging.info("自動起動を有効にしました")
        else:
            if path.exists():
                path.unlink()
            logging.info("自動起動を無効にしました")

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
            all_filled = all(e.get().strip() for e in self._entries.values())
            if all_filled:
                logging.info("自動接続: Workerを起動します")
                self._root.after(500, self._on_start)
            else:
                logging.warning("自動接続: 設定が未入力のためスキップしました")

        self._root.mainloop()

    # --- タスク状態更新 (Consumer から別スレッドで呼ばれる) ---
    # SWIM API ジョブタイプの日本語ラベル
    _JOB_LABELS = {
        "collect_notams": "NOTAM収集",
        "collect_pireps": "PIREP収集",
        "collect_pkg_weather": "PKG気象収集",
        "collect_airports": "空港一覧取得",
        "collect_airport_profiles": "空港詳細取得",
        "collect_airspace_data": "空域データ取得",
        "collect_flight_foids": "フライト一覧取得",
        "collect_flight_details": "フライト詳細取得",
        "fetch_maintenance_info": "メンテ情報取得",
        "capability_test": "権限テスト",
    }

    def _on_task_state_changed(self, state: str, job_type: str = "",
                                total: int = 0, errors: int = 0):
        """Consumer から別スレッドで呼ばれるタスク状態変化コールバック"""
        if state == "processing":
            label = self._JOB_LABELS.get(job_type, job_type)
            msg = f"● 実行中: {label}"
        else:
            # idle
            if errors > 0:
                msg = f"● 接続中 (処理済 {total} 件, エラー {errors})"
            else:
                msg = f"● 接続中 (処理済 {total} 件)"
        self._root.after(0, lambda: self._status_var.set(msg))

    def _check_rollback_marker_after_ready(self) -> None:
        """前回アップデートがロールバックされた場合、ユーザーに通知する (起動 1 秒後)"""
        marker = _get_base_dir() / "data" / ".update_rollback.json"
        if not marker.exists():
            return
        try:
            info = _load_json(marker)
        except Exception:
            info = {}
        from_version = info.get("rolled_back_from", "?")

        def notify():
            try:
                messagebox.showwarning(
                    "前回のアップデートは失敗しました",
                    f"{from_version} へのアップデートが起動確認に失敗したため、"
                    f"自動的に前バージョンにロールバックされました。\n\n"
                    f"詳細は swim-worker-update.log を確認してください。",
                )
            finally:
                try:
                    marker.unlink()
                except Exception:
                    pass

        self._root.after(1000, notify)

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
        snooze = _load_json(UPDATE_SNOOZE_PATH)
        if snooze.get("version") != version:
            return False
        until_str = snooze.get("until")
        if not until_str:
            return False
        try:
            until = datetime.fromisoformat(until_str)
        except ValueError:
            return False
        return datetime.now(timezone.utc) < until

    def _set_snooze(self, version: str) -> None:
        """「後で」選択時、このバージョンを一定時間スキップする。"""
        until = datetime.now(timezone.utc) + timedelta(hours=SNOOZE_DURATION_HOURS)
        data = {"version": version, "until": until.isoformat()}
        try:
            _save_json(UPDATE_SNOOZE_PATH, data)
            logging.info(
                "アップデート v%s を %d 時間スキップしました", version, SNOOZE_DURATION_HOURS
            )
        except Exception as e:
            logging.debug("snooze 保存失敗 (無視): %s", e)

    def _clear_snooze(self) -> None:
        """snooze 情報を消去 (Yes 選択時や別バージョン検知時)。"""
        try:
            if UPDATE_SNOOZE_PATH.exists():
                UPDATE_SNOOZE_PATH.unlink()
        except Exception:
            pass

    def _is_auto_update_enabled(self) -> bool:
        """auto_update 設定が有効か (Linux CLI 版では常に False = 従来挙動)。"""
        return bool(getattr(self, "_auto_update_var", None)) and bool(
            self._auto_update_var.get()
        )

    def _on_update_detected(self, new_version: str):
        """Consumer から呼ばれる (別スレッド)。

        - 常にアップデートボタンは表示する
        - auto_update 有効: 5秒カウントダウン → 自動アップデート (キャンセル可)
        - auto_update 無効: 従来通り確認ダイアログ
        - snooze 中のバージョンはポップアップ/カウントダウンをスキップ (ボタンは残す)
        """
        # UI スレッドで "⬆ アップデート" ボタン表示
        self._root.after(0, lambda: self._show_update_button(new_version))

        # 既にこのバージョンで重複プロンプトを抑制
        if getattr(self, "_update_prompted_version", None) == new_version:
            return
        self._update_prompted_version = new_version

        # snooze 期間中は静かにボタンだけ残す
        if self._is_snoozed(new_version):
            logging.info("アップデート v%s は snooze 期間中のためプロンプトを抑制", new_version)
            return

        if self._is_auto_update_enabled():
            self._root.after(0, lambda: self._prompt_auto_update_countdown(new_version))
        else:
            self._root.after(0, lambda: self._prompt_update(new_version))

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
        if sys.platform == "win32":
            asset = "swim-worker-windows.exe"
        elif sys.platform == "darwin":
            asset = "swim-worker-macos"
        else:
            return None
        return f"https://github.com/Meku-30/swim-worker/releases/download/v{version}/{asset}"

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

        win = tk.Toplevel(self._root)
        win.title("自動アップデート")
        win.geometry("420x160")
        win.resizable(False, False)
        win.transient(self._root)
        try:
            win.grab_set()
        except tk.TclError:
            pass

        countdown = [5]
        cancelled = [False]

        ttk.Label(
            win,
            text=f"新しいバージョン v{new_version} が利用可能です",
            font=("", 10, "bold"),
        ).pack(pady=(15, 4))
        ttk.Label(
            win,
            text=f"現在: v{__version__}",
            font=("", 9),
        ).pack(pady=(0, 10))
        msg_var = tk.StringVar(value=f"{countdown[0]} 秒後に自動でアップデートします...")
        ttk.Label(win, textvariable=msg_var, font=("", 10)).pack(pady=(0, 8))

        btn_frame = ttk.Frame(win)
        btn_frame.pack(pady=(0, 10))

        def do_now():
            """即座にアップデート開始"""
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            self._clear_snooze()
            self._start_update(new_version, download_url)

        def do_skip():
            """今回はキャンセル (snooze)"""
            cancelled[0] = True
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            self._set_snooze(new_version)

        ttk.Button(btn_frame, text="今すぐ", command=do_now).pack(side="left", padx=5)
        ttk.Button(
            btn_frame, text=f"スキップ ({SNOOZE_DURATION_HOURS}h)", command=do_skip
        ).pack(side="left", padx=5)

        def tick():
            if cancelled[0]:
                return
            countdown[0] -= 1
            if countdown[0] <= 0:
                # 自動実行
                try:
                    win.grab_release()
                except Exception:
                    pass
                try:
                    win.destroy()
                except Exception:
                    pass
                self._clear_snooze()
                self._start_update(new_version, download_url)
                return
            msg_var.set(f"{countdown[0]} 秒後に自動でアップデートします...")
            self._root.after(1000, tick)

        self._root.after(1000, tick)

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

    def _update_dialog_status(self, text: str) -> None:
        """_do_update スレッドから UI スレッド経由で進捗ダイアログのステータスを更新"""
        dlg = self._update_progress_dialog
        if dlg is None:
            return
        self._root.after(0, lambda: dlg.set_status(text))

    def _update_dialog_progress(self, percent: float, detail: str = "") -> None:
        dlg = self._update_progress_dialog
        if dlg is None:
            return
        self._root.after(0, lambda: dlg.set_progress(percent, detail))

    def _update_dialog_indeterminate(self, detail: str = "") -> None:
        dlg = self._update_progress_dialog
        if dlg is None:
            return
        self._root.after(0, lambda: dlg.set_indeterminate(detail))

    def _close_update_dialog(self) -> None:
        dlg = self._update_progress_dialog
        self._update_progress_dialog = None
        if dlg is None:
            return
        self._root.after(0, dlg.close)

    def _do_update(self, new_version: str, download_url: str):
        """新exeをダウンロードし、ヘルパースクリプト経由で置き換え → 再起動"""
        try:
            logging.info("アップデート v%s をダウンロード中...", new_version)
            self._update_dialog_status("ダウンロード中...")
            self._update_dialog_indeterminate("新しいバージョンを取得しています")

            # ダウンロード先 (exeと同じディレクトリ)
            base = _get_base_dir()
            if sys.platform == "win32":
                new_exe = base / "swim-worker-gui.new.exe"
            else:
                new_exe = base / "swim-worker.new"

            from curl_cffi.requests import Session, BrowserType
            with Session(impersonate=BrowserType.chrome136, timeout=120.0) as client:
                # stream=False で全体をメモリに読み込む (curl_cffi では stream=True の扱いが不安定)
                resp = client.get(download_url, allow_redirects=True)
                if resp.status_code != 200:
                    raise RuntimeError(f"ダウンロード失敗: status={resp.status_code}")
                content = resp.content
                if not content or len(content) < 1024 * 1024:  # 1MB未満は異常
                    raise RuntimeError(f"ダウンロードサイズ異常: {len(content) if content else 0} bytes")
                with new_exe.open("wb") as f:
                    f.write(content)
            size_mb = new_exe.stat().st_size / 1024 / 1024
            logging.info("ダウンロード完了: %s (%.1f MB)", new_exe, size_mb)
            self._update_dialog_progress(100, f"ダウンロード完了 ({size_mb:.1f} MB)")

            # Worker停止 (停止処理自体を別スレッドに逃がし UI ブロックを回避)
            if self._worker_running:
                self._update_dialog_status("Worker を停止中...")
                stop_done = threading.Event()

                def _stop_async():
                    try:
                        self._on_stop()
                    except Exception as e:
                        logging.debug("停止処理エラー (無視): %s", e)
                    finally:
                        stop_done.set()

                threading.Thread(target=_stop_async, daemon=True).start()
                # 最大 15 秒まで停止完了を待つ (進捗ダイアログが動き続ける)
                stop_done.wait(timeout=15.0)

            # 現在のexeのパス
            if getattr(sys, 'frozen', False):
                current_exe = Path(sys.executable)
            else:
                logging.warning("開発環境ではアップデート不可")
                self._close_update_dialog()
                return
            self._update_dialog_status("差し替えスクリプトを起動中...")

            # ヘルパースクリプト作成
            if sys.platform == "win32":
                script_path = base / "swim-worker-update.bat"
                log_path = base / "swim-worker-update.log"
                old_exe = current_exe.with_suffix(current_exe.suffix + ".old")
                startup_ok = base / "data" / ".startup_ok"
                rollback_marker = base / "data" / ".update_rollback.json"
                # move をリトライする (旧exeが解放されるまで最大30秒待機)
                # Phase 2: .old バックアップ + 起動成功判定 + ロールバック
                script = (
                    "@echo off\r\n"
                    f'echo [%DATE% %TIME%] update start > "{log_path}"\r\n'
                    "set /a COUNT=0\r\n"
                    ":retry\r\n"
                    "set /a COUNT+=1\r\n"
                    f'echo [%DATE% %TIME%] attempt %COUNT% >> "{log_path}"\r\n'
                    "if %COUNT% gtr 30 goto fail\r\n"
                    "ping 127.0.0.1 -n 2 > nul\r\n"
                    # Phase 2: 旧 exe を .old にバックアップ (失敗時のロールバック用)
                    f'copy /Y "{current_exe}" "{old_exe}" >> "{log_path}" 2>&1\r\n'
                    f'move /Y "{new_exe}" "{current_exe}" >> "{log_path}" 2>&1\r\n'
                    "if errorlevel 1 goto retry\r\n"
                    f'echo [%DATE% %TIME%] move success >> "{log_path}"\r\n'
                    # Phase 2: 前回の startup marker を削除 (新 exe の成功判定用)
                    f'if exist "{startup_ok}" del "{startup_ok}" >> "{log_path}" 2>&1\r\n'
                    # ファイルシステム同期待ち
                    "ping 127.0.0.1 -n 2 > nul\r\n"
                    # PyInstaller 6.9+ の bootloader が _PYI_ARCHIVE_FILE を
                    # 継承していると onefile 展開をスキップして python DLL 読み込み失敗する
                    # (親exeが終了して _MEI tmpdir が消えているため)
                    # 公式対応: PYINSTALLER_RESET_ENVIRONMENT=1 + 関連envを削除
                    'set "_PYI_ARCHIVE_FILE="\r\n'
                    'set "_PYI_APPLICATION_HOME_DIR="\r\n'
                    'set "_PYI_PARENT_PROCESS_LEVEL="\r\n'
                    'set "_MEIPASS2="\r\n'
                    'set "PYINSTALLER_RESET_ENVIRONMENT=1"\r\n'
                    f'start "" /D "{base}" "{current_exe}"\r\n'
                    f'echo [%DATE% %TIME%] new exe started >> "{log_path}"\r\n'
                    # Phase 2: 起動成功判定ループ (最大30秒 startup_ok を待つ)
                    "set /a WAIT=0\r\n"
                    ":waitok\r\n"
                    "set /a WAIT+=1\r\n"
                    "if %WAIT% gtr 30 goto rollback\r\n"
                    "ping 127.0.0.1 -n 2 > nul\r\n"
                    f'if exist "{startup_ok}" goto success\r\n'
                    "goto waitok\r\n"
                    ":success\r\n"
                    f'echo [%DATE% %TIME%] startup OK after %WAIT%s >> "{log_path}"\r\n'
                    # 成功: .old を削除
                    f'if exist "{old_exe}" del "{old_exe}" >> "{log_path}" 2>&1\r\n'
                    'del "%~f0"\r\n'
                    "exit /b 0\r\n"
                    ":rollback\r\n"
                    f'echo [%DATE% %TIME%] ROLLBACK: startup_ok not found in 60s >> "{log_path}"\r\n'
                    # 新 exe を消して旧 exe を戻す (taskkill で走ってる新 exe を止める)
                    f'taskkill /F /IM "{current_exe.name}" >> "{log_path}" 2>&1\r\n'
                    "ping 127.0.0.1 -n 3 > nul\r\n"
                    f'move /Y "{old_exe}" "{current_exe}" >> "{log_path}" 2>&1\r\n'
                    # GUI 起動時にロールバック通知するためのマーカー書き込み (JSON)
                    f'mkdir "{rollback_marker.parent}" 2>nul\r\n'
                    f'echo {{"rolled_back_from":"v{new_version}"}} > "{rollback_marker}"\r\n'
                    f'start "" /D "{base}" "{current_exe}"\r\n'
                    'del "%~f0"\r\n'
                    "exit /b 0\r\n"
                    ":fail\r\n"
                    f'echo [%DATE% %TIME%] FAILED after %COUNT% attempts >> "{log_path}"\r\n'
                    "exit /b 1\r\n"
                )
                # パスに日本語が含まれる場合に備えて mbcs (システム ANSI) で書き込む
                script_path.write_bytes(script.encode("mbcs", errors="replace"))
                # PyInstaller 6.9+ の "Failed to load Python DLL" 対策として
                # 子プロセスの環境変数から _PYI_* を除去し、PYINSTALLER_RESET_ENVIRONMENT を設定
                # 参考: https://pyinstaller.org/en/stable/runtime-information.html
                clean_env = os.environ.copy()
                for k in ("_PYI_ARCHIVE_FILE", "_PYI_APPLICATION_HOME_DIR",
                          "_PYI_PARENT_PROCESS_LEVEL", "_MEIPASS2"):
                    clean_env.pop(k, None)
                clean_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
                # CREATE_NO_WINDOW: コンソールは持つが非表示
                CREATE_NO_WINDOW = 0x08000000
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                CREATE_BREAKAWAY_FROM_JOB = 0x01000000
                subprocess.Popen(
                    ["cmd", "/c", str(script_path)],
                    creationflags=(
                        CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                        | CREATE_BREAKAWAY_FROM_JOB
                    ),
                    env=clean_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
            else:  # darwin
                script_path = base / "swim-worker-update.sh"
                old_exe = current_exe.with_suffix(current_exe.suffix + ".old")
                startup_ok = base / "data" / ".startup_ok"
                rollback_marker = base / "data" / ".update_rollback.json"
                log_path = base / "swim-worker-update.log"
                # Phase 2: .old バックアップ + 起動成功判定 + ロールバック
                script = f"""#!/bin/bash
set -u
LOG="{log_path}"
echo "[$(date)] update start" > "$LOG"
sleep 3
# 旧 exe をバックアップ
cp "{current_exe}" "{old_exe}" >> "$LOG" 2>&1 || true
mv "{new_exe}" "{current_exe}" >> "$LOG" 2>&1
chmod +x "{current_exe}"
# 前回の startup marker 削除
rm -f "{startup_ok}"
# 新 exe 起動
"{current_exe}" &
echo "[$(date)] new exe started" >> "$LOG"
# 起動成功判定 (最大 60 秒)
for i in $(seq 1 60); do
    sleep 1
    if [ -f "{startup_ok}" ]; then
        echo "[$(date)] startup OK after ${{i}}s" >> "$LOG"
        rm -f "{old_exe}"
        rm -- "$0"
        exit 0
    fi
done
# ロールバック
echo "[$(date)] ROLLBACK: startup_ok not found in 60s" >> "$LOG"
pkill -f "{current_exe.name}" >> "$LOG" 2>&1 || true
sleep 2
mv "{old_exe}" "{current_exe}" >> "$LOG" 2>&1
mkdir -p "{rollback_marker.parent}"
echo '{{"rolled_back_from":"v{new_version}"}}' > "{rollback_marker}"
"{current_exe}" &
rm -- "$0"
exit 0
"""
                script_path.write_text(script, encoding="utf-8")
                os.chmod(script_path, 0o755)
                subprocess.Popen(
                    ["bash", str(script_path)],
                    start_new_session=True,
                    close_fds=True,
                )

            logging.info("アップデータを起動しました。まもなく再起動します")
            self._update_dialog_status("再起動中...")
            self._update_dialog_progress(100, "ヘルパースクリプトに引き継ぎました")
            # 1秒後に強制終了 → exe ファイルロック解放 → bat が move 成功
            self._root.after(1000, self._quit_for_update)
        except Exception as e:
            logging.exception("アップデート失敗")
            self._close_update_dialog()
            self._root.after(0, lambda: messagebox.showerror(
                "アップデート失敗", f"アップデートに失敗しました:\n{e}"
            ))

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
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
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
