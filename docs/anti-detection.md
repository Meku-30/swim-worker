# SWIM bot検知回避 対策一覧

SWIM（航空情報共有基盤）ポータルからのデータ収集において、bot検知を回避するために実装している全対策をまとめたドキュメント。

Worker（SWIM API直接アクセス）とCoordinator（スケジュール制御・メンテ情報API）の両面で対策を行っている。

## 1. Worker側の対策 (swim-worker)

### 1.1 TLSフィンガープリント偽装

| 項目 | 実装 |
|------|------|
| ライブラリ | `curl_cffi` |
| ブラウザ指定 | `BrowserType.chrome136` |
| HTTP/2 | curl_cffiが自動的に有効化 |
| TLSハンドシェイク | Chrome 136と同一のCipher Suite順序・拡張を再現 |

通常のPython HTTPクライアント（requests, httpx, aiohttp）はTLSハンドシェイクのCipher Suite順序やTLS拡張がブラウザと異なるため、JA3/JA4フィンガープリントで即座にbotと識別される。curl_cffiはChrome実バイナリのTLSスタックをリンクして使用するため、TLSレベルでの検知を回避する。

### 1.2 HTTPヘッダーの一貫性

#### XHR用ヘッダー（API呼び出し時）

```python
_XHR_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Origin": "https://web.swim.mlit.go.jp",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
```

`Cache-Control` / `Pragma` は含めない。Chromeは通常のXHR/fetchでこれらを送信しない（明示的に `cache: "no-cache"` を指定した場合のみ）。不要なヘッダーの付与は検知の手がかりになる。

#### ページナビゲーション用ヘッダー（ページ読み込み時）

```python
_NAV_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,...",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",          # 初回アクセス時。遷移時は "same-site" に上書き
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
```

`Cache-Control` / `Pragma` は含めない。Chromeが `Cache-Control: no-cache` を送信するのは Ctrl+Shift+R（強制リロード）のみ。初回アクセスやリンク遷移では送信しない。

**重要**: `User-Agent`、`Sec-Ch-Ua`、`Sec-Ch-Ua-Platform` はcurl_cffiのchrome136デフォルトに任せる。これらを手動設定するとTLSフィンガープリントとの不整合が生じ、逆に検知される。

### 1.3 ログインフロー

```
1. GET https://top.swim.mlit.go.jp/      (NAV_HEADERS, Sec-Fetch-Site: none)
2. 1-3秒ランダム待機（SPA読み込み時間を再現）
3. POST https://top.swim.mlit.go.jp/swim/webapi/login  (XHR_HEADERS)
4. 0.5-1.5秒ランダム待機（リダイレクト遅延を再現）
5. GET https://web.swim.mlit.go.jp/       (NAV_HEADERS, Sec-Fetch-Site: same-site)
6. 0.5-1.5秒ランダム待機
7. 以降、web.swim.mlit.go.jp へのXHRでAPI呼び出し
```

ステップ5が重要: 実ブラウザではログイン後にSPAが `web.swim` へリダイレクトする。これがないと「`web.swim` へのdocumentナビゲーションなしにXHRが来た」と検知される可能性がある。`top.swim` → `web.swim` はサブドメイン間遷移のため `Sec-Fetch-Site: same-site` が正しい。

ログインフロー中の全Cookie（ページGET + ログインPOST + web.swim遷移）を永続セッションに移す。保存済みCookieでセッション復元できる場合はこのフロー全体をスキップし、不要なアクセスを削減する。

### 1.4 SPA初期化・ブラウズ画面ナビゲーション

実ブラウザでサービスページを開くと、API呼び出しの前にSPA初期化リクエストが自動で発生する。Workerでもこの遷移をセッション中に各サービス1回再現する。リクエスト内容・順序・HTTPメソッドはPlaywrightで実際のChrome操作をキャプチャして特定したもの。

```
サービスページを開く際の実ブラウザの挙動（Playwrightキャプチャで実測）:
1. ブラウズ画面の初回GET（HTMLシェル読み込み）
2. SPA初期化: ライセンスPOST → ATCMAP設定 → auto_filter → 初期データAPI POST
3. 設定ファイルGET + リソースバンドルGET（message/webfw/user）
4. ブラウズ画面の追加GET × 3回（Angular SPAルーティング、resource/userの後）
5. 追加API POST → velocity.json以降の設定ファイルGET
6. 以降のAPI呼び出し（XHRヘッダー + Referer付き）
```

