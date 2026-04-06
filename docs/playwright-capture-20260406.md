# Playwright トラフィックキャプチャ記録 (2026-04-06)

> **目的**: swim-worker の HTTP ヘッダー偽装が実際のブラウザトラフィックと一致しているか検証するため、
> Playwright (Headless Chromium 145) でSWIMポータルの全フローをキャプチャした。
> この記録は `anti-detection.md` の実装根拠として使用する。

## キャプチャ環境

| 項目 | 値 |
|------|-----|
| 日時 | 2026-04-06 15:35 - 16:30 JST |
| ツール | Playwright (async_api) |
| ブラウザ | Headless Chromium 145.0.7632.6 (Linux x86_64) |
| Viewport | 1920x1080 |
| Locale | ja-JP |
| 実行マシン | ubuntu-laptop (192.168.1.115) |

## キャプチャファイル一覧

| ファイル | リクエスト数 | 対象 |
|---------|------------|------|
| `/tmp/login_capture.json` | 28 | ログインフロー全体 (top.swim → web.swim) |
| `/tmp/data_api_capture.json` | 179 | ログイン + f2aspr + f2dnrq SPA初期化 + データAPI |
| `scripts/header_capture_20260406_153556.json` | 52 | SPA初期化 (accept-language なし版) |
| `scripts/header_capture_20260406_154027.json` | 51 | SPA初期化 (accept-language: ja-JP 版) |
| `scripts/header_capture_20260406_154820.json` | 216 | 全フロー統合キャプチャ |

キャプチャスクリプト: `scripts/capture_headers.py`, `/tmp/capture_login.py`, `/tmp/capture_data_apis.py`

---

## 1. ヘッダーパターン分類

SWIMポータルは **Angular SPA** (top.swim, web.swim/service) に **jQuery レガシーコンポーネント** (web.swim/f2aspr, f2dnrq のbrowseページ) が混在する構成。
リクエスト種別ごとに明確に異なるヘッダーシグネチャを持つ。

### 1.1 Document Navigation (ページ遷移)

```
accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7
accept-language: ja-JP
sec-fetch-dest: document
sec-fetch-mode: navigate
sec-fetch-site: none           (ブックマーク/アドレスバー入力時)
               same-site       (top.swim → web.swim クロスサブドメイン遷移時)
sec-fetch-user: ?1
upgrade-insecure-requests: 1
```

- `origin`: なし
- `x-requested-with`: なし
- `referer`: ブックマーク時はなし、クロスサブドメイン遷移時は `https://top.swim.mlit.go.jp/`

### 1.2 Angular XHR (データAPI POST)

Worker のデータ収集 (`execute_api`) で使用するパターン。

```
accept: application/json, text/plain, */*
accept-language: ja-JP
content-type: application/json
origin: https://web.swim.mlit.go.jp
referer: https://web.swim.mlit.go.jp/{app}/browse/{service_id}
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-origin
```

- `x-requested-with`: **なし** (Angular HttpClient は送信しない)
- `origin`: POST のみ (GET では送信されない)

### 1.3 Angular XHR (GET)

top.swim / web.swim/service の Angular SPA が発火する XHR GET。

```
accept: application/json, text/plain, */*
accept-language: ja-JP
content-type: application/json    ← GETでも付与 (Angular HttpClient の挙動)
referer: https://{host}/{path}
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-origin
```

- `origin`: **なし** (GETリクエストのため)
- `x-requested-with`: **なし**

### 1.4 Angular リソースバンドル GET

```
accept: */*
accept-language: ja-JP
referer: https://web.swim.mlit.go.jp/{app}/browse/{service_id}
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-origin
```

- `content-type`: なし
- `origin`: なし
- `x-requested-with`: なし
- 対象: `/web/resource/message`, `/web/resource/webfw`, `/web/resource/user`

### 1.5 jQuery XHR GET (設定ファイル)

browseページの jQuery `$.ajax` が発火する設定ファイル読み込み。

```
accept: application/json, text/javascript, */*; q=0.01
accept-language: ja-JP
referer: https://web.swim.mlit.go.jp/{app}/browse/{service_id}
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-origin
x-requested-with: XMLHttpRequest
```

