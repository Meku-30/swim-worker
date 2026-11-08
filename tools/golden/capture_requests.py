"""Python 版の SwimClient が送る要求を、送り先だけ指紋の確認先に替えて取り、見本にする。

本番の SWIM には一切送らない。SwimClient が出す要求 (method・ヘッダー・body) を、
同じ curl_cffi のセッション (同じ impersonate・Cookie) のまま https://tls.peet.ws/api/all に送り、
確認先が「受け取った」と返したもの (TLS・HTTP/2 の指紋、HEADERS フレームの並び) を記録する。
SwimClient には場面ごとに作った応答を返す。

使い方 (リリースと同じ版の依存で):
    python tools/golden/capture_requests.py --out rust/tests/golden/requests
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import curl_cffi  # noqa: E402
from curl_cffi.requests import AsyncSession  # noqa: E402

from swim_worker import auth  # noqa: E402

PEET_URL = "https://tls.peet.ws/api/all"
PEET_HOST = "tls.peet.ws"
USER = "dummy-user"
PASS = "dummy-pass"

_orig_request = AsyncSession.request
_steps: list[dict] = []


class FakeResponse:
    def __init__(self, url: str, status: int = 200, data=None, cookies=None):
        self.url = url
        self.status_code = status
        self._data = data if data is not None else {}
        self.cookies = cookies or {}
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


def _fake_for(method: str, url: str, session: AsyncSession) -> FakeResponse:
    """SwimClient が先へ進めるだけの作った応答"""
    if url == auth.SWIM_LOGIN_URL:
        session.cookies.set("SESSION", "dummy-session", domain=".mlit.go.jp", secure=True)
        return FakeResponse(url, data={
            "statusCode": 0,
            "datas": {"redirectUrl": auth.SWIM_DEFAULT_REDIRECT_URL},
        }, cookies={"SESSION": "dummy-session"})
    if url.startswith(auth.SWIM_TOP_URL + "/swim/api/informations"):
        return FakeResponse(url, data={"datas": []})
    return FakeResponse(url, data={"statusCode": 0, "datas": {}})


def _wire_headers(peet: dict) -> list[list[str]]:
    for frame in peet.get("http2", {}).get("sent_frames", []):
        if frame.get("frame_type") == "HEADERS":
            out = []
            for line in frame["headers"]:
                name, _, value = line.partition(": ")
                if name == ":authority":
                    value = "<authority>"
                elif name == "cookie":
                    value = "<cookie>"
                out.append([name, value])
            return out
    raise RuntimeError("確認先の応答に HEADERS フレームがない")


async def _capturing_request(self: AsyncSession, method, url, **kwargs):
    parts = urlsplit(url)
    # 元の宛先の Cookie を確認先にも付ける (Cookie ヘッダーの位置を見本に残すため)
    for c in list(self.cookies.jar):
        if parts.hostname and parts.hostname.endswith(c.domain.lstrip(".")):
            self.cookies.set(c.name, c.value, domain=PEET_HOST)
    resp = await _orig_request(self, method, PEET_URL, **kwargs)
    peet = resp.json()
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    if kwargs.get("params"):
        from urllib.parse import urlencode
        path += ("&" if "?" in path else "?") + urlencode(kwargs["params"])
    _steps.append({
        "method": method,
        "host": parts.hostname,
        "path": path,
        "headers": _wire_headers(peet),
        "body": kwargs.get("json"),
        "tls": {"ja4": peet["tls"]["ja4"], "peetprint_hash": peet["tls"]["peetprint_hash"]},
        "http2": {"akamai_fingerprint": peet["http2"]["akamai_fingerprint"]},
    })
    await asyncio.sleep(0.3)  # 確認先に負荷をかけない
    return _fake_for(method, url, self)


async def _no_sleep(_secs, *a, **k):
    return None


def _patch():
    AsyncSession.request = _capturing_request
    # 待ち時間は 0 にする (分布は tools/golden/sample_delays.py で別に見る)
    auth.asyncio = types.SimpleNamespace(**{n: getattr(asyncio, n) for n in dir(asyncio) if not n.startswith("__")})
    auth.asyncio.sleep = _no_sleep
    auth.validate_swim_url = lambda u: u


async def _logged_in(tmp: Path) -> auth.SwimClient:
    c = auth.SwimClient(USER, PASS, cookie_file=str(tmp / "none.json"))
    await c.login()
    return c


async def scene_login(tmp):
    c = auth.SwimClient(USER, PASS, cookie_file=str(tmp / "login.json"))
    _steps.clear()
    await c.login()
    await c.close()


async def scene_cookie_restore(tmp):
    f = tmp / "restore.json"
    f.write_text(json.dumps({"SESSION": "dummy-session"}))
    os.chmod(f, 0o600)
    c = auth.SwimClient(USER, PASS, cookie_file=str(f))
    _steps.clear()
    await c.login()
    await c.close()


async def _scene_browse(tmp, prefix: str):
    c = await _logged_in(tmp)
    _steps.clear()
    await c._ensure_browse_page(f"{auth.SWIM_PORTAL_URL}/{prefix}/web/dummy")
    await c.close()


async def scene_browse_f2aspr(tmp):
    await _scene_browse(tmp, "f2aspr")


async def scene_browse_f2dnrq(tmp):
    await _scene_browse(tmp, "f2dnrq")


async def scene_execute_api(tmp):
    c = await _logged_in(tmp)
    urls = [
        f"{auth.SWIM_PORTAL_URL}/f2aspr/web/FLV850/dummy",
        f"{auth.SWIM_PORTAL_URL}/f2dnrq/web/FUV201/dummy",
        f"{auth.SWIM_PORTAL_URL}/service/api/dummy",
    ]
    for u in urls:  # 画面の初期化は別の場面で取るので、訪問済みにしておく
        c._visited_pages.add(auth._get_referer(u))
    _steps.clear()
    for u in urls:
        await c.execute_api(u, {"ctrlInfo": {}, "dummy": 1})
    await c.close()


async def scene_fetch_public_get(tmp):
    c = auth.SwimClient(USER, PASS, cookie_file=str(tmp / "pub.json"))
    _steps.clear()
    await c.fetch_public_get(f"{auth.SWIM_TOP_URL}/swim/api/informations", params={"lang": "ja"})
    await c.close()


SCENES = {
    "login": scene_login,
    "cookie_restore": scene_cookie_restore,
    "browse_f2aspr": scene_browse_f2aspr,
    "browse_f2dnrq": scene_browse_f2dnrq,
    "execute_api": scene_execute_api,
    "fetch_public_get": scene_fetch_public_get,
}


async def main(out: Path):
    _patch()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        for name, fn in SCENES.items():
            await fn(Path(d))
            data = {
                "scene": name,
                "curl_cffi": curl_cffi.__version__,
                "impersonate": str(auth._BROWSER_TYPE.value),
                "steps": list(_steps),
            }
            (out / f"{name}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            print(f"{name}: {len(_steps)} 件")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    asyncio.run(main(ap.parse_args().out))
