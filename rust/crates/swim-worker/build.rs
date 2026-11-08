//! scripts/release_pubkeys/*.pub.pem を、更新の署名を確かめる公開鍵として埋め込む (Python 版・install.sh と同じ鍵)
use std::path::PathBuf;

fn main() {
    let dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap()).join("../../../scripts/release_pubkeys");
    println!("cargo:rerun-if-changed={}", dir.display());
    let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)
        .map(|d| d.filter_map(|e| e.ok().map(|e| e.path())).filter(|p| p.to_string_lossy().ends_with(".pub.pem")).collect())
        .unwrap_or_default();
    files.sort();
    let mut out = String::from("pub const RELEASE_PUBKEYS_PEM: &[&str] = &[\n");
    for f in &files {
        println!("cargo:rerun-if-changed={}", f.display());
        let pem = std::fs::read_to_string(f).unwrap();
        out.push_str(&format!("    {pem:?},\n"));
    }
    out.push_str("];\n");
    std::fs::write(PathBuf::from(std::env::var("OUT_DIR").unwrap()).join("release_pubkeys.rs"), out).unwrap();
}