- `origin`: **なし** (GETリクエストのため)
- `content-type`: なし

### 1.6 jQuery XHR (テキスト系)

`ATCMAP.settings` や `LuciadRIALicense` など、テキストを返すリクエスト。

```
accept: text/plain, */*; q=0.01
accept-language: ja-JP
referer: https://web.swim.mlit.go.jp/{app}/browse/{service_id}
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-origin
x-requested-with: XMLHttpRequest
```

- POST の場合のみ `origin: https://web.swim.mlit.go.jp` が追加
- `content-type`: なし (LuciadRIALicense POST でもなし)

### 1.7 LuciadRIA fetch (地図タイルJSON)

```
accept: application/javascript, application/json
accept-language: ja-JP
referer: https://web.swim.mlit.go.jp/{app}/browse/{service_id}
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-origin
```

- 対象: `/js/lib/WebGIS/layer/UTM{0,1,2,3}.json`
- `x-requested-with`: なし
- resource_type: fetch

### 1.8 WMS GetCapabilities (f2lfss)

```
accept: */*
accept-language: ja-JP
referer: https://web.swim.mlit.go.jp/{app}/browse/{service_id}
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-origin
```

- 対象: `/f2lfss/ogc/wms/{layer}?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0`
- resource_type: fetch

---

## 2. ログインフロー詳細

### Phase 1: ポータルトップページ (top.swim)

| # | Method | URL | resource_type | ヘッダーパターン |
|---|--------|-----|---------------|----------------|
| 1 | GET | `/swim/` | document | Navigation (sec-fetch-site: none) |
| 2 | GET | `/assets/i18n/ja.json` | xhr | Angular GET |
| 3 | PUT | `/swim/api/statistics/countup` | xhr | Angular POST (body: `{}`) |
| 4 | GET | `/swim/api/informations?dispCount=5` | xhr | Angular GET |
| 5-6 | GET | `/swim/api/services/public?...serviceType={1,2}` | xhr | Angular GET |
| 7-8 | GET | `/swim/api/master/servicecategories` | xhr | Angular GET |

### Phase 2: ログインPOST

| # | Method | URL | Status | ヘッダーパターン |
|---|--------|-----|--------|----------------|
| 9 | **POST** | `/swim/api/login` | 200 | Angular POST |

```
accept: application/json, text/plain, */*
content-type: application/json
origin: https://top.swim.mlit.go.jp
referer: https://top.swim.mlit.go.jp/swim/login
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-origin
```

- **Body**: `{"userId":"<REDACTED>","password":"<REDACTED>"}`
- **`x-requested-with`: なし** (Angular HttpClient)
- レスポンスで `MSMSI` + `MSMAI` Cookie がSet-Cookieされる

### Phase 3: 認証済みポータルへリダイレクト

| # | Method | URL | ヘッダーパターン |
|---|--------|-----|----------------|
| 10 | GET | `web.swim.mlit.go.jp/service/portal?lang=ja` | Navigation |

```
accept: text/html,application/xhtml+xml,application/xml;q=0.9,...
referer: https://top.swim.mlit.go.jp/
sec-fetch-dest: document
sec-fetch-mode: navigate
sec-fetch-site: same-site       ← top.swim → web.swim のクロスサブドメイン遷移
sec-fetch-user: ?1
upgrade-insecure-requests: 1
cookie: MSMSI=<value>; MSMAI=<value>
```

### Phase 4: 認証済みポータルSPAロード (web.swim/service)

| # | Method | URL | 用途 |
|---|--------|-----|------|
| 11 | GET | `/service/api/accounts/summary` | アカウント概要 |
| 12 | GET | `/service/api/informations?dispCount=5` | お知らせ |
| 13 | GET | `/service/api/services/usage?...` | 利用中サービス |
| 14-15 | GET | `/service/api/services/public?...serviceType={1,2}` | 公開サービス |
| 16-19 | GET | `/service/api/accounts/summary` | アカウント概要 (重複呼出) |
| 17+ | GET | `/service/api/master/servicecategories` | カテゴリマスター (5回+) |
| 18 | GET | `/assets/i18n/ja.json` | i18n翻訳 |

