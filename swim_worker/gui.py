"""SWIM Worker GUI"""
import asyncio
import logging
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from pathlib import Path

# System tray support (Windows only)
_HAS_TRAY = False
if sys.platform == "win32":
    try:
        import pystray
        from PIL import Image, ImageDraw
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
        self._root.geometry("520x600")
        self._root.resizable(False, False)

        self._worker_thread: threading.Thread | None = None
        self._worker_running = False
        self._consumer = None

        self._tray_icon = None
        self._build_ui()
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

        # --- 自動起動 (Windows only) ---
        if sys.platform == "win32":
            self._autostart_var = tk.BooleanVar(value=self._check_autostart())
            autostart_cb = ttk.Checkbutton(
                btn_frame, text="Windows起動時に自動起動",
                variable=self._autostart_var, command=self._toggle_autostart,
            )
            autostart_cb.pack(side="right")

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
        """Create a simple colored circle icon for the system tray"""
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        colors = {"green": (0, 180, 0), "gray": (128, 128, 128), "red": (200, 0, 0)}
        draw.ellipse([8, 8, 56, 56], fill=colors.get(color, (128, 128, 128)))
        return img

    def _setup_tray(self):
        """Setup system tray icon"""
        if not _HAS_TRAY:
            return

        menu = pystray.Menu(
            pystray.MenuItem("表示", self._tray_show),
            pystray.MenuItem("終了", self._tray_quit),
        )
        self._tray_icon = pystray.Icon(
            "swim-worker",
            self._create_tray_icon("gray"),
            "SWIM Worker",
            menu,
        )

    def _tray_show(self, icon=None, item=None):
        """Show the main window from tray"""
        self._root.after(0, self._root.deiconify)

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

    def _minimize_to_tray(self):
        """Minimize window to system tray"""
        if not _HAS_TRAY or not self._tray_icon:
            return
        self._root.withdraw()
        if not self._tray_icon.visible:
            threading.Thread(target=self._tray_icon.run, daemon=True).start()

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
            field = env_map.get(key.strip())
            if field and field in self._entries:
                self._entries[field].delete(0, tk.END)
                self._entries[field].insert(0, value.strip())

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

        ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logging.info("設定を保存しました")

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
        from swim_worker.consumer import TaskConsumer

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

                await redis_client.ping()
                logging.info("Redis接続成功")
                self._root.after(0, lambda: self._status_var.set("● 接続中 (タスク待ち)"))

                swim_client = SwimClient(
                    username=settings.swim_username,
                    password=settings.swim_password,
                )

                self._consumer = TaskConsumer(
                    redis_client=redis_client,
                    swim_client=swim_client,
                    worker_name=settings.worker_name,
                    heartbeat_interval=settings.heartbeat_interval,
                )

                await self._consumer.run()

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

    # --- Windows 自動起動 ---
    def _get_startup_shortcut_path(self) -> Path:
        """Windowsスタートアップフォルダのショートカットパス"""
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        return startup / "SWIM Worker.bat"

    def _check_autostart(self) -> bool:
        if sys.platform != "win32":
            return False
        return self._get_startup_shortcut_path().exists()

    def _toggle_autostart(self):
        shortcut = self._get_startup_shortcut_path()
        if self._autostart_var.get():
            # スタートアップにbatを作成
            if getattr(sys, 'frozen', False):
                exe_path = sys.executable
            else:
                exe_path = f'python -m swim_worker'
            bat_content = f'@echo off\ncd /d "{_get_base_dir()}"\nstart "" "{exe_path}"\n'
            shortcut.write_text(bat_content, encoding="utf-8")
            logging.info("自動起動を有効にしました")
        else:
            if shortcut.exists():
                shortcut.unlink()
            logging.info("自動起動を無効にしました")

    def run(self):
        """GUIメインループ"""
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._root.mainloop()

    def _on_close(self):
        """ウィンドウ閉じる時"""
        if self._worker_running and _HAS_TRAY:
            self._minimize_to_tray()  # トレイに最小化してバックグラウンド動作を継続
        else:
            self._force_quit()


def main():
    app = WorkerGUI()
    app.run()


if __name__ == "__main__":
    main()
