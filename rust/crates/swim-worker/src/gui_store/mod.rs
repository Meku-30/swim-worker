//! GUI の設定の読み書き (Python の settings_store.py と同じ形)。
//! - `.env` は GUI と CLI で共通 (KEY=VALUE)。値は単引用符で囲み `\\` と `\'` だけをエスケープ。
//!   手で足したキー・コメントは残す。同じフォルダの一時ファイルに書いてから置き換える
//! - パスワード (Redis・SWIM) は資格情報マネージャー (Windows) に置き、`.env` には書かない。
//!   使えない・失敗したときは `.env` に書く
//! - 既存の `.env` にパスワードがあれば資格情報マネージャーに移し、読み戻せたら `.env` から消す
//! - `data/gui_settings.json` (auto_update・settings_collapsed・rollback_count)、`data/update_snooze.json`
//!
//! Python 版に戻った (巻き戻し・出し直し) ときも同じ設定を読めるよう、書く形は Python と同じにする
pub mod secrets;

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use chrono::{DateTime, Duration, Utc};
use serde_json::{json, Map, Value};

use secrets::{CredBackend, Keyring};

const LOG: &str = "swim_worker.settings_store";
pub const KEYRING_SERVICE: &str = "swim-worker";
pub const SECRET_STORE_ID_KEY: &str = "SWIM_WORKER_SECRET_ID";
const REDIS_PORT: &str = "6380";
pub const ROLLBACK_DISABLE_THRESHOLD: u64 = 2;
pub const SNOOZE_HOURS: i64 = 24;

/// 画面の欄
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Fields {
    pub redis_host: String,
    pub redis_username: String,
    pub redis_password: String,
    pub swim_username: String,
    pub swim_password: String,
    pub worker_name: String,
}

/// 欄の名前と `.env` のキー
const FIELDS: [(&str, &str); 6] = [
    ("redis_host", "REDIS_HOST"),
    ("redis_username", "REDIS_USERNAME"),
    ("redis_password", "REDIS_PASSWORD"),
    ("swim_username", "SWIM_USERNAME"),
    ("swim_password", "SWIM_PASSWORD"),
    ("worker_name", "WORKER_NAME"),
];
const SECRET_FIELDS: [&str; 2] = ["redis_password", "swim_password"];

impl Fields {
    pub fn get(&self, name: &str) -> &str {
        match name {
            "redis_host" => &self.redis_host,
            "redis_username" => &self.redis_username,
            "redis_password" => &self.redis_password,
            "swim_username" => &self.swim_username,
            "swim_password" => &self.swim_password,
            "worker_name" => &self.worker_name,
            _ => "",
        }
    }

    fn set(&mut self, name: &str, v: String) {
        match name {
            "redis_host" => self.redis_host = v,
            "redis_username" => self.redis_username = v,
            "redis_password" => self.redis_password = v,
            "swim_username" => self.swim_username = v,
            "swim_password" => self.swim_password = v,
            "worker_name" => self.worker_name = v,
            _ => {}
        }
    }
}

fn env_key(field: &str) -> &'static str {
    FIELDS.iter().find(|(f, _)| *f == field).map_or("", |(_, k)| k)
}

/// .env に書く値 (単引用符。python-dotenv と同じく \ と ' だけをエスケープ)
pub fn quote_value(v: &str) -> String {
    format!("'{}'", v.replace('\\', "\\\\").replace('\'', "\\'"))
}

fn unescape(s: &str, q: char) -> String {
    let mut out = String::new();
    let mut it = s.chars().peekable();
    while let Some(c) = it.next() {
        if c == '\\' {
            if let Some(&n) = it.peek() {
                if n == '\\' || n == q {
                    out.push(n);
                    it.next();
                    continue;
                }
            }
        }
        out.push(c);
    }
    out
}

fn unquote(raw: &str) -> String {
    let raw = raw.trim();
    let n = raw.chars().count();
    if n >= 2 && raw.starts_with('\'') && raw.ends_with('\'') {
        return unescape(&raw[1..raw.len() - 1], '\'');
    }
    if n >= 2 && raw.starts_with('"') && raw.ends_with('"') {
        return unescape(&raw[1..raw.len() - 1], '"').replace("\\n", "\n");
    }
    // 引用符なし: v1.1 までの GUI が書いた形。値はそのまま (# もコメント扱いしない)
    raw.to_string()
}