| サービス | 初期化リクエスト数 | ブラウズ画面 | 内訳 |
|---------|------------------|-----------|------|
| f2aspr (PKG気象/PIREP/便/空港等) | 23件 + ブラウズGET×4 | `/f2aspr/browse/flv850s001` | LuciadRIALicense(POST), ATCMAP設定, 設定JSON×12, リソースバンドル×3, 初期データAPI×4(FLV901/FLV811/FLV802/FLV934) |
| f2dnrq (NOTAM) | 25件 + ブラウズGET×4 | `/f2dnrq/browse/FUV201` | LuciadRIALicense(POST), ATCMAP設定, 設定JSON×12, リソースバンドル×3, USV005(POST)×2, UTMレイヤー×4 |

ブラウズ画面は**利用登録済みサービスのみ**にアクセスする。未登録サービスのページにアクセスすることは不自然な行動になるため。

#### Playwrightキャプチャ記録

2026-03-04にPlaywright (Chromium) で実際のSWIMポータルにログインし、各サービスページを開いた際のネットワークリクエストを2回記録した。キャプチャデータは `swim-api-legacy/scripts/investigation_results/` に保存されている。

- **71_all_data_api_calls.json** (11:20, 72リクエスト) — 1回目のキャプチャ
- **94_all_api_calls.json** (11:27, 131リクエスト) — 2回目のキャプチャ

**重要な発見**: 2回のキャプチャでSPA初期化リクエストの**順序に差異**がある。これはAngular SPAの非同期JavaScript読み込みによるもので、毎回同じ順序になるとは限らない。

| 項目 | 71_ (1回目) | 94_ (2回目) |
|------|------------|------------|
| LuciadRIA vs ATCMAP | ATCMAP → LuciadRIA | LuciadRIA → ATCMAP |
| API POST (FLV901等) | SPA init中に出現せず、groupLayer後 | auto_filter直後に出現 |
| USV005 1回目 (f2dnrq) | groupLayer後に1回のみ | auto_filter直後 + groupLayer後 |
| browse GETs位置 | resource/user後 | resource/user後 |
| 設定ファイル群の順序 | 同一 | 同一 |

**安定している部分**: browse GETsは常にresource/userの後、設定ファイル群(velocity〜groupLayer)の順序は固定。
**可変な部分**: LuciadRIA/ATCMAPの前後、API POSTの出現タイミング（SPA init前半 or groupLayer後）。

Workerの `_SPA_INIT_REQUESTS` は94_（2回目）の順序に基づいて実装している。毎回固定順序で送信するが、上記の通り実ブラウザでも順序にばらつきがあるため、固定であること自体は検知リスクにならない。

#### SPA初期化POST bodyの正確な送信

2026-04-02にPlaywrightで実測し、SPA初期化のPOSTリクエストbodyを特定した。以前は全て `json={}` で送信していたが、正しいbodyを送信するよう修正済み。

| 初期化API | 送信body |
|----------|---------|
| `LuciadRIALicense` | body なし（空POST） |
| `web/FLV901/LGV300` | `_BASIC_BODY` |
| `web/FLV811/LGV231` | `_BASIC_BODY` + `"profileType": 0, "lang": "ja"` |
| `web/FLV802/LGV205` | `_BASIC_BODY` + `"profileType": 0` |
| `web/FLV934/LGV387` | `_BASIC_BODY` |
| `web/FUV201/USV005` | `_BASIC_BODY` |

`_BASIC_BODY` = `{"msgHeader":{"jnlInfo":{"jnlRegistFlag":0},"tsusuInfo":{}},"ctrlInfo":{},"ctrlHeader":{}}`

#### SPA初期化フェーズブレイク

実ブラウザのSPA読み込みではJSパース・実行によりリクエスト群の間に自然な「間」が入る。Workerでもこれを再現し、特定のポイントで長めの遅延を挿入する。

