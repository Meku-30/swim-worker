//! SWIM のクライアント (Python の swim_worker/auth.py)。
//! 送る要求 (順番・ヘッダー・待ち時間・新しいセッションを作る位置) は Python 版と同じにする。
pub mod client;
pub mod fake;
pub mod headers;
pub mod transport;

pub use client::SwimClient;

pub const SWIM_LOGIN_URL: &str = "https://top.swim.mlit.go.jp/swim/api/login";
pub const SWIM_SESSION_CHECK_URL: &str = "https://web.swim.mlit.go.jp/service/api/accounts/summary";
pub const SWIM_PORTAL_URL: &str = "https://web.swim.mlit.go.jp";
pub const SWIM_TOP_URL: &str = "https://top.swim.mlit.go.jp";
pub const SWIM_DEFAULT_REDIRECT_URL: &str = "https://web.swim.mlit.go.jp/service/portal?lang=ja";

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SwimError {
    /// ログイン・API の失敗 (一時的なものを含む)
    #[error("{0}")]
    Auth(String),
    /// 401/403 で確定した失敗 (capability は「未権限」として扱う)
    #[error("{message}")]
    Unauthorized { message: String, status: u16 },
    /// 応答が SWIM の外へリダイレクトされた (メンテナンス中の可能性)
    #[error("SWIM の外へリダイレクトされました (メンテナンス中の可能性): {0}")]
    RedirectOutside(String),
    /// 許可していない URL
    #[error("{0}")]
    Disallowed(String),
}

/// url が https://*.swim.mlit.go.jp (ポートは無しか 443、ユーザー情報なし) なら Ok (auth.py validate_swim_url)
pub fn validate_swim_url(url: &str) -> Result<(), SwimError> {
    let short: String = url.chars().take(100).collect();
    if url.is_empty() {
        return Err(SwimError::Disallowed("URL が空です".into()));
    }
    if url.chars().any(|c| !(('\u{21}'..='\u{7e}').contains(&c)) || c == '\\') {
        return Err(SwimError::Disallowed(format!("URL に使えない文字があります: {short:?}")));
    }
    let Some(rest) = url.get(..8).filter(|s| s.eq_ignore_ascii_case("https://")).map(|_| &url[8..]) else {
        return Err(SwimError::Disallowed(format!("https 以外の URL は使えません: {short}")));
    };
    let netloc = rest.split(['/', '?', '#']).next().unwrap_or("");
    if netloc.contains('@') || netloc.contains('[') {
        return Err(SwimError::Disallowed(format!("許可していない URL です: {short}")));
    }
    let (host, port) = netloc.split_once(':').unwrap_or((netloc, ""));
    if !(port.is_empty() || port == "443") || !allowed_host(host) {
        return Err(SwimError::Disallowed(format!("SWIM 以外の URL は使えません: {short}")));
    }
    Ok(())
}

/// `(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+swim\.mlit\.go\.jp` (大文字小文字を問わない)
fn allowed_host(host: &str) -> bool {
    let h = host.to_ascii_lowercase();
    let Some(prefix) = h.strip_suffix(".swim.mlit.go.jp") else {
        return false;
    };
    !prefix.is_empty()
        && prefix.split('.').all(|l| {
            !l.is_empty()
                && l.len() <= 63
                && l.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
                && !l.starts_with('-')
                && !l.ends_with('-')
        })
}

/// リダイレクトの最終の URL が SWIM の外なら RedirectOutside (クエリ・フラグメントは出さない)
pub fn check_response_url(final_url: &str) -> Result<(), SwimError> {
    if final_url.is_empty() || validate_swim_url(final_url).is_ok() {
        return Ok(());
    }
    let shown = match url::Url::parse(final_url) {
        // Python は scheme://netloc/path (netloc はポートを含む)
        Ok(u) => match u.port() {
            Some(p) => format!("{}://{}:{p}{}", u.scheme(), u.host_str().unwrap_or(""), u.path()),
            None => format!("{}://{}{}", u.scheme(), u.host_str().unwrap_or(""), u.path()),
        },
        Err(_) => final_url.split(['?', '#']).next().unwrap_or("").to_string(),
    };
    Err(SwimError::RedirectOutside(shown.chars().take(150).collect()))
}
