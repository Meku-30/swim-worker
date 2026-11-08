//! GUI の自動更新 (Python の updater.py・update_verify.py・gui_helpers.build_windows_update_script)
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, SystemTime};

use async_trait::async_trait;
use serde_json::Value;
use swim_worker::updater::verify::{check_release_line, parse_sha256sums, verify_sums_signature};
use swim_worker::updater::{
    cleanup_stale_update_files, download_and_verify, helper_bat, ChunkStream, Fetcher, HelperPaths, UpdateError,
};

const G: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/golden/updater");

fn g(name: &str) -> Vec<u8> {
    std::fs::read(Path::new(G).join(name)).unwrap()
}

fn pem(name: &str) -> String {
    String::from_utf8(g(name)).unwrap()
}

#[test]
fn verify_accepts_python_signed_sums() {
    let (a, b) = (pem("key_a.pub.pem"), pem("key_b.pub.pem"));
    assert_eq!(verify_sums_signature(&g("SHA256SUMS"), &g("SHA256SUMS.sig"), &[&a, &b]).unwrap(), 1);
}

#[test]
fn verify_rejects_wrong_key_bad_len_missing_keys_tamper() {
    let (a, b) = (pem("key_a.pub.pem"), pem("key_b.pub.pem"));
    let sums = g("SHA256SUMS");
    let sig = g("SHA256SUMS.sig");
    assert!(verify_sums_signature(&sums, &sig, &[&a]).is_err(), "知らない鍵");
    assert!(verify_sums_signature(&sums, &sig[..63], &[&b]).is_err(), "63 バイト");
    assert!(verify_sums_signature(&sums, &sig, &[]).is_err(), "鍵が未設定");
    assert!(verify_sums_signature(&sums, &sig, &[&a, &a, &b]).is_err(), "3 本以上");
    let mut t = sums.clone();
    t[30] ^= 1;
    assert!(verify_sums_signature(&t, &sig, &[&b]).is_err(), "改ざん");
    assert!(verify_sums_signature(&sums, &sig, &["-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"]).is_err());
}

#[test]
fn embedded_release_keys_match_repo() {
    let keys = swim_worker::updater::verify::release_pubkeys();
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../scripts/release_pubkeys");
    let mut files: Vec<PathBuf> = std::fs::read_dir(dir).unwrap().map(|e| e.unwrap().path()).collect();
    files.sort();
    assert!(!keys.is_empty());
    assert_eq!(keys.len(), files.len());
    for (k, f) in keys.iter().zip(files) {
        assert_eq!(k.trim(), std::fs::read_to_string(f).unwrap().trim());
    }
}

#[test]
fn release_line_must_match_tag() {
    let sums = g("SHA256SUMS");
    assert!(check_release_line(&sums, "v1.3.1").is_ok());
    assert!(check_release_line(&sums, "v1.3.2").is_err());
    assert!(check_release_line(b"# swim-worker-release v1.3.1 \n", "v1.3.1").is_err());
}

#[test]
fn parse_sums_like_python() {
    let m = parse_sha256sums(std::str::from_utf8(&g("SHA256SUMS")).unwrap());
    assert_eq!(m.get("swim-worker-windows.exe").map(String::as_str), Some(std::str::from_utf8(&g("exe.sha256")).unwrap()));
    assert_eq!(m.get("install.sh").map(String::as_str), Some("0".repeat(64).as_str()), "* を落とす");
    assert_eq!(m.len(), 2, "コメント行は数えない");
}

/// 偽の取り先: URL ごとに中身を返す
struct FakeFetch {
    files: Vec<(String, Vec<u8>)>,
    chunk: usize,
    stall_after: Option<usize>,
    calls: Mutex<Vec<String>>,
}

struct FakeStream {
    data: Vec<u8>,
    pos: usize,
    chunk: usize,
    stall_after: Option<usize>,
    sent: usize,
}

#[async_trait]
impl ChunkStream for FakeStream {
    fn status(&self) -> u16 {
        200
    }
    fn total(&self) -> Option<u64> {
        Some(self.data.len() as u64)
    }
    async fn next_chunk(&mut self) -> Result<Option<Vec<u8>>, String> {
        if self.stall_after == Some(self.sent) {
            tokio::time::sleep(Duration::from_secs(3600)).await;
        }
        if self.pos >= self.data.len() {
            return Ok(None);
        }
        let end = (self.pos + self.chunk).min(self.data.len());
        let c = self.data[self.pos..end].to_vec();
        self.pos = end;
        self.sent += 1;
        Ok(Some(c))
    }
}

#[async_trait]
impl Fetcher for FakeFetch {
    async fn get(&self, url: &str, max: usize) -> Result<Vec<u8>, String> {
        self.calls.lock().unwrap().push(url.into());
        let b = self.files.iter().find(|(u, _)| url.ends_with(u.as_str())).map(|(_, b)| b.clone()).ok_or("404")?;
        if b.len() > max {
            return Err("大きすぎます".into());
        }
        Ok(b)
    }
    async fn stream(&self, url: &str) -> Result<Box<dyn ChunkStream>, String> {
        self.calls.lock().unwrap().push(url.into());
        let data = self.files.iter().find(|(u, _)| url.ends_with(u.as_str())).map(|(_, b)| b.clone()).ok_or("404")?;
        Ok(Box::new(FakeStream { data, pos: 0, chunk: self.chunk, stall_after: self.stall_after, sent: 0 }))
    }
}

const URL: &str = "https://github.com/Meku-30/swim-worker/releases/download/v1.3.1/swim-worker-windows.exe";

