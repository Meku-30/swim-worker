# swim-worker

SWIM（航空情報共有基盤）分散収集システムのWorkerノード。

中央のCoordinator からRedis経由でタスクを受け取り、SWIMポータルのAPIを実行して結果を返します。

## 仕組み

```
[Coordinator] --タスク--> [Redis] --タスク--> [このWorker]
                                                  ↓
                                           SWIMにログイン
                                           API実行
                                                  ↓
[Coordinator] <--結果--- [Redis] <--結果--- [このWorker]
```

- Worker は自分専用のキュー (`tasks:{worker_name}`) を監視します
- タスクを受け取ると SWIM API を実行し、結果をそのまま Redis に返します
- 30秒ごとにハートビートを送信し、Coordinator に生存を通知します
- SWIM の認証情報は Worker 内のみに保持され、中央には送信されません

## 必要なもの

- **SWIMポータルのアカウント** (ID + パスワード)
- **Redis接続情報** (管理者から提供)
- **CA証明書** (`ca.crt`、リポジトリに同梱)
- 以下のいずれかの実行環境:
  - Docker + Docker Compose
  - Python 3.12 以上

## クイックスタート (実行ファイル)

Python や Docker をインストールしたくない場合、ビルド済みの実行ファイルを利用できます。

1. [Releases ページ](https://github.com/Meku-30/swim-worker/releases) から OS に合ったファイルをダウンロード
   - Windows: `swim-worker-windows.exe`
   - Mac: `swim-worker-macos`
   - Linux: `swim-worker-linux`
2. 同じフォルダに `.env` ファイルと `ca.crt` を配置（下記「設定ファイルを作成」参照）
3. 実行
   - Windows: `swim-worker-windows.exe` をダブルクリック
   - Mac/Linux: `chmod +x swim-worker-* && ./swim-worker-linux` (or macos)

## セットアップ

### 1. リポジトリを取得

```bash
git clone https://github.com/Meku-30/swim-worker.git
cd swim-worker
```

### 2. 設定ファイルを作成

`.env.example` をコピーして `.env` を作成し、各項目を記入してください。

```bash
cp .env.example .env
```

```env
REDIS_HOST=<管理者から提供されるIP>
REDIS_PORT=6380
REDIS_PASSWORD=<管理者から提供されるパスワード>
REDIS_CA_CERT=./ca.crt
SWIM_USERNAME=<あなたのSWIM ID>
SWIM_PASSWORD=<あなたのSWIMパスワード>
WORKER_NAME=<一意な名前（例: tanaka）>
```

| 項目 | 説明 |
|------|------|
| `REDIS_HOST` | Redis サーバーの IP アドレス（管理者から提供） |
| `REDIS_PORT` | Redis ポート（通常 `6380`） |
| `REDIS_PASSWORD` | Redis パスワード（管理者から提供） |
| `REDIS_CA_CERT` | TLS CA 証明書のパス（同梱の `ca.crt` をそのまま使用） |
| `SWIM_USERNAME` | SWIM ポータルのログインID |
| `SWIM_PASSWORD` | SWIM ポータルのパスワード |
| `WORKER_NAME` | この Worker の名前（他と重複しない任意の名前） |

### 3. 起動

#### Docker（推奨）

```bash
docker compose up -d
```

停止:
```bash
docker compose down
```

#### Python 直接実行

```bash
pip install -r requirements.txt
python -m swim_worker
```

停止: `Ctrl+C`

### 4. 管理者に承認を依頼

初回起動時、Worker は「承認待ち (pending)」状態で登録されます。
管理者に承認してもらうと、タスクの配布が開始されます。

## 動作確認

Worker が正しく動作しているか確認するには、ログを確認してください。

#### Docker

```bash
docker compose logs -f
```

正常な場合、以下のようなログが表示されます:

```
INFO  swim_worker.consumer: Worker 'tanaka' を登録しました (pending)
INFO  swim_worker.consumer: Worker 'tanaka' 起動
```

タスクを受信・実行すると:

```
INFO  swim_worker.consumer: タスク実行開始: abc12345 (type=collect_pireps)
INFO  swim_worker.consumer: タスク成功: abc12345
```

## トラブルシューティング

| 症状 | 原因 | 対処 |
|------|------|------|
| `Redis接続失敗` | Redis接続情報が間違っている | `.env` の REDIS_HOST, REDIS_PORT, REDIS_PASSWORD を確認 |
| `TLS connection error` | CA証明書が見つからない | `ca.crt` が同じディレクトリにあるか確認 |
| タスクが来ない | 管理者が承認していない or Coordinator が停止中 | 管理者に確認 |
| `ログインAPI失敗` | SWIM認証情報が間違っている | `.env` の SWIM_USERNAME, SWIM_PASSWORD を確認 |

## 技術仕様

| 項目 | 値 |
|------|-----|
| 言語 | Python 3.12 |
| Redis通信 | TLS (ポート6380) + パスワード認証 |
| SWIM認証 | httpx + Cookie認証 (domain: mlit.go.jp) |
| ハートビート | 30秒間隔、TTL 90秒 |
| タスクキュー | Redis List (BLPOP、5秒タイムアウト) |
| 結果TTL | 1時間 |
| シグナル | SIGINT/SIGTERM でグレースフル停止 |
