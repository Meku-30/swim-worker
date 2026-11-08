"""Windows の更新の通し試験 (CI の Windows で動かす。手元では動かさない)。

旧 Python の GUI の更新の処理 (updater.download_and_verify → launch_update_helper) を、使い捨ての鍵で署名した
手元の置き場 (http://127.0.0.1) に向けて動かし、Rust の GUI に入れ替わることを確かめる。

場面:
  A 旧 Python → Rust: .bat が差し替えて起動し、120 秒以内に data/.startup_ok、巻き戻しの記録が無い。
    Python が保存した設定 (.env と資格情報マネージャー) を Rust が読めた (自動接続の検証が通ったログ)
  B Rust が保存した設定を Python が読める (巻き戻し・出し直しで入れ直させない)
  C 壊れた版に更新 → 120 秒後に前の exe に戻り、.update_rollback.json が書かれる
  D Rust → Rust: Rust の GUI (試運転のビルド) が自分で次の版を取り、.bat が差し替えて起動する。
    120 秒以内に新しい exe の起動の印、巻き戻しなし、.bat の move の試しが 30 回未満

使い方: python tools/update_trial_windows.py <Rust の swim-worker-gui.exe> <試運転のビルドの exe>
"""
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from swim_worker import release_keys, settings_store, updater  # noqa: E402

FAILS: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("OK  " if ok else "NG  ") + what, flush=True)
    if not ok:
        FAILS.append(what)


def make_release(root: Path, key: Ed25519PrivateKey, version: str, exe: bytes) -> None:
    d = root / f"v{version}"
    d.mkdir(parents=True)
    (d / "swim-worker-windows.exe").write_bytes(exe)
    sums = (f"# swim-worker-release v{version}\n"
            f"{hashlib.sha256(exe).hexdigest()}  swim-worker-windows.exe\n").encode()
    (d / "SHA256SUMS").write_bytes(sums)
    (d / "SHA256SUMS.sig").write_bytes(key.sign(sums))


def serve(root: Path) -> int:
    h = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(root), **k)  # noqa: E731
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s.server_address[1]


def wait_for(path: Path, secs: float) -> float | None:
    t = time.monotonic()
    while time.monotonic() - t < secs:
        if path.exists():
            return time.monotonic() - t
        time.sleep(1)
    return None


def kill_all() -> None:
    subprocess.run(["taskkill", "/F", "/IM", "swim-worker-gui.exe"], capture_output=True)
    time.sleep(2)


def update(base: Path, port: int, version: str, current: str) -> None:
    """旧 GUI の _do_update と同じ流れ (ダウンロード → 検証 → 差し替えの .bat)"""
    url = f"http://127.0.0.1:{port}/v{version}/swim-worker-windows.exe"
    new_exe = updater.new_exe_path(base)
    updater.download_and_verify(url, new_exe, current_version=current)
    updater.launch_update_helper(base=base, current_exe=base / "swim-worker-gui.exe", new_exe=new_exe,
                                 new_version=version)


def allow_in_ci_job() -> None:
    """GitHub の Windows はジョブの外に出ること (CREATE_BREAKAWAY_FROM_JOB) を許さないので、試験の中だけ外す。
    利用者の PC では旧 GUI がこの指定のまま動いている (配布物は変えない)"""
    class Popen(subprocess.Popen):  # クラスのまま (asyncio などが継承するので関数にしない)
        def __init__(self, *a, **k):
            if "creationflags" in k:
                k["creationflags"] &= ~0x01000000
            super().__init__(*a, **k)

    subprocess.Popen = Popen


