//! GUI 版の自動更新 (Python の updater.py)。
//! 流れ: SHA256SUMS と SHA256SUMS.sig を取り、埋め込みの公開鍵で署名を確かめる (通らなければ本体を取る前に止まる)
//! → 本体を `.part` に書きつつ SHA-256 を計算 → 一致したら置く → 差し替えの .bat を GUI から切り離して起動
//! → GUI が終わる → .bat が exe を差し替えて起動し、起動の印 (data/.startup_ok) が 120 秒で出なければ戻す
pub mod verify;

use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use async_trait::async_trait;

use crate::autostart::bat_escape;
use crate::consumer::version::parse_version;

const LOG: &str = "swim_worker.updater";

pub const RELEASE_DOWNLOAD_BASE: &str = "https://github.com/Meku-30/swim-worker/releases/download";
pub const LATEST_RELEASE_URL: &str = "https://github.com/Meku-30/swim-worker/releases/latest";
pub const ASSET_NAME: &str = "swim-worker-windows.exe";
pub const STALE_UPDATE_FILE: Duration = Duration::from_secs(3 * 24 * 3600);
pub const MIN_ASSET_SIZE: u64 = 1024 * 1024;
pub const MAX_ASSET_SIZE: u64 = 300 * 1024 * 1024;
pub const MAX_SUMS_SIZE: usize = 64 * 1024;
pub const MAX_SIG_SIZE: usize = 1024;
/// 接続・無通信がこれだけ続いたら諦める
pub const CONNECT_STALL_TIMEOUT: Duration = Duration::from_secs(60);
/// 全体の上限 (遅い回線でも 30 分)
pub const MAX_DOWNLOAD: Duration = Duration::from_secs(30 * 60);

