# swim-worker

SWIM（航空情報共有基盤）の分散データ収集に参加するためのプログラムです。

あなたのPCで動かすだけで、航空データの収集に貢献できます。
SWIMポータルのアカウントがあれば誰でも参加可能です。

## はじめに必要なもの

管理者 (meku) から以下を教えてもらってください：

| 教えてもらうもの | 説明 |
|---------------|------|
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

### ステップ 2: 起動して設定

ダウンロードした `swim-worker-windows.exe` をダブルクリックすると設定画面が開きます。

各欄を記入してください：

| 欄 | 入力する内容 |
|----|------------|
| Redis ホスト | 管理者から教えてもらったアドレス |
| Redis パスワード | 管理者から教えてもらったパスワード |
| SWIM ID | あなたのSWIMログインID |
| SWIM パスワード | あなたのSWIMパスワード |
| Worker 名 | あなたの名前（ローマ字、例: tanaka） |

記入したら **「▶ 起動」** をクリック。

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

### 最小化・トレイアイコン

ウィンドウの最小化ボタンを押すとシステムトレイ（通知領域）に格納されます。トレイアイコンはレーダー型で、稼働中は緑、停止中はグレー、エラー時は赤に変わります。

トレイアイコンをクリックするとウィンドウが再表示されます。

---

## Mac の場合（GUI版）

### ステップ 1: ダウンロード

[Releases ページ](https://github.com/Meku-30/swim-worker/releases/latest) から `swim-worker-macos` をダウンロードしてください。

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
Raspberry Pi 4/5 + 64bit OS (Pi OS Bookworm 等) で動作確認済みです。

### 推奨: ワンライナーインストール

```bash
# まずスクリプトを DL して中身を確認してから実行することを推奨
curl -fsSL -o install.sh https://github.com/Meku-30/swim-worker/releases/latest/download/install.sh
less install.sh
sudo bash install.sh
```

install.sh が以下を自動で行います:

- お使いのアーキテクチャ (amd64 / arm64) に合うバイナリを DL し、SHA256 で整合性検証
- 専用ユーザー `swim-worker` (システムアカウント、ログイン不可) を作成
- `/opt/swim-worker/` にバイナリ配置
- `.env` を対話式に作成 (入力値は `chmod 600` で保護)
- systemd サービスとして登録 (自動起動)

対話で以下を聞かれるので、管理者から教えてもらった値と、あなたの SWIM 認証情報を入力してください:

| 欄 | 入力する内容 |
|----|------------|
| Redis ホスト | 管理者から教えてもらったアドレス |
| Redis パスワード | 管理者から教えてもらったパスワード |
| SWIM ユーザー名 | あなたのSWIMログインID |
| SWIM パスワード | あなたのSWIMパスワード |
| Worker 名 | あなたの名前（ローマ字、例: tanaka） |

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
sudo systemctl disable --now swim-worker && \
  sudo rm -rf /opt/swim-worker /etc/systemd/system/swim-worker.service && \
  sudo userdel swim-worker            # 完全削除
```

### 手動インストールしたい場合 (install.sh を使わない方法)

[Releases ページ](https://github.com/Meku-30/swim-worker/releases/latest) から以下を DL:

- バイナリ: `swim-worker-linux-amd64` または `swim-worker-linux-arm64`
- `.env.example`
- `SHA256SUMS`

SHA256 を検証してから実行:

```bash
sha256sum -c SHA256SUMS --ignore-missing
chmod +x ./swim-worker-linux-*
mv .env.example .env   # 中身を編集
./swim-worker-linux-amd64   # お使いのアーキに応じて
```

---

## うまくいかないとき

| 症状 | やること |
|------|---------|
| `Redis接続失敗` | Redisホスト・パスワードを確認。管理者に連絡 |
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

停止: `Ctrl+C`

### GUI 版 / 開発用 (オプション)

```bash
pip install -r requirements-gui.txt   # GUI 版を動かす場合
pip install -r requirements-dev.txt   # pytest / PyInstaller ビルド用
```

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