| タイミング | 遅延 | 理由 |
|-----------|------|------|
| ATCMAP.settings 後 | 0.3-1.0秒 | JSパース・設定読み込み |
| resource/user 後 | 0.3-1.0秒 | SPAルーティング完了 |
| groupLayer.json 後 | 0.3-1.0秒 | マップレイヤー初期化 |
| その他のリクエスト間 | 0.02-0.15秒 | 非同期並行ダウンロード |

##### キャプチャ1回目: 71_all_data_api_calls.json (11:20)

**f2aspr（空域プロファイル）:**

```
# SPA初期化
 1. GET  200 js/lib/WebGIS/ATCMAP.settings
 2. POST 200 LuciadRIALicense
 3. GET  200 settings/auto_filter.json
 4. GET  200 settings/map_disp.json
 5. GET  200 web/resource/message
 6. GET  200 web/resource/webfw
 7. GET  200 web/resource/user
     [GET browse/flv850s001 ×3]         ← Angular SPAルーティング
 8. GET  200 settings/velocity.json
 9. GET  200 settings/default_view.json
    ... (default_font〜groupLayerは94_と同一順序)
19. GET  200 settings/groupLayer.json
# ここまでSPA初期化。API POSTはSPA init中に出現せず、以降のユーザー操作で発生:
20. POST 200 web/FLV901/LGV300
```

**f2dnrq（デジタルノータム）:**

```
# SPA初期化
 1. GET  200 js/lib/WebGIS/ATCMAP.settings
 2. POST 200 LuciadRIALicense
 3. GET  200 settings/auto_filter.json
 4. GET  200 settings/map_disp.json
 5. GET  200 web/resource/message
 6. GET  200 web/resource/webfw
 7. GET  200 web/resource/user
     [GET browse/FUV201 ×3]             ← Angular SPAルーティング
 8. GET  200 settings/velocity.json
    ... (default_font〜groupLayerは94_と同一順序)
19. GET  200 settings/groupLayer.json
20. POST 200 web/FUV201/USV005           ← 1回のみ（94_では2回）
21. GET  200 js/lib/WebGIS/layer/UTM1.json
22. GET  200 js/lib/WebGIS/layer/UTM0.json  ← UTM0/1の順序が94_と逆
23. GET  200 js/lib/WebGIS/layer/UTM2.json
24. GET  200 js/lib/WebGIS/layer/UTM3.json
```

##### キャプチャ2回目: 94_all_api_calls.json (11:27) — Worker実装のベース

**f2aspr（空域プロファイル）— `/f2aspr/browse/flv850s001` を開いた際のリクエスト:**

```
# SPA初期化（ページ読み込み時に自動発生）
 1. POST 200 LuciadRIALicense
 2. GET  200 js/lib/WebGIS/ATCMAP.settings
 3. GET  200 settings/auto_filter.json
 4. POST   ? web/FLV901/LGV300
 5. POST   ? web/FLV811/LGV231
 6. GET  200 settings/map_disp.json
 7. GET  200 web/resource/message
 8. GET  200 web/resource/webfw
 9. GET  200 web/resource/user
     [GET browse/flv850s001 ×3]         ← Angular SPAルーティング
10. POST   ? web/FLV802/LGV205
11. POST   ? web/FLV934/LGV387
12. GET  200 settings/velocity.json
13. GET  200 settings/default_view.json
14. GET  200 settings/default_font.json
15. GET  200 settings/default_dire_dist_position.json
16. GET  200 settings/shape_datablock_setting.json
17. GET  200 settings/default_color.json
18. GET  200 settings/map_disp.json
19. GET  200 settings/menu.json
20. GET  200 settings/commonMenuSetting.json
21. GET  200 settings/toolbarSetting.json
22. GET  200 settings/blink_info.json
23. GET  200 settings/groupLayer.json
# ここまでがSPA初期化。以降はユーザー操作に応じたAPI呼び出し:
24. POST 200 web/FLV901/LGV300
25. POST   ? web/FLV921/LGV359
    ... (FLV909/FLV913/FLV914/FLV915/FLV916/FLV918/FLV919/FLV920/FLV921/FLV807/FLV811/FLV802)
43. POST 200 web/FLV934/LGV387
```

**f2dnrq（デジタルノータム）— `/f2dnrq/browse/FUV201` を開いた際のリクエスト:**

