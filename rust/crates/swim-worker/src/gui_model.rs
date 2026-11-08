//! 画面の文言と判定 (Windows に依存しない。Python の gui_helpers.py・gui.py と同じ文言)
use std::collections::VecDeque;

use crate::config::valid_worker_name;
use crate::gui_store::Fields;
use crate::runner::{RunnerState, StopReason};

pub const LOG_MAX_LINES: usize = 1000;
pub const LOG_KEEP_LINES: usize = 800;

const FIELD_LABELS: [(&str, &str); 6] = [
    ("redis_host", "Redis ホスト"),
    ("redis_username", "Redis ユーザー名"),
    ("redis_password", "Redis パスワード"),
    ("swim_username", "SWIM ID"),
    ("swim_password", "SWIM パスワード"),
    ("worker_name", "Worker 名"),
];

pub const WORKER_NAME_RULE_MESSAGE: &str = "Worker 名は半角英数字・ピリオド・アンダースコア・ハイフンのみ (1〜32 文字) です";

/// 起動前の確認。問題があれば見せる文。全部必須。パスワードは空白も値の一部なので strip しない
pub fn validate_fields(f: &Fields) -> Option<String> {
    for (key, label) in FIELD_LABELS {
        let v = f.get(key);
        let empty = if key.ends_with("password") { v.is_empty() } else { v.trim().is_empty() };
        if empty {
            let hint = if key == "redis_username" { "管理者から教えてもらったユーザー名を入力してください。" } else { "設定を記入してください。" };
            return Some(format!("{label} が空です。{hint}"));
        }
    }
    if !valid_worker_name(f.worker_name.trim()) {
        return Some(WORKER_NAME_RULE_MESSAGE.into());
    }
    None
}

fn job_label(job: &str) -> &str {
    match job {
        "collect_notams" => "NOTAM収集",
        "collect_pireps" => "PIREP収集",
        "collect_pkg_weather" => "PKG気象収集",
        "collect_airports" => "空港一覧取得",
        "collect_flight_foids" => "フライト一覧取得",
        "collect_flight_details" => "フライト詳細取得",
        "capability_test" => "権限テスト",
        j => j,
    }
}

/// タスクの状態を状態欄の文字列にする
pub fn task_state_text(state: &str, job_type: &str, total: u64, errors: u64) -> String {
    if state == "processing" {
        return format!("● 実行中: {}", job_label(job_type));
    }
    if errors > 0 {
        return format!("● 接続中 (処理済 {total} 件, エラー {errors})");
    }
    format!("● 接続中 (処理済 {total} 件)")
}

pub fn status_for_runner(s: &RunnerState) -> String {
    match s {
        RunnerState::Connecting => "● 起動中...".into(),
        RunnerState::Waiting { attempt, delay_secs } => format!("Redis 再接続待ち ({attempt} 回目、{delay_secs} 秒後に再試行)"),
        RunnerState::Running => "● 接続中 (タスク待ち)".into(),
    }
}

pub fn status_for_stop(r: &StopReason) -> &'static str {
    match r {
        StopReason::User => "停止中",
        StopReason::Duplicate(_) => "重複起動エラー",
        StopReason::AuthFailed(_) => "Redis 認証エラー (設定欄のユーザー名・パスワードを確認)",
        StopReason::Error(_) => "エラーで停止しました (ログを確認)",
    }
}

pub fn duplicate_message(worker_name: &str) -> String {
    format!(
        "同じ Worker 名 '{worker_name}' で別のプロセスが稼働中のため起動できません。\n\n考えられる原因:\n  • 他の PC や VPS で同名ワーカーが動いている\n  • 前回クラッシュ時の古い heartbeat が残っている\n    (数分で自動解放されます)\n\n別の worker_name を設定するか、もう一方を停止してください。"
    )
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum PromptKind {
    /// 自動適用: 5 秒のカウントダウン
    Countdown,
    /// 確認のダイアログ
    Prompt,
}

/// 新版を知ったときに出すもの (snooze 中は None)
pub fn update_prompt_kind(snoozed: bool, auto_update: bool) -> Option<PromptKind> {
    if snoozed {
        None
    } else if auto_update {
        Some(PromptKind::Countdown)
    } else {
        Some(PromptKind::Prompt)
    }
}

pub fn update_prompt_message(new_version: &str, current: &str) -> String {
    format!(
        "新しいバージョン v{new_version} が利用可能です。\n現在のバージョン: v{current}\n\n今すぐアップデートしますか？\n（ダウンロード後、自動で再起動します）\n\n「いいえ」を選ぶと {} 時間、確認ダイアログを表示しません\n（右上の「アップデート」ボタンからはいつでも実行できます）",
        crate::gui_store::SNOOZE_HOURS
    )
}

/// 前回の更新が巻き戻ったときの知らせ
pub fn rollback_message(from_version: &str, reason: &str, disabled: bool) -> String {
    let mut t = format!("v{from_version} へのアップデートが起動確認に失敗したため、自動的に前バージョンにロールバックされました。\n\n");
    if reason == "move_failed" {
        t.push_str("原因: 新しい exe への置き換えに失敗しました (ウイルス対策ソフトのスキャン等)。\n\n");
    }
    if disabled {
        t.push_str("同じバージョンで 2 回失敗したため、自動更新を停止しました。\n設定で再度有効にするか、手動で更新してください。\n\n");
    }
    t.push_str("詳細は swim-worker-update.log を確認してください。");
    t
}

/// ログ欄の中身の変え方
#[derive(Debug, Clone, PartialEq)]
pub enum LogEdit {
    /// 末尾に足す (EM_REPLACESEL。全文を取り直さない)
    Append(String),
    /// 1000 行を超えたので 800 行に縮めて全部入れ直す (200 行に 1 回)
    Reset(String),
}

/// ログ欄の行 (1000 行を超えたら古い行を消して 800 行に戻す)
#[derive(Debug, Default)]
pub struct LogBuffer {
    lines: VecDeque<String>,
}

impl LogBuffer {
    pub fn push(&mut self, line: String) -> LogEdit {
        let add = format!("{line}\r\n");
        self.lines.push_back(line);
        if self.lines.len() > LOG_MAX_LINES {
            while self.lines.len() > LOG_KEEP_LINES {
                self.lines.pop_front();
            }
            let mut all = String::new();
            for l in &self.lines {
                all.push_str(l);
                all.push_str("\r\n");
            }
            return LogEdit::Reset(all);
        }
        LogEdit::Append(add)
    }
}