/// `^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$` と同じ
fn parse_line(line: &str) -> Option<(String, &str)> {
    let mut s = line.trim_start();
    if let Some(rest) = s.strip_prefix("export") {
        if rest.starts_with(|c: char| c.is_whitespace()) {
            s = rest.trim_start();
        }
    }
    let mut chars = s.char_indices();
    let (_, first) = chars.next()?;
    if !(first.is_ascii_alphabetic() || first == '_') {
        return None;
    }
    let end = s.find(|c: char| !(c.is_ascii_alphanumeric() || c == '_')).unwrap_or(s.len());
    let key = &s[..end];
    let rest = s[end..].trim_start();
    let val = rest.strip_prefix('=')?;
    Some((key.to_string(), val))
}

/// .env を (KEY, 値) の並びにする (1 行 1 キー。後に書いたものが勝つ)。読めなければ空
pub fn read_env_values(path: &Path) -> Vec<(String, String)> {
    read_env_values_checked(path).unwrap_or_default()
}

/// .env の中身。無ければ空。UTF-8 でない・読めないときはエラー (空として扱って保存で中身を捨てないため)
fn read_env_text(path: &Path) -> std::io::Result<String> {
    match std::fs::read_to_string(path) {
        Ok(t) => Ok(t),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(String::new()),
        Err(e) => Err(e),
    }
}

/// read_env_values と同じ。ただし読めないときはエラー
pub fn read_env_values_checked(path: &Path) -> std::io::Result<Vec<(String, String)>> {
    let text = read_env_text(path)?;
    let mut out: Vec<(String, String)> = Vec::new();
    for line in text.lines() {
        if line.trim().is_empty() || line.trim_start().starts_with('#') {
            continue;
        }
        if let Some((k, v)) = parse_line(line) {
            let v = unquote(v);
            if let Some(e) = out.iter_mut().find(|(ek, _)| *ek == k) {
                e.1 = v;
            } else {
                out.push((k, v));
            }
        }
    }
    Ok(out)
}

fn lookup<'a>(values: &'a [(String, String)], key: &str) -> Option<&'a str> {
    values.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str())
}

/// 同じフォルダの一時ファイル (Unix では 0600) に書いてから置き換える
pub fn write_private(path: &Path, text: &str) -> std::io::Result<()> {
    use std::io::Write;
    let dir = path.parent().unwrap_or(Path::new("."));
    std::fs::create_dir_all(dir)?;
    let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let tmp = dir.join(format!(".{name}.{}.tmp", uuid::Uuid::new_v4().simple()));
    let r = (|| {
        let mut o = std::fs::OpenOptions::new();
        o.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            o.mode(0o600);
        }
        let mut f = o.open(&tmp)?;
        f.write_all(text.as_bytes())?;
        f.sync_all()?;
        drop(f);
        std::fs::rename(&tmp, path)
    })();
    if r.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    r
}

/// .env の指定キーだけを書き換える (None は削除)。他の行・コメントはそのまま。
/// 値は書く値そのもの (quote_value 済み)。無いキーは末尾に足す
pub fn update_env_file(path: &Path, updates: &[(String, Option<String>)]) -> std::io::Result<()> {
    let text = read_env_text(path)?;
    let mut out: Vec<String> = Vec::new();
    let mut done: Vec<&str> = Vec::new();
    for line in text.lines() {
        let key = if line.trim_start().starts_with('#') { None } else { parse_line(line).map(|(k, _)| k) };
        if let Some(k) = key {
            if let Some((uk, uv)) = updates.iter().find(|(uk, _)| *uk == k) {
                if !done.contains(&uk.as_str()) {
                    if let Some(v) = uv {
                        out.push(format!("{k}={v}"));
                    }
                }
                done.push(uk.as_str()); // 同じキーの 2 行目以降は消す
                continue;
            }
        }
        out.push(line.to_string());
    }
    for (k, v) in updates {
        if !done.contains(&k.as_str()) {
            if let Some(v) = v {
                out.push(format!("{k}={v}"));
            }
        }
    }
    write_private(path, &(out.join("\n") + "\n"))
}

/// 画面の欄を .env と資格情報マネージャーに読み書きする
pub struct SettingsStore<'a> {
    env_path: PathBuf,
    keyring: Option<Keyring<'a>>,
    missing: Mutex<Vec<&'static str>>,
}

