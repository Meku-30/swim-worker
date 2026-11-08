//! 試運転用の偽の SWIM (feature `trial` のときだけ。配布物には入らない)。
//! 応答の決まりを JSON のファイル (`SWIM_TRIAL_RESPONSES`) から読む。Python 版の比べる道具
//! (`tools/compare/fake_swim.py`) と同じファイル・同じ決まりで応答を選ぶ:
//! URL に `url_contains` を含み、残りの回数 (`times`、無ければ何回でも) がある最初の決まり。回数は全セッションで共有
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use serde::Deserialize;
use serde_json::Value;
use swim_http::OrderedHeaders;

use super::transport::{HttpResponse, Session, SessionFactory};

#[derive(Debug, Clone, Deserialize)]
pub struct Rule {
    pub url_contains: String,
    #[serde(default = "ok")]
    pub status: u16,
    #[serde(default)]
    pub json: Option<Value>,
    /// json の代わりにそのまま返す本文 (壊れた JSON など)
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub times: Option<u32>,
    /// 返す前に待つ秒数 (時間切れの場面)
    #[serde(default)]
    pub delay_secs: f64,
    #[serde(default)]
    pub set_cookie: bool,
    /// リダイレクトのあとの URL のふり (SWIM の外への場面)
    #[serde(default)]
    pub final_url: Option<String>,
}

fn ok() -> u16 {
    200
}

#[derive(Deserialize)]
struct Case {
    #[serde(default)]
    responses: Vec<Rule>,
}

#[derive(Deserialize)]
struct File {
    #[serde(default)]
    defaults: Vec<Rule>,
    #[serde(default)]
    cases: Vec<Case>,
}

/// 場面の応答を先に、既定 (ログイン・残り全部) をあとに並べる
pub fn load_rules(text: &str) -> Result<Vec<Rule>, String> {
    let f: File = serde_json::from_str(text).map_err(|e| format!("応答のファイルを読めない: {e}"))?;
    Ok(f.cases.into_iter().flat_map(|c| c.responses).chain(f.defaults).collect())
}

#[derive(Clone)]
pub struct TrialFactory {
    rules: Arc<Mutex<Vec<Rule>>>,
}

impl TrialFactory {
    pub fn new(rules: Vec<Rule>) -> Self {
        Self { rules: Arc::new(Mutex::new(rules)) }
    }

    /// 決まりを選び、回数を 1 つ減らす
    fn pick(&self, url: &str) -> Option<Rule> {
        let mut rules = self.rules.lock().unwrap();
        let r = rules.iter_mut().find(|r| url.contains(&r.url_contains) && r.times != Some(0))?;
        if let Some(t) = r.times.as_mut() {
            *t -= 1;
        }
        Some(r.clone())
    }
}

struct TrialSession {
    cookies: Vec<(String, String)>,
    f: TrialFactory,
}

#[async_trait]
impl Session for TrialSession {
    async fn request(&mut self, _method: &str, url: &str, _headers: &OrderedHeaders, _body: Option<&Value>)
        -> Result<HttpResponse, String> {
        let r = self.f.pick(url).ok_or_else(|| format!("試運転の応答が無い: {url}"))?;
        if r.delay_secs > 0.0 {
            tokio::time::sleep(Duration::from_secs_f64(r.delay_secs)).await;
        }
        let body = match (&r.text, &r.json) {
            (Some(t), _) => t.clone().into_bytes(),
            (None, Some(v)) => v.to_string().into_bytes(),
            (None, None) => b"{}".to_vec(),
        };
        if r.set_cookie {
            self.set_cookie("SESSION", "trial-session");
        }
        let final_url = r.final_url.clone().unwrap_or_else(|| url.into());
        Ok(HttpResponse { status: r.status, final_url, body, set_cookies: usize::from(r.set_cookie) })
    }
    fn cookies(&self) -> Vec<(String, String)> {
        self.cookies.clone()
    }
    fn set_cookie(&mut self, name: &str, value: &str) {
        self.cookies.retain(|(n, _)| n != name);
        self.cookies.push((name.into(), value.into()));
    }
}

impl SessionFactory for TrialFactory {
    fn new_session(&self) -> Box<dyn Session> {
        Box::new(TrialSession { cookies: Vec::new(), f: self.clone() })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn trial_responses_from_file() {
        let text = r#"{"defaults":[{"url_contains":"swim.mlit.go.jp","json":{"d":1}}],
            "cases":[{"name":"a","responses":[
                {"url_contains":"/cmp/a","status":401,"times":1},
                {"url_contains":"/cmp/a","text":"{broken"}]}]}"#;
        let f = TrialFactory::new(load_rules(text).unwrap());
        let mut s1 = f.new_session();
        let mut s2 = f.new_session();
        let h = OrderedHeaders::default();
        // 回数はセッションをまたいで数える
        assert_eq!(s1.request("POST", "https://web.swim.mlit.go.jp/cmp/a", &h, None).await.unwrap().status, 401);
        let r = s2.request("POST", "https://web.swim.mlit.go.jp/cmp/a", &h, None).await.unwrap();
        assert_eq!((r.status, r.body), (200, b"{broken".to_vec()));
        let r = s1.request("GET", "https://web.swim.mlit.go.jp/other", &h, None).await.unwrap();
        assert_eq!(r.body, br#"{"d":1}"#.to_vec());
        assert!(s1.request("GET", "https://example.com/", &h, None).await.is_err());
    }
}