def main(rust_exe: Path, trial_exe: Path) -> None:
    allow_in_ci_job()
    work = Path(tempfile.mkdtemp(prefix="swim-update-trial-"))
    base = work / "app"
    (base / "data").mkdir(parents=True)
    www = work / "www"
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    # 試し専用: Python の更新の処理が試しの鍵を使う (この処理の中だけ。配布物は変えない)
    release_keys.RELEASE_PUBKEYS_PEM = (pem,)
    rust = rust_exe.read_bytes()
    broken = (Path(os.environ["SystemRoot"]) / "System32" / "where.exe").read_bytes()  # すぐ終わる exe
    broken += b"\0" * (1024 * 1024)  # 1MB 未満は更新の処理が断るので足す
    make_release(www, key, "1.3.0", rust)
    make_release(www, key, "1.3.1", broken)
    port = serve(www)

    # 旧 (Python) の GUI が保存した設定: .env と資格情報マネージャー
    fields = {"redis_host": "127.0.0.1", "redis_username": "trial-user", "redis_password": "p@ss 日本",
              "swim_username": "trial", "swim_password": "s'q", "worker_name": "trial-w1"}
    store = settings_store.SettingsStore(base / ".env")
    check(store.uses_keyring, "Python の keyring が使える")
    store.save(fields, auto_connect=True)
    # 今の exe (旧 Python の GUI のつもり。中身は何でもよい: .bat は置き換えるだけ)
    shutil.copy(sys.executable, base / "swim-worker-gui.exe")

    print("== A 旧 Python → Rust (v1.3.0)", flush=True)
    update(base, port, "1.3.0", current="1.2.2")
    t = wait_for(base / "data" / ".startup_ok", 150)
    check(t is not None, f"起動の印 ({t and round(t)} 秒)")
    time.sleep(5)
    check(not (base / "data" / ".update_rollback.json").exists(), "巻き戻しの記録が無い")
    check((base / "swim-worker-gui.exe").read_bytes() == rust, "exe が Rust に置き換わった")
    log = (base / "swim-worker.log").read_text(encoding="utf-8", errors="replace") if (base / "swim-worker.log").exists() else ""
    check("自動接続: Workerを起動します" in log, "Python の設定 (資格情報マネージャーのパスワードを含む) を Rust が読めた")
    upd = (base / "swim-worker-update.log")
    if upd.exists():
        print(upd.read_text(errors="replace"))

    print("== B Rust が保存した設定を Python が読める", flush=True)
    got, auto = settings_store.SettingsStore(base / ".env").load()
    check(got == fields and auto is True, f"Python が読めた ({'一致' if got == fields else got})")

    print("== C 壊れた版 (v1.3.1) → 巻き戻し", flush=True)
    kill_all()
    (base / "data" / ".startup_ok").unlink(missing_ok=True)
    update(base, port, "1.3.1", current="1.3.0")
    # .bat が記録を書き、戻った Rust の GUI が起動してすぐ読んで数え、snooze を置き、記録を消す
    prefs = base / "data" / "gui_settings.json"

    def counted() -> bool:
        try:
            return json.loads(prefs.read_text(encoding="utf-8")).get("rollback_count", {}).get("1.3.1") == 1
        except Exception:
            return False

    t0 = time.monotonic()
    while time.monotonic() - t0 < 200 and not counted():
        time.sleep(1)
    check(counted(), f"巻き戻しを数えた rollback_count=1 ({round(time.monotonic() - t0)} 秒)")
    snooze = base / "data" / "update_snooze.json"
    check(snooze.exists() and json.loads(snooze.read_text(encoding="utf-8")).get("version") == "1.3.1", "壊れた版を 24 時間 snooze")
    check(not (base / "data" / ".update_rollback.json").exists(), "記録は読んだあと消える")
    time.sleep(5)
    check((base / "swim-worker-gui.exe").read_bytes() == rust, "前の exe (Rust 1.3.0) に戻った")
    time.sleep(10)
    log = (base / "swim-worker.log").read_text(encoding="utf-8", errors="replace")
    print(log[-3000:])
    kill_all()

    print("== D Rust → Rust (Rust の GUI が自分で v1.3.2 に更新)", flush=True)
    trial = trial_exe.read_bytes()
    newer = trial + b"\0" * 16 + b"swim-trial-1.3.2"  # 差し替わったかを中身で見分ける (末尾の余りは実行に影響しない)
    make_release(www, key, "1.3.2", newer)
    shutil.copy(trial_exe, base / "swim-worker-gui.exe")
    for f in (".startup_ok", ".trial_update_started", ".update_rollback.json"):
        (base / "data" / f).unlink(missing_ok=True)
    (base / "swim-worker-update.log").unlink(missing_ok=True)
    pub = work / "trial.pub.pem"
    pub.write_text(pem, encoding="ascii")
    env = {**os.environ, "SWIM_WORKER_BASE": str(base), "SWIM_TRIAL_UPDATE_TO": "1.3.2",
           "SWIM_TRIAL_RELEASE_BASE": f"http://127.0.0.1:{port}", "SWIM_TRIAL_PUBKEY": str(pub),
           "SWIM_TRIAL_NO_BREAKAWAY": "1"}
    subprocess.Popen([str(base / "swim-worker-gui.exe")], cwd=base, env=env)
    t0 = time.monotonic()
    upd_log = base / "swim-worker-update.log"

    def updated() -> bool:
        try:
            return "startup OK" in upd_log.read_text(errors="replace")
        except OSError:
            return False

    while time.monotonic() - t0 < 240 and not updated():
        time.sleep(1)
    check(updated(), f"新しい exe の起動の確認 ({round(time.monotonic() - t0)} 秒、更新の開始の 5 秒を含む)")
    text = upd_log.read_text(errors="replace") if upd_log.exists() else ""
    print(text)
    attempts = text.count("attempt ")
    check(0 < attempts < 30, f".bat の move の試し {attempts} 回 (30 回未満)")
    check((base / "swim-worker-gui.exe").read_bytes() == newer, "exe が v1.3.2 に置き換わった")
    check(not (base / "data" / ".update_rollback.json").exists(), "巻き戻しの記録が無い")
    log = (base / "swim-worker.log").read_text(encoding="utf-8", errors="replace")
    print(log[-3000:])
    kill_all()
    # 資格情報マネージャーの試しの項目を消す
    for k in ("REDIS_PASSWORD", "SWIM_PASSWORD"):
        try:
            import keyring
            sid = settings_store.read_env_values(base / ".env").get(settings_store.SECRET_STORE_ID_KEY)
            keyring.delete_password(settings_store.KEYRING_SERVICE, f"{sid}:{k}")
        except Exception:
            pass
    if FAILS:
        raise SystemExit(f"NG {len(FAILS)} 件: {FAILS}")
    print("全部 OK")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