impl<'a> SettingsStore<'a> {
    pub fn new(env_path: PathBuf, cred: Option<&'a dyn CredBackend>) -> Self {
        Self { env_path, keyring: cred.map(Keyring::new), missing: Mutex::new(Vec::new()) }
    }

    pub fn uses_keyring(&self) -> bool {
        self.keyring.is_some()
    }

    /// load() が見つけた、以前資格情報マネージャーに置いたまま読めないパスワードの欄
    pub fn missing_secrets(&self) -> Vec<&'static str> {
        self.missing.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }

    fn user(store_id: &str, env_key: &str) -> String {
        format!("{store_id}:{env_key}")
    }

    fn store_id(values: &[(String, String)], updates: &mut Vec<(String, Option<String>)>) -> String {
        let cur = lookup(values, SECRET_STORE_ID_KEY).unwrap_or("");
        if cur.len() == 32 && cur.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
            return cur.to_string();
        }
        let id = uuid::Uuid::new_v4().simple().to_string();
        updates.push((SECRET_STORE_ID_KEY.into(), Some(quote_value(&id))));
        id
    }

    /// 資格情報マネージャーに置いて読み戻しで確かめる。空なら消す
    fn keyring_set(&self, kr: &Keyring<'_>, store_id: &str, env_key: &str, value: &str) -> bool {
        let user = Self::user(store_id, env_key);
        if value.is_empty() {
            let _ = kr.delete_password(KEYRING_SERVICE, &user);
            return true;
        }
        match kr.set_password(KEYRING_SERVICE, &user, value).and_then(|_| kr.get_password(KEYRING_SERVICE, &user)) {
            Ok(Some(v)) => v == value,
            Ok(None) => false,
            Err(e) => {
                tracing::warn!(target: LOG, "keyring への保存に失敗 ({env_key}): {}", e.kind());
                false
            }
        }
    }

    /// (欄の値, AUTO_CONNECT) を返す。.env に残っているパスワードは資格情報マネージャーへ移す
    pub fn load(&self) -> std::io::Result<(Fields, Option<bool>)> {
        let values = read_env_values_checked(&self.env_path)?;
        let mut f = Fields::default();
        for (field, key) in FIELDS {
            f.set(field, lookup(&values, key).unwrap_or("").to_string());
        }
        let auto = lookup(&values, "AUTO_CONNECT").map(|a| a.trim().eq_ignore_ascii_case("true"));
        let Some(kr) = &self.keyring else {
            let mut miss = Vec::new();
            if lookup(&values, SECRET_STORE_ID_KEY).is_some_and(|v| !v.is_empty()) {
                miss = SECRET_FIELDS.iter().copied().filter(|s| f.get(s).is_empty()).collect();
                if !miss.is_empty() {
                    tracing::warn!(target: LOG, "保存したパスワードを読めません (以前の版は OS の資格情報ストアに保存していました)。設定画面で入れ直して保存してください");
                }
            }
            *self.missing.lock().unwrap_or_else(|e| e.into_inner()) = miss;
            return Ok((f, auto));
        };
        let mut updates: Vec<(String, Option<String>)> = Vec::new();
        let mut store_id = lookup(&values, SECRET_STORE_ID_KEY).unwrap_or("").to_string();
        if SECRET_FIELDS.iter().any(|s| lookup(&values, env_key(s)).is_some_and(|v| !v.is_empty())) {
            store_id = Self::store_id(&values, &mut updates);
        }
        for field in SECRET_FIELDS {
            let key = env_key(field);
            if let Some(in_env) = lookup(&values, key).filter(|v| !v.is_empty()) {
                // 移行: 資格情報マネージャーに置いて読み戻せたら .env から消す
                if self.keyring_set(kr, &store_id, key, in_env) {
                    updates.push((key.into(), None));
                    tracing::info!(target: LOG, "{key} を .env から OS の資格情報ストアに移しました");
                }
                f.set(field, in_env.to_string());
                continue;
            }
            if store_id.is_empty() {
                continue;
            }
            match kr.get_password(KEYRING_SERVICE, &Self::user(&store_id, key)) {
                Ok(Some(v)) => f.set(field, v),
                Ok(None) => {}
                Err(e) => tracing::warn!(target: LOG, "keyring から {key} を読めません: {}", e.kind()),
            }
        }
        if updates.iter().any(|(_, v)| v.is_none()) && self.env_path.exists() {
            if let Err(e) = update_env_file(&self.env_path, &updates) {
                tracing::warn!(target: LOG, ".env の書き換えに失敗 (パスワードは .env に残ります): {e}");
            }
        }
        Ok((f, auto))
    }

    /// 欄の値を保存する。パスワードの置き場所 ("keyring" / "env") を返す
    pub fn save(&self, f: &Fields, auto_connect: bool) -> std::io::Result<&'static str> {
        // 読めない .env (UTF-8 でないなど) は書き換えない
        read_env_text(&self.env_path)?;
        let mut updates: Vec<(String, Option<String>)> = Vec::new();
        for (field, key) in FIELDS {
            if SECRET_FIELDS.contains(&field) {
                continue;
            }
            updates.push((key.into(), Some(quote_value(f.get(field).trim()))));
        }
        updates.push(("REDIS_PORT".into(), Some(quote_value(REDIS_PORT))));
        updates.push(("AUTO_CONNECT".into(), Some(quote_value(if auto_connect { "true" } else { "false" }))));
        let mut where_ = if self.keyring.is_some() { "keyring" } else { "env" };
        let store_id = match &self.keyring {
            Some(_) => Self::store_id(&read_env_values_checked(&self.env_path)?, &mut updates),
            None => String::new(),
        };
        for field in SECRET_FIELDS {
            let key = env_key(field);
            let v = f.get(field); // パスワードは strip しない
            let stored = self.keyring.as_ref().is_some_and(|kr| self.keyring_set(kr, &store_id, key, v));
            if stored {
                updates.push((key.into(), None));
            } else {
                if self.keyring.is_some() {
                    where_ = "env";
                }
                updates.push((key.into(), Some(quote_value(v))));
            }
        }
        update_env_file(&self.env_path, &updates)?;
        Ok(where_)
    }

    /// .env の AUTO_CONNECT だけを書き換える。.env が無ければ何もしない (false)
    pub fn set_auto_connect(&self, v: bool) -> std::io::Result<bool> {
        if !self.env_path.exists() {
            return Ok(false);
        }
        update_env_file(&self.env_path, &[("AUTO_CONNECT".into(), Some(quote_value(if v { "true" } else { "false" })))])?;
        Ok(true)
    }
}