fn fake(exe: Vec<u8>) -> FakeFetch {
    FakeFetch {
        files: vec![
            ("/v1.3.1/SHA256SUMS".into(), g("SHA256SUMS")),
            ("/v1.3.1/SHA256SUMS.sig".into(), g("SHA256SUMS.sig")),
            ("/v1.3.1/swim-worker-windows.exe".into(), exe),
        ],
        chunk: 64 * 1024,
        stall_after: None,
        calls: Mutex::new(Vec::new()),
    }
}

fn good_exe() -> Vec<u8> {
    let mut v = b"MZ".to_vec();
    v.extend(std::iter::repeat_n(0u8, 1024 * 1024));
    v
}

async fn run(f: &FakeFetch, dest: &Path, current: &str) -> Result<u64, UpdateError> {
    let keys = [pem("key_a.pub.pem"), pem("key_b.pub.pem")];
    let keys: Vec<&str> = keys.iter().map(String::as_str).collect();
    download_and_verify(f, URL, dest, current, &keys, &|_| {}).await
}

#[tokio::test]
async fn download_verifies_and_places_file() {
    let d = tempfile::tempdir().unwrap();
    let dest = d.path().join("swim-worker-gui.new.exe");
    let n = run(&fake(good_exe()), &dest, "1.3.0").await.unwrap();
    assert_eq!(n, good_exe().len() as u64);
    assert_eq!(std::fs::read(&dest).unwrap(), good_exe());
    assert!(!d.path().join("swim-worker-gui.new.exe.part").exists());
}

#[tokio::test]
async fn download_rejects_hash_mismatch_and_leaves_no_part() {
    let d = tempfile::tempdir().unwrap();
    let dest = d.path().join("new.exe");
    let mut bad = good_exe();
    bad[5] = 1;
    assert!(run(&fake(bad), &dest, "1.3.0").await.is_err());
    assert!(!dest.exists() && !d.path().join("new.exe.part").exists());
}

#[tokio::test]
async fn download_rejects_small_file() {
    let d = tempfile::tempdir().unwrap();
    let e = run(&fake(b"MZ".to_vec()), &d.path().join("n.exe"), "1.3.0").await.unwrap_err();
    assert!(e.to_string().contains("サイズ") || e.to_string().contains("SHA256"), "{e}");
}

#[tokio::test]
async fn refuses_not_newer_version_before_download() {
    let d = tempfile::tempdir().unwrap();
    let f = fake(good_exe());
    assert!(run(&f, &d.path().join("n.exe"), "1.3.1").await.is_err());
    assert!(run(&f, &d.path().join("n.exe"), "1.4.0").await.is_err());
    assert!(f.calls.lock().unwrap().is_empty(), "取りに行く前に断る");
}

#[tokio::test]
async fn unsigned_release_is_refused() {
    let d = tempfile::tempdir().unwrap();
    let mut f = fake(good_exe());
    f.files.retain(|(u, _)| !u.ends_with(".sig"));
    let e = run(&f, &d.path().join("n.exe"), "1.3.0").await.unwrap_err();
    assert!(e.to_string().contains("署名"), "{e}");
    assert!(!f.calls.lock().unwrap().iter().any(|u| u.ends_with(".exe")), "本体は取らない");
}

#[tokio::test(start_paused = true)]
async fn stalled_download_gives_up_after_60s() {
    let d = tempfile::tempdir().unwrap();
    let mut f = fake(good_exe());
    f.stall_after = Some(2);
    let t = tokio::time::Instant::now();
    assert!(run(&f, &d.path().join("n.exe"), "1.3.0").await.is_err());
    assert_eq!(t.elapsed().as_secs(), 60);
    assert!(!d.path().join("n.exe.part").exists());
}

#[test]
fn helper_bat_matches_python_minus_pyinstaller_lines() {
    let golden: Value =
        serde_json::from_str(&std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/golden/bat.json")).unwrap())
            .unwrap();
    for c in golden.as_array().unwrap() {
        let base = PathBuf::from(c["base"].as_str().unwrap());
        // Windows のパスを Linux で組み立てるので、区切りは文字列で足す
        let j = |s: &str| PathBuf::from(format!("{}\\{s}", c["base"].as_str().unwrap()));
        let p = HelperPaths {
            base: base.clone(),
            current_exe: j("swim-worker-gui.exe"),
            new_exe: j("swim-worker-gui.new.exe"),
            old_exe: j("swim-worker-gui.exe.old"),
            startup_ok: j("data\\.startup_ok"),
            rollback_marker: j("data\\.update_rollback.json"),
            log_path: j("swim-worker-update.log"),
        };
        let want: String = c["helper_bat"]
            .as_str()
            .unwrap()
            .split_inclusive("\r\n")
            .filter(|l| !(l.contains("_PYI_") || l.contains("_MEIPASS2") || l.contains("PYINSTALLER_RESET_ENVIRONMENT")
                || l.starts_with("        # ")))
            .collect();
        let got = helper_bat(&p, "1.3.1");
        assert_eq!(got, want);
        assert_eq!(c["helper_bat"].as_str().unwrap().lines().count() - got.lines().count(), 5);
    }
}

#[test]
fn cleanup_removes_only_old_files() {
    let d = tempfile::tempdir().unwrap();
    for n in ["a.old", "b.new", "c.new.exe", "d.part", "keep.exe", "data.json"] {
        std::fs::write(d.path().join(n), b"x").unwrap();
    }
    let now = SystemTime::now();
    cleanup_stale_update_files(d.path(), now);
    assert_eq!(std::fs::read_dir(d.path()).unwrap().count(), 6, "新しいものは消さない");
    cleanup_stale_update_files(d.path(), now + Duration::from_secs(3 * 24 * 3600 + 10));
    let mut left: Vec<String> = std::fs::read_dir(d.path()).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect();
    left.sort();
    assert_eq!(left, vec!["data.json", "keep.exe"]);
}