#[derive(Debug, thiserror::Error)]
pub enum UpdateError {
    #[error("{0}")]
    Verify(String),
    #[error("{0}")]
    Fetch(String),
    #[error("{0}")]
    Io(#[from] std::io::Error),
}

/// 進み具合 (GUI の進捗の窓へ)
#[derive(Debug, Clone, PartialEq)]
pub enum Progress {
    Phase(&'static str),
    Bytes { done: u64, total: Option<u64> },
}

/// 本体の取り出し (少しずつ)
#[async_trait]
pub trait ChunkStream: Send {
    fn status(&self) -> u16;
    fn total(&self) -> Option<u64>;
    /// 次のかたまり。終わりは None
    async fn next_chunk(&mut self) -> Result<Option<Vec<u8>>, String>;
}

/// 取り先 (本物は wreq、テストは偽物)
#[async_trait]
pub trait Fetcher: Send + Sync {
    /// 小さいファイル (SHA256SUMS など)。200 以外・max を超えたらエラー
    async fn get(&self, url: &str, max: usize) -> Result<Vec<u8>, String>;
    async fn stream(&self, url: &str) -> Result<Box<dyn ChunkStream>, String>;
}

/// 版のタグからダウンロードの URL (GitHub API を使わない)
pub fn download_url(version: &str) -> String {
    format!("{RELEASE_DOWNLOAD_BASE}/v{version}/{ASSET_NAME}")
}

/// ダウンロード先 (exe と同じフォルダ)
pub fn new_exe_path(base: &Path) -> PathBuf {
    base.join("swim-worker-gui.new.exe")
}

fn release_base_url(url: &str) -> &str {
    url.rsplit_once('/').map_or(url, |(b, _)| b)
}

/// 同じリリースの SHA256SUMS と .sig を取り、確かめてから SHA256SUMS を返す。
/// (1) 公開鍵での署名、(2) 先頭行の版が URL のタグと一致、(3) 今の版より新しい
pub async fn fetch_checksums(f: &dyn Fetcher, base_url: &str, current: &str, keys: &[&str]) -> Result<String, UpdateError> {
    let tag = base_url.trim_end_matches('/').rsplit('/').next().unwrap_or("");
    let new = parse_version(tag).filter(|_| tag.starts_with('v'));
    let Some(new) = new else {
        return Err(UpdateError::Verify(format!("ダウンロード先のタグが不正です: {tag:?}")));
    };
    match parse_version(current) {
        Some(cur) if new > cur => {}
        _ => return Err(UpdateError::Verify(format!("{tag} は今の版 (v{current}) より新しくないため更新しません"))),
    }
    let sums = f.get(&format!("{base_url}/SHA256SUMS"), MAX_SUMS_SIZE).await.map_err(|e| UpdateError::Fetch(format!("SHA256SUMS 取得失敗: {e}")))?;
    let sig = f
        .get(&format!("{base_url}/SHA256SUMS.sig"), MAX_SIG_SIZE)
        .await
        .map_err(|e| UpdateError::Verify(format!("SHA256SUMS.sig を取得できません (署名のないリリース): {e}")))?;
    let i = verify::verify_sums_signature(&sums, &sig, keys)?;
    verify::check_release_line(&sums, tag)?;
    tracing::info!(target: LOG, "SHA256SUMS の署名 OK ({tag}, {} 本目の鍵)", i + 1);
    String::from_utf8(sums).map_err(|_| UpdateError::Verify("SHA256SUMS が UTF-8 ではありません".into()))
}

/// exe を取り、署名を確かめた SHA256SUMS と一致したら dest に置く。途中で失敗したら何も残さない。書いたバイト数を返す
pub async fn download_and_verify(
    f: &dyn Fetcher,
    url: &str,
    dest: &Path,
    current: &str,
    keys: &[&str],
    progress: &(dyn Fn(Progress) + Sync),
) -> Result<u64, UpdateError> {
    let asset = url.rsplit('/').next().unwrap_or("");
    progress(Progress::Phase("チェックサムを取得しています"));
    let sums = fetch_checksums(f, release_base_url(url), current, keys).await?;
    let expected = verify::parse_sha256sums(&sums)
        .get(asset)
        .cloned()
        .ok_or_else(|| UpdateError::Verify(format!("SHA256SUMS に {asset} のエントリがありません")))?;
    progress(Progress::Phase("新しいバージョンを取得しています"));
    let part = dest.with_file_name(format!("{}.part", dest.file_name().map(|n| n.to_string_lossy()).unwrap_or_default()));
    let r = download_inner(f, url, &part, &expected, asset, progress).await;
    match r {
        Ok(n) => {
            std::fs::rename(&part, dest)?;
            Ok(n)
        }
        Err(e) => {
            let _ = std::fs::remove_file(&part);
            Err(e)
        }
    }
}

async fn download_inner(
    f: &dyn Fetcher,
    url: &str,
    part: &Path,
    expected: &str,
    asset: &str,
    progress: &(dyn Fn(Progress) + Sync),
) -> Result<u64, UpdateError> {
    use std::io::Write;
    let started = tokio::time::Instant::now();
    let mut s = tokio::time::timeout(CONNECT_STALL_TIMEOUT, f.stream(url))
        .await
        .map_err(|_| UpdateError::Fetch("ダウンロードの接続が時間切れになりました".into()))?
        .map_err(UpdateError::Fetch)?;
    if s.status() != 200 {
        return Err(UpdateError::Fetch(format!("ダウンロード失敗: status={}", s.status())));
    }
    let total = s.total();
    if total.is_some_and(|t| t > MAX_ASSET_SIZE) {
        return Err(UpdateError::Fetch(format!("ダウンロードが大きすぎます: {} bytes", total.unwrap_or(0))));
    }
    let mut file = std::fs::File::create(part)?;
    let mut ctx = ring::digest::Context::new(&ring::digest::SHA256);
    let mut done = 0u64;
    let mut last_report: Option<tokio::time::Instant> = None;
    loop {
        let next = tokio::time::timeout(CONNECT_STALL_TIMEOUT, s.next_chunk())
            .await
            .map_err(|_| UpdateError::Fetch("ダウンロードが止まったため中止しました".into()))?
            .map_err(UpdateError::Fetch)?;
        let Some(chunk) = next else { break };
        if chunk.is_empty() {
            continue;
        }
        done += chunk.len() as u64;
        if done > MAX_ASSET_SIZE {
            return Err(UpdateError::Fetch(format!("ダウンロードが大きすぎます: {MAX_ASSET_SIZE} bytes 超")));
        }
        let now = tokio::time::Instant::now();
        if now - started > MAX_DOWNLOAD {
            return Err(UpdateError::Fetch("ダウンロードに時間がかかりすぎたため中止しました".into()));
        }
        file.write_all(&chunk)?;
        ctx.update(&chunk);
        if last_report.is_none_or(|t| now - t >= Duration::from_millis(200)) {
            last_report = Some(now);
            progress(Progress::Bytes { done, total });
        }
    }
    file.flush()?;
    file.sync_all()?;
    drop(file);
    progress(Progress::Bytes { done, total: Some(total.unwrap_or(done)) });
    if done < MIN_ASSET_SIZE {
        return Err(UpdateError::Fetch(format!("ダウンロードサイズ異常: {done} bytes")));
    }
    progress(Progress::Phase("整合性を検証しています"));
    let actual: String = ctx.finish().as_ref().iter().map(|b| format!("{b:02x}")).collect();
    if actual != expected {
        return Err(UpdateError::Verify(format!(
            "SHA256 不一致: {asset} (expected={}…, actual={}…)",
            &expected[..expected.len().min(16)],
            &actual[..16]
        )));
    }
    tracing::info!(target: LOG, "SHA256 検証 OK: {asset} ({}…)", &actual[..16]);
    Ok(done)
}

/// 差し替えの .bat が使う場所
#[derive(Debug, Clone)]
pub struct HelperPaths {
    pub base: PathBuf,
    pub current_exe: PathBuf,
    pub new_exe: PathBuf,
    pub old_exe: PathBuf,
    pub startup_ok: PathBuf,
    pub rollback_marker: PathBuf,
    pub log_path: PathBuf,
}

impl HelperPaths {
    pub fn for_exe(base: &Path, current_exe: &Path) -> Self {
        let mut old = current_exe.as_os_str().to_owned();
        old.push(".old");
        Self {
            base: base.to_path_buf(),
            current_exe: current_exe.to_path_buf(),
            new_exe: new_exe_path(base),
            old_exe: PathBuf::from(old),
            startup_ok: base.join("data").join(".startup_ok"),
            rollback_marker: base.join("data").join(".update_rollback.json"),
            log_path: base.join("swim-worker-update.log"),
        }
    }
}

/// 差し替えの .bat (CRLF)。Python の build_windows_update_script から PyInstaller の環境変数の 5 行を除いたもの。
/// 旧 exe が放されるまで move を 30 回まで試し、新 exe を起動して起動の印を約 120 秒待つ。
/// 出なければ旧 exe に戻し、`.update_rollback.json` を書く (move に失敗したときも旧 exe を起動して印を書く)
pub fn helper_bat(p: &HelperPaths, new_version: &str) -> String {
    let e = |x: &Path| bat_escape(&x.to_string_lossy());
    let (base, cur, new, old, ok, marker, log) =
        (e(&p.base), e(&p.current_exe), e(&p.new_exe), e(&p.old_exe), e(&p.startup_ok), e(&p.rollback_marker), e(&p.log_path));
    // 親のフォルダ・exe の名前は Windows のパスとして切る (Linux で組み立てても同じになるように \ でも切る)
    let marker_s = p.rollback_marker.to_string_lossy();
    let marker_dir = bat_escape(marker_s.rsplit_once(['\\', '/']).map_or("", |(d, _)| d));
    let cur_s = p.current_exe.to_string_lossy();
    let exe_name = bat_escape(cur_s.rsplit(['\\', '/']).next().unwrap_or(&cur_s));
    [
        "@echo off".to_string(),
        format!(r#"echo [%DATE% %TIME%] update start > "{log}""#),
        "set /a COUNT=0".into(),
        ":retry".into(),
        "set /a COUNT+=1".into(),
        format!(r#"echo [%DATE% %TIME%] attempt %COUNT% >> "{log}""#),
        "if %COUNT% gtr 30 goto fail".into(),
        "ping 127.0.0.1 -n 2 > nul".into(),
        format!(r#"copy /Y "{cur}" "{old}" >> "{log}" 2>&1"#),
        format!(r#"move /Y "{new}" "{cur}" >> "{log}" 2>&1"#),
        "if errorlevel 1 goto retry".into(),
        format!(r#"echo [%DATE% %TIME%] move success >> "{log}""#),
        format!(r#"if exist "{ok}" del "{ok}" >> "{log}" 2>&1"#),
        "ping 127.0.0.1 -n 2 > nul".into(),
        format!(r#"start "" /D "{base}" "{cur}""#),
        format!(r#"echo [%DATE% %TIME%] new exe started >> "{log}""#),
        "set /a WAIT=0".into(),
        ":waitok".into(),
        "set /a WAIT+=1".into(),
        "if %WAIT% gtr 120 goto rollback".into(),
        "ping 127.0.0.1 -n 2 > nul".into(),
        format!(r#"if exist "{ok}" goto success"#),
        "goto waitok".into(),
        ":success".into(),
        format!(r#"echo [%DATE% %TIME%] startup OK after %WAIT%s >> "{log}""#),
        format!(r#"if exist "{old}" del "{old}" >> "{log}" 2>&1"#),
        r#"del "%~f0""#.into(),
        "exit /b 0".into(),
        ":rollback".into(),
        format!(r#"echo [%DATE% %TIME%] ROLLBACK: startup_ok not found in 120s >> "{log}""#),
        format!(r#"taskkill /F /IM "{exe_name}" >> "{log}" 2>&1"#),
        "ping 127.0.0.1 -n 3 > nul".into(),
        format!(r#"move /Y "{old}" "{cur}" >> "{log}" 2>&1"#),
        format!(r#"mkdir "{marker_dir}" 2>nul"#),
        format!(r#"echo {{"rolled_back_from":"v{new_version}"}} > "{marker}""#),
        format!(r#"start "" /D "{base}" "{cur}""#),
        r#"del "%~f0""#.into(),
        "exit /b 0".into(),
        ":fail".into(),
        format!(r#"echo [%DATE% %TIME%] FAILED after %COUNT% attempts, restarting current exe >> "{log}""#),
        format!(r#"mkdir "{marker_dir}" 2>nul"#),
        format!(r#"echo {{"rolled_back_from":"v{new_version}","reason":"move_failed"}} > "{marker}""#),
        format!(r#"start "" /D "{base}" "{cur}""#),
        r#"del "%~f0""#.into(),
        "exit /b 1".into(),
    ]
    .iter()
    .map(|l| format!("{l}\r\n"))
    .collect()
}

/// 差し替えの .bat を書いて、GUI から切り離したプロセスで起動する
pub fn launch_helper(p: &HelperPaths, new_version: &str) -> std::io::Result<()> {
    let script = p.base.join("swim-worker-update.bat");
    std::fs::write(&script, crate::autostart::encode_bat(&helper_bat(p, new_version), &crate::autostart::system_ansi))?;
    let mut cmd = std::process::Command::new("cmd");
    cmd.arg("/c").arg(&script);
    cmd.stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        const CREATE_BREAKAWAY_FROM_JOB: u32 = 0x0100_0000;
        cmd.creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB);
    }
    cmd.spawn()?;
    Ok(())
}

/// 3 日より古い `*.old`・`*.new`・`*.new.exe`・`*.part` を消す (起動時の片付け)
pub fn cleanup_stale_update_files(base: &Path, now: SystemTime) {
    let Ok(rd) = std::fs::read_dir(base) else { return };
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if !(name.ends_with(".old") || name.ends_with(".new") || name.ends_with(".new.exe") || name.ends_with(".part")) {
            continue;
        }
        let old = e.metadata().and_then(|m| m.modified()).ok().and_then(|m| now.duration_since(m).ok());
        if old.is_some_and(|d| d > STALE_UPDATE_FILE) && std::fs::remove_file(e.path()).is_ok() {
            tracing::debug!(target: LOG, "古いアップデートファイルを削除: {name}");
        }
    }
}

/// 本物の取り先 (wreq の Chrome の形。Python は curl_cffi の chrome136)
pub struct WreqFetcher {
    client: wreq::Client,
}

impl WreqFetcher {
    pub fn new() -> Result<Self, String> {
        let (client, _jar) = swim_http::build_client().map_err(|e| e.to_string())?;
        Ok(Self { client })
    }
}

struct WreqStream {
    resp: wreq::Response,
    status: u16,
    total: Option<u64>,
}

#[async_trait]
impl ChunkStream for WreqStream {
    fn status(&self) -> u16 {
        self.status
    }
    fn total(&self) -> Option<u64> {
        self.total
    }
    async fn next_chunk(&mut self) -> Result<Option<Vec<u8>>, String> {
        use http_body_util::BodyExt;
        loop {
            match self.resp.frame().await {
                None => return Ok(None),
                Some(Err(e)) => return Err(e.to_string()),
                Some(Ok(f)) => {
                    if let Ok(d) = f.into_data() {
                        return Ok(Some(d.to_vec()));
                    }
                }
            }
        }
    }
}

#[async_trait]
impl Fetcher for WreqFetcher {
    async fn get(&self, url: &str, max: usize) -> Result<Vec<u8>, String> {
        let name = url.rsplit('/').next().unwrap_or(url).to_string();
        let too_big = || format!("{name} が大きすぎます");
        // 上限を超えたら読むのをやめる (全部を読み込んでから比べない)。全体を CONNECT_STALL_TIMEOUT で切る
        let read = async {
            let r = self.client.get(url).send().await.map_err(|e| e.to_string())?;
            if r.status().as_u16() != 200 {
                return Err(format!("{name} 取得失敗: status={}", r.status().as_u16()));
            }
            if r.content_length().is_some_and(|n| n > max as u64) {
                return Err(too_big());
            }
            let mut s = WreqStream { status: 200, total: None, resp: r };
            let mut out = Vec::new();
            while let Some(c) = s.next_chunk().await? {
                if out.len() + c.len() > max {
                    return Err(too_big());
                }
                out.extend_from_slice(&c);
            }
            Ok(out)
        };
        tokio::time::timeout(CONNECT_STALL_TIMEOUT, read).await.map_err(|_| format!("{name} の取得が時間切れになりました"))?
    }

    async fn stream(&self, url: &str) -> Result<Box<dyn ChunkStream>, String> {
        let resp = self.client.get(url).send().await.map_err(|e| e.to_string())?;
        let status = resp.status().as_u16();
        let total = resp.content_length();
        Ok(Box::new(WreqStream { resp, status, total }))
    }
}

/// GitHub の最新リリースの版 ("1.3.1")。releases/latest のリダイレクト先のタグから (GitHub API を使わない)
pub async fn fetch_latest_release_version(client: &wreq::Client) -> Option<String> {
    let r = match client.head(LATEST_RELEASE_URL).send().await {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!(target: LOG, "GitHub の最新版の確認に失敗: {e}");
            return None;
        }
    };
    latest_from_url(r.uri().to_string().as_str())
}

/// `.../releases/tag/v1.2.3` の URL から版を取り出す
pub fn latest_from_url(url: &str) -> Option<String> {
    let tag = url.rsplit_once("/releases/tag/")?.1;
    parse_version(tag)?;
    Some(tag.trim_start_matches('v').to_string())
}
