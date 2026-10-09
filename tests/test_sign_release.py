"""scripts/sign-release.sh: draft を確かめて署名し、上げて公開する (gh は偽物に差し替える)"""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.signing_helpers import make_key

ROOT = Path(__file__).resolve().parent.parent
SIGN = ROOT / "scripts" / "sign-release.sh"
TAG = "v9.9.9"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl が要る")

FAKE_GH = r"""#!/usr/bin/env bash
# 偽の gh: $FAKE_RELEASE のファイルを draft のリリースとして扱う
set -euo pipefail
echo "$*" >> "$FAKE_LOG"
[[ "$1" == release ]] || exit 2
cmd="$2"; shift 2
tag="$1"; shift
dir="" pattern="" files=()
while (( $# )); do
    case "$1" in
        -R) shift 2 ;;
        -D) dir="$2"; shift 2 ;;
        -p) pattern="$2"; shift 2 ;;
        --json|-q) shift 2 ;;
        --draft=false) echo published > "$FAKE_STATE"; shift ;;
        *) files+=("$1"); shift ;;
    esac
done
case "$cmd" in
    view) [[ "$(cat "$FAKE_STATE")" == draft ]] && echo true || echo false ;;
    download)
        mkdir -p "$dir"
        if [[ -n "$pattern" ]]; then cp "$FAKE_RELEASE/$pattern" "$dir/"; else cp "$FAKE_RELEASE"/* "$dir/"; fi ;;
    upload) cp "${files[@]}" "$FAKE_RELEASE/" ;;
    edit) : ;;
esac
"""

ASSETS = ("swim-worker-linux-amd64", "swim-worker-linux-arm64", "swim-worker-linux",
          "swim-worker-macos", "swim-worker-windows.exe")
SOURCES = {"install.sh": "scripts/install.sh", "swim-worker-update.sh": "scripts/swim-worker-update.sh",
           "swim-worker.service": "scripts/swim-worker.service",
           "swim-worker-update.service": "scripts/swim-worker-update.service",
           "swim-worker-update.timer": "scripts/swim-worker-update.timer"}


@pytest.fixture
def env(tmp_path):
    key = make_key(tmp_path / "key", "signing")
    os.chmod(key.private, 0o600)
    # リポジトリの代わり: 公開鍵を埋め込んだ install.sh・更新スクリプト
    src = tmp_path / "src"
    for rel in SOURCES.values():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        text = (ROOT / rel).read_text()
        if rel.endswith(".sh"):
            s = text.index("# BEGIN RELEASE PUBKEYS\n") + len("# BEGIN RELEASE PUBKEYS\n")
            e = text.index("# END RELEASE PUBKEYS\n")
            text = (text[:s] + "read -r -d '' RELEASE_PUBKEYS_PEM <<'PUBKEYEOF' || true\n"
                    + key.public_pem + "PUBKEYEOF\n" + text[e:])
        (src / rel).write_text(text)
    rel = tmp_path / "release"
    rel.mkdir()
    for n in ASSETS:
        (rel / n).write_bytes(n.encode() * 10)
    for n, p in SOURCES.items():
        shutil.copy2(src / p, rel / n)
    _write_sums(rel, TAG)
    (tmp_path / "state").write_text("draft")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(FAKE_GH)
    (bindir / "gh").chmod(0o755)
    e = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_RELEASE": str(rel),
         "FAKE_STATE": str(tmp_path / "state"), "FAKE_LOG": str(tmp_path / "gh.log"),
         "SWIM_RELEASE_KEY": str(key.private), "SWIM_RELEASE_SRC_DIR": str(src)}
    return {"env": e, "release": rel, "key": key, "tmp": tmp_path, "src": src}


def _write_sums(rel: Path, tag: str):
    lines = [f"# swim-worker-release {tag}\n"]
    for f in sorted(rel.iterdir()):
        if f.name.startswith("SHA256SUMS"):
            continue
        lines.append(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}\n")
    (rel / "SHA256SUMS").write_text("".join(lines))


