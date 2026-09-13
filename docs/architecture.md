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
5. タスク受信 → SWIMにログイン → API実行 → **(条件次第で) パース実行 → zstd 圧縮** → 結果をRedisに返却
6. PCの電源を切ったりWorkerを止めても、他のWorkerがカバー

**SWIM認証情報はWorker内のみに保持され、中央サーバーには送信されません。**

---

## 帯域削減機構 (v1.0.1+)

Worker → Coordinator の Redis 通信量を削減する機構。月あたりの outbound 帯域に上限がある環境 (無料枠の VPS 等) の Worker でも余裕を持って稼働させるため。

### 圧縮: zstandard level 6

従来の `gzip.compress()` (level 9 デフォルト) を `zstandard.ZstdCompressor(level=6)` に置換。実測で gzip L9 比 約 5% の追加削減。L3 では gzip L9 に負けるため L6 を採用。

### Worker 側パース (オプション、動的制御)

特定の job_type については Worker 側で Coordinator のパーサーを実行し、SWIM 生レスポンスではなく **パース済みリスト** を送る。未使用フィールドやメタデータが削減される。

対応 job_type は Coordinator が管理する許可リストに登録されたもののみ (60秒キャッシュ)。空なら全 raw 送信。Coordinator 側の管理コマンドで動的に切り替えられるため、**Worker 側のコード変更・タグ打ちは不要**。

結果の JSON には `format: "parsed"` フラグが付く。旧 Coordinator は `format` 未設定を期待する既存フローにフォールバック (後方互換)。

### パーサーの同期

`swim_worker/parsers/` 以下は `swim-coordinator/coordinator/parsers/` の完全コピー。`scripts/sync_parsers.sh` でコピー、`scripts/check_parsers_synced.sh` で差分検知。parse() 関数は DB 非依存、store() は関数内で sqlalchemy import されるため Worker 環境で呼ばない限り問題なし。

### datetime の扱い (v1.0.2+)

parse() 関数の返り値に含まれる日時フィールドは **ISO 8601 文字列 (UTC)** として返す。Python の `datetime` オブジェクトをそのまま返すと `json.dumps` でシリアライズエラー ("Object of type datetime is not JSON serializable") が発生するため。Coordinator 側の store() 関数が冒頭で `_coerce_dt()` により str → datetime に復元する。

v1.0.1 は parse() が datetime を返していたため、pkg_weather 等 parsed 送信時に Worker が結果送信失敗 → Coordinator タイムアウト → 再配布多発の不具合あり。v1.0.2 で修正済み。

### 実測削減率と確定運用 (2026-04-24確定、全 job_type 判定完了)

| job_type | 削減率 | 方針 |
|---|---|---|
| pkg_weather | -89〜92% | ★ parse (whitelist、稼働中) |
| flight_details | -65% | ★ parse (whitelist、稼働中) |
| flight_foids | -67% | ★ parse (whitelist、v1.0.5でblocklist解除、稼働中) |
| notam | -1.79% | ✗ raw維持 (parseでほぼ効果なし) |
| pirep | -21.5% | ✗ raw維持 (parseで逆効果) |

notam/pirep が parse で悪化する理由: 各 parser が item ごとに `raw_data` フィールド (元 JSON のコピー) を保持する設計のため、parsed 出力に元データが分散して圧縮前の冗長度が上がり zstd の効きが悪くなる。

判定用の `result_size_logs`/サンプル保存機構は判定完了により 2026-04-24 に撤去済み。将来新ジョブの parse 判定が必要になった場合は再実装する。現在の whitelist 状態は Coordinator 側で確認できる。

### Coordinator 側の互換性

Coordinator は下記すべてを同時に受理可能 (Worker バージョン混在 OK):
- 圧縮: zstd / gzip / 生 JSON (マジックバイト判定)
- 形式: `format=parsed` / `format 未設定` (旧 raw)

---

## HTTP クライアントの実装方針

