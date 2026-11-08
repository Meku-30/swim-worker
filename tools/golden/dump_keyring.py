"""Windows の資格情報マネージャーに、Python の keyring が書く項目の形を見本にする (Windows でだけ動く)。

settings_store.py と同じ呼び方 (サービス名 swim-worker、ユーザー名 "<store_id>:<ENV_KEY>") で
作った値を書き、CredEnumerateW で項目の TargetName・UserName・CredentialBlob の符号化を読み、最後に消す。
使い方: python tools/golden/dump_keyring.py --out keyring_windows.json
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import keyring  # noqa: E402

from swim_worker.settings_store import KEYRING_SERVICE  # noqa: E402

STORE_ID = "0123456789abcdef0123456789abcdef"
KEYS = ["REDIS_PASSWORD", "SWIM_PASSWORD"]
VALUE = "dummy-value-日本"


class CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
        ("LastWritten", wt.FILETIME), ("CredentialBlobSize", wt.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wt.DWORD),
        ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR),
    ]


def enumerate_creds():
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    count = wt.DWORD()
    creds = ctypes.POINTER(ctypes.POINTER(CREDENTIAL))()
    if not advapi.CredEnumerateW(None, 0, ctypes.byref(count), ctypes.byref(creds)):
        return []
    out = []
    for i in range(count.value):
        c = creds[i].contents
        blob = bytes(c.CredentialBlob[:c.CredentialBlobSize])
        out.append({"target_name": c.TargetName, "user_name": c.UserName, "type": c.Type,
                    "persist": c.Persist, "blob": blob})
    advapi.CredFree(creds)
    return out


def main(out: Path):
    users = [f"{STORE_ID}:{k}" for k in KEYS]
    for u in users:
        keyring.set_password(KEYRING_SERVICE, u, VALUE)
    try:
        found = []
        for c in enumerate_creds():
            if KEYRING_SERVICE in (c["target_name"] or "") or (c["user_name"] or "") in users:
                enc = "utf-16-le" if c["blob"] == VALUE.encode("utf-16-le") else (
                    "utf-8" if c["blob"] == VALUE.encode("utf-8") else "other")
                found.append({k: v for k, v in c.items() if k != "blob"} | {"blob_encoding": enc})
        data = {
            "keyring_backend": type(keyring.get_keyring()).__module__ + "." + type(keyring.get_keyring()).__name__,
            "service": KEYRING_SERVICE, "users": users, "value": VALUE, "credentials": found,
            "read_back": [keyring.get_password(KEYRING_SERVICE, u) == VALUE for u in users],
        }
        out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(json.dumps(data, ensure_ascii=False, indent=1))
    finally:
        for u in users:
            try:
                keyring.delete_password(KEYRING_SERVICE, u)
            except Exception:
                pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    main(ap.parse_args().out)