```
# SPA初期化
 1. POST 200 LuciadRIALicense
 2. GET  200 js/lib/WebGIS/ATCMAP.settings
 3. GET  200 settings/auto_filter.json
 4. POST   ? web/FUV201/USV005
 5. GET  200 settings/map_disp.json
 6. GET  200 web/resource/message
 7. GET  200 web/resource/webfw
 8. GET  200 web/resource/user
     [GET browse/FUV201 ×3]             ← Angular SPAルーティング
 9. GET  200 settings/velocity.json
10. GET  200 settings/default_view.json
11. GET  200 settings/default_font.json
12. GET  200 settings/default_dire_dist_position.json
13. GET  200 settings/shape_datablock_setting.json
14. GET  200 settings/default_color.json
15. GET  200 settings/map_disp.json
16. GET  200 settings/menu.json
17. GET  200 settings/commonMenuSetting.json
18. GET  200 settings/toolbarSetting.json
19. GET  200 settings/blink_info.json
20. GET  200 settings/groupLayer.json
21. POST 200 web/FUV201/USV005           ← 2回目
22. GET  200 js/lib/WebGIS/layer/UTM0.json
23. GET  200 js/lib/WebGIS/layer/UTM1.json
24. GET  200 js/lib/WebGIS/layer/UTM2.json
25. GET  200 js/lib/WebGIS/layer/UTM3.json
# ここまでがSPA初期化。
26. POST   ? RegistPreValue
27. POST 403 RegistPreValue
```

**補足**:
- status `?` はPlaywrightのレスポンスキャプチャで記録されなかったもの（POSTリクエストのレスポンスが非同期で完了した等）。実際にはすべて200で応答していると考えられる。
- キャプチャ時には `/f2lfss/` (地図WMSサービス) へのリクエスト47件も同時に発生しているが、これらは地図タイル取得でありWorkerでは再現不要。
- `RegistPreValue` はNOTAM検索の前回値保存で、Workerでは不要。

### 1.5 API別Refererヘッダー

SWIMポータルはAngular SPAで、各APIは利用登録済みサービスのブラウズ画面から呼び出される。Refererはユーザーが実際にアクセスするブラウズ画面のURLを使用する。

| APIパス | Referer（ブラウズ画面URL） | サービス |
|---------|-------------------------|---------|
| `/f2dnrq/` (NOTAM/空港一覧) | `/f2dnrq/browse/FUV201` | デジタルノータムリクエスト (S2019) |
| `/f2aspr/` (PKG気象/PIREP/便一覧/便詳細/空港プロファイル/空域気象/SIGMET) | `/f2aspr/browse/flv850s001` | 空域プロファイル (S2010) |

f2aspr系APIは全て空域プロファイルサービスのブラウズ画面（`flv850s001`）が入口。実際のブラウザ操作でも全f2aspr APIはこの画面から呼び出される。

### 1.6 リクエスト遅延

| 種類 | タイミング | 遅延 | 目的 |
|------|----------|------|------|
| リクエスト前遅延 | 各API呼び出し前 | 対数正規分布（中央値4秒、P99=15秒、clip 1.5-25秒） | 人間のブラウジング間隔を再現（学術的根拠に基づく） |
| レスポンス後遅延 | APIレスポンス受信後 | 指数分布（中央値~0.38秒、稀に1-2秒） | ブラウザのDOM更新・レンダリング時間を再現。均一分布より自然なばらつき |
| エラー時遅延 | 401/403/接続エラー後 | 5-15秒ランダム + 再ログイン | 即座のリトライを避ける |

リクエスト前遅延は環境変数 `REQUEST_DELAY_MEDIAN` / `REQUEST_DELAY_P99` / `REQUEST_DELAY_CLIP_MIN` / `REQUEST_DELAY_CLIP_MAX` で調整可能。

対数正規分布の採用根拠:
- Blenn & Van Mieghem (2016) "Are human interactivity times lognormal?": 人間のインタラクション時間は対数正規分布が最良フィット
- Gianvecchio et al. (2008) USENIX Security: 均一分布でランダム化したBotも条件付きエントロピー分析で検知可能
- 対数正規分布は正の歪度（短い間隔が多く、稀に長い間隔）を持ち、人間の自然なブラウジングパターンに一致する

### 1.7 応答速度ベーススロットリング

