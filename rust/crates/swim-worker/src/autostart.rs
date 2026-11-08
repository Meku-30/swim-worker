//! Windows のログイン時の自動起動 (スタートアップのフォルダの `SWIM Worker.bat`)。Python の autostart.py と同じ中身。
//! cmd.exe はバッチをシステムの ANSI コードページ (日本語の Windows は CP932) で読むので、書ければそれで書く。
//! 書けない文字があれば 2 行目に `chcp 65001` を入れて UTF-8 で書く
use std::path::{Path, PathBuf};

pub const BAT_NAME: &str = "SWIM Worker.bat";
const LOG: &str = "swim_worker.autostart";

/// バッチに埋め込む文字列の % を %% にする (変数展開されないように)
pub fn bat_escape(s: &str) -> String {
    s.replace('%', "%%")
}

/// スタートアップの .bat の中身 (CRLF)
pub fn bat_content(base: &Path, exe: &Path) -> String {
    format!(
        "@echo off\r\ncd /d \"{}\"\r\nstart \"\" \"{}\"\r\n",
        bat_escape(&base.to_string_lossy()),
        bat_escape(&exe.to_string_lossy())
    )
}

/// .bat をバイト列にする。`ansi` はシステムの ANSI で書ければ Some
pub fn encode_bat(text: &str, ansi: &dyn Fn(&str) -> Option<Vec<u8>>) -> Vec<u8> {
    if let Some(b) = ansi(text) {
        return b;
    }
    match text.split_once("\r\n") {
        Some((first, rest)) => format!("{first}\r\nchcp 65001 > nul\r\n{rest}").into_bytes(),
        None => text.as_bytes().to_vec(),
    }
}

/// システムの ANSI コードページで書く (Python の "mbcs" の strict と同じく、近い文字への置き換えはしない)
#[cfg(windows)]
pub fn system_ansi(text: &str) -> Option<Vec<u8>> {
    use windows_sys::Win32::Globalization::{WideCharToMultiByte, CP_ACP, WC_NO_BEST_FIT_CHARS};
    let w: Vec<u16> = text.encode_utf16().collect();
    if w.is_empty() {
        return Some(Vec::new());
    }
    let mut used_default = 0i32;
    // SAFETY: 長さを先に聞いてから、その大きさの領域に書かせる
    unsafe {
        let n = WideCharToMultiByte(CP_ACP, WC_NO_BEST_FIT_CHARS, w.as_ptr(), w.len() as i32, std::ptr::null_mut(), 0,
            std::ptr::null(), &mut used_default);
        if n <= 0 || used_default != 0 {
            return None;
        }
        let mut out = vec![0u8; n as usize];
        let m = WideCharToMultiByte(CP_ACP, WC_NO_BEST_FIT_CHARS, w.as_ptr(), w.len() as i32, out.as_mut_ptr(), n,
            std::ptr::null(), &mut used_default);
        if m != n || used_default != 0 {
            return None;
        }
        Some(out)
    }
}

#[cfg(not(windows))]
pub fn system_ansi(text: &str) -> Option<Vec<u8>> {
    Some(text.as_bytes().to_vec())
}

/// `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\SWIM Worker.bat`
pub fn startup_bat_path() -> Option<PathBuf> {
    let appdata = std::env::var_os("APPDATA")?;
    Some(PathBuf::from(appdata).join("Microsoft").join("Windows").join("Start Menu").join("Programs").join("Startup").join(BAT_NAME))
}

pub fn file_bytes(base: &Path, exe: &Path) -> Vec<u8> {
    encode_bat(&bat_content(base, exe), &system_ansi)
}

pub fn is_enabled(path: &Path) -> bool {
    path.exists()
}

pub fn enable(path: &Path, base: &Path, exe: &Path) -> std::io::Result<()> {
    if let Some(d) = path.parent() {
        std::fs::create_dir_all(d)?;
    }
    std::fs::write(path, file_bytes(base, exe))?;
    tracing::info!(target: LOG, "自動起動を有効にしました");
    Ok(())
}

pub fn disable(path: &Path) -> std::io::Result<()> {
    if path.exists() {
        std::fs::remove_file(path)?;
    }
    tracing::info!(target: LOG, "自動起動を無効にしました");
    Ok(())
}

/// 自動起動のファイルが今の exe・フォルダと違う中身なら書き直す (exe を動かした・古い形式)
pub fn rewrite_if_stale(path: &Path, base: &Path, exe: &Path) -> std::io::Result<bool> {
    match std::fs::read(path) {
        Ok(cur) if cur != file_bytes(base, exe) => {
            std::fs::write(path, file_bytes(base, exe))?;
            Ok(true)
        }
        _ => Ok(false),
    }
}
