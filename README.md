# swim-worker

SWIM（航空情報共有基盤）の分散データ収集に参加するためのプログラムです。

あなたのPCで動かすだけで、航空データの収集に貢献できます。
SWIMポータルのアカウントがあれば誰でも参加可能です。

## はじめに必要なもの

管理者 (meku) から以下を受け取ってください：

| もらうもの | 説明 |
|-----------|------|
| `ca.crt` | セキュリティ証明書ファイル |
| Redis パスワード | サーバーへの接続パスワード |
| Redis ホスト | サーバーの接続先アドレス |

あなた自身で用意するもの：

| 必要なもの | 説明 |
|-----------|------|
| SWIMアカウント | [SWIMポータル](https://www.swim.mlit.go.jp/) のログインID・パスワード |

---

## セットアップ（3ステップ）

### ステップ 1: ファイルをダウンロード

[Releases ページ](https://github.com/Meku-30/swim-worker/releases/latest) から、自分のOSに合ったセットをダウンロードしてください。

| OS | ダウンロードするファイル |
|----|----------------------|
| Windows | `swim-worker-windows.exe` |
| Mac | `swim-worker-macos` |
| Linux | `swim-worker-linux` |

ダウンロードしたら、好きなフォルダに入れてください（例: デスクトップに `swim-worker` フォルダを作る）。

### ステップ 2: 設定ファイルを作る

ダウンロードしたファイルと**同じフォルダ**に、以下の2つのファイルを配置します。

#### (1) `ca.crt`

管理者から受け取った `ca.crt` ファイルをそのまま同じフォルダに置いてください。

#### (2) `.env` ファイル

テキストエディタ（メモ帳でOK）で新しいファイルを作り、以下の内容を書いて `.env` という名前で保存してください。

```
REDIS_HOST=管理者から教えてもらったアドレス
REDIS_PORT=6380
REDIS_PASSWORD=管理者から教えてもらったパスワード
REDIS_CA_CERT=./ca.crt
SWIM_USERNAME=あなたのSWIMログインID
SWIM_PASSWORD=あなたのSWIMパスワード
WORKER_NAME=あなたの名前（ローマ字、例: tanaka）
```

> **Windowsの注意**: メモ帳で保存するとき、ファイル名を `.env` にして「ファイルの種類」を「すべてのファイル」にしてください。`.env.txt` になってしまうと動きません。

保存後、フォルダの中身はこうなっているはずです：

```
swim-worker/
  swim-worker-windows.exe  (または swim-worker-linux, swim-worker-macos)
  .env
  ca.crt
```

### ステップ 3: 起動

#### Windows

`start.bat` をダブルクリック。または `swim-worker-windows.exe` を直接ダブルクリック。

#### Mac / Linux

ターミナルで：
```bash
chmod +x ./swim-worker-linux   # (Macなら swim-worker-macos)
./start.sh
```

#### 起動したら

以下のようなメッセージが表示されれば成功です：

```
INFO  Redis接続成功
INFO  Worker 'tanaka' を登録しました (pending)
INFO  Worker 'tanaka' 起動
```

起動後、**管理者に「起動しました」と連絡**してください。
管理者が承認すると、自動的にタスクの受信が始まります。

---

## 停止方法

- **Windows**: ウィンドウを閉じる、または `Ctrl+C`
- **Mac / Linux**: `Ctrl+C`

再度起動したいときは、もう一度実行ファイルを起動するだけです。

---

## うまくいかないとき

| 表示されるメッセージ | 原因 | やること |
|-------------------|------|---------|
| `Redis接続失敗` | サーバーに繋がらない | `.env` の REDIS_HOST, REDIS_PASSWORD を確認。管理者に連絡 |
| `TLS connection error` | 証明書が見つからない | `ca.crt` が `.env` と同じフォルダにあるか確認 |
| `ログインAPI失敗` | SWIMのIDかパスワードが違う | `.env` の SWIM_USERNAME, SWIM_PASSWORD を確認 |
| タスクが来ない | まだ承認されていない | 管理者に連絡して承認してもらう |
| `.env` が認識されない (Windows) | ファイル名が `.env.txt` になっている | ファイル拡張子を表示して `.txt` を削除 |

それでも解決しない場合は、管理者に画面のスクリーンショットを送ってください。

---

## Docker で動かす場合（上級者向け）

Docker を使える方は以下の方法でも起動できます。

```bash
git clone https://github.com/Meku-30/swim-worker.git
cd swim-worker
cp .env.example .env   # 設定を記入
docker compose up -d
```

停止: `docker compose down`
ログ: `docker compose logs -f`

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