全て Angular GET パターン。Cookie `MSMSI` + `MSMAI` が全リクエストに送信される。

---

## 3. SPA初期化フロー詳細

### 3.1 f2aspr (航空気象・運航情報) - `/f2aspr/browse/flv850s001`

browseページ遷移後に自動発火するリクエスト一覧。

#### jQuery リクエスト (X-Requested-With: XMLHttpRequest)

| Method | URLパス | Accept |
|--------|---------|--------|
| POST | `/f2aspr/LuciadRIALicense` | `text/plain, */*; q=0.01` |
| GET | `/f2aspr/js/lib/WebGIS/ATCMAP.settings` | `text/plain, */*; q=0.01` |
| GET | `/f2aspr/settings/auto_filter.json` | `application/json, text/javascript, */*; q=0.01` |
| GET | `/f2aspr/settings/map_disp.json` | `application/json, text/javascript, */*; q=0.01` |
| GET | `/f2aspr/settings/velocity.json` | `application/json, text/javascript, */*; q=0.01` |
| GET | `/f2aspr/browse/flv850s001` (XHR再取得) | `application/json, text/javascript, */*; q=0.01` |
| GET | `/f2aspr/settings/default_view.json` | `application/json, text/javascript, */*; q=0.01` |
| GET | `/f2aspr/settings/default_font.json` | `application/json, text/javascript, */*; q=0.01` |
| GET | `/f2aspr/settings/default_dire_dist_position.json` | `application/json, text/javascript, */*; q=0.01` |
| GET | `/f2aspr/settings/shape_datablock_setting.json` | `application/json, text/javascript, */*; q=0.01` |
| GET | `/f2aspr/settings/default_color.json` | `application/json, text/javascript, */*; q=0.01` |
| GET | `/f2aspr/settings/menu.json` | `application/json, text/javascript, */*; q=0.01` |
| GET | `/f2aspr/settings/commonMenuSetting.json` | `application/json, text/javascript, */*; q=0.01` |
| GET | `/f2aspr/settings/toolbarSetting.json` | `application/json, text/javascript, */*; q=0.01` |
| GET | `/f2aspr/settings/blink_info.json` | `application/json, text/javascript, */*; q=0.01` |
| GET | `/f2aspr/settings/groupLayer.json` | `application/json, text/javascript, */*; q=0.01` |

計 16リクエスト (POST 1 + GET 15)

#### Angular リソースバンドル

| Method | URLパス | Accept |
|--------|---------|--------|
| GET | `/f2aspr/web/resource/message` | `*/*` |
| GET | `/f2aspr/web/resource/webfw` | `*/*` |
| GET | `/f2aspr/web/resource/user` | `*/*` |

#### Angular データAPI POST

| URLパス | 追加パラメータ | 用途 |
|---------|--------------|------|
| `/f2aspr/web/FLV901/LGV300` | なし | METAR/TAF |
| `/f2aspr/web/FLV811/LGV231` | `"profileType":0,"lang":"ja"` | 空港プロファイル |
| `/f2aspr/web/FLV802/LGV205` | `"profileType":0` | プロファイル |
| `/f2aspr/web/FLV934/LGV387` | なし | 設定情報 |
| `/f2aspr/web/FLV921/LGV359` | なし | ATIS |
| `/f2aspr/web/FLV909/LGV330` | なし | 運航情報 |
| `/f2aspr/web/FLV913/LGV350` | なし | 気象概況A |
| `/f2aspr/web/FLV913/LGV351` | なし | 気象概況B |
| `/f2aspr/web/FLV914/LGV352` | `"layerName":"Aerodome_Weather_Status"` | 空港気象ステータス |
| `/f2aspr/web/FLV915/LGV353` | なし | 気象レイヤー |
| `/f2aspr/web/FLV916/LGV354` | なし | 気象レイヤー |
| `/f2aspr/web/FLV918/LGV356` | `"layerName":"SIGMET"` | SIGMET |
| `/f2aspr/web/FLV919/LGV357` | `"layerNameList":[]` | 気象レイヤー(空リスト) |
| `/f2aspr/web/FLV920/LGV358` | `"layerNameList":[14種PIREP/気象]` | PIREP + 悪天候 |
| `/f2aspr/web/FLV807/LGV226` | `"callerKind":0,...,"notamCd23":[49種]` | NOTAM |

