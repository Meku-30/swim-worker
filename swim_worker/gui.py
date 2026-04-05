"""SWIM Worker GUI"""
import asyncio
import logging
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from pathlib import Path

from swim_worker import __version__

# System tray support (Windows + macOS)
_HAS_TRAY = False
if sys.platform in ("win32", "darwin"):
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
        try:
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

    def _minimize_to_tray(self):
        """Minimize window to system tray — タスクバーからも消える"""
        if not _HAS_TRAY or not self._tray_icon:
            return
        self._root.withdraw()  # ウィンドウ非表示（タスクバーからも消える）
        if not self._tray_icon.visible:
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
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
                )

                self._consumer = TaskConsumer(
                    redis_client=redis_client,
                    swim_client=swim_client,
                    worker_name=settings.worker_name,
                    heartbeat_interval=settings.heartbeat_interval,
                    on_update_available=self._on_update_detected,
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
        # 最小化ボタン（ー）でトレイに格納
        try:
            self._root.bind("<Iconify>", self._on_iconify)
        except tk.TclError:
            logging.debug("<Iconify>イベント未対応: トレイ最小化は閉じるボタンのみ")

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

    # --- 自動アップデート ---
    def _on_update_detected(self, new_version: str):
        """Consumerから呼ばれる (別スレッド)。

        アップデートボタンを表示 + 初回のみポップアップを出す。
        ユーザーがキャンセルした場合もボタンは残るので、あとから再アップデート可能。
        """
        # UIスレッドに転送
        self._root.after(0, lambda: self._show_update_button(new_version))
        # 初回のみポップアップ表示 (ボタン追加後も Worker 再起動までは重複表示しない)
        if getattr(self, "_update_prompted_version", None) == new_version:
            return
        self._update_prompted_version = new_version
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
        """新バージョン検知時の確認ダイアログ"""
        download_url = self._get_download_url(new_version)
        if not download_url:
            logging.warning("このプラットフォームはアップデート非対応")
            return
        answer = messagebox.askyesno(
            "アップデートがあります",
            f"新しいバージョン v{new_version} が利用可能です。\n"
            f"現在のバージョン: v{__version__}\n\n"
            f"今すぐアップデートしますか？\n"
            f"（ダウンロード後、自動で再起動します）",
        )
        if not answer:
            logging.info("アップデートはキャンセルされました")
            return
        # 別スレッドでダウンロード開始
        threading.Thread(
            target=self._do_update, args=(new_version, download_url), daemon=True,
        ).start()

    def _do_update(self, new_version: str, download_url: str):
        """新exeをダウンロードし、ヘルパースクリプト経由で置き換え → 再起動"""
        try:
            logging.info("アップデート v%s をダウンロード中...", new_version)
            # ダウンロード先 (exeと同じディレクトリ)
            base = _get_base_dir()
            if sys.platform == "win32":
                new_exe = base / "swim-worker-gui.new.exe"
            else:
                new_exe = base / "swim-worker.new"

            from curl_cffi.requests import Session, BrowserType
            with Session(impersonate=BrowserType.chrome136, timeout=120.0) as client:
                resp = client.get(download_url, stream=True)
                if resp.status_code != 200:
                    raise RuntimeError(f"ダウンロード失敗: status={resp.status_code}")
                with new_exe.open("wb") as f:
                    # curl_cffi はstream時 iter_content を使う
                    content = resp.content if hasattr(resp, "content") else resp.body
                    f.write(content)
            logging.info("ダウンロード完了: %s", new_exe)

            # Worker停止
            if self._worker_running:
                self._root.after(0, self._on_stop)

            # 現在のexeのパス
            if getattr(sys, 'frozen', False):
                current_exe = Path(sys.executable)
            else:
                logging.warning("開発環境ではアップデート不可")
                return

            # ヘルパースクリプト作成
            if sys.platform == "win32":
                script_path = base / "swim-worker-update.bat"
                script = (
                    "@echo off\r\n"
                    "timeout /t 3 /nobreak > nul\r\n"
                    f'move /Y "{new_exe}" "{current_exe}"\r\n'
                    f'start "" "{current_exe}"\r\n'
                    'del "%~f0"\r\n'
                )
                # パスに日本語が含まれる場合に備えて mbcs (システム ANSI) で書き込む
                script_path.write_bytes(script.encode("mbcs", errors="replace"))
                subprocess.Popen(
                    ["cmd", "/c", str(script_path)],
                    creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                    close_fds=True,
                )
            else:  # darwin
                script_path = base / "swim-worker-update.sh"
                script = (
                    "#!/bin/bash\n"
                    "sleep 3\n"
                    f'mv "{new_exe}" "{current_exe}"\n'
                    f'chmod +x "{current_exe}"\n'
                    f'"{current_exe}" &\n'
                    'rm "$0"\n'
                )
                script_path.write_text(script, encoding="utf-8")
                os.chmod(script_path, 0o755)
                subprocess.Popen(
                    ["bash", str(script_path)],
                    start_new_session=True,
                    close_fds=True,
                )

            logging.info("アップデータを起動しました。3秒後に再起動します")
            self._root.after(1000, self._force_quit)
        except Exception as e:
            logging.exception("アップデート失敗")
            self._root.after(0, lambda: messagebox.showerror(
                "アップデート失敗", f"アップデートに失敗しました:\n{e}"
            ))

    def _on_iconify(self, event=None):
        """最小化ボタンが押された時 → トレイに格納"""
        if _HAS_TRAY and self._tray_icon:
            # iconify のデフォルト動作をキャンセルして withdraw（タスクバーからも消す）
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

    try:
        app = WorkerGUI()
        app.run()
    except Exception as e:
        logging.exception("致命的エラーで終了")
        _write_crash_log(e)
        raise


if __name__ == "__main__":
    main()
