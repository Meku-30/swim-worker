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

ステップ5はサービスページのセッションを確立するために必要で、これを省略すると後続のAPI呼び出しが失敗することがあります。遷移先はログイン応答の `redirectUrl` に従いますが、`https://*.swim.mlit.go.jp` 以外を指していれば従わず既定のポータルへ遷移します。

### アクセス先の制限

Worker がアクセスするのは `https://<サブドメイン>.swim.mlit.go.jp` (ポート 443、ユーザー情報なし) だけです。Coordinator から届くタスクの URL がこの範囲になければ、SWIM へ何も送らずにタスクを失敗にします。リダイレクト後の最終 URL も同じ範囲か確認します。範囲の外へリダイレクトされた (メンテナンス中のお知らせページなど) ときは、結果の `error` に「SWIM の外へリダイレクトされました (メンテナンス中の可能性): <ホストとパス>」と書き、結果のトップレベルに `"error_kind": "redirect_outside"` を付けます (Coordinator が通常のエラーと区別するための印。ほかのエラーには付けません)。Cookie は `Secure` 付きで扱います。

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

ログイン成功後のセッションCookieをファイルに保存し、再起動時に復元します。有効なCookieがあればログインAPIを呼ばないため、ポータルへの認証リクエストを削減できます。稼働中も 10 分おきに保存し直すので、異常終了しても次回の起動で復元できます。復元を試すのは起動後の最初のログインだけで、失効した Cookie を何度も試しません。ログイン失敗の抑制中は Cookie の復元も含めてポータルにアクセスしません。

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
| Windows | `swim-worker-windows.exe` (PyInstaller GUI) | amd64 | GUI からポップアップ経由で更新 (同 release の署名済み `SHA256SUMS` で検証) |
| macOS | `swim-worker-macos` (PyInstaller GUI) | arm64 (Apple Silicon) のみ | Windows と同様 |
| Linux / Raspberry Pi | `swim-worker-linux-{amd64,arm64}` + `install.sh` + systemd unit | amd64 / arm64 | systemd timer による自動更新 |

Linux CLI バイナリは glibc 2.35+ 互換 (`ubuntu-22.04` runner でビルド) で、Pi OS Bookworm / Debian 12 / Ubuntu 22.04+ / Fedora / RHEL 系で動作します。

### install.sh の処理フロー

```
curl | bash install.sh
  ↓
1. uname -m でアーキテクチャ自動判定 (amd64 / arm64)
2. GitHub Releases (タグに固定) から SHA256SUMS と SHA256SUMS.sig を DL し、
   埋め込みの公開鍵で署名を、先頭行で版を検証 (通らなければここで中止。下の「更新物の署名」)
3. 以下を DL し、SHA256SUMS でハッシュを検証:
   - swim-worker-linux-{ARCH}
   - swim-worker.service
   - swim-worker-update.service / .timer
   - swim-worker-update.sh (固定の更新スクリプト)
4. 専用システムユーザー swim-worker を作成 (uid 999, nologin)
5. /opt/swim-worker/ に配置 (ディレクトリ・バイナリ・.version は root:root、data/ だけ swim-worker の 750)
6. .env を対話生成 (値は単引用符で囲み、root:swim-worker の 640。Worker は読むだけ)
7. systemd unit・固定の更新スクリプト (/usr/local/libexec/swim-worker/update.sh、root:root 0755)
   配置 + 通信許可の drop-in + 自動更新 timer を enable --now
```

署名の検証に OpenSSL 3.0 以上 (`openssl pkeyutl -rawin`。1.1.1 には無い) を使う。`openssl version` を先に確かめ、足りなければ版を示してインストール・更新を失敗させる。

**RELEASE_TAG 環境変数**で特定バージョンを指定可能 (検証/手動ロールバック用)。通常は GitHub API で最新 stable のタグを調べ、そのタグ (`releases/download/<タグ>`) に固定してダウンロードする。`releases/latest/download` はタグを調べてからダウンロードするまでの間に別の版が公開されると `.version` と中身がずれるため使わない。タグが取れない・形式が `vX.Y.Z` でなければ失敗する。

以前の install.sh は /opt/swim-worker 全体を swim-worker ユーザーの持ち物にしていた。`--auto` (自動更新) のたびに所有者を上の形に直すので、既存のインストールも次の自動更新で移行される。

### 更新物の署名

リリースは CI が **draft** で作り、管理者が自分の端末にだけ置いた Ed25519 の秘密鍵 (パスフレーズ付き) で `SHA256SUMS` に署名して `SHA256SUMS.sig` (生の 64 バイト) を上げてから公開する (`scripts/sign-release.sh`、手順は [release-signing.md](release-signing.md))。CI (GitHub) には秘密鍵を置かないので、CI やリポジトリへの書き込み権限だけでは Worker が受け付けるリリースを作れない。

