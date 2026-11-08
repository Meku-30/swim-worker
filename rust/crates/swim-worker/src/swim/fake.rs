//! テスト用の偽の通信。送った要求を記録し、決めた応答を返す (ネットに出ない)。
//! Cookie の扱い: 応答の set_cookies が 1 以上ならそのセッションに `SESSION=dummy-session` が入る。
//! 記録するヘッダーは、Cookie を持たないセッションでは `cookie` を落とす (実際の送信と同じ)
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use serde_json::Value;
use swim_http::OrderedHeaders;

use super::transport::{HttpResponse, Session, SessionFactory};

#[derive(Debug, Clone)]
pub struct Recorded {
    /// 何番目に作られたセッションか (0 から)
    pub session: usize,
    pub method: String,
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub body: Option<Value>,
}

pub type Responder = dyn Fn(&str, &str, Option<&Value>) -> Result<HttpResponse, String> + Send + Sync;

#[derive(Clone)]
pub struct FakeFactory {
    pub log: Arc<Mutex<Vec<Recorded>>>,
    sessions: Arc<Mutex<usize>>,
    responder: Arc<Responder>,
}

impl FakeFactory {
    pub fn new(responder: impl Fn(&str, &str, Option<&Value>) -> Result<HttpResponse, String> + Send + Sync + 'static) -> Self {
        Self { log: Arc::default(), sessions: Arc::default(), responder: Arc::new(responder) }
    }
    pub fn take(&self) -> Vec<Recorded> {
        std::mem::take(&mut *self.log.lock().unwrap())
    }
}

pub fn ok_json(url: &str, v: Value) -> HttpResponse {
    HttpResponse { status: 200, final_url: url.into(), body: v.to_string().into_bytes(), set_cookies: 0 }
}

struct FakeSession {
    id: usize,
    cookies: Vec<(String, String)>,
    f: FakeFactory,
}

#[async_trait]
impl Session for FakeSession {
    async fn request(&mut self, method: &str, url: &str, headers: &OrderedHeaders, body: Option<&Value>)
        -> Result<HttpResponse, String> {
        let mut h = headers.0.clone();
        if self.cookies.is_empty() {
            h.retain(|(n, _)| n != "cookie");
        }
        self.f.log.lock().unwrap().push(Recorded {
            session: self.id,
            method: method.into(),
            url: url.into(),
            headers: h,
            body: body.cloned(),
        });
        let r = (self.f.responder)(method, url, body)?;
        if r.set_cookies > 0 {
            self.cookies.push(("SESSION".into(), "dummy-session".into()));
        }
        Ok(r)
    }
    fn cookies(&self) -> Vec<(String, String)> {
        self.cookies.clone()
    }
    fn set_cookie(&mut self, name: &str, value: &str) {
        self.cookies.retain(|(n, _)| n != name);
        self.cookies.push((name.into(), value.into()));
    }
}

impl SessionFactory for FakeFactory {
    fn new_session(&self) -> Box<dyn Session> {
        let mut n = self.sessions.lock().unwrap();
        let id = *n;
        *n += 1;
        Box::new(FakeSession { id, cookies: Vec::new(), f: self.clone() })
    }
}