全て共通ヘッダー:
```
accept: application/json, text/plain, */*
content-type: application/json
origin: https://web.swim.mlit.go.jp
referer: https://web.swim.mlit.go.jp/f2aspr/browse/flv850s001
sec-fetch-dest: empty
sec-fetch-mode: cors
sec-fetch-site: same-origin
x-requested-with: (なし)
```

共通 POST body 基本構造:
```json
{"msgHeader":{"jnlInfo":{"jnlRegistFlag":0},"tsusuInfo":{}},"ctrlInfo":{},"ctrlHeader":{}}
```

#### LuciadRIA 地図タイル

| URLパス | Accept |
|---------|--------|
| `/f2aspr/js/lib/WebGIS/layer/UTM0.json` | `application/javascript, application/json` |
| `/f2aspr/js/lib/WebGIS/layer/UTM1.json` | `application/javascript, application/json` |
| `/f2aspr/js/lib/WebGIS/layer/UTM2.json` | `application/javascript, application/json` |
| `/f2aspr/js/lib/WebGIS/layer/UTM3.json` | `application/javascript, application/json` |

#### WMS GetCapabilities (f2lfss 経由)

f2aspr 遷移時に約76レイヤーの GetCapabilities リクエストが発火:

- `baseline_*` (16): 空港、ウェイポイント、航法施設、ルート、空域
- `adp_*` (8): QNH、VRP、PACOTS、DataLink等
- `wx_*` (46): 高層風 10気圧面 + レーダーエコー強度/雲頂 各18面
- `aerodrome_map_*` (4): 滑走路、誘導路、エプロン、スポット
- `navi_*` (1): 訓練試験空域

### 3.2 f2dnrq (飛行計画) - `/f2dnrq/browse/FUV201`

f2aspr と同じ SPA 基盤で、パターンは完全に並行。

#### jQuery リクエスト

| Method | URLパス |
|--------|---------|
| POST | `/f2dnrq/LuciadRIALicense` |
| GET | `/f2dnrq/js/lib/WebGIS/ATCMAP.settings` |
| GET | `/f2dnrq/settings/auto_filter.json` |
| GET | `/f2dnrq/settings/map_disp.json` |
| GET | `/f2dnrq/settings/velocity.json` |
| GET | `/f2dnrq/browse/FUV201` (XHR再取得) |
| GET | `/f2dnrq/settings/default_view.json` |
| GET | `/f2dnrq/settings/default_font.json` |
| GET | `/f2dnrq/settings/default_dire_dist_position.json` |
| GET | `/f2dnrq/settings/shape_datablock_setting.json` |
| GET | `/f2dnrq/settings/default_color.json` |
| GET | `/f2dnrq/settings/menu.json` |
| GET | `/f2dnrq/settings/commonMenuSetting.json` |
| GET | `/f2dnrq/settings/toolbarSetting.json` |
| GET | `/f2dnrq/settings/blink_info.json` |
| GET | `/f2dnrq/settings/groupLayer.json` |

#### Angular リソースバンドル

`/f2dnrq/web/resource/{message,webfw,user}` (f2aspr と同一パターン)

#### Angular データAPI POST

| URLパス | 用途 |
|---------|------|
| `/f2dnrq/web/FUV201/USV005` | 飛行計画一覧 |

#### WMS GetCapabilities

f2dnrq は 5レイヤーのみ: `geojson_ac`, `aerodrome_map_{runway,taxiway,apron,aircraftstand}`

---

## 4. Cookie 管理

### Cookie 名と用途

| Cookie | 用途 | ドメイン |
|--------|------|---------|
| `MSMSI` | セッション管理 | `.mlit.go.jp` (実測確認済み) |
| `MSMAI` | 認証トークン | `.mlit.go.jp` (実測確認済み) |

### Cookie ライフサイクル

1. `POST /swim/api/login` の成功レスポンスで `Set-Cookie` される
2. `top.swim` → `web.swim` のクロスサブドメイン遷移時に送信される (domain が `.mlit.go.jp` のため)
3. `MSMSI` は頻繁に値がローテーションされる (セッション固定攻撃対策)
4. `MSMAI` はログインセッション中は比較的安定