- 公開鍵は `swim_worker/release_keys.py` (GUI)・`scripts/install.sh`・`scripts/swim-worker-update.sh` に埋め込む (元は `scripts/release_pubkeys/*.pub.pem`、`scripts/set-release-pubkeys.sh` で書く。3 か所の一致はテストで確かめる)。ふだんは 1 本、鍵を計画的に入れ替えるときだけ 2 本まで
- `SHA256SUMS` の先頭行は `# swim-worker-release vX.Y.Z` (CI が書く)。署名はこの行ごとなので版に結び付き、古い版の署名済みファイルを別のタグとして出しても通らない
- 検証側 (install.sh・固定の更新スクリプト・GUI) は (1) 埋め込みの公開鍵のどれかで署名が通る、(2) 先頭行の版が取りに行ったタグと一致、(3) 自動更新では今の版より新しい、を確かめてから本体を落とす。署名のないリリースには更新しない
- 公開鍵が未設定のままだとリリース CI が失敗する (`scripts/release_pubkeys.py check --require`)
- install.sh・更新スクリプト・GUI の古い版 (署名に対応する前) は、署名に対応した版へは従来どおり SHA256 だけで更新される。そこから先は署名が必須

### systemd hardening

`swim-worker.service` は以下の hardening を適用:
- `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp`, `PrivateDevices`
- `CapabilityBoundingSet=` (全 capability 剥奪)
- `RestrictAddressFamilies=AF_INET AF_INET6`
- `SystemCallFilter=@system-service`、`SystemCallArchitectures=native`
- `UMask=0077`
- `IPAddressDeny=` で LAN (10/8・172.16/12・192.168/16)・リンクローカル (169.254/16・fe80::/10)・CGNAT (100.64/10)・fc00::/7 への通信を閉じる。ループバックは閉じない (systemd-resolved の 127.0.0.53)。名前解決のサーバー (家庭のルーター、クラウドのメタデータ DNS など) と Redis がこの範囲にある環境は、install.sh が `/etc/resolv.conf` と `.env` の Redis ホストから `swim-worker.service.d/10-ip-allow.conf` に `IPAddressAllow=` を書く (通常インストール・自動更新のたび)。値はその時点で固定なので、systemd-resolved を使わない機で DNS サーバーの IP が変わると次の更新まで名前解決できない (`sudo bash install.sh` で書き直す。Worker は起動時に、許可されていない DNS サーバーがあればログに警告を出す)。IPAddressAllow/Deny は宛先 IP だけでポートを区別できないので、「プライベート帯域の 53 番だけ通す」のような書き方はできず、unit の再読み込みなしに起動時に作り直すこともできないため、この形にしている
- `MemoryDenyWriteExecute` は無効のまま (curl_cffi が使う cffi のクロージャが書き込み+実行可能なメモリを使うため)
- `MemoryMax=256M`
- `After=time-sync.target` (Pi の RTC なし環境で TLS 証明書検証失敗を回避)

`swim-worker-update.service` (root で動く) は、更新に要る場所 (`/opt/swim-worker`・`/etc/systemd/system`・`/usr/local/libexec/swim-worker`・ロック) 以外を書けなくする: `ProtectSystem=full` + `ReadWritePaths=`、`ProtectHome=true`、`PrivateTmp`、`PrivateDevices`、`NoNewPrivileges`、カーネル・cgroup の保護、`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK`、`SystemCallArchitectures=native`。

### 自動更新機構

`swim-worker-update.timer` が 6時間 + 最大2時間ランダムずらしで起動し、固定の更新スクリプト `/usr/local/libexec/swim-worker/update.sh` を実行。以下のガードを順に評価し、**ダウンロードの前に**止められるものは止める:

1. **ローカル opt-out**: `/opt/swim-worker/.no-auto-update` があれば skip
2. **バージョン比較**: 現行 == 最新なら service 無触で早期 exit
3. **ダウングレード防止**: 現行 > 最新なら skip (prerelease 検証中の保護)
4. **ロールバック済みの版**: 前回ロールバックした版 (`.failed-version`) なら skip
5. **Coordinator kill switch**: Coordinator 側で自動更新が有効化されていなければ skip。この確認は Redis へ TLS 接続して行い、更新スクリプトに埋め込んだ CA 証明書 (Worker 本体の `certs.py` と同一) でサーバー証明書を検証する (v1.1.0 以前は検証なしで AUTH を送っていた)
6. **Staged rollout whitelist**: Coordinator 側に更新対象の whitelist が設定されている場合、含まれる worker_name のみ更新
7. **Major version skip**: メジャーバージョン変更 (例: 0.x → 1.x) は自動更新しない (手動必須)

