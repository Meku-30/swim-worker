"""配布物 (Dockerfile・systemd unit) と certs のテスト"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_certs_has_no_temp_file_helper():
    """埋め込み CA は ssl_ca_data で渡す。一時ファイルに書き出す get_ca_cert_path は使わない"""
    from swim_worker import certs
    assert not hasattr(certs, "get_ca_cert_path")
    assert certs.CA_CERT_PEM.startswith("-----BEGIN CERTIFICATE-----")


def test_dockerfile_healthcheck_uses_embedded_ca_data():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    hc = text[text.index("HEALTHCHECK"):]
    assert "get_ca_cert_path" not in hc
    assert "ssl_ca_data=CA_CERT_PEM" in hc


def _unit(name: str) -> dict[str, list[str]]:
    """unit ファイルを {キー: [値, ...]} にする (コメント・セクション行は除く)"""
    out: dict[str, list[str]] = {}
    for line in (ROOT / "scripts" / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[")):
            continue
        m = re.match(r"^([A-Za-z]+)=(.*)$", line)
        if m:
            out.setdefault(m.group(1), []).append(m.group(2))
    return out


def test_worker_unit_hardening():
    u = _unit("swim-worker.service")
    assert u["UMask"] == ["0077"]
    assert u["SystemCallArchitectures"] == ["native"]
    deny = " ".join(u.get("IPAddressDeny", [])).split()
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
                "100.64.0.0/10", "fc00::/7", "fe80::/10"):
        assert net in deny, net
    # ループバックは閉じない (systemd-resolved のスタブ 127.0.0.53 で名前解決するため)
    assert not any(n.startswith("127.") or n == "::1/128" or n == "localhost" for n in deny)


def test_worker_unit_memory_deny_write_execute_comment_is_accurate():
    text = (ROOT / "scripts" / "swim-worker.service").read_text(encoding="utf-8")
    idx = text.index("MemoryDenyWriteExecute=false")
    comment = text[:idx].rsplit("\n\n", 1)[-1]
    # 「禁止」と書いて false にしている矛盾をなくす
    assert "禁止" not in comment.splitlines()[-1]
    assert "無効" in comment


# --- W3-1 CI / W3-2 依存のロック ---------------------------------------------

import yaml  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"


def _req_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            names |= _req_names(path.parent / line[3:].strip())
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)", line)
        names.add(m.group(1).lower().replace("_", "-"))
    return names


def _lock_pins(path: Path) -> dict[str, str]:
    pins = {}
    for m in re.finditer(r"^([a-z0-9_.-]+)==([^\s;\\]+)", path.read_text(encoding="utf-8"), re.M):
        pins[m.group(1).replace("_", "-")] = m.group(2)
    return pins


def test_locks_cover_requirements_with_hashes():
    for src, lock in (("requirements.txt", "requirements.lock"),
                      ("requirements-dev.txt", "requirements-dev.lock")):
        pins = _lock_pins(ROOT / lock)
        for name in _req_names(ROOT / src):
            assert name in pins, f"{lock} に {name} がありません (scripts/lock-deps.sh)"
        text = (ROOT / lock).read_text(encoding="utf-8")
        entries = re.split(r"\n(?=[a-z0-9])", text.split("\n", 2)[-1])
        for entry in entries:
            if entry.strip() and not entry.startswith("#"):
                assert "--hash=sha256:" in entry, entry.splitlines()[0]
    dev = _lock_pins(ROOT / "requirements-dev.lock")
    for name in ("pyinstaller", "cryptography", "keyring", "pywin32-ctypes", "pyobjc-core"):
        assert name in dev, name


def test_installs_use_hash_locks():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--require-hashes -r requirements.lock" in docker
    for wf in ("build-release.yml", "gui-smoke.yml"):
        text = (WORKFLOWS / wf).read_text(encoding="utf-8")
        runs = [l for l in text.splitlines() if "pip install" in l]
        assert runs and all("--require-hashes -r requirements-dev.lock" in l for l in runs), wf


def _steps(wf: dict):
    for name, job in wf["jobs"].items():
        for step in job.get("steps", []):
            yield name, job, step


def test_actions_are_pinned_to_commit_sha():
    for path in WORKFLOWS.glob("*.yml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*(?:-\s*)?uses:\s*(\S+)(.*)$", line)
            if not m:
                continue
            ref = m.group(1).split("@", 1)
            assert len(ref) == 2 and re.fullmatch(r"[0-9a-f]{40}", ref[1]), f"{path.name}: {line}"
            assert re.search(r"#\s*v\d+\.\d+\.\d+", m.group(2)), f"版のコメントが無い: {line}"


def test_workflow_permissions_are_per_job():
    rel = yaml.safe_load((WORKFLOWS / "build-release.yml").read_text(encoding="utf-8"))
    assert rel["permissions"] == {}
    for name, job in rel["jobs"].items():
        want = {"contents": "write"} if name == "release" else {"contents": "read"}
        assert job.get("permissions") == want, name
    smoke = yaml.safe_load((WORKFLOWS / "gui-smoke.yml").read_text(encoding="utf-8"))
    assert smoke["permissions"] == {"contents": "read"}
    for wf in (rel, smoke):
        for _, _, step in _steps(wf):
            if "actions/checkout" in step.get("uses", ""):
                assert step.get("with", {}).get("persist-credentials") is False


def test_release_is_draft_complete_and_version_bound():
    rel = yaml.safe_load((WORKFLOWS / "build-release.yml").read_text(encoding="utf-8"))
    steps = rel["jobs"]["release"]["steps"]
    prep = next(s for s in steps if s.get("name") == "Prepare release files")["run"]
    assert "|| true" not in prep and "set -euo pipefail" in prep
    assert "release/swim-worker-update.sh" in prep
    sums = next(s for s in steps if s.get("name") == "Generate SHA256SUMS")["run"]
    assert 'echo "# swim-worker-release ${GITHUB_REF_NAME}"' in sums
    create = next(s for s in steps if "action-gh-release" in s.get("uses", ""))
    assert create["with"]["draft"] is True
    assert create["with"]["fail_on_unmatched_files"] is True
    build = rel["jobs"]["build"]
    assert all("latest" not in inc["os"] for inc in build["strategy"]["matrix"]["include"])
    upload = next(s for s in build["steps"] if "upload-artifact" in s.get("uses", ""))
    assert upload["with"]["if-no-files-found"] == "error"


def test_release_refuses_existing_release_for_tag():
    """同じタグで CI を再実行しても draft が二重にできない: 既にリリース (draft 含む) があれば失敗"""
    rel = yaml.safe_load((WORKFLOWS / "build-release.yml").read_text(encoding="utf-8"))
    steps = rel["jobs"]["release"]["steps"]
    idx = next(i for i, s in enumerate(steps) if s.get("name") == "Refuse existing release for this tag")
    create = next(i for i, s in enumerate(steps) if "action-gh-release" in s.get("uses", ""))
    assert idx < create
    step = steps[idx]
    assert "gh api" in step["run"] and "exit 1" in step["run"] and "--paginate" in step["run"]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"


def test_keyring_is_bundled_only_on_windows():
    """macOS の GUI はキーチェーンを使わない (確認ダイアログで起動が止まる) ので keyring を入れない"""
    text = (ROOT / "scripts" / "build_exe.py").read_text(encoding="utf-8")
    assert "keyring.backends.macOS" not in text
    assert '"--hidden-import", "keyring.backends.Windows"' in text
