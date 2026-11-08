//! SWIM との通信の継ぎ目。本物は swim-http (wreq、Chrome 136 と同じ TLS・HTTP/2)。
//! Python の curl_cffi の AsyncSession 1 つ = Session 1 つ (Cookie の置き場と TLS の再開の記録を持つ)
use std::sync::Arc;

use async_trait::async_trait;
use serde_json::Value;
use swim_http::OrderedHeaders;
use wreq::cookie::Jar;

#[derive(Debug, Clone)]
pub struct HttpResponse {
    pub status: u16,
    /// リダイレクトのあとの最終の URL
    pub final_url: String,
    pub body: Vec<u8>,
    /// この応答の Set-Cookie の数 (ログインの成否の判定に使う)
    pub set_cookies: usize,
}

#[async_trait]
pub trait Session: Send {
    async fn request(&mut self, method: &str, url: &str, headers: &OrderedHeaders, body: Option<&Value>)
        -> Result<HttpResponse, String>;
    /// 置き場の Cookie (名前, 値)
    fn cookies(&self) -> Vec<(String, String)>;
    /// `.mlit.go.jp` の Secure な Cookie として置く (Python の cookies.set(..., domain=".mlit.go.jp", secure=True))
    fn set_cookie(&mut self, name: &str, value: &str);
}

pub trait SessionFactory: Send + Sync {
    fn new_session(&self) -> Box<dyn Session>;
}

pub struct WreqFactory;

impl SessionFactory for WreqFactory {
    fn new_session(&self) -> Box<dyn Session> {
        let (client, jar) = swim_http::build_client().expect("HTTP クライアントを作れない");
        Box::new(WreqSession { client, jar })
    }
}

pub struct WreqSession {
    client: wreq::Client,
    jar: Arc<Jar>,
}

/// Cookie を置くときの基準の URL (ドメインは .mlit.go.jp を明示する)
const COOKIE_BASE: &str = "https://web.swim.mlit.go.jp/";

#[async_trait]
impl Session for WreqSession {
    async fn request(&mut self, method: &str, url: &str, headers: &OrderedHeaders, body: Option<&Value>)
        -> Result<HttpResponse, String> {
        let resp = swim_http::send(&self.client, method, url, headers, body).await.map_err(|e| e.to_string())?;
        let status = resp.status().as_u16();
        let final_url = resp.uri().to_string();
        let set_cookies = resp.headers().get_all("set-cookie").iter().count();
        let body = resp.bytes().await.map_err(|e| e.to_string())?.to_vec();
        Ok(HttpResponse { status, final_url, body, set_cookies })
    }
    fn cookies(&self) -> Vec<(String, String)> {
        self.jar.get_all().map(|c| (c.name().to_string(), c.value().to_string())).collect()
    }
    fn set_cookie(&mut self, name: &str, value: &str) {
        self.jar.add(format!("{name}={value}; Domain=.mlit.go.jp; Path=/; Secure").as_str(), COOKIE_BASE);
    }
}
