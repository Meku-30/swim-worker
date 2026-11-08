//! GUI のログ: ファイル (`swim-worker.log`、5MB × 3 世代。Python の RotatingFileHandler と同じ) と画面のログ欄へ。
//! ファイルは Python と同じ形 `2026-11-08 12:34:56,789 [INFO] swim_worker.consumer: 本文`、画面は `12:34:56 本文`
use std::fmt::Write as _;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

use tracing::field::{Field, Visit};
use tracing::{Event, Level, Subscriber};
use tracing_subscriber::layer::Context;
use tracing_subscriber::Layer;

pub const LOG_MAX_BYTES: u64 = 5 * 1024 * 1024;
pub const LOG_BACKUPS: u32 = 3;

/// 大きさが上限を超える前に `.1`・`.2`・`.3` へ回す
pub struct RotatingFile {
    path: PathBuf,
    max: u64,
    backups: u32,
    file: Option<std::fs::File>,
    size: u64,
}

impl RotatingFile {
    pub fn new(path: PathBuf, max: u64, backups: u32) -> Self {
        let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
        Self { path, max, backups, file: None, size }
    }

    fn backup(&self, n: u32) -> PathBuf {
        let mut s = self.path.as_os_str().to_owned();
        s.push(format!(".{n}"));
        PathBuf::from(s)
    }

    fn rotate(&mut self) {
        self.file = None;
        let _ = std::fs::remove_file(self.backup(self.backups));
        for n in (1..self.backups).rev() {
            let _ = std::fs::rename(self.backup(n), self.backup(n + 1));
        }
        let _ = std::fs::rename(&self.path, self.backup(1));
        self.size = 0;
    }

    /// 1 行書く (失敗は捨てる。ログで止まらない)
    pub fn write_line(&mut self, line: &str) {
        let n = line.len() as u64 + 1;
        if self.size > 0 && self.size + n > self.max {
            self.rotate();
        }
        if self.file.is_none() {
            self.file = std::fs::OpenOptions::new().create(true).append(true).open(&self.path).ok();
        }
        if let Some(f) = self.file.as_mut() {
            if writeln!(f, "{line}").is_ok() {
                self.size += n;
            }
        }
    }
}

struct Msg(String);

impl Visit for Msg {
    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        if field.name() == "message" {
            let _ = write!(self.0, "{value:?}");
        } else {
            let _ = write!(self.0, " {}={value:?}", field.name());
        }
    }
    fn record_str(&mut self, field: &Field, value: &str) {
        if field.name() == "message" {
            self.0.push_str(value);
        } else {
            let _ = write!(self.0, " {}={value}", field.name());
        }
    }
}

pub type LineSink = Box<dyn Fn(String) + Send + Sync>;

/// INFO 以上をファイルと画面へ
pub struct GuiLogLayer {
    file: Mutex<RotatingFile>,
    sink: LineSink,
}

impl GuiLogLayer {
    pub fn new(file: RotatingFile, sink: LineSink) -> Self {
        Self { file: Mutex::new(file), sink }
    }
}

impl<S: Subscriber> Layer<S> for GuiLogLayer {
    fn on_event(&self, event: &Event<'_>, _ctx: Context<'_, S>) {
        let meta = event.metadata();
        let level = match *meta.level() {
            Level::ERROR => "ERROR",
            Level::WARN => "WARNING",
            Level::INFO => "INFO",
            _ => return,
        };
        let mut m = Msg(String::new());
        event.record(&mut m);
        let now = chrono::Local::now();
        let line = format!("{},{:03} [{level}] {}: {}", now.format("%Y-%m-%d %H:%M:%S"), now.timestamp_subsec_millis(), meta.target(), m.0);
        self.file.lock().unwrap_or_else(|e| e.into_inner()).write_line(&line);
        (self.sink)(format!("{} {}", now.format("%H:%M:%S"), m.0));
    }
}