通ったら、そのタグの `SHA256SUMS` と `SHA256SUMS.sig` を取って署名と版を検証し、`install.sh` を取ってハッシュを確かめ、`SWIM_UPDATE_TAG=<タグ> bash install.sh --auto` を実行する。install.sh はバイナリ・更新スクリプト・unit を同じ検証で差し替え、再起動して起動を確かめる。

**旧方式からの移行**: v1.2.x までの `swim-worker-update.service` (TimeoutStartSec=600) は最新の `install.sh` を取って `--auto` で実行していた。新しい install.sh は `SWIM_UPDATE_TAG` なしの `--auto` で呼ばれると、最新リリースの更新スクリプト・update.service・timer だけを署名を確かめて一時ディレクトリに取り、その更新スクリプトを `--guard-only` で実行して一時停止 (kill switch)・段階配布 (whitelist) を確かめる (判定は更新スクリプトの実装をそのまま使い、install.sh に二重に持たない)。止められたら何も置かずに終わり、次回また試す。通れば 3 つを置いて daemon-reload し、すぐ終わる (バイナリの更新や起動待ちのような長い処理は旧 unit の時間制限の下ではしない)。本体の unit (IPAddressDeny 付き) とバイナリは、次の timer で新しい update.service (TimeoutStartSec=900) が更新スクリプト経由で通信許可の drop-in と一緒に入れる。

- 本体の unit は、どの経路でも通信許可の drop-in を書けてから置く (`install_worker_unit`)。IPAddressDeny 付きの unit が drop-in なしで入ると、DNS がルーターにある機が名前解決できなくなるため
- opt-out (`.no-auto-update`) の機も移行する。旧 update.service は署名を確かめない最新の install.sh を root で実行し続けるため。移行も一時停止・段階配布には従う (管理者が止めている間は配布物を何も替えない、を優先)。バイナリを更新しないのは新しい更新スクリプトの opt-out の確認が守る
- Redis の認証に失敗している機は、本来の更新と同じく移行も続ける

更新時は旧バイナリを `swim-worker.old`、unit・drop-in・更新スクリプト・update.service・timer を `<名前>.old` に退避し、`.version` はバイナリの置き換えと同時に書く (途中で止まっても中身と食い違わない)。再起動の前に起動成功マーカー (`data/.startup_ok`) を消す。新しい版が Redis に接続して登録まで済むとマーカーを書くので、それを最大 120 秒待ち、書かれなければ自動ロールバックする (バイナリ・`.version`・退避したファイルを戻して daemon-reload。更新前に無かったファイルは消す)。旧版に戻しても起動しない場合は版ではなく環境 (Redis・ネットワークの停止など) の問題として `.failed-version` を書かず、次回の自動更新で同じ版を再試行する。Redis の認証に失敗している Worker はマーカーを書けないので、60 秒後に稼働しているかだけを見る。

GUI 版 (Windows / macOS) の自動更新の案内も、Worker 本体が上の 5〜7 (管理者の一時停止・段階配布・メジャー版) と同じ判定をしてから出す。

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

### 次のリリースでの変更 (2026-10-08 総点検)

