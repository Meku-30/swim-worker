# CLAUDE.md — SWIM Worker

SWIM 非公式 API の収集実行コンポーネント。Coordinator からジョブを受け取り、SWIM ポータルからデータを収集する。

このリポジトリの親ワークスペース (ローカル環境) の `CLAUDE.md` に定義された作業ルールを継承する。全体像は `swim-api/CLAUDE.md` を参照。

## 公開設定

⚠️ **このリポジトリは Public** (`Meku-30/swim-worker`)

SWIM 3 リポジトリの中で唯一の公開リポジトリ。以下を**絶対にコミットしない**。

- SWIM ポータルの認証情報、Redis のパスワード・接続文字列、API キー、トークン
- 自宅の内部 IP (192.168.x.x)、NAS のパス、ホスト名、Tailscale IP
- Coordinator / API 側の内部エンドポイント

値を記載する必要がある場合は `<REDACTED: see .env VARIABLE_NAME>` と書く。コミット前に差分を必ず確認すること。

## 稼働環境

VPS 2 台 + Windows PC 1 台 + Raspberry Pi (予定)。Linux amd64 / arm64 対応。

## 主要機能

### Worker Capability

job_type ごとの SWIM 権限をテストで検出し、Coordinator に報告する。Coordinator は対応可能な Worker にのみジョブを配布する。

### 自動アップデート

| 種別 | 方式 |
|------|------|
| GUI (Windows / macOS) | ポップアップ確認後に GitHub Releases から自動 DL・再起動 |
| CLI (Linux / Pi / VPS) | systemd timer (6h + ランダム) で `install.sh --auto` が稼働 |

全体制御は Coordinator 側 Redis の kill switch (`swim:auto_update_enabled`) と staged rollout whitelist (`swim:auto_update_whitelist`) で行う。操作は `swim-coordinator/scripts/swim-admin` を使う。

## ドキュメント

- **運用ランブック**: `swim-coordinator/docs/admin-runbook.md` — Worker 管理の全操作を集約
