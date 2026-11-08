//! パーサーの診断 (Python の `parsers/diagnostics.py`)。既知のキー以外に実データがあるとき・
//! `a or b` で択一しているキーが同時に非空のときに、`SWIM_PARSER_DIAG_DIR` の
//! `{job_type}_unknown_samples/` に Python と同じ名前・中身で保存する (未設定なら DEBUG のログだけ)。
use std::io;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use crate::pycompat::{char_len, py_repr, repr_str};
use crate::pyjson;

/// dict として読めるもの (`Value` か、parser が受け取った raw の `Map`)
pub trait AsDict {
    fn as_dict(&self) -> Option<&Map<String, Value>>;
}

impl AsDict for Value {
    fn as_dict(&self) -> Option<&Map<String, Value>> {
        self.as_object()
    }
}

impl AsDict for std::borrow::Cow<'_, Value> {
    fn as_dict(&self) -> Option<&Map<String, Value>> {
        self.as_object()
    }
}

impl AsDict for Map<String, Value> {
    fn as_dict(&self) -> Option<&Map<String, Value>> {
        Some(self)
    }
}

const LOG: &str = "swim_parsers::diagnostics";
pub const DIAG_DIR_ENV: &str = "SWIM_PARSER_DIAG_DIR";
pub const MAX_SAMPLES_PER_JOB_TYPE: usize = 500;
pub const MAX_SAMPLE_BYTES: usize = 1024 * 1024;

/// 保存先と上限 (Python は環境変数とモジュールの定数。テストは monkeypatch で変える)
#[derive(Debug, Clone)]
pub struct Diag {
    pub dir: Option<PathBuf>,
    pub max_samples: usize,
    pub max_bytes: usize,
}

impl Diag {
    pub fn from_env() -> Self {
        let dir = std::env::var(DIAG_DIR_ENV)
            .ok()
            .map(|v| crate::parsers::py::strip(&v).to_string())
            .filter(|v| !v.is_empty());
        Diag {
            dir: dir.map(PathBuf::from),
            max_samples: MAX_SAMPLES_PER_JOB_TYPE,
            max_bytes: MAX_SAMPLE_BYTES,
        }
    }

    /// known・ignored 以外のキーに実データ (None・[]・{} 以外) があれば保存する
    pub fn check_unknown_keys<D: AsDict + ?Sized>(&self, job_type: &str, raw: &D, known: &[&str], ignored: &[&str]) {
        let Some(m) = raw.as_dict() else { return };
        let unknown: Vec<&String> = m
            .iter()
            .filter(|(k, v)| !known.contains(&k.as_str()) && !ignored.contains(&k.as_str()) && !is_empty_like(v))
            .map(|(k, _)| k)
            .collect();
        if unknown.is_empty() {
            return;
        }
        let msg = format!("{job_type}: 未知カテゴリに実データを検出: keys={}", keys_repr(&unknown));
        if self.dir.is_some() {
            tracing::info!(target: LOG, "{msg}");
        } else {
            tracing::debug!(target: LOG, "{msg}");
        }
        self.save(job_type, "unknown_keys", m, &unknown);
    }

    /// `a or b or c` で択一しているキーのうち、複数が同時に非空なら保存する (後ろのキーが黙って捨てられる)
    pub fn check_key_collision<D: AsDict + ?Sized>(&self, job_type: &str, raw: &D, candidates: &[&str]) {
        let Some(m) = raw.as_dict() else { return };
        let populated: Vec<&String> = candidates
            .iter()
            .filter_map(|k| {
                m.get_key_value(*k)
                    .filter(|(_, v)| crate::pycompat::py_truthy(v))
                    .map(|(k, _)| k)
            })
            .collect();
        if populated.len() <= 1 {
            return;
        }
        let msg = format!(
            "{job_type}: 複数の既知キーが同時に非空 (orチェーンでデータロスの恐れ): keys={}",
            keys_repr(&populated)
        );
        if self.dir.is_some() {
            tracing::warn!(target: LOG, "{msg}");
        } else {
            tracing::debug!(target: LOG, "{msg}");
        }
        self.save(job_type, "key_collision", m, &populated);
    }

