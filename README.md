# swim-worker

SWIM（航空情報共有基盤）の分散データ収集に参加するためのプログラムです。

あなたのPCで動かすだけで、航空データの収集に貢献できます。
SWIMポータルのアカウントがあれば誰でも参加可能です。

## はじめに必要なもの

管理者 (meku) から以下を教えてもらってください：

| 教えてもらうもの | 説明 |
|---------------|------|
| Redis ユーザー名 | サーバーへの接続ユーザー名 (Worker ごとに発行。必須) |
| Redis パスワード | サーバーへの接続パスワード |
| Redis ホスト | サーバーの接続先アドレス |

あなた自身で用意するもの：

| 必要なもの | 説明 |
|-----------|------|
| SWIMアカウント | [SWIMポータル](https://www.swim.mlit.go.jp/) のログインID・パスワード |

---

## Windows の場合（GUI版）

### ステップ 1: ダウンロード

[Releases ページ](https://github.com/Meku-30/swim-worker/releases/latest) から `swim-worker-windows.exe` をダウンロードしてください。**これ1つだけ**でOKです。

設定ファイル (`.env`) と SWIM ログインの Cookie は exe と同じフォルダに保存されます。ダウンロードフォルダや共有フォルダに置かず、`C:\Users\<name>\swim-worker\` のような自分専用のフォルダに置いて実行することをおすすめします。

Redis と SWIM の**パスワードは Windows の資格情報マネージャー** (Mac はキーチェーン) に保存され、`.env` には書かれません。以前のバージョンで `.env` に保存したパスワードは、起動時に自動で資格情報マネージャーへ移ります (移せなかった場合は `.env` に残ります)。資格情報マネージャーが使えない環境では、従来どおり `.env` (本人だけが読める権限) に保存します。

### ステップ 2: 起動して設定

ダウンロードした `swim-worker-windows.exe` をダブルクリックすると設定画面が開きます。

各欄を記入してください：

| 欄 | 入力する内容 |
|----|------------|
| Redis ホスト | 管理者から教えてもらったアドレス |
| Redis ユーザー名 | 管理者から教えてもらったユーザー名 (必須) |
| Redis パスワード | 管理者から教えてもらったパスワード |
| SWIM ID | あなたのSWIMログインID |
| SWIM パスワード | あなたのSWIMパスワード |
| Worker 名 | あなたの名前（ローマ字、例: tanaka）。半角英数字・`.` `_` `-` のみ、1〜32文字。空白や日本語は使えません |

記入したら **「▶ 起動」** をクリック。

- サーバーにつながらない間は、状態欄に「Redis 再接続待ち」と出て自動で再試行し続けます (間隔は最大 5 分)。ユーザー名・パスワードの誤りのときは再試行せず「Redis 認証エラー」と出ます
- **「■ 停止」やアプリの終了は、処理中のタスクを終えてから**止まります (数十秒かかることがあります)

### ステップ 3: 承認を待つ

画面に以下が表示されれば接続成功です：

```
21:50:00 Redis接続成功
21:50:00 Worker 'tanaka' を登録しました (pending)
21:50:00 Worker 'tanaka' 起動
```

**管理者に「起動しました」と連絡**してください。承認されると自動的にタスクの受信が始まります。

### 自動起動

画面下部の **「Windows起動時に自動起動」** にチェックを入れると、PC起動時に自動で立ち上がります。

### 起動時に自動接続

**「起動時に自動接続」** にチェックを入れると、設定欄が全て入力された状態でアプリが起動したときに自動で「▶ 起動」が押された状態になります。自動アップデート後の再起動時も、このチェックが入っていれば接続まで自動で行われます（OFFの場合は再起動のたびに手動で「▶ 起動」を押す必要があります）。

### 最小化・トレイアイコン

ウィンドウの最小化ボタンを押すとシステムトレイ（通知領域）に格納されます。トレイアイコンはレーダー型で、稼働中は緑、停止中はグレー、エラー時は赤に変わります。

トレイアイコンをクリックするとウィンドウが再表示されます。

---

## Mac の場合（GUI版）

### ステップ 1: ダウンロード

[Releases ページ](https://github.com/Meku-30/swim-worker/releases/latest) から `swim-worker-macos` をダウンロードしてください。

**Apple Silicon (arm64) のみ対応です。Intel Mac では動作しません。**

### ステップ 2: 起動して設定

```bash
chmod +x ./swim-worker-macos
./swim-worker-macos
```

初回起動時に「開発元が未確認」と表示された場合は、ファイルを右クリック →「開く」で起動できます。

Windows版と同じ設定画面が開くので、各欄を記入して **「▶ 起動」** をクリックしてください。

### ステップ 3: 承認を待つ

Windows版と同様です。**管理者に「起動しました」と連絡**してください。

### 自動起動

画面下部の **「ログイン時に自動起動」** にチェックを入れると、Macログイン時に自動で立ち上がります。

---

## Linux / Raspberry Pi の場合（CLI版）

amd64 (x86_64) と arm64 (aarch64) の両方に対応しています。
Raspberry Pi 4/5 + 64bit OS (Pi OS Bookworm 等) での動作を想定していますが、
実機での動作確認はまだ行っていません（CIではarm64バイナリのビルドのみ実施）。

### 推奨: ワンライナーインストール

```bash
# まずスクリプトを DL して中身を確認してから実行することを推奨
curl -fsSL -o install.sh https://github.com/Meku-30/swim-worker/releases/latest/download/install.sh
less install.sh
sudo bash install.sh
```

install.sh 自体の署名も確かめたい場合は、[「リリースの署名を自分で確かめる」](#リリースの署名を自分で確かめる) の手順で `install.sh` を検証してから実行してください。

install.sh が以下を自動で行います:

- 最新版のタグを調べ、お使いのアーキテクチャ (amd64 / arm64) に合うバイナリをそのタグから DL し、検証する: `SHA256SUMS` の署名 (`SHA256SUMS.sig`、Ed25519) を install.sh に埋め込んだ公開鍵で確かめ、`SHA256SUMS` の先頭行の版がそのタグと一致し、各ファイルのハッシュが一致すること。署名のないリリースは入れません (**OpenSSL 3.0 以上**が必要。Pi OS Bookworm・Debian 12・Ubuntu 22.04 以降は標準で入っています。Debian 11・Ubuntu 20.04 などの OpenSSL 1.1.1 では署名を確かめられないため、インストール・自動更新が止まります。`openssl version` で確認できます)
- 専用ユーザー `swim-worker` (システムアカウント、ログイン不可) を作成
- `/opt/swim-worker/` にバイナリ配置 (root の持ち物。Worker が書けるのは `data/` だけ)
- `.env` を対話式に作成 (`root:swim-worker` の `640`。Worker は読むだけ、他のユーザーは読めない)
- 自動更新用の固定の更新スクリプトを `/usr/local/libexec/swim-worker/update.sh` に置く
- systemd サービスとして登録 (自動起動)。LAN 内の機器への通信は閉じ、名前解決のサーバーと Redis だけ通す設定 (`/etc/systemd/system/swim-worker.service.d/10-ip-allow.conf`) も置く

対話で以下を聞かれるので、管理者から教えてもらった値と、あなたの SWIM 認証情報を入力してください:

| 欄 | 入力する内容 |
|----|------------|
| Redis ホスト | 管理者から教えてもらったアドレス |
| Redis ユーザー名 | 管理者から教えてもらったユーザー名 (必須) |
| Redis パスワード | 管理者から教えてもらったパスワード |
| SWIM ユーザー名 | あなたのSWIMログインID |
| SWIM パスワード | あなたのSWIMパスワード |
| Worker 名 | あなたの名前（ローマ字、例: tanaka）。半角英数字・`.` `_` `-` のみ、1〜32文字。空白や日本語は使えません |

### 起動

```bash
sudo systemctl start swim-worker
sudo systemctl status swim-worker
```

起動できたら **管理者に「起動しました」と連絡**してください。

### ログ / 停止 / アンインストール

```bash
sudo journalctl -u swim-worker -f     # ライブログ
sudo systemctl stop swim-worker       # 停止
sudo systemctl disable --now swim-worker swim-worker-update.timer && \
  sudo rm -rf /opt/swim-worker /etc/systemd/system/swim-worker*.{service,timer,old} \
    /etc/systemd/system/swim-worker.service.d /usr/local/libexec/swim-worker && \
  sudo userdel swim-worker            # 完全削除
```

### 自動更新について

install.sh は `swim-worker-update.timer` (6時間間隔 + 最大2時間ランダム) を有効化します。
timer は固定の更新スクリプト `/usr/local/libexec/swim-worker/update.sh` を root で実行します。
管理者 (meku) が新バージョンを署名して公開し、更新を許可すると、自動で:

1. 管理者の一時停止・段階配布・メジャー版の変更でないことを確かめる (ここまではダウンロードしない)
2. そのリリースの `SHA256SUMS` と署名 `SHA256SUMS.sig` を取り、埋め込みの公開鍵で署名と版を検証
3. 署名を確かめた `install.sh` で新バイナリを DL + 検証し、旧バイナリ・unit・通信許可の設定・更新スクリプトを `.old` として保持
4. swim-worker を再起動
5. 新しい版が Redis につながって登録まで済む (起動成功マーカー `data/.startup_ok` を書く) のを最大 120 秒待つ → 済まなければ自動ロールバック (バイナリ・`.version`・unit・通信許可の設定・更新スクリプトを元に戻す)

署名のないリリース、署名が合わないリリース、今の版より新しくない版には更新しません。

以前の版 (v1.2.x まで) の自動更新は最新の install.sh をそのまま実行していました。その仕組みが署名付きの新しい版の install.sh を実行すると、管理者の一時停止・段階配布の確認を通ったうえで、固定の更新スクリプトと新しい update.service・timer の設定だけに置き換わります (手作業は要りません)。Worker 本体の更新は、その次の自動更新 (6〜8 時間後) で新しい仕組みが行います。自動更新を止めている (`.no-auto-update`) 機も仕組みの置き換えは行い、本体は更新しません。

**ネットワークを変えたら `sudo bash install.sh` をもう一度実行してください。** Worker は LAN 内への通信を閉じていて、名前解決のサーバー (DNS) と Redis だけをインストール時の値で通しています。systemd-resolved を使っていない機 (Raspberry Pi OS Bookworm の NetworkManager など、`/etc/resolv.conf` にルーターの IP が直接書かれている機) で、別のネットワークにつないだ・ルーターを替えたなどで DNS サーバーの IP が変わると、次の自動更新まで名前解決できません。install.sh を実行し直すと通信の許可が書き直されます (設定 `.env` はそのまま)。Worker は起動時に、今の DNS サーバーが許可されていなければログ (`journalctl -u swim-worker`) に警告を出します。

**自動更新を止めたい場合** (Pi 管理者向け):

```bash
sudo touch /opt/swim-worker/.no-auto-update   # 個別 opt-out
# または
sudo systemctl disable --now swim-worker-update.timer  # timer 自体を無効化
```

### 手動インストールしたい場合 (install.sh を使わない方法)

[Releases ページ](https://github.com/Meku-30/swim-worker/releases/latest) から以下を DL:

- バイナリ: `swim-worker-linux-amd64` または `swim-worker-linux-arm64`
- `SHA256SUMS` と `SHA256SUMS.sig`

[署名](#リリースの署名を自分で確かめる)と SHA256 を検証してから、`.env` を自前で作成して実行:

```bash
sha256sum -c SHA256SUMS --ignore-missing   # 先頭の版の行は「形式が不正」と警告されるが無視してよい
chmod +x ./swim-worker-linux-*

cat > .env <<EOF
REDIS_HOST=管理者から教えてもらったアドレス
REDIS_PORT=6380
REDIS_USERNAME=管理者から教えてもらったユーザー名  # 指定がなければこの行は不要
REDIS_PASSWORD=管理者から教えてもらったパスワード
SWIM_USERNAME=あなたのSWIMログインID
SWIM_PASSWORD=あなたのSWIMパスワード
WORKER_NAME=あなたの名前（ローマ字、例: tanaka）  # 半角英数字・`.` `_` `-` のみ、1〜32文字。空白や日本語は使えません
EOF
chmod 600 .env

./swim-worker-linux-amd64   # お使いのアーキに応じて
```

### リリースの署名を自分で確かめる

リリースの `SHA256SUMS` は管理者の Ed25519 鍵で署名されています (`SHA256SUMS.sig`)。
公開鍵はこのリポジトリの [`scripts/release_pubkeys/`](scripts/release_pubkeys/) にあります
(install.sh・自動更新・GUI に埋め込んであるものと同じ)。OpenSSL 3.0 以上で確かめられます (1.1.1 には `pkeyutl -rawin` がありません):

```bash
TAG=v1.3.0   # 確かめる版
for f in SHA256SUMS SHA256SUMS.sig install.sh; do
  curl -fsSLO "https://github.com/Meku-30/swim-worker/releases/download/${TAG}/${f}"
done
curl -fsSL -o release.pub.pem https://raw.githubusercontent.com/Meku-30/swim-worker/master/scripts/release_pubkeys/key1.pub.pem
openssl pkeyutl -verify -pubin -inkey release.pub.pem -rawin -in SHA256SUMS -sigfile SHA256SUMS.sig
head -1 SHA256SUMS                           # 「# swim-worker-release ${TAG}」であること
sha256sum -c SHA256SUMS --ignore-missing     # 版の行の「形式が不正」の警告は無視してよい
```

`Signature Verified Successfully` と `install.sh: OK` が出れば、管理者が署名したものです。

---

## うまくいかないとき

| 症状 | やること |
|------|---------|
| `Redis接続失敗` / 状態欄が「Redis 再接続待ち」のまま | Redisホスト・ネットワークを確認。自動で再試行し続けます。直らなければ管理者に連絡 |
| 状態欄が「Redis 認証エラー」 | 設定欄の Redis ユーザー名・パスワードを確認 (管理者から教えてもらったもの) |
| `TLS connection error` | 証明書エラー。管理者に連絡 |
| `ログインAPI失敗` | SWIM ID・パスワードを確認 |
| タスクが来ない | 管理者に承認してもらう |
| `別の swim-worker プロセスが既に起動しています` | 既に起動中の Worker がある。システムトレイのレーダーアイコンを確認。二重起動は防止されています |
| `worker_name '...' は既に別プロセスで稼働中です` | 同じ Worker 名で別のPC / VPS が動いている可能性。別の名前を設定するか、もう一方を停止してください。前回クラッシュ後すぐに再起動した場合は最大90秒待つと自動解放されます |

それでも解決しない場合は、管理者に画面のスクリーンショットを送ってください。

---

## Docker で動かす場合（上級者向け）

```bash
git clone https://github.com/Meku-30/swim-worker.git
cd swim-worker
cp .env.example .env   # 設定を記入
docker compose up -d
```

停止: `docker compose down` / ログ: `docker compose logs -f`

---

## Python で動かす場合（上級者向け）

Python **3.10 以上** がインストールされていれば動作します。
(Raspberry Pi OS Bookworm / Ubuntu 24.04 はデフォルトの Python 3.11/3.12 でそのまま動きます)

```bash
git clone https://github.com/Meku-30/swim-worker.git
cd swim-worker
cp .env.example .env   # 設定を記入
pip install -r requirements.txt
python -m swim_worker
```

停止: `Ctrl+C` (処理中のタスクを終えてから止まる。もう一度押すと即停止。Windows でも同じ)

### GUI 版 / 開発用 (オプション)

```bash
pip install -r requirements-gui.txt   # GUI 版を動かす場合
pip install -r requirements-dev.txt   # pytest / PyInstaller ビルド用
# CI と同じ版で揃えるなら、ハッシュ付きのロックから: pip install --require-hashes -r requirements-dev.lock
python -m swim_worker.gui             # GUI 版 (.env・data/ はカレントディレクトリ)
```

GUI の画面のテスト (`tests/test_gui_smoke.py`) は画面が要ります。画面の無い Linux では skip されるので、`xvfb-run python -m pytest` のように仮想ディスプレイで走らせてください。

依存を変えたら `scripts/lock-deps.sh` (uv が要る) でロック (`requirements.lock`・`requirements-dev.lock`) を作り直してコミットします。リリースの手順 (CI → 署名 → 公開) は [docs/release-signing.md](docs/release-signing.md) にあります。

SWIM の API を手で確かめる調査用スクリプトは `scripts/dev/` にあります (`probe_*.py`、`capture_headers.py`)。パスワードは引数では受け取らず、環境変数 `SWIM_PASSWORD` か入力で渡します。

---

## 仕組み（参考）

```
[中央サーバー] --タスク--> [Redis] --タスク--> [あなたのWorker]
                                                    ↓
                                             SWIMにログイン
                                             データ取得
                                                    ↓
[中央サーバー] <--結果--- [Redis] <--結果--- [あなたのWorker]
```

- あなたのSWIM ID・パスワードはあなたのPC内だけで使われ、中央サーバーには送信されません
- 30秒ごとに「動いてるよ」という信号を送り、中央サーバーが監視します
- PCの電源を切ったりWorkerを止めても、他のWorkerがカバーするので問題ありません