/// JSON ファイル (無い・壊れていれば空)
pub fn load_json(path: &Path) -> Map<String, Value> {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| match v {
            Value::Object(m) => Some(m),
            _ => None,
        })
        .unwrap_or_default()
}

/// JSON ファイル (Python の json.dumps(ensure_ascii=False, indent=2) と同じ形)
pub fn save_json(path: &Path, m: &Map<String, Value>) -> std::io::Result<()> {
    let text = serde_json::to_string_pretty(&Value::Object(m.clone())).map_err(std::io::Error::other)?;
    write_private(path, &text)
}

/// data/gui_settings.json。知らないキーは残す
#[derive(Debug, Clone, Default)]
pub struct GuiPrefs(Map<String, Value>);

impl GuiPrefs {
    pub fn load(path: &Path) -> Self {
        Self(load_json(path))
    }

    pub fn from_value(v: Value) -> Self {
        match v {
            Value::Object(m) => Self(m),
            _ => Self::default(),
        }
    }

    pub fn value(&self) -> Value {
        Value::Object(self.0.clone())
    }

    pub fn save(&self, path: &Path) -> std::io::Result<()> {
        save_json(path, &self.0)
    }

    pub fn auto_update(&self) -> bool {
        self.0.get("auto_update").and_then(Value::as_bool).unwrap_or(false)
    }

    pub fn set_auto_update(&mut self, v: bool) {
        self.0.insert("auto_update".into(), json!(v));
    }

    pub fn settings_collapsed(&self) -> bool {
        self.0.get("settings_collapsed").and_then(Value::as_bool).unwrap_or(false)
    }

    pub fn set_settings_collapsed(&mut self, v: bool) {
        self.0.insert("settings_collapsed".into(), json!(v));
    }
}