- 新版の案内 (GUI の自動更新の通知) は管理者の一時停止・段階配布・メジャー版スキップを通ったときだけ出す (Linux の自動更新と同じ判定)。Coordinator・GitHub から来るバージョンは `X.Y.Z` 以外を受け付けない
- タスクの URL を `https://*.swim.mlit.go.jp` に限定 (上の「アクセス先の制限」)
- 結果の書き込みは接続エラーなら短い間隔で計 3 回試す。失敗しても集計・GUI の表示は戻す。強制タイムアウトでも失敗の結果を返す (Coordinator が配布のタイムアウトまで待たない)
- Redis の認証失敗は 60 秒おきに再試行 (接続エラーは従来どおり 5 秒)
- 生存確認 (heartbeat) は自分のものだけを延長し (GET で確かめてから SETEX)、同じ Worker 名の別プロセスが持っていたら止まる
- 停止 (SIGTERM・Ctrl+C) は処理中のタスクの結果を書いてから止まる。2 回目は即停止。Windows の CLI でも同じ
- install.sh: タグに固定したダウンロード、起動成功マーカーでのロールバック判定、/opt/swim-worker の所有者の見直し、通信先の制限 (上の各節)
- Docker の HEALTHCHECK は埋め込み CA を一時ファイルに書かずに渡す
- GUI の設定保存: `.env` は本人だけが読める権限 (0600) で、一時ファイルに書いてから置き換える。値は単引用符で囲み、手で足したキー・コメントは残す。パスワードは前後の空白も含めてそのまま保存する
- GUI のパスワードは Windows の資格情報マネージャー / macOS のキーチェーン (keyring) に置き、`.env` には書かない。使えない環境では `.env` に書く。既存の `.env` のパスワードは起動時に移し、読み戻せたら `.env` から消す。CLI は従来どおり `.env` だけを読む
- GUI の Redis 接続は、認証エラー・重複起動以外なら上限 5 分のバックオフで無期限に再試行する (以前は 10 回で諦めていた)。実行中に切れたときも入り直す
- GUI の Redis ユーザー名は必須
- GUI の停止・終了・更新前の停止は、処理中のタスクの結果を書いてから止まるのを待つ (待つのは画面の外のスレッド。一定時間で中断)。どの終わり方 (エラー・重複起動・認証エラー) でも、ボタン・設定欄・トレイの色を起動前に戻す
- GUI の画面 (Tk) を触るのはメインスレッドだけにした。Worker・トレイ・更新のスレッドはキューに積み、メインスレッドがまとめて反映する (ログも)
- GUI の更新のダウンロードはストリームで書きながらハッシュを計算し、先に `SHA256SUMS` を取る。無通信 60 秒・全体 30 分で諦める。進捗を MB で表示する
- 更新物の署名: リリースは draft で作って管理者が署名してから公開し、install.sh・固定の更新スクリプト・GUI は署名と版を確かめてからしか更新しない (上の「更新物の署名」)。Linux の自動更新は固定の更新スクリプトに移した (上の「自動更新機構」)。OpenSSL 3.0 以上が要る
- CI: 権限はジョブごと、Actions はコミット SHA に固定、ビルドの前にテスト、成果物が欠けたら失敗、runner を固定。依存はハッシュ付きでロック (`requirements.lock`・`requirements-dev.lock`、`--require-hashes`)
- heartbeat の所有確認は GET → SETEX (Redis の EVAL は使わない)
- 自動起動ファイル: macOS の plist は XML をエスケープし、Windows の .bat はパスの `%` をエスケープ、システムの文字コードで書けない文字があれば UTF-8 で書く。更新用のスクリプトも同様
- `gui.py` を分割 (`settings_store.py`・`autostart.py`・`updater.py`・`worker_runner.py`)。調査用スクリプトは `scripts/dev/` へ

### v1.2.1 での変更 (2026-10-06)

- Redis の認証に失敗した時 (パスワードの作り直し・入力ミスなど) も自動更新が止まらないようにした。GUI 版は GitHub の最新リリースを確認して更新の案内を出し、Linux 版の自動更新は GitHub の最新版で更新を続ける。Redis に届かない時は従来どおり更新しない
- GUI 版は認証エラーを再試行せず、状態欄に「Redis 認証エラー」と出す

### v1.2.0 での変更 (2026-10-06)

- Redis にユーザー名付きで接続できる (`REDIS_USERNAME`、GUI 版は設定欄「Redis ユーザー名」)。空なら従来どおり。管理者が Worker ごとにユーザーを発行し、各 Worker が触れる範囲を自分の分だけに絞るため
- タスク結果の保存先を Worker 名ごとに分けた (上と同じ理由。Coordinator は配布先の名前で結果を受け取る)
- Redis の権限不足エラーは 60 秒おきに再試行し、設定の確認を促すログを出す

### v1.1.4 での修正 (2026-09-13)

- capability テストの結果には `reason` (`unauthorized` = 401/403、`transient` = それ以外の失敗) が付き、Coordinator は `transient` を判定不能として前回結果を維持する
  それまでは SWIM の 5xx・セッション失効・ネットワーク断でも「非対応」として 23 時間記録されていた

### v1.1.3 での修正 (2026-09-12 レビュー)

- 更新後の起動確認: `.startup_ok` を GUI が表示された時点 (2 秒生存) で書く。以前は Worker が Redis に接続した時にしか書かれず、「起動時に自動接続」OFF の利用者は更新のたびに 120 秒後にロールバックされていた
- ロールバック後は同一バージョンを snooze し、2 回連続なら自動更新を OFF にして通知する
- Windows ヘルパー: 新 exe への置き換えに失敗した場合も旧 exe を再起動する (`reason=move_failed`)
- macOS ヘルパー: 停止は PID 指定 (`pkill -f` は自分自身を止めていた)、PyInstaller 環境変数のリセット、トレイはメインスレッドで実行。Apple Silicon のみ対応
- Worker 名は `[A-Za-z0-9._-]{1,32}` に制限 (`CLIENT SETNAME` 失敗の予防)
- GUI の停止は Redis 再試行中でも中断でき、停止完了までは再起動できない。停止時に SWIM/Redis クライアントを解放する
- capability テストの 403 では再ログイン・Cookie 破棄をしない
- GUI ログは 5MB × 3 世代でローテーション
- ロックファイルは常に `data/swim-worker.lock`。埋め込み CA は一時ファイルを作らず渡す
- Docker 経路 (上級者向け) がビルド・起動できるよう修正
- `install.sh --auto` はロールバックした版を `.failed-version` に記録し再試行しない