    // 保存するときだけ値を写す (無効なら名前だけで足りる)
    fn save(&self, job_type: &str, tag: &str, raw: &Map<String, Value>, keys: &[&String]) {
        let Some(base) = &self.dir else {
            tracing::debug!(target: LOG, "{job_type}: 診断サンプル保存はスキップ ({DIAG_DIR_ENV} 未設定)");
            return;
        };
        let safe: String = job_type
            .chars()
            .map(|c| {
                if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                    c
                } else {
                    '_'
                }
            })
            .take(64)
            .collect();
        let directory = base.join(format!("{safe}_unknown_samples"));
        let payload = Value::Object(keys.iter().map(|k| ((*k).clone(), raw[k.as_str()].clone())).collect());
        if let Err(e) = self.write(job_type, tag, &payload, &directory) {
            // 診断は本処理に影響させない。1 行で
            tracing::warn!(target: LOG, "{job_type}: 診断サンプルの保存に失敗 ({}): {e}", directory.display());
        }
    }

    fn write(&self, job_type: &str, tag: &str, payload: &Value, directory: &Path) -> Result<(), String> {
        makedirs(directory).map_err(|(e, p)| os_error(&e, Some(&p)))?;
        let count = std::fs::read_dir(directory)
            .map_err(|e| os_error(&e, Some(directory)))?
            .count();
        if count >= self.max_samples {
            tracing::debug!(target: LOG, "{job_type}: 診断サンプルが上限 ({} 件) のため保存しない", self.max_samples);
            return Ok(());
        }
        let mut text = pyjson::dumps_unicode_indent(payload, 2);
        if text.len() > self.max_bytes {
            let mut keys: Vec<&String> = payload.as_object().into_iter().flat_map(|m| m.keys()).collect();
            keys.sort();
            keys.truncate(100);
            text = pyjson::dumps_unicode_indent(&json!({"truncated": true, "keys": keys, "size": char_len(&text)}), 2);
        }
        let ts = chrono::Utc::now().format("%Y%m%dT%H%M%S%6f");
        let path = directory.join(format!("{tag}_{ts}.json"));
        std::fs::write(&path, text).map_err(|e| os_error(&e, Some(&path)))?;
        tracing::info!(target: LOG, "{job_type}: 診断サンプルを保存: {}", path.display());
        Ok(())
    }
}

pub fn check_unknown_keys<D: AsDict + ?Sized>(job_type: &str, raw: &D, known: &[&str], ignored: &[&str]) {
    Diag::from_env().check_unknown_keys(job_type, raw, known, ignored);
}

pub fn check_key_collision<D: AsDict + ?Sized>(job_type: &str, raw: &D, candidates: &[&str]) {
    Diag::from_env().check_key_collision(job_type, raw, candidates);
}

// `v not in (None, [], {})`
fn is_empty_like(v: &Value) -> bool {
    match v {
        Value::Null => true,
        Value::Array(a) => a.is_empty(),
        Value::Object(m) => m.is_empty(),
        _ => false,
    }
}

// `list(d.keys())` の repr
fn keys_repr(keys: &[&String]) -> String {
    py_repr(&Value::Array(
        keys.iter().map(|k| Value::String((*k).clone())).collect(),
    ))
}

// os.makedirs(exist_ok=True)。失敗したら mkdir したその場所と一緒に返す
fn makedirs(p: &Path) -> Result<(), (io::Error, PathBuf)> {
    if let Some(head) = p.parent().filter(|h| !h.as_os_str().is_empty()) {
        if !head.exists() {
            match makedirs(head) {
                Err((e, _)) if e.kind() == io::ErrorKind::AlreadyExists => {}
                r => r?,
            }
        }
    }
    match std::fs::create_dir(p) {
        Err(_) if p.is_dir() => Ok(()),
        r => r.map_err(|e| (e, p.to_path_buf())),
    }
}

// Python の OSError の str: `[Errno 20] Not a directory: '/path'`
fn os_error(e: &io::Error, path: Option<&Path>) -> String {
    let Some(code) = e.raw_os_error() else {
        return e.to_string();
    };
    let text = e.to_string();
    let strerror = text.strip_suffix(&format!(" (os error {code})")).unwrap_or(&text);
    match path {
        Some(p) => format!("[Errno {code}] {strerror}: {}", repr_str(&p.display().to_string())),
        None => format!("[Errno {code}] {strerror}"),
    }
}