/// 巻き戻しを数え、同じ版で 2 回になったら自動適用を止める。止めたら true (gui_helpers.next_rollback_state)
pub fn next_rollback_state(g: &mut GuiPrefs, from_version: &str) -> bool {
    let mut counts = g.0.get("rollback_count").and_then(Value::as_object).cloned().unwrap_or_default();
    let n = counts.get(from_version).and_then(Value::as_u64).unwrap_or(0) + 1;
    counts.insert(from_version.into(), json!(n));
    g.0.insert("rollback_count".into(), Value::Object(counts));
    let disable = n >= ROLLBACK_DISABLE_THRESHOLD && g.auto_update();
    if disable {
        g.set_auto_update(false);
    }
    disable
}

/// 「後で」を選んだ版を 24 時間聞かない (updater.SnoozeStore)
pub struct Snooze {
    path: PathBuf,
}

impl Snooze {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn clear(&self) {
        let _ = std::fs::remove_file(&self.path);
    }

    /// 指定の版がまだ待ちの間か。別の版・期限切れ・壊れたファイルは消す
    pub fn is_snoozed(&self, version: &str, now: DateTime<Utc>) -> bool {
        let m = load_json(&self.path);
        if m.is_empty() {
            return false;
        }
        if m.get("version").and_then(Value::as_str) != Some(version) {
            self.clear();
            return false;
        }
        let until = m.get("until").and_then(Value::as_str).and_then(|s| DateTime::parse_from_rfc3339(s).ok());
        match until {
            Some(u) if now < u.with_timezone(&Utc) => true,
            _ => {
                self.clear();
                false
            }
        }
    }

    /// 起動時の片付け: 期限切れ・壊れたものだけ消す (updater.SnoozeStore.cleanup_expired)
    pub fn cleanup_expired(&self, now: DateTime<Utc>) {
        if !self.path.exists() {
            return;
        }
        let m = load_json(&self.path);
        let until = m.get("until").and_then(Value::as_str).and_then(|s| DateTime::parse_from_rfc3339(s).ok());
        if until.is_none_or(|u| now >= u.with_timezone(&Utc)) {
            self.clear();
        }
    }

    pub fn set(&self, version: &str, now: DateTime<Utc>) {
        let until = now + Duration::hours(SNOOZE_HOURS);
        let mut m = Map::new();
        m.insert("version".into(), json!(version));
        // Python の datetime.isoformat() と同じく +00:00 の形 (Python の fromisoformat で読める)
        m.insert("until".into(), json!(until.to_rfc3339_opts(chrono::SecondsFormat::Micros, false)));
        match save_json(&self.path, &m) {
            Ok(()) => tracing::info!(target: "swim_worker.updater", "アップデート v{version} を {SNOOZE_HOURS} 時間スキップしました"),
            Err(e) => tracing::debug!(target: "swim_worker.updater", "snooze 保存失敗 (無視): {e}"),
        }
    }
}

/// 前回の更新が巻き戻った記録
#[derive(Debug, Clone, PartialEq)]
pub struct RollbackInfo {
    pub from: String,
    pub reason: String,
    /// 同じ版で 2 回目なので自動適用を止めた
    pub disabled: bool,
}

/// 起動したらすぐ: `data/.update_rollback.json` を読んで数え、snooze を置き、記録を消す (Python の
/// _check_rollback_marker_after_ready。ダイアログだけ後で出す)。Worker が新版を知るより前に済ませる
pub fn apply_rollback_marker(data_dir: &Path, snooze: &Snooze, now: DateTime<Utc>) -> Option<RollbackInfo> {
    let marker = data_dir.join(".update_rollback.json");
    if !marker.exists() {
        return None;
    }
    let info = load_json(&marker);
    let from = info.get("rolled_back_from").and_then(Value::as_str).unwrap_or("?").trim_start_matches('v').to_string();
    let reason = info.get("reason").and_then(Value::as_str).unwrap_or("").to_string();
    let prefs_path = data_dir.join("gui_settings.json");
    let mut g = GuiPrefs::load(&prefs_path);
    let disabled = next_rollback_state(&mut g, &from);
    if let Err(e) = g.save(&prefs_path) {
        tracing::debug!(target: LOG, "GUI 設定保存失敗 (無視): {e}");
    }
    if from != "?" {
        snooze.set(&from, now);
    }
    let _ = std::fs::remove_file(&marker);
    Some(RollbackInfo { from, reason, disabled })
}