SWIMポータルはブラウザ（Chrome）での利用を前提に作られた、jQuery と Angular が混在する SPA です。Worker は HTTP クライアントとして、ポータルが想定する通信手順に沿って動作するよう実装しています。

### TLS スタック

一般的な Python HTTP クライアント（requests, httpx, aiohttp）は、TLS ハンドシェイクの Cipher Suite 順序や TLS 拡張がブラウザと異なります。

本 Worker では [`curl_cffi`](https://github.com/lexiforest/curl_cffi) を使用し、Chrome と同一の TLS スタック・HTTP/2 設定で接続します。`User-Agent` や `Sec-Ch-Ua` 系ヘッダーは curl_cffi のデフォルトに任せ、クライアント全体として一貫した挙動になるようにしています。

### HTTPヘッダー

SWIMポータルはリクエスト種別によってヘッダーパターンが異なります。Playwright で実際のブラウザ操作をキャプチャし、以下のパターンを特定して実装に反映しています。

| パターン | 対象 | 特徴 |
|---------|------|------|
| Document | ページ遷移 | `Sec-Fetch-Dest: document`, `Upgrade-Insecure-Requests: 1` |
| jQuery XHR | 設定ファイル取得 | `X-Requested-With: XMLHttpRequest` |
| Angular resource | リソースバンドル | `Accept: */*` |
| Angular API | データAPI POST | `Origin` 付き |

### ログインフロー

ブラウザでのログイン操作と同じ順序でリクエストを送ります。

1. トップページのGET（ナビゲーションヘッダー付き）
2. ランダム待機（SPA読み込み時間）
3. ログインAPI POST
4. ランダム待機（リダイレクト遅延）
5. サービスページへの遷移GET（`Sec-Fetch-Site: same-site`）
6. SPA初期化リクエスト群

ステップ5はサービスページのセッションを確立するために必要で、これを省略すると後続のAPI呼び出しが失敗することがあります。

### SPA初期化

ブラウザでサービスページを開くと、API呼び出しの前に SPA 初期化リクエスト（ライセンス POST、設定ファイル群、リソースバンドル等）が発生します。Worker でもセッション中にサービスごとに1回これを実行し、初期化後にデータAPI呼び出しを行います。

### リクエスト間隔

ポータルへの負荷を抑えるため、リクエストの間に待機を入れています。一定間隔での連続アクセスにならないよう、実際の利用間隔の分布として知られる対数正規分布を用いています。

| 種類 | 分布 | 説明 |
|------|------|------|
| リクエスト前 | 対数正規分布 (中央値4秒) | 操作間隔に相当する待機 |
| レスポンス後 | 指数分布 (~0.28秒) | 後続処理までの待機 |
| エラー後 | 5-15秒 + 再ログイン | 即座のリトライを避ける |

対数正規分布の採用は Blenn & Van Mieghem (2016) "Are human interactivity times lognormal?" に基づいています。

### Cookie永続化

ログイン成功後のセッションCookieをファイルに保存し、再起動時に復元します。有効なCookieがあればログインAPIを呼ばないため、ポータルへの認証リクエストを削減できます。

---

## Coordinator側のアクセス制御

Worker 単体の挙動に加えて、Coordinator 側でもポータルへのアクセス総量とタイミングを制御しています。

| 制御 | 説明 |
|------|------|
| **ジョブ開始ジッター** | 各ジョブの開始時にランダム遅延（最大30秒）。同一時刻へのアクセス集中を防止 |
| **空港順序シャッフル** | 複数空港へのアクセス順序を毎回ランダム化し、特定空港への偏りを避ける |
| **深夜帯の間引き** | JST 1:00-6:00は航空閑散時間帯のため、NOTAM/PIREPの頻度を半減 |
| **アカウント・回線の分散** | 各 Worker が自身の SWIM アカウント・自身の回線で接続するため、単一アカウント／単一 IP への負荷集中が起きない |
| **応答速度スロットリング** | サーバー応答が遅い場合、自動でアクセス頻度を下げる |

---

## 配布と自動更新 (v1.0.0+)

### プラットフォーム別の配布形態

| プラットフォーム | 配布形態 | アーキ | 自動更新 |
|----------------|---------|-------|---------|
| Windows | `swim-worker-windows.exe` (PyInstaller GUI) | amd64 | GUI からポップアップ経由で更新 (DL 後に同 release の `SHA256SUMS` で検証) |
| macOS | `swim-worker-macos` (PyInstaller GUI) | arm64 (Apple Silicon) のみ | Windows と同様 |
| Linux / Raspberry Pi | `swim-worker-linux-{amd64,arm64}` + `install.sh` + systemd unit | amd64 / arm64 | systemd timer による自動更新 |

Linux CLI バイナリは glibc 2.35+ 互換 (`ubuntu-22.04` runner でビルド) で、Pi OS Bookworm / Debian 12 / Ubuntu 22.04+ / Fedora / RHEL 系で動作します。

### install.sh の処理フロー

```
curl | bash install.sh
  ↓
1. uname -m でアーキテクチャ自動判定 (amd64 / arm64)
2. GitHub Releases から以下を DL:
   - swim-worker-linux-{ARCH}
   - SHA256SUMS
   - swim-worker.service
   - swim-worker-update.service / .timer
3. SHA256SUMS で整合性検証
4. 専用システムユーザー swim-worker を作成 (uid 999, nologin)
5. /opt/swim-worker/ に配置 (chmod 755 swim-worker:swim-worker)
6. .env を対話生成 (値は単引用符で囲み、chmod 600)
7. systemd unit 配置 + 自動更新 timer を enable --now
```

**RELEASE_TAG 環境変数**で特定バージョンを指定可能 (検証/手動ロールバック用)。通常は /releases/latest (最新 stable) を使う。

### systemd hardening

`swim-worker.service` は以下の hardening を適用:
- `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp`, `PrivateDevices`
- `CapabilityBoundingSet=` (全 capability 剥奪)
- `RestrictAddressFamilies=AF_INET AF_INET6`
- `SystemCallFilter=@system-service`
- `MemoryMax=256M`
- `After=time-sync.target` (Pi の RTC なし環境で TLS 証明書検証失敗を回避)

### 自動更新機構

`swim-worker-update.timer` が 6時間 + 最大2時間ランダムずらしで起動し、`install.sh --auto` を実行。以下のガードを順に評価:

1. **バージョン比較**: 現行 == 最新なら service 無触で早期 exit
2. **ダウングレード防止**: 現行 > 最新なら skip (prerelease 検証中の保護)
3. **ローカル opt-out**: `/opt/swim-worker/.no-auto-update` があれば skip
4. **Coordinator kill switch**: Coordinator 側で自動更新が有効化されていなければ skip。この確認は Redis へ TLS 接続して行い、install.sh に埋め込んだ CA 証明書 (Worker 本体の `certs.py` と同一) でサーバー証明書を検証する (v1.1.0 以前は検証なしで AUTH を送っていた)
5. **Staged rollout whitelist**: Coordinator 側に更新対象の whitelist が設定されている場合、含まれる worker_name のみ更新
6. **Major version skip**: メジャーバージョン変更 (例: 0.x → 1.x) は自動更新しない (手動必須)

更新時は旧バイナリを `swim-worker.old` として保持、60秒後に `is-active` + `NRestarts < 2` で検証、失敗すれば自動ロールバック。

**kill switch / staged rollout の制御は管理者が Coordinator 側で行う。** 手順は Coordinator 側の運用ドキュメント（非公開）を参照。

### 特定Workerの更新チェックを今すぐ走らせたい場合

`swim-worker-update.timer` の発火（6時間 + 最大2時間ランダム、起動直後は `OnBootSec=15min` + 同ランダム）を待たずに、タイマーが呼ぶ oneshot サービスを直接startすれば同じ処理が即座に走る（kill switch / whitelist 等のガードはそのまま評価される）。

```bash
ssh <worker-host>
sudo systemctl start swim-worker-update.service
# 結果確認
sudo journalctl -u swim-worker-update.service --no-pager -n 15
```

次回の予定発火時刻は `systemctl list-timers swim-worker-update.timer` で確認できる。OS再起動直後は `OnBootSec=15min` が優先されるため、6時間サイクルの途中で再起動しても最大8時間待つわけではなく、起動から15分〜2時間15分後には次のチェックが走る。

### OS側の自動再起動 (unattended-upgrades)

常時稼働させる Linux Worker では `unattended-upgrades` を有効化し、`Unattended-Upgrade::Automatic-Reboot "true"` + `Automatic-Reboot-Time` を明示設定することを推奨する。カーネル等の再起動必須パッチが入った場合、手動確認なしで指定時刻に自動再起動される。設定しない場合は `/var/run/reboot-required` が放置され、更新が適用されないまま稼働し続けることになる。

`swim-worker.service` は `Restart=always` + `WantedBy=multi-user.target` なので、OS再起動時も自動的に起動し直す。Redisへの再接続もハートビート/コンシューマー双方のループが持つリトライロジックで自動的に回復する。

### 既知の障害: consume_loop 停止 (v1.0.7以前, 2026-07-20)

`execute_task()` 内の処理（SWIMへのHTTPリクエストが有力候補）がハングすると、`_consume_loop` 全体が `blpop` に戻れず永久停止する不具合があった。`_heartbeat_loop` は別の asyncio タスクなので生き続け、ダッシュボード上は `alive: true` のまま、タスクだけが `tasks:{worker_name}` キューに際限なく溜まり続けた（実際に230件超の滞留が発生）。

v1.0.8で `_consume_loop` が `execute_task()` を `asyncio.wait_for(..., timeout=task_hard_timeout)` (デフォルト300秒、`TASK_HARD_TIMEOUT` 環境変数で調整可) で包むよう修正し、ハングしても強制的に打ち切って次のタスクへ戻れるようにした。合わせて Coordinator 側のタスクキュー全体へのTTL設定 (Worker処理が少し遅れるだけで未処理タスクを巻き込んで消えるバグ) も撤去済み。

### 既知の障害: ダッシュボードの IP/状態テーブルから Worker が消える (v1.1.0以前, 2026-09-11)

Worker は Redis 接続に `CLIENT SETNAME {worker_name}` で名前を付け、Coordinator は `CLIENT LIST` の `name=` から Worker の接続元 IP を取得してダッシュボードに表示する。v1.1.0 以前は `run()` 開始時に `client_setname()` を 1 回呼ぶだけだったため、コネクションプール内の 1 本にしか名前が付かず、その接続がタイムアウト等で張り直された時点で名前が消えていた。ハートビート自体は別の接続で届き続けるので `alive` 判定は正常なのに、IP/状態テーブルには行が出ない（またはオフライン表示になる）状態になる。Redis から遠い (レイテンシの大きい) 環境ほど発生しやすい。

加えて `redis_blpop_timeout` (30秒) が `redis_socket_timeout` (30秒) と同値だったため、サーバーの `BLPOP` nil 応答 (30秒 + RTT) より先にクライアント側の socket_timeout が発火し、毎サイクル `TimeoutError` → 再接続になっていた。redis-py 6 以降はデフォルトで 3 回まで無言でリトライするため警告ログには稀にしか出ないが、`CLIENT LIST` 上では `blpop` 接続の age が常に 30 秒未満で、接続の張り直しが継続的に起きていた。

修正 (v1.1.1, 2026-09-12 リリース):
- `aioredis.Redis(..., client_name=worker_name)` を指定し、redis-py が接続確立 (再接続含む) のたびに `CLIENT SETNAME` を送るようにした。実行時の `client_setname()` 呼び出しは削除。CLI / GUI ともに `swim_worker/redis_client.py` の共通ファクトリを使う (GUI が独自に生成していて設定漏れが起きていた)
- `redis_blpop_timeout` のデフォルトを 20 秒に短縮 (`socket_timeout` より短くすること)。これにより定期的に出ていた `Redis接続エラー（コンシューマー）… Timeout reading from …` 警告 (≈6 分に 1 件) も解消される
- `redis[hiredis]` を 8.x 系に固定 (CI build ごとに最新版の挙動変化を取り込まないため)

同時に修正した関連事項:
- GUI 自動更新: DL した exe を同 release の `SHA256SUMS` で検証してから差し替える (`swim_worker/update_verify.py`)
- GUI の停止操作を asyncio ループのスレッドで実行 (`call_soon_threadsafe`)。UI スレッドから直接 `Task.cancel()` していたため停止が BLPOP 待ち分遅れることがあった
- タスク強制タイムアウト時にも GUI へ idle を通知 (「処理中」表示のまま固まる問題)
- `install.sh --auto` の kill switch 確認で Redis サーバー証明書を検証するようにした (上記)
- SWIM ログイン失敗時に指数バックオフ (60 秒 → 最大 30 分) を入れ、認証情報が誤っている間はタスクごとにログイン API を叩かないようにした (アカウントロック予防)。保存済み Cookie での復元は抑制の対象外

### 既知の障害: パーサー診断が Worker 側でファイルを書いていた (v1.1.1以前, 2026-09-12)

Coordinator と共通の `parsers/diagnostics.py` が、未知の応答キーを検出すると `/app/data/{job_type}_unknown_samples/` に生レスポンスの断片を保存していた。このパスは Coordinator コンテナ用で、Worker では Windows GUI がシステムドライブ直下に `\app\data\…` を作成して書き続け、systemd (`ProtectSystem=strict`) の Linux ではディレクトリ作成に失敗して毎回スタックトレース付きの ERROR ログが出ていた。

修正 (v1.1.2, 2026-09-12 リリース): 保存先を環境変数 `SWIM_PARSER_DIAG_DIR` による明示オプトインにし、未設定 (= Worker) では保存せず DEBUG ログのみとした。Worker の利用者に見せるログは、接続状態・タスクの開始/成功/失敗・バージョン通知など利用者が対処できる事象に限る方針。既に作成された `\app\data\*_unknown_samples\` は自動削除しないので、手動で削除する。

### v1.1.3 での修正 (2026-09-12 レビュー)

- 更新後の起動確認: `.startup_ok` を GUI が表示された時点 (2 秒生存) で書く。以前は Worker が Redis に接続した時にしか書かれず、「起動時に自動接続」OFF の利用者は更新のたびに 120 秒後にロールバックされていた
- ロールバック後は同一バージョンを snooze し、2 回連続なら自動更新を OFF にして通知する
- Windows ヘルパー: 新 exe への置き換えに失敗した場合も旧 exe を再起動する (`reason=move_failed`)
- macOS ヘルパー: 停止は PID 指定 (`pkill -f` は自分自身を止めていた)、PyInstaller 環境変数のリセット、トレイはメインスレッドで実行。Apple Silicon のみ対応
- Worker 名は `[A-Za-z0-9._-]{1,32}` に制限 (`CLIENT SETNAME` 失敗の予防)
- GUI の停止は Redis 再試行中でも中断でき、停止完了までは再起動できない。停止時に SWIM/Redis クライアントを解放する
- capability テストの 403 では再ログイン・Cookie 破棄をしない
- capability テストの結果には `reason` (`unauthorized` = 401/403、`transient` = それ以外の失敗) が付き、Coordinator は `transient` を判定不能として前回結果を維持する
- GUI ログは 5MB × 3 世代でローテーション
- ロックファイルは常に `data/swim-worker.lock`。埋め込み CA は一時ファイルを作らず渡す
- Docker 経路 (上級者向け) がビルド・起動できるよう修正
- `install.sh --auto` はロールバックした版を `.failed-version` に記録し再試行しない
