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

実ブラウザでサービスページを開くと、API呼び出しの前にSPA初期化リクエストが自動で発生する。Workerでもこの遷移をセッション中に各サービス1回再現する。

```
サービスページを開く際の実ブラウザの挙動:
1. SPA初期化（ライセンス、設定ファイル群、リソースバンドル）
2. ブラウズ画面のGET（Angular SPAルーティング）
3. 以降のAPI呼び出し（XHRヘッダー + Referer付き）
```

| サービス | SPA初期化 | ブラウズ画面 |
|---------|----------|-----------|
| f2dnrq (NOTAM) | 8リクエスト（LuciadRIALicense, 設定JSON, リソースバンドル等） | `/f2dnrq/browse/FUV201` |
| f2aspr (PKG気象/PIREP/便/空港等) | 9リクエスト（同上 + FLV901/FLV811初期データ） | `/f2aspr/browse/flv850s001` |

ブラウズ画面は**利用登録済みサービスのみ**にアクセスする。未登録サービスのページにアクセスすることは不自然な行動になるため。

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
| リクエスト前遅延 | 各API呼び出し前 | 2-8秒ランダム（均一分布） | 人間のブラウジング間隔を再現 |
| レスポンス後遅延 | APIレスポンス受信後 | 指数分布（中央値~0.38秒、稀に1-2秒） | ブラウザのDOM更新・レンダリング時間を再現。均一分布より自然なばらつき |
| エラー時遅延 | 401/403/接続エラー後 | 5-15秒ランダム + 再ログイン | 即座のリトライを避ける |

リクエスト前遅延は環境変数 `REQUEST_DELAY_MIN` / `REQUEST_DELAY_MAX` で調整可能。

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
| PKG気象 (Full/Half/HF) | 2 | 次回スケジュールまでスキップ |
| フライト詳細 | 3 | 1時間後にリトライ |
| NOTAM/PIREP/空港/復旧チェック | 1 | 制限なし |

### 2.7 深夜帯の間引き

JST 1:00-6:00（UTC 16:00-21:00）はアクセス頻度を下げる。航空の閑散時間帯にブラウザユーザーが頻繁にアクセスすることは不自然なため。

| ジョブ | 通常 | 深夜帯 | 方式 |
|--------|------|--------|------|
| NOTAM | 60分 | 120分 | 奇数時（UTC）をスキップ |
| PIREP | 10分 | 30分 | minute//10 が 0,3 のみ実行（:03,:33のみ） |
| PKG気象 | 変更なし | 変更なし | — |
| フライト詳細 | 変更なし | 変更なし | — |

### 2.8 フライト詳細の間隔

18時間間隔の `IntervalTrigger` を使用。CronTriggerと異なり起動時刻に依存するため、毎日異なる時間にアクセスが発生する。

---

## 3. IP・アカウント分散

各Workerは異なるPC/VPS上で動作し、それぞれ異なるSWIMアカウントを使用する。SWIMポータルには異なるIPアドレス・異なるアカウントからアクセスするため、1アカウントあたりのリクエスト数が分散される。Worker数が増えるほど自然にIP・アカウント分散が実現される。

---

## 4. 1日あたりSWIMリクエスト推定

| パターン | req/日 |
|---------|--------|
| 通常時（SPECI/ATISリトライなし） | 約1,900-2,900 |
| SPECI/リトライ活発時 | 約1,900-4,100 |

### ジョブ別内訳

| ジョブ | 通常 req/日 | 備考 |
|--------|-----------|------|
| NOTAM | ~86 | FIR + 空港バッチ、深夜120分 |
| PIREP | ~124 | 深夜30分 |
| PKG Full (:05) | ~672 | 全空港29バッチ × 24回 |
| PKG Half (:35) | ~120 | 21空港5バッチ × 24回 |
| PKG HF (5分おき) | 0-1,200 | ATIS リトライ/SPECI空港（動的） |
| フライト詳細 | ~400-1,000 | 18時間間隔、137空港 + 新規便詳細 |
| 空港一覧 | ≈0 | 7日間隔 |
| 復旧チェック | 0 | メンテナンス中のみ |

### Coordinator直接アクセス（SWIM外含む）

| ジョブ | req/日 | 接続先 |
|--------|--------|--------|
| メンテ情報チェック | 8 | SWIM情報API |
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
