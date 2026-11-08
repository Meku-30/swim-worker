//! 更新物 (リリースの SHA256SUMS) の署名を確かめる (Python の update_verify.py)。
//! 管理者が Ed25519 の鍵で SHA256SUMS に署名し、Worker は埋め込みの公開鍵で確かめる。
//! 先頭行 `# swim-worker-release <タグ>` も署名の対象で、取りに行ったタグと一致することを見る
use std::collections::BTreeMap;

use super::UpdateError;

pub const SIGNATURE_SIZE: usize = 64;
pub const MAX_RELEASE_KEYS: usize = 2;
pub const RELEASE_LINE_PREFIX: &str = "# swim-worker-release ";

// scripts/release_pubkeys/*.pub.pem (install.sh・更新スクリプト・Python 版と同じ鍵) をビルド時に埋め込む
include!(concat!(env!("OUT_DIR"), "/release_pubkeys.rs"));

/// 埋め込みの公開鍵 (PEM)
pub fn release_pubkeys() -> Vec<&'static str> {
    // 試運転のビルドだけ: 使い捨ての鍵 (PEM のファイル) に差し替えられる。配布物は埋め込みの鍵だけ
    #[cfg(feature = "trial")]
    if let Ok(p) = std::env::var("SWIM_TRIAL_PUBKEY") {
        if let Ok(pem) = std::fs::read_to_string(&p) {
            return vec![Box::leak(pem.into_boxed_str())];
        }
    }
    RELEASE_PUBKEYS_PEM.to_vec()
}

/// `sha256sum` 形式 ("<hex>  <name>" / "<hex> *<name>") を {name: hex} にする
pub fn parse_sha256sums(text: &str) -> BTreeMap<String, String> {
    let mut m = BTreeMap::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let mut it = line.splitn(2, char::is_whitespace);
        let (Some(d), Some(name)) = (it.next(), it.next()) else { continue };
        let name = name.trim_start().trim_start_matches('*').trim();
        m.insert(name.to_string(), d.to_ascii_lowercase());
    }
    m
}

fn b64_decode(s: &str) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    let (mut acc, mut bits) = (0u32, 0u32);
    for c in s.bytes().filter(|c| !c.is_ascii_whitespace()) {
        if c == b'=' {
            break;
        }
        let v = match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return None,
        } as u32;
        acc = (acc << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    Some(out)
}

/// Ed25519 の SubjectPublicKeyInfo (DER) の頭。これに 32 バイトの鍵が続く
const ED25519_SPKI_PREFIX: [u8; 12] = [0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00];

fn ed25519_key(pem: &str) -> Result<[u8; 32], UpdateError> {
    let body: String = pem
        .lines()
        .map(str::trim)
        .skip_while(|l| *l != "-----BEGIN PUBLIC KEY-----")
        .skip(1)
        .take_while(|l| *l != "-----END PUBLIC KEY-----")
        .collect();
    let der = b64_decode(&body).ok_or_else(|| UpdateError::Verify("公開鍵を読めません".into()))?;
    if der.len() != 44 || der[..12] != ED25519_SPKI_PREFIX {
        return Err(UpdateError::Verify("公開鍵が Ed25519 ではありません".into()));
    }
    let mut k = [0u8; 32];
    k.copy_from_slice(&der[12..]);
    Ok(k)
}

/// SHA256SUMS の署名を、公開鍵のどれかで確かめる。通れば何本目 (0 始まり) で通ったか
pub fn verify_sums_signature(sums: &[u8], sig: &[u8], pubkeys_pem: &[&str]) -> Result<usize, UpdateError> {
    if pubkeys_pem.is_empty() {
        return Err(UpdateError::Verify("更新の署名を確かめる公開鍵が未設定です (この版では自動更新できません)".into()));
    }
    if pubkeys_pem.len() > MAX_RELEASE_KEYS {
        return Err(UpdateError::Verify(format!("公開鍵は {MAX_RELEASE_KEYS} 本までです ({} 本)", pubkeys_pem.len())));
    }
    let keys = pubkeys_pem.iter().map(|p| ed25519_key(p)).collect::<Result<Vec<_>, _>>()?;
    if sig.len() != SIGNATURE_SIZE {
        return Err(UpdateError::Verify(format!(
            "SHA256SUMS の署名の長さが不正です ({} バイト、{SIGNATURE_SIZE} のはず)",
            sig.len()
        )));
    }
    for (i, k) in keys.iter().enumerate() {
        if ring::signature::UnparsedPublicKey::new(&ring::signature::ED25519, k).verify(sums, sig).is_ok() {
            return Ok(i);
        }
    }
    Err(UpdateError::Verify("SHA256SUMS の署名を確かめられません (改ざん、または知らない鍵の署名)".into()))
}

/// 署名した SHA256SUMS の先頭行が、取りに行ったタグと完全に一致するか
pub fn check_release_line(sums: &[u8], tag: &str) -> Result<(), UpdateError> {
    let first = sums.split(|b| *b == b'\n').next().unwrap_or_default();
    if first != format!("{RELEASE_LINE_PREFIX}{tag}").as_bytes() {
        let shown = String::from_utf8_lossy(&first[..first.len().min(60)]).into_owned();
        return Err(UpdateError::Verify(format!(
            "SHA256SUMS の版の行が {tag} と一致しません ({shown:?})。別の版のファイルの可能性があります"
        )));
    }
    Ok(())
}
