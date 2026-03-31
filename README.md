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

[Releases ページ](https://github.com/Meku-30/swim-worker/releases/latest) から以下の **2つ** をダウンロードして、同じフォルダに入れてください。

- `swim-worker-windows.exe`
- `ca.crt`

### ステップ 2: 起動して設定

`swim-worker-windows.exe` をダブルクリックすると設定画面が開きます。

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

---

## Mac / Linux の場合（CLI版）

### ステップ 1: ダウンロード

[Releases ページ](https://github.com/Meku-30/swim-worker/releases/latest) から以下をダウンロードして同じフォルダに入れてください。

- Mac: `swim-worker-macos` + `ca.crt` + `.env.example`
- Linux: `swim-worker-linux` + `ca.crt` + `.env.example`

### ステップ 2: 設定ファイルを作る

`.env.example` を `.env` にリネームして、中身を書き換えます。

```
REDIS_HOST=管理者から教えてもらったアドレス
REDIS_PORT=6380
REDIS_PASSWORD=管理者から教えてもらったパスワード
REDIS_CA_CERT=./ca.crt
SWIM_USERNAME=あなたのSWIMログインID
SWIM_PASSWORD=あなたのSWIMパスワード
WORKER_NAME=あなたの名前（ローマ字、例: tanaka）
```

### ステップ 3: 起動

```bash
chmod +x ./swim-worker-linux   # Macなら: chmod +x ./swim-worker-macos
./swim-worker-linux             # Macなら: ./swim-worker-macos
```

起動後、**管理者に「起動しました」と連絡**してください。

停止は `Ctrl+C` です。

---

## うまくいかないとき

| 症状 | やること |
|------|---------|
| `Redis接続失敗` | Redisホスト・パスワードを確認。管理者に連絡 |
| `TLS connection error` | `ca.crt` が実行ファイルと同じフォルダにあるか確認 |
| `ログインAPI失敗` | SWIM ID・パスワードを確認 |
| タスクが来ない | 管理者に承認してもらう |

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

Python 3.12 以上がインストールされている場合：

```bash
git clone https://github.com/Meku-30/swim-worker.git
cd swim-worker
cp .env.example .env   # 設定を記入
pip install -r requirements.txt
python -m swim_worker
```

停止: `Ctrl+C`

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
