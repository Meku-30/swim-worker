//! 自動起動の .bat (Python の autostart.py・gui_helpers.encode_bat)
use std::path::Path;

use serde_json::Value;
use swim_worker::autostart::{bat_content, encode_bat};

fn golden() -> Value {
    serde_json::from_str(&std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/golden/bat.json")).unwrap()).unwrap()
}

#[test]
fn bat_matches_python() {
    for c in golden().as_array().unwrap() {
        let got = bat_content(Path::new(c["base"].as_str().unwrap()), Path::new(c["exe"].as_str().unwrap()));
        assert_eq!(got, c["startup_bat"].as_str().unwrap());
    }
}

#[test]
fn encode_uses_ansi_when_possible_else_utf8_with_chcp() {
    // ANSI に書けるなら そのバイト列
    let ascii_only = |s: &str| if s.is_ascii() { Some(s.as_bytes().to_vec()) } else { None };
    for c in golden().as_array().unwrap() {
        let text = c["startup_bat"].as_str().unwrap();
        let got = encode_bat(text, &ascii_only);
        match c["startup_utf8_fallback"].as_str() {
            None => assert_eq!(got, text.as_bytes()),
            Some(fb) => assert_eq!(String::from_utf8(got).unwrap(), fb, "2 行目に chcp 65001 を入れて UTF-8"),
        }
    }
}

#[test]
fn rewrite_only_when_content_differs() {
    use swim_worker::autostart::{enable, rewrite_if_stale};
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("SWIM Worker.bat");
    let base = d.path().join("w");
    enable(&p, &base, &base.join("swim-worker-gui.exe")).unwrap();
    assert!(!rewrite_if_stale(&p, &base, &base.join("swim-worker-gui.exe")).unwrap());
    assert!(rewrite_if_stale(&p, &base, &base.join("moved.exe")).unwrap());
    assert!(!rewrite_if_stale(&d.path().join("none.bat"), &base, &base).unwrap(), "無ければ作らない");
}

/// 日本語の Windows (CP932) で、Python と同じバイト列になる (CI の Windows)
#[cfg(windows)]
#[test]
fn system_ansi_matches_python_cp932() {
    use swim_worker::autostart::system_ansi;
    if unsafe { windows_sys::Win32::Globalization::GetACP() } != 932 {
        return; // 日本語でない Windows では比べない
    }
    for c in golden().as_array().unwrap() {
        let got = system_ansi(c["startup_bat"].as_str().unwrap()).map(|b| b.iter().map(|x| format!("{x:02x}")).collect::<String>());
        assert_eq!(got.as_deref(), c["startup_cp932_hex"].as_str());
    }
}