def _run(env, *args):
    return subprocess.run(["bash", str(SIGN), *args], capture_output=True, text=True,
                          env=env["env"], timeout=60, stdin=subprocess.DEVNULL)


def test_signs_uploads_and_publishes(env):
    r = _run(env, "--yes", TAG)
    assert r.returncode == 0, r.stderr + r.stdout
    sig = env["release"] / "SHA256SUMS.sig"
    assert sig.stat().st_size == 64
    assert (env["tmp"] / "state").read_text().strip() == "published"
    # 上がった署名は Worker の検証 (Python) でも通る
    pytest.importorskip("cryptography")
    from swim_worker.update_verify import check_release_line, verify_sums_signature
    sums = (env["release"] / "SHA256SUMS").read_bytes()
    assert verify_sums_signature(sums, sig.read_bytes(), (env["key"].public_pem,)) == 0
    check_release_line(sums, TAG)


def test_no_publish_keeps_draft(env):
    r = _run(env, "--no-publish", TAG)
    assert r.returncode == 0, r.stderr
    assert (env["release"] / "SHA256SUMS.sig").exists()
    assert (env["tmp"] / "state").read_text().strip() == "draft"


def test_declined_confirmation_keeps_draft(env):
    r = _run(env, TAG)  # stdin が空 = 「y」以外
    assert r.returncode == 0
    assert (env["tmp"] / "state").read_text().strip() == "draft"


def _assert_refused(env, r, match):
    assert r.returncode != 0, r.stdout
    assert match in r.stderr, r.stderr
    assert not (env["release"] / "SHA256SUMS.sig").exists()
    assert (env["tmp"] / "state").read_text().strip() == "draft"


def test_refuses_published_release(env):
    (env["tmp"] / "state").write_text("published")
    r = _run(env, "--yes", TAG)
    assert r.returncode != 0 and "draft ではありません" in r.stderr
    assert not (env["release"] / "SHA256SUMS.sig").exists()


def test_refuses_wrong_release_line(env):
    _write_sums(env["release"], "v1.0.0")
    _assert_refused(env, _run(env, "--yes", TAG), "先頭行")


def test_refuses_hash_mismatch(env):
    (env["release"] / "swim-worker-macos").write_bytes(b"tampered")
    _assert_refused(env, _run(env, "--yes", TAG), "ハッシュ不一致")


def test_refuses_unlisted_or_missing_asset(env):
    (env["release"] / "extra.bin").write_bytes(b"x")
    _assert_refused(env, _run(env, "--yes", TAG), "載っていない")
    (env["release"] / "extra.bin").unlink()
    (env["release"] / "swim-worker-linux").unlink()
    _write_sums(env["release"], TAG)
    _assert_refused(env, _run(env, "--yes", TAG), "swim-worker-linux がありません")


def test_refuses_script_differing_from_tag(env):
    p = env["release"] / "swim-worker-update.sh"
    p.write_text(p.read_text() + "\n# evil\n")
    _write_sums(env["release"], TAG)
    _assert_refused(env, _run(env, "--yes", TAG), "swim-worker-update.sh がタグ")


def test_refuses_key_not_embedded_in_release(env):
    """埋め込みの公開鍵と違う鍵で署名しようとしたら上げない"""
    other = make_key(env["tmp"] / "other", "other")
    env["env"]["SWIM_RELEASE_KEY"] = str(other.private)
    os.chmod(other.private, 0o600)
    _assert_refused(env, _run(env, "--yes", TAG), "確かめられません")


def test_refuses_bad_tag(env):
    r = _run(env, "--yes", "v1.2.3;rm")
    assert r.returncode != 0 and "形式" in r.stderr


def test_shellcheck():
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck が無い")
    r = subprocess.run(["shellcheck", str(SIGN), str(ROOT / "scripts" / "set-release-pubkeys.sh"),
                        str(ROOT / "scripts" / "lock-deps.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