サーバーの応答時間を監視し、高負荷時に自動でアクセス頻度を下げる。

| 条件 | 動作 |
|------|------|
| 応答 > 10秒 | 追加遅延を +2秒（最大15秒まで） |
| 応答 ≤ 10秒 | 追加遅延を -0.5秒（0秒まで） |

### 1.8 Cookie永続化

- ログイン成功後、セッションCookieをJSONファイルに保存（`/app/data/.swim_cookies.json`）
- 再起動時に保存済みCookieを復元し、有効ならログインAPIを呼ばない
- 不要な再ログインを削減し、ログイン頻度の異常を防ぐ

### 1.9 タイムアウト

全APIリクエストに60秒のタイムアウトを設定。

---

## 2. Coordinator側の対策 (swim-coordinator)

### 2.1 ジョブ開始ジッター

各収集ジョブの実行開始時にランダム遅延を挿入。毎回同一タイミングでのアクセスパターンを崩す。

| ジョブ種別 | 最大ジッター | 理由 |
|-----------|-------------|------|
| 通常ジョブ（NOTAM, PIREP, PKG Full/Half, フライト詳細等） | 60秒 | トリガー間隔が10分以上 |
| PKG高頻度収集 (HF) | 10秒 | 5分間隔のため60秒では次トリガーと衝突 |

```
設定: JOB_JITTER_SECONDS=60（デフォルト、HFは10秒固定）
```

### 2.2 空港順序シャッフル

NOTAM、PKG気象、フライト詳細など、複数空港を順にアクセスするジョブでは毎回 `random.shuffle()` で順序をランダム化。常に同じ順序でアクセスするパターンを防ぐ。

### 2.3 バッチサイズランダム化

空港のチャンク分割時、チャンク数は固定（リクエスト回数は変わらない）だが、各チャンクの空港数はランダムに配分する。

例: 145空港を5件ずつ = 29リクエスト（固定）、各リクエストの空港数は3-7件でランダムに変動。

### 2.4 全リクエスト逐次化

以前は並列dispatchを行っていたが、同一セッションから短時間に複数の同時リクエストが飛ぶパターンはbot的であるため、全て逐次実行に変更。

### 2.5 Coordinator直接アクセスのChrome偽装

メンテナンス情報API（`top.swim.mlit.go.jp/swim/api/informations`）へのアクセスもcurl_cffi (chrome136) を使用し、XHRヘッダーとRefererを付与。

```python
async with AsyncSession(impersonate=BrowserType.chrome136) as client:
    resp = await client.get(url, headers={
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://top.swim.mlit.go.jp/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    })
```

### 2.6 最小Worker数チェック

Worker数が閾値に満たない場合、ジョブをスキップしてリクエストを発生させない。集中的なリクエスト発生を防ぐ。

| ジョブ群 | 最小Worker数 | 未達時の動作 |
|---------|-------------|------------|
| PKG気象 Full/Half | 3 | 次回スケジュールまでスキップ |
| PKG気象 HF | 5 | 次回スケジュールまでスキップ |
| フライト詳細 Phase1 | 4 | 30分後に再判定 |
| フライト詳細 Phase3/4 | 7 | 30分後に再判定 |
| NOTAM/PIREP/空港/復旧チェック | 3 | 制限なし |

### 2.7 深夜帯の間引き

JST 1:00-6:00（UTC 16:00-21:00）はアクセス頻度を下げる。航空の閑散時間帯にブラウザユーザーが頻繁にアクセスすることは不自然なため。

| ジョブ | 通常 | 深夜帯 | 方式 |
|--------|------|--------|------|
| NOTAM | 60分 | 120分 | 奇数時（UTC）をスキップ |
| PIREP | 10分 | 30分 | minute//10 が 0,3 のみ実行（:03,:33のみ） |
| PKG気象 | 変更なし | 変更なし | — |
| フライト詳細 | 変更なし | 変更なし | — |

### 2.8 フライト詳細の制御

30分ごとにWorker数と前回実行からの経過時間で判定する。Phase1（基本データ）とPhase3/4（詳細補完）は独立したmin_workers設定。

| 条件 | 実行内容 |
|------|---------|
| Worker >= 7 かつ flight_full から18h経過 | フルパイプライン (Phase1 + Phase3 + Phase4) |
| Worker >= 4 かつ flight_phase1 から12h経過 | Phase1のみ（基本データ保存） |

