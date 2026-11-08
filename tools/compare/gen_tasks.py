"""比べる場面の一覧 (tasks.json) を作る。解析の見本 (rust/tests/golden/parse/) の SWIM の応答と、エラーの場面。
URL は場面ごとに別 (https://web.swim.mlit.go.jp/cmp/<名>) にし、応答の決まりが混ざらないようにする。
使い方: python tools/compare/gen_tasks.py > tools/compare/tasks.json
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "https://web.swim.mlit.go.jp/cmp/"
NOTAM = {"notamList": [{"number": "A0001/26", "location": "RJTT", "contents": "x",
                        "startDate": "2026/03/01 00:00", "endDate": "2026/04/01 00:00", "notamCode": "QMRLC"}]}
# 解析を止める job_type (両方の Worker で止める): raw の比べ
RAW_JOB = "collect_pireps"


def case(name, job_type, responses, params=None):
    p = dict(params or {})
    p["url"] = BASE + name
    p.setdefault("body", {})
    return {"name": name, "task": {"job_type": job_type, "params": p},
            "responses": [{"url_contains": BASE + name, **r}
                          for r in responses]}


def main() -> None:
    cases = []
    for f in sorted((ROOT / "rust/tests/golden/parse").glob("*.json")):
        for i, c in enumerate(json.loads(f.read_text(encoding="utf-8"))):
            cases.append(case(f"{f.stem}-{i}", f.stem, [{"json": c["data"]}], c.get("params")))
    cases += [
        case("unknown-job", "collect_unknown", [{"json": NOTAM}]),
        case("empty-list", "collect_notams", [{"json": {"notamList": []}}]),
        case("relogin-401", "collect_notams", [{"status": 401, "text": "expired", "times": 1}, {"json": NOTAM}]),
        case("status-500", "collect_notams", [{"status": 500, "text": "oops"}]),
        case("status-403-twice", "collect_notams", [{"status": 403, "text": "forbidden"}]),
        case("broken-json", "collect_notams", [{"text": "{not json"}]),
        case("not-dict", "collect_notams", [{"json": [1, 2.5, None, True]}]),
        case("numbers", "collect_unknown", [{"json": {"i": 1, "f": 1.0, "e": 1e-7, "big": 12345678901234567890,
                                                      "s": "日本 ", "n": None, "b": False}}]),
        case("redirect-outside", "collect_notams", [{"final_url": "https://example.com/maintenance", "text": "<html>"}]),
        case("timeout", "collect_notams", [{"delay_secs": 60, "json": NOTAM}]),
        {"name": "public-get", "task": {"job_type": "collect_unknown",
                                        "params": {"url": BASE + "public-get", "method": "GET", "params": {"a": "1"}}},
         "responses": [{"url_contains": BASE + "public-get", "json": {"informations": [{"id": 1}]}}]},
        {"name": "capability", "task": {"job_type": "capability_test", "params": {"tests": [
            {"job_type": "collect_notams", "url": BASE + "cap-ok", "body": {}},
            {"job_type": "collect_pireps", "url": BASE + "cap-403", "body": {}},
            {"job_type": "collect_aip", "url": BASE + "cap-500", "body": {}},
            {"job_type": "collect_airports", "url": "https://example.com/x", "body": {}}]}},
         "responses": [{"url_contains": BASE + "cap-ok", "json": NOTAM},
                       {"url_contains": BASE + "cap-403", "status": 403, "text": "no permission"},
                       {"url_contains": BASE + "cap-500", "status": 500, "text": "oops"}]},
    ]
    defaults = [
        {"url_contains": "/swim/api/login", "set_cookie": True,
         "json": {"statusCode": 0, "datas": {"redirectUrl": "https://web.swim.mlit.go.jp/service/portal?lang=ja"}}},
        {"url_contains": "swim.mlit.go.jp", "json": {}},
    ]
    json.dump({"raw_job": RAW_JOB, "defaults": defaults, "cases": cases}, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
