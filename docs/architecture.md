# アーキテクチャと技術的アプローチ

## システム概要

swim-workerは、SWIM（航空情報共有基盤）ポータルから航空データを分散収集するシステムの一部です。

```
[中央サーバー (Coordinator)]
    │
    │ タスク配布 / 結果回収
    │
    ├──→ [Redis (タスクキュー)]
    │         │
    │         ├──→ [Worker A]  ──→  SWIM API
    │         ├──→ [Worker B]  ──→  SWIM API
    │         └──→ [Worker C]  ──→  SWIM API
    │
    └──→ [API Server]  ──→  利用者
```

### 役割分担

| コンポーネント | 役割 |
|-------------|------|
| **Coordinator** | ジョブスケジューリング、タスク配布、結果パース、DB保存 |
| **Worker (このリポジトリ)** | SWIMへのログイン、API実行、結果返却 |
| **API Server** | REST APIでデータ提供 |
| **Redis** | Coordinator↔Worker間のタスクキュー・ハートビート |

### 収集データ

NOTAM、気象情報 (METAR/TAF/ATIS)、PIREP、フライト詳細、空港情報をSWIM APIから取得しています。

---

## Workerの動作フロー

1. 起動時にRedisへ接続し、`workers:pending` に自身を登録
2. 管理者が承認すると `workers:approved` に移動
3. 30秒ごとにハートビートを送信（Coordinatorが生存監視）
4. 自分のタスクキュー (`tasks:{worker_name}`) を `BLPOP` で監視
5. タスク受信 → SWIMにログイン → API実行 → 結果をRedisに返却
6. PCの電源を切ったりWorkerを止めても、他のWorkerがカバー

**SWIM認証情報はWorker内のみに保持され、中央サーバーには送信されません。**

---

## ブラウザ偽装の技術的アプローチ

SWIMポータルはbot検知を実施しているため、Workerは実際のブラウザ（Chrome）と同じ振る舞いをするよう設計しています。

### TLSフィンガープリント

通常のPython HTTPクライアント（requests, httpx, aiohttp）はTLSハンドシェイクのCipher Suite順序やTLS拡張がブラウザと異なり、JA3/JA4フィンガープリントでbotと識別されます。

本Workerでは [`curl_cffi`](https://github.com/lexiforest/curl_cffi) を使用し、Chrome実バイナリのTLSスタックをリンクすることでTLSレベルでの検知を回避しています。`User-Agent` や `Sec-Ch-Ua` 系ヘッダーはcurl_cffiのデフォルトに任せ、TLSフィンガープリントとの整合性を保っています。

### HTTPヘッダーの再現

SWIMポータルはjQueryとAngularが混在したSPAで、リクエスト種別によってヘッダーパターンが異なります。Playwrightで実際のChrome操作をキャプチャし、以下のパターンを特定・再現しています。

| パターン | 対象 | 特徴 |
|---------|------|------|
| Document | ページ遷移 | `Sec-Fetch-Dest: document`, `Upgrade-Insecure-Requests: 1` |
| jQuery XHR | 設定ファイル取得 | `X-Requested-With: XMLHttpRequest` |
| Angular resource | リソースバンドル | `Accept: */*` |
| Angular API | データAPI POST | `Origin` 付き |

### ログインフローの再現

実ブラウザのログイン操作を忠実に再現しています。

1. トップページのGET（ナビゲーションヘッダー付き）
2. ランダム待機（SPA読み込み時間）
3. ログインAPI POST
4. ランダム待機（リダイレクト遅延）
5. サービスページへの遷移GET（`Sec-Fetch-Site: same-site`）
6. SPA初期化リクエスト群

ステップ5が重要で、これがないと「サービスページへのナビゲーションなしにAPIリクエストが来た」と検知される可能性があります。

### SPA初期化の再現

実ブラウザでサービスページを開くと、API呼び出しの前にSPA初期化リクエスト（ライセンスPOST、設定ファイル群、リソースバンドル等）が自動発生します。Workerでもこれをセッション中にサービスごとに1回再現し、初期化後にデータAPI呼び出しを行います。

### リクエスト遅延

人間のブラウジングパターンを再現するため、学術論文に基づいた対数正規分布のランダム遅延を使用しています。

| 種類 | 分布 | 説明 |
|------|------|------|
| リクエスト前 | 対数正規分布 (中央値4秒) | 人間のインタラクション間隔 |
| レスポンス後 | 指数分布 (~0.28秒) | ブラウザのDOM更新時間 |
| エラー後 | 5-15秒 + 再ログイン | 即座のリトライ回避 |

対数正規分布の採用は Blenn & Van Mieghem (2016) "Are human interactivity times lognormal?" に基づいており、均一分布のランダム遅延よりも自然なパターンを再現します。

### Cookie永続化

ログイン成功後のセッションCookieをファイルに保存し、再起動時に復元します。有効なCookieがあればログインAPIを呼ばず、不要な再ログインによる異常パターンを防ぎます。

---

## Coordinator側の制御

Workerの偽装に加え、Coordinatorでもアクセスパターンの自然さを制御しています。

| 対策 | 説明 |
|------|------|
| **ジョブ開始ジッター** | 各ジョブの開始時にランダム遅延（最大30秒）。毎回同一タイミングでのアクセスを防止 |
| **空港順序シャッフル** | 複数空港へのアクセス順序を毎回ランダム化 |
| **深夜帯の間引き** | JST 1:00-6:00は航空閑散時間帯のため、NOTAM/PIREPの頻度を半減 |
| **IP・アカウント分散** | 各Workerが異なるIP・異なるSWIMアカウントで接続。リクエスト負荷を自然に分散 |
| **応答速度スロットリング** | サーバー応答が遅い場合、自動でアクセス頻度を下げる |
