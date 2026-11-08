"""比べる道具: 今の Python の Worker を、偽の SWIM で動かす (SWIM には繋がない)。

curl_cffi の AsyncSession を偽物に差し替え、`SWIM_TRIAL_RESPONSES` の JSON から応答を返す。
応答の選び方は Rust の試運転のビルド (rust/crates/swim-worker/src/swim/trial.rs) と同じ:
URL に url_contains を含み、残りの回数 (times、無ければ何回でも) がある最初の決まり。回数は全セッションで共有。
使い方 (リポの根で): python tools/compare/fake_swim.py   (設定は .env と環境変数。本物の Worker と同じ)
"""
import asyncio
import json
import os
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def load_rules(text: str) -> list[dict]:
    f = json.loads(text)
    rules = [r for c in f.get("cases", []) for r in c.get("responses", [])] + f.get("defaults", [])
    return [dict(r) for r in rules]


RULES: list[dict] = []


def pick(url: str) -> dict | None:
    for r in RULES:
        if r["url_contains"] in url and r.get("times") != 0:
            if r.get("times") is not None:
                r["times"] -= 1
            return r
    return None


class FakeCookies(dict):
    def set(self, name, value, domain=None, secure=None, **_):
        self[name] = value


class FakeResponse:
    def __init__(self, url: str, status: int, text: str, cookies: dict):
        self.url, self.status_code, self.text, self.cookies = url, status, text, cookies
        self.content = text.encode()
        self.headers = {}

    def json(self):
        return json.loads(self.text)


class FakeAsyncSession:
    def __init__(self, *a, **k):
        self.cookies = FakeCookies()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def close(self):
        pass

    async def _req(self, url: str) -> FakeResponse:
        r = pick(url)
        if r is None:
            raise RuntimeError(f"試運転の応答が無い: {url}")
        if r.get("delay_secs"):
            await asyncio.sleep(r["delay_secs"])
        text = r["text"] if r.get("text") is not None else json.dumps(r.get("json") if r.get("json") is not None else {})
        got = FakeCookies()
        if r.get("set_cookie"):
            got["SESSION"] = "trial-session"
            self.cookies["SESSION"] = "trial-session"
        return FakeResponse(r.get("final_url") or url, r.get("status", 200), text, got)

    async def get(self, url, **k):
        return await self._req(url)

    async def post(self, url, **k):
        return await self._req(url)


def main() -> None:
    RULES.extend(load_rules(Path(os.environ["SWIM_TRIAL_RESPONSES"]).read_text(encoding="utf-8")))
    from swim_worker import auth
    auth.AsyncSession = FakeAsyncSession
    sys.argv = ["swim_worker"]
    runpy.run_module("swim_worker", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