Phase1とPhase3/4は連続実行されるため、Phase1で取得したfoidがSWIM上で失効する前にPhase3で補完される。

### 2.9 結果ポーリングの指数バックオフ

CoordinatorがWorkerの結果をRedisからポーリングする際、指数バックオフを使用。初回0.3秒→0.6秒→1.2秒→2.4秒→上限3.0秒。以前は `uniform(1.5, 3.0)` の固定ランダム間隔だった。Workerの処理完了が早ければすぐ結果を取得でき、バッチ処理の遅延累積を削減する。SWIM側からは見えない内部通信のため、bot検知には影響しない。

### 2.10 SPECIモード安全タイムアウト

PKG高頻度収集(HF)はSPECI検知で活性化されるが、以下の3条件いずれかで自動解除される:

| 解除条件 | 方式 |
|---------|------|
| SWIMデータで通常METAR検知 | PKGレスポンス内のMETAR/SPECI判別で、通常METARが出ていれば解除 |
| ADDS定時METAR検知 | aviationweather.govで当該空港の定時METARを検知 |
| 90分安全タイムアウト | activate後90分経過で自動解除（ADDSとSWIM両方が検知に失敗した場合の安全弁） |

SWIMとADDSのOR条件で、どちらかが先に通常METARを検知すれば解除される。

---

## 3. IP・アカウント分散

各Workerは異なるPC/VPS上で動作し、それぞれ異なるSWIMアカウントを使用する。SWIMポータルには異なるIPアドレス・異なるアカウントからアクセスするため、1アカウントあたりのリクエスト数が分散される。Worker数が増えるほど自然にIP・アカウント分散が実現される。

---

## 4. 1日あたりSWIMリクエスト推定 (実測 2026-04-06)

| パターン | req/日 |
|---------|--------|
| Phase1のみ（Worker 4-6） | ≈1,600 |
| フル実行（Worker 7+） | ≈5,200 |
| フル + SPECI活発時 | ≈5,200-6,400 |

### ジョブ別内訳

| ジョブ | 1回のreq | 頻度 | 24h推定 | 割合 |
|--------|---------|------|---------|------|
| PKG Full | ~29 | 毎時 | ~696 | 14% |
| PKG Half | ~5 | 毎時 | ~120 | 2% |
| PKG HF | 0~数件 | 10回/h | ~120 | 2% |
| NOTAM | ~4 | 60分 | ~96 | 2% |
| PIREP | 1 | 6回/h | ~112 | 2% |
| フライト Phase1 | 137 | 12h (4+Worker) | ~274 | 5% |
| フライト Phase3 | ~2,000 | 18h (7+Worker) | ~2,667 | 53% |
| フライト Phase4 | ~800 | 18h (7+Worker) | ~1,067 | 21% |
| 空港一覧 | 1 | 7日 | ≈0 | 0% |
| **合計** | | | **~5,153** | 100% |

フライト詳細 (Phase1+3+4) がSWIM全リクエストの **約78%** を占める。

### Coordinator直接アクセス（SWIM外含む）

| ジョブ | req/日 | 接続先 |
|--------|--------|--------|
| メンテ情報チェック | 8 | SWIM情報API (Worker経由+フォールバック) |
| ADDS METAR監視 | 720 | aviationweather.gov（SWIM外） |

---

## 5. 注意事項

### やってはいけないこと

- **同一Workerで異なるUser-Agent / Sec-Ch-Ua-Platform を使う**: TLSフィンガープリントとの矛盾で検知される
- **curl_cffiのデフォルトヘッダーを手動上書きする**: UA/Sec-Ch-Ua系はTLS一致のためライブラリに委任すること
- **Refererに存在しないページURLを使う**: SWIMポータルの実在する画面URL（/browse/xxxパス）を使うこと
- **並列リクエストを復活させる**: 同時複数リクエストはbot的パターン
- **深夜帯の間引きを外す**: 閑散時間帯の高頻度アクセスは不自然

### 対策の効果確認

日次統計通知（JST 23:55、Discord Webhook）で1日のリクエスト数を監視する。想定値を大幅に超えている場合は設定を見直す。
