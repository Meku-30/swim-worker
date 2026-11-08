"""Rust 版と照合するための見本 (Python 版が送る要求) の形を確かめる。

見本は tools/golden/capture_requests.py で作る (rust/tests/golden/README.md)。
"""
import json
import re
from pathlib import Path

import pytest

GOLDEN = Path(__file__).resolve().parent.parent / "rust" / "tests" / "golden" / "requests"
SCENES = ["login", "cookie_restore", "browse_f2aspr", "browse_f2dnrq", "execute_api", "fetch_public_get"]
# 最初の接続は 16 個の拡張、2 回目からは TLS のセッション再開で pre_shared_key が増えて 17 個
JA4_FULL = "t13d1516h2_8daaf6152771_d8a2da3f94cd"
JA4_RESUMED = "t13d1517h2_8daaf6152771_b6f405a00624"
AKAMAI = "1:65536;2:0;4:6291456;6:262144|15663105|0|m,a,s,p"


def _load(scene):
    return json.loads((GOLDEN / f"{scene}.json").read_text(encoding="utf-8"))


def _all_steps():
    for scene in SCENES:
        for i, step in enumerate(_load(scene)["steps"]):
            yield scene, i, step


def test_golden_scenes_exist():
    for scene in SCENES:
        data = _load(scene)
        assert data["scene"] == scene
        assert data["steps"], scene


def test_golden_order_is_wire_order():
    for scene, i, step in _all_steps():
        names = [h[0] for h in step["headers"]]
        assert names[:4] == [":method", ":authority", ":scheme", ":path"], (scene, i)
        assert all(n == n.lower() for n in names), (scene, i)


def test_golden_has_no_real_credentials():
    for scene in SCENES:
        text = (GOLDEN / f"{scene}.json").read_text(encoding="utf-8")
        # 内部の住所の形 (文字列を分けて書くのは、公開リポの検査がこの行自体を止めないため)
        for bad in ("192" + ".168.", ".local", "/" + "share/", "100" + ".64."):
            assert bad not in text, (scene, bad)
    for scene, i, step in _all_steps():
        for name, value in step["headers"]:
            if name == "cookie":
                assert value == "<cookie>", (scene, i)
            if name == ":authority":
                assert value == "<authority>", (scene, i)
        body = step.get("body")
        if isinstance(body, dict) and "password" in body:
            assert body["userId"] == "dummy-user"
            assert body["password"] == "dummy-pass"


def test_tls_matches_spike():
    # 見本を取る最初の要求 (login の 1 つ目) は新しい接続。再開されるかは接続の使い回し次第
    assert _load("login")["steps"][0]["tls"]["ja4"] == JA4_FULL
    for scene, i, step in _all_steps():
        assert step["tls"]["ja4"] in (JA4_FULL, JA4_RESUMED), (scene, i)
        assert step["http2"]["akamai_fingerprint"] == AKAMAI, (scene, i)


@pytest.mark.parametrize("scene", SCENES)
def test_paths_are_swim_paths(scene):
    for step in _load(scene)["steps"]:
        assert re.match(r"^/[^ ]*$", step["path"]), step["path"]
        assert step["host"].endswith(".swim.mlit.go.jp")