### ドメイン確認

Cookie domain `.mlit.go.jp` は 2026-04-06 に curl_cffi の実際の `Set-Cookie` レスポンスヘッダーから確認:

```
set-cookie: MSMSI=...; domain=.mlit.go.jp; path=/; ...
set-cookie: MSMAI=...; domain=.mlit.go.jp; path=/; ...
```

---

## 5. Worker 実装との対応

### 5.1 完全一致が確認されたもの

| 項目 | キャプチャ | Worker実装 | 判定 |
|------|----------|-----------|------|
| ログインURL | `POST /swim/api/login` | `SWIM_LOGIN_URL` | 一致 |
| ログインBody | `{"userId":..., "password":...}` | `json={"userId":..., "password":...}` | 一致 |
| ログインReferer | `https://top.swim.mlit.go.jp/swim/login` | `Referer: f"{SWIM_TOP_URL}/swim/login"` | 一致 |
| リダイレクトURL | `web.swim.mlit.go.jp/service/portal?lang=ja` | `data.datas.redirectUrl` から取得 | 一致 |
| リダイレクトSec-Fetch-Site | `same-site` | `"Sec-Fetch-Site": "same-site"` | 一致 |
| リダイレクトReferer | `https://top.swim.mlit.go.jp/` | `"Referer": f"{SWIM_TOP_URL}/"` | 一致 |
| データAPI Accept | `application/json, text/plain, */*` | `_SESSION_HEADERS["Accept"]` | 一致 |
| データAPI Origin (POST) | `https://web.swim.mlit.go.jp` | `_POST_EXTRA_HEADERS["Origin"]` | 一致 |
| データAPI X-Requested-With | なし | セッションデフォルトに含めない | 一致 |
| jQuery X-Requested-With | `XMLHttpRequest` | `header_type="jquery"` で付与 | 一致 |
| jQuery Accept (JSON) | `application/json, text/javascript, */*; q=0.01` | `header_type="jquery"` | 一致 |
| jQuery Accept (text) | `text/plain, */*; q=0.01` | `header_type="jquery_text"` | 一致 |
| Angular リソース Accept | `*/*` | `header_type="angular_res"` | 一致 |
| LuciadRIA Accept | `application/javascript, application/json` | `header_type="luciadria"` | 一致 |
| browseページ Sec-Fetch-Site | `none` (ブックマーク想定) | `_NAV_HEADERS` | 一致 |
| Cookie domain | `.mlit.go.jp` | `domain=".mlit.go.jp"` | 一致 |

### 5.2 注意事項

- **Headless Chrome 検知**: キャプチャ環境は Headless Chromium のため `sec-ch-ua` に `HeadlessChrome` が含まれる。Worker は curl_cffi の `BrowserType.chrome136` TLS fingerprint を使うためこの問題はない
- **User-Agent**: キャプチャの UA は `HeadlessChrome/145` だが、Worker は curl_cffi が Chrome 136 の UA を自動生成するため問題なし
- **content-type on GET**: Angular SPA は GET リクエストにも `content-type: application/json` を付与する。Worker のセッションデフォルトにはこれを含めていないが、Sec-Fetch-* の正確さの方が検知回避上重要

---

## 6. キャプチャスクリプト

### scripts/capture_headers.py

SPA初期化トラフィックのキャプチャ用。ログイン → f2aspr browse → 特定レスポンス待ち → JSON保存。

### /tmp/capture_login.py

ログインフロー専用。top.swim → ログイン → web.swim リダイレクトまでの全リクエストをキャプチャ。

### /tmp/capture_data_apis.py

データ収集API全体キャプチャ。ログイン → f2aspr (60秒待機) → f2dnrq (60秒待機) でSPA初期化+自動発火APIを網羅的にキャプチャ。

全スクリプト共通:
- `page.on("response")` ハンドラで `await request.all_headers()` (async) を使用
- `wait_until="domcontentloaded"` (`networkidle` はSPAで永遠にタイムアウトするため不使用)
- `locale="ja-JP"` 設定で `Accept-Language: ja-JP` を生成
