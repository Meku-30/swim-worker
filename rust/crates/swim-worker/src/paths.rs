//! Worker が読み書きするファイルの置き場所 (Python の swim_worker/paths.py の frozen のときと同じ)。
//! 基準は実行ファイルのあるフォルダ。書き込むものは `data/` の下
//! (install.sh の systemd の unit は `data/` だけを書き込み可能にしている)。
//! どの関数もディレクトリは作らない。書く側が作る。
use std::path::{Path, PathBuf};

pub const BASE_ENV: &str = "SWIM_WORKER_BASE";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Paths {
    base: PathBuf,
}

impl Paths {
    pub fn new(base: PathBuf) -> Self {
        Self { base }
    }

    /// `SWIM_WORKER_BASE` (開発・テスト用) があればそこ、なければ実行ファイルのフォルダ
    pub fn from_env_or_exe(override_base: Option<String>) -> Self {
        if let Some(b) = override_base.filter(|s| !s.is_empty()) {
            return Self::new(PathBuf::from(b));
        }
        let exe = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("."));
        Self::new(exe.parent().map(Path::to_path_buf).unwrap_or_else(|| PathBuf::from(".")))
    }

    pub fn current() -> Self {
        Self::from_env_or_exe(std::env::var(BASE_ENV).ok())
    }

    pub fn base(&self) -> &Path {
        &self.base
    }
    pub fn data_dir(&self) -> PathBuf {
        self.base.join("data")
    }
    /// 起動成功の印。更新後の巻き戻しの判定 (GUI の差し替え・install.sh --auto) が見る
    pub fn startup_marker(&self) -> PathBuf {
        self.data_dir().join(".startup_ok")
    }
    /// 前回のプロセスの instance token (自分の前の世代が残した heartbeat を回収するため)
    pub fn last_instance_token(&self) -> PathBuf {
        self.data_dir().join(".last_instance_token")
    }
    /// SWIM の Cookie の保存先。override (COOKIE_FILE) があればそれ
    pub fn cookie_file(&self, override_path: &str) -> PathBuf {
        if override_path.is_empty() {
            self.data_dir().join(".swim_cookies.json")
        } else {
            PathBuf::from(override_path)
        }
    }
    /// 同じマシンでの多重起動を防ぐロックファイル
    pub fn lock(&self) -> PathBuf {
        self.data_dir().join("swim-worker.lock")
    }
}
