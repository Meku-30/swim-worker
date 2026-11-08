"""Windows の更新の通し試験 (CI の Windows で動かす。手元では動かさない)。

旧 Python の GUI の更新の処理 (updater.download_and_verify → launch_update_helper) を、使い捨ての鍵で署名した
手元の置き場 (http://127.0.0.1) に向けて動かし、Rust の GUI に入れ替わることを確かめる。

場面:
  A 旧 Python → Rust: .bat が差し替えて起動し、120 秒以内に data/.startup_ok、巻き戻しの記録が無い。
    Python が保存した設定 (.env と資格情報マネージャー) を Rust が読めた (自動接続の検証が通ったログ)
  B Rust が保存した設定を Python が読める (巻き戻し・出し直しで入れ直させない)
  C 壊れた版に更新 → 120 秒後に前の exe に戻り、.update_rollback.json が書かれる

使い方: python tools/update_trial_windows.py <Rust の swim-worker-gui.exe>
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


def main(rust_exe: Path) -> None:
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
    marker = base / "data" / ".update_rollback.json"
    t = wait_for(marker, 200)
    check(t is not None, f"巻き戻しの記録 ({t and round(t)} 秒)")
    if t is not None:
        info = json.loads(marker.read_text(encoding="utf-8-sig", errors="replace").strip() or "{}")
        check(info.get("rolled_back_from") == "v1.3.1", f"記録の中身 {info}")
    time.sleep(5)
    check((base / "swim-worker-gui.exe").read_bytes() == rust, "前の exe (Rust 1.3.0) に戻った")
    time.sleep(10)
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
    main(Path(sys.argv[1]).resolve())
