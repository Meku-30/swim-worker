//! SwimClient (Python の auth.py の SwimClient と同じ手順)
use std::collections::BTreeSet;
use std::path::PathBuf;

use rand::rngs::StdRng;
use rand::SeedableRng;
use serde_json::{json, Value};
use tokio::sync::Mutex;
use tokio::time::Instant;

use super::headers::{build, BodyKind, NAV_ACCEPT};
use super::transport::{HttpResponse, Session, SessionFactory};
use super::{
    check_response_url, validate_swim_url, SwimError, SWIM_DEFAULT_REDIRECT_URL, SWIM_LOGIN_URL, SWIM_PORTAL_URL,
    SWIM_SESSION_CHECK_URL, SWIM_TOP_URL,
};
use crate::delays::{post_response, sleep_secs, uniform};

/// セッションの既定のヘッダー (XHR とナビゲーションで共通。auth.py _SESSION_HEADERS)
const SESSION_HEADERS: &[(&str, &str)] = &[
    ("Accept", "application/json, text/plain, */*"),
    ("Accept-Language", "ja,en-US;q=0.9,en;q=0.8"),
    ("Sec-Fetch-Dest", "empty"),
    ("Sec-Fetch-Mode", "cors"),
    ("Sec-Fetch-Site", "same-origin"),
];

/// ページの読み込み (auth.py _NAV_HEADERS)
const NAV_HEADERS: &[(&str, &str)] = &[
    ("Accept", NAV_ACCEPT),
    ("Accept-Language", "ja,en-US;q=0.9,en;q=0.8"),
    ("Sec-Fetch-Dest", "document"),
    ("Sec-Fetch-Mode", "navigate"),
    ("Sec-Fetch-Site", "none"),
    ("Sec-Fetch-User", "?1"),
    ("Upgrade-Insecure-Requests", "1"),
];

/// API の種類ごとの Referer (auth.py _REFERER_MAP)
const REFERER_MAP: &[(&str, &str)] = &[
    ("/f2dnrq/", "https://web.swim.mlit.go.jp/f2dnrq/browse/FUV201"),
    ("/f2aspr/", "https://web.swim.mlit.go.jp/f2aspr/browse/flv850s001"),
];

#[derive(Clone, Copy, PartialEq, Eq)]
enum H {
    Jquery,
    JqueryText,
    Angular,
    AngularRes,
    Luciadria,
}

/// サービスの画面を開いたときに読み込まれるもの (auth.py _SPA_INIT_REQUESTS。順番・body は実測)
type InitReq = (&'static str, &'static str, Option<fn() -> Value>, H);

fn basic() -> Value {
    json!({"msgHeader": {"jnlInfo": {"jnlRegistFlag": 0}, "tsusuInfo": {}}, "ctrlInfo": {}, "ctrlHeader": {}})
}
fn basic_profile_lang() -> Value {
    let mut v = basic();
    v["profileType"] = json!(0);
    v["lang"] = json!("ja");
    v
}
fn basic_profile() -> Value {
    let mut v = basic();
    v["profileType"] = json!(0);
    v
}

const SETTINGS_TAIL: &[InitReq] = &[
    ("GET", "settings/velocity.json", None, H::Jquery),
    ("GET", "settings/default_view.json", None, H::Jquery),
    ("GET", "settings/default_font.json", None, H::Jquery),
    ("GET", "settings/default_dire_dist_position.json", None, H::Jquery),
    ("GET", "settings/shape_datablock_setting.json", None, H::Jquery),
    ("GET", "settings/default_color.json", None, H::Jquery),
    ("GET", "settings/map_disp.json", None, H::Jquery),
    ("GET", "settings/menu.json", None, H::Jquery),
    ("GET", "settings/commonMenuSetting.json", None, H::Jquery),
    ("GET", "settings/toolbarSetting.json", None, H::Jquery),
    ("GET", "settings/blink_info.json", None, H::Jquery),
    ("GET", "settings/groupLayer.json", None, H::Jquery),
];

fn spa_init(prefix: &str) -> Vec<InitReq> {
    let mut v: Vec<InitReq> = Vec::new();
    match prefix {
        "f2aspr" => {
            v.extend_from_slice(&[
                ("POST", "LuciadRIALicense", None, H::JqueryText),
                ("GET", "js/lib/WebGIS/ATCMAP.settings", None, H::JqueryText),
                ("GET", "settings/auto_filter.json", None, H::Jquery),
                ("POST", "web/FLV901/LGV300", Some(basic as fn() -> Value), H::Angular),
                ("POST", "web/FLV811/LGV231", Some(basic_profile_lang as fn() -> Value), H::Angular),
                ("GET", "settings/map_disp.json", None, H::Jquery),
                ("GET", "web/resource/message", None, H::AngularRes),
                ("GET", "web/resource/webfw", None, H::AngularRes),
                ("GET", "web/resource/user", None, H::AngularRes),
                ("POST", "web/FLV802/LGV205", Some(basic_profile as fn() -> Value), H::Angular),
                ("POST", "web/FLV934/LGV387", Some(basic as fn() -> Value), H::Angular),
            ]);
            v.extend_from_slice(SETTINGS_TAIL);
        }
        "f2dnrq" => {
            v.extend_from_slice(&[
                ("POST", "LuciadRIALicense", None, H::JqueryText),
                ("GET", "js/lib/WebGIS/ATCMAP.settings", None, H::JqueryText),
                ("GET", "settings/auto_filter.json", None, H::Jquery),
                ("POST", "web/FUV201/USV005", Some(basic as fn() -> Value), H::Angular),
                ("GET", "settings/map_disp.json", None, H::Jquery),
                ("GET", "web/resource/message", None, H::AngularRes),
                ("GET", "web/resource/webfw", None, H::AngularRes),
                ("GET", "web/resource/user", None, H::AngularRes),
            ]);
            v.extend_from_slice(SETTINGS_TAIL);
            v.extend_from_slice(&[
                ("POST", "web/FUV201/USV005", Some(basic as fn() -> Value), H::Angular),
                ("GET", "js/lib/WebGIS/layer/UTM0.json", None, H::Luciadria),
                ("GET", "js/lib/WebGIS/layer/UTM1.json", None, H::Luciadria),
                ("GET", "js/lib/WebGIS/layer/UTM2.json", None, H::Luciadria),
                ("GET", "js/lib/WebGIS/layer/UTM3.json", None, H::Luciadria),
            ]);
        }
        _ => {}
    }
    v
}

/// 実ブラウザでは JS の読み込みでこの後に長めの間隔が入る (auth.py _PHASE_BREAK_AFTER)
const PHASE_BREAK_AFTER: &[&str] = &["ATCMAP.settings", "web/resource/user", "settings/groupLayer.json"];

fn get_referer(url: &str) -> String {
    REFERER_MAP
        .iter()
        .find(|(p, _)| url.contains(p))
        .map_or_else(|| format!("{SWIM_PORTAL_URL}/"), |(_, r)| r.to_string())
}

const COOKIE_SAVE_INTERVAL_SECS: f64 = 600.0;
const SLOW_THRESHOLD_SECS: f64 = 10.0;

struct State {
    session: Option<Box<dyn Session>>,
    is_ready: bool,
    login_failures: u32,
    login_blocked_until: Option<Instant>,
    extra_delay: f64,
    visited_pages: BTreeSet<String>,
    cookie_restore_tried: bool,
    last_cookie_save: Option<Instant>,
    rng: StdRng,
}

pub struct SwimClient {
    username: String,
    password: String,
    cookie_file: PathBuf,
    factory: Box<dyn SessionFactory>,
    login_backoff_base: f64,
    login_backoff_max: f64,
    st: Mutex<State>,
}

fn json_body(r: &HttpResponse) -> Option<Value> {
    serde_json::from_slice(&r.body).ok()
}

impl SwimClient {
    pub fn new(username: &str, password: &str, cookie_file: PathBuf, factory: Box<dyn SessionFactory>) -> Self {
        tracing::info!(target: "swim_worker.auth", "Cookie保存先: {}", cookie_file.display());
        Self {
            username: username.into(),
            password: password.into(),
            cookie_file,
            factory,
            login_backoff_base: 60.0,
            login_backoff_max: 1800.0,
            st: Mutex::new(State {
                session: None,
                is_ready: false,
                login_failures: 0,
                login_blocked_until: None,
                extra_delay: 0.0,
                visited_pages: BTreeSet::new(),
                cookie_restore_tried: false,
                last_cookie_save: None,
                rng: StdRng::from_os_rng(),
            }),
        }
    }

    /// テスト用: 乱数の種を固定する
    pub async fn seed(&self, seed: u64) {
        self.st.lock().await.rng = StdRng::seed_from_u64(seed);
    }

    /// テスト用: 画面の初期化を済んだことにする
    pub async fn mark_visited(&self, api_url: &str) {
        self.st.lock().await.visited_pages.insert(get_referer(api_url));
    }

    async fn req(
        s: &mut dyn Session,
        method: &str,
        url: &str,
        session_defaults: &[(&str, &str)],
        request: &[(&str, &str)],
        body: Option<&Value>,
    ) -> Result<HttpResponse, String> {
        let kind = match (method, body) {
            (_, Some(b)) => BodyKind::Json(swim_http::python_json(b).len()),
            ("POST", None) => BodyKind::EmptyPost,
            _ => BodyKind::None,
        };
        let h = build(session_defaults, request, kind);
        s.request(method, url, &h, body).await
    }

    fn save_cookies(&self, st: &mut State, debug: bool) {
        let Some(s) = st.session.as_ref() else { return };
        let map: serde_json::Map<String, Value> = s.cookies().into_iter().map(|(k, v)| (k, Value::String(v))).collect();
        let n = map.len();
        let r = (|| -> std::io::Result<()> {
            if let Some(d) = self.cookie_file.parent() {
                std::fs::create_dir_all(d)?;
            }
            write_private(&self.cookie_file, serde_json::to_string(&map)?.as_bytes())
        })();
        match r {
            Ok(()) => {
                st.last_cookie_save = Some(Instant::now());
                if debug {
                    tracing::debug!(target: "swim_worker.auth", "Cookie保存: {n}個 → {}", self.cookie_file.display());
                } else {
                    tracing::info!(target: "swim_worker.auth", "Cookie保存: {n}個 → {}", self.cookie_file.display());
                }
            }
            Err(e) => tracing::warn!(target: "swim_worker.auth", "Cookie保存失敗: {e}"),
        }
    }

    fn load_cookies(&self) -> Option<Vec<(String, String)>> {
        let text = match std::fs::read_to_string(&self.cookie_file) {
            Ok(t) => t,
            Err(_) => {
                tracing::debug!(target: "swim_worker.auth", "Cookieファイルなし: {}", self.cookie_file.display());
                return None;
            }
        };
        match serde_json::from_str::<serde_json::Map<String, Value>>(&text) {
            Ok(m) if !m.is_empty() => {
                tracing::info!(target: "swim_worker.auth", "保存済みCookie読み込み: {}個 ({})", m.len(), self.cookie_file.display());
                Some(m.into_iter().map(|(k, v)| (k, v.as_str().map_or_else(|| v.to_string(), str::to_string))).collect())
            }
            Ok(_) => None,
            Err(e) => {
                tracing::warn!(target: "swim_worker.auth", "Cookie読み込み失敗: {e}");
                None
            }
        }
    }

    fn record_login_failure(&self, st: &mut State) {
        st.login_failures += 1;
        let wait = (self.login_backoff_base * 2f64.powi(st.login_failures as i32 - 1)).min(self.login_backoff_max);
        st.login_blocked_until = Some(Instant::now() + std::time::Duration::from_secs_f64(wait));
        tracing::warn!(target: "swim_worker.auth", "ログイン失敗 {} 回目、次回ログインを {wait:.0} 秒抑制します", st.login_failures);
    }

    pub async fn login(&self) -> Result<(), SwimError> {
        let mut st = self.st.lock().await;
        self.login_locked(&mut st).await
    }

    async fn login_locked(&self, st: &mut State) -> Result<(), SwimError> {
        if let Some(until) = st.login_blocked_until {
            let now = Instant::now();
            if until > now {
                return Err(SwimError::Auth(format!(
                    "ログイン抑制中 (連続失敗 {} 回、残り {:.0} 秒)",
                    st.login_failures,
                    (until - now).as_secs_f64()
                )));
            }
        }
        // 起動後の最初のログインだけ、保存済みの Cookie を試す
        let mut saved = None;
        if !st.cookie_restore_tried {
            st.cookie_restore_tried = true;
            saved = self.load_cookies();
        }
        if let Some(saved) = saved {
            let mut s = self.factory.new_session();
            for (k, v) in &saved {
                s.set_cookie(k, v);
            }
            st.session = Some(s);
            let s = st.session.as_deref_mut().expect("session");
            // web.swim へのナビゲーション (ブックマークから開き直すのと同じ)
            match Self::req(s, "GET", SWIM_DEFAULT_REDIRECT_URL, SESSION_HEADERS, NAV_HEADERS, None).await {
                Ok(_) => sleep_secs(uniform(&mut st.rng, 0.5, 1.0)).await,
                Err(e) => tracing::debug!(target: "swim_worker.auth", "Cookie復元後のナビゲーション失敗: {e}"),
            }
            let s = st.session.as_deref_mut().expect("session");
            if let Ok(r) = Self::req(s, "GET", SWIM_SESSION_CHECK_URL, SESSION_HEADERS, &[], None).await {
                if r.status == 200 {
                    st.is_ready = true;
                    st.visited_pages.clear();
                    tracing::info!(target: "swim_worker.auth", "保存済みCookieでセッション復元成功");
                    return Ok(());
                }
            }
            tracing::info!(target: "swim_worker.auth", "保存済みCookie失効、再ログイン");
        }

        tracing::info!(target: "swim_worker.auth", "SWIMポータルにログイン開始");
        let all_cookies = match self.login_flow(st).await {
            Ok(c) => c,
            Err(e) => {
                self.record_login_failure(st);
                return Err(e);
            }
        };
        st.login_failures = 0;
        st.login_blocked_until = None;
        let mut s = self.factory.new_session();
        // Cookie のドメインは .mlit.go.jp (2026-04-06 実測。省略すると 403)
        for (k, v) in &all_cookies {
            s.set_cookie(k, v);
        }
        st.session = Some(s);
        st.is_ready = true;
        st.visited_pages.clear();
        self.save_cookies(st, false);
        tracing::info!(target: "swim_worker.auth", "SWIMポータルにログイン成功");
        Ok(())
    }

    /// 一時セッションでログインし、取れた Cookie を返す
    async fn login_flow(&self, st: &mut State) -> Result<Vec<(String, String)>, SwimError> {
        let err = |e: String| SwimError::Auth(format!("ログインAPI呼び出しエラー: {e}"));
        let mut tmp = self.factory.new_session();
        // 1. ポータルのページ (URL を直接入れたのと同じ)
        Self::req(tmp.as_mut(), "GET", &format!("{SWIM_TOP_URL}/swim/"), &[], NAV_HEADERS, None).await.map_err(err)?;
        sleep_secs(uniform(&mut st.rng, 1.0, 3.0)).await;
        // 2. ログインの POST (SPA の XHR)
        let mut login_headers: Vec<(&str, &str)> = SESSION_HEADERS.to_vec();
        let origin = SWIM_TOP_URL.to_string();
        let referer = format!("{SWIM_TOP_URL}/swim/login");
        login_headers.push(("Origin", &origin));
        login_headers.push(("Referer", &referer));
        let body = json!({"userId": self.username, "password": self.password});
        let resp = Self::req(tmp.as_mut(), "POST", SWIM_LOGIN_URL, &[], &login_headers, Some(&body)).await.map_err(err)?;
        if resp.status != 200 {
            return Err(SwimError::Auth(format!("ログインAPI失敗 (status={})", resp.status)));
        }
        // JSON でない応答は通す (Cookie が取れていれば成功)
        let data = match json_body(&resp) {
            Some(Value::Object(m)) => Some(m),
            Some(_) => return Err(SwimError::Auth("ログインAPIの応答が不正です".into())),
            None => None,
        };
        if let Some(d) = data.as_ref().filter(|d| !d.is_empty()) {
            let code = d.get("statusCode").cloned().unwrap_or(json!(-1));
            if code != json!(0) {
                let msg = d.get("message").map_or_else(|| "unknown".into(), |m| m.as_str().map_or_else(|| m.to_string(), str::to_string));
                return Err(SwimError::Auth(format!("ログインAPIエラー (statusCode={code}, message={msg})")));
            }
        }
        if resp.set_cookies == 0 {
            return Err(SwimError::Auth("ログイン後にCookieを取得できませんでした".into()));
        }
        // 3. web.swim への遷移 (応答の redirectUrl。SWIM の外なら従わず既定のポータル)
        let mut redirect = SWIM_DEFAULT_REDIRECT_URL.to_string();
        if let Some(c) = data.as_ref().and_then(|d| d.get("datas")).and_then(|d| d.get("redirectUrl")).and_then(Value::as_str) {
            if !c.is_empty() {
                match validate_swim_url(c) {
                    Ok(()) => redirect = c.to_string(),
                    Err(e) => tracing::warn!(target: "swim_worker.auth", "ログイン後の遷移先を無視: {e}"),
                }
            }
        }
        sleep_secs(uniform(&mut st.rng, 0.5, 1.5)).await;
        let top_ref = format!("{SWIM_TOP_URL}/");
        // Python は {**_NAV_HEADERS, "Referer": ..., "Sec-Fetch-Site": "same-site"} (値だけ替わり位置は元のまま)
        let mut nav2: Vec<(&str, &str)> = NAV_HEADERS.to_vec();
        nav2.push(("Referer", &top_ref));
        for h in nav2.iter_mut() {
            if h.0 == "Sec-Fetch-Site" {
                h.1 = "same-site";
            }
        }
        Self::req(tmp.as_mut(), "GET", &redirect, &[], &nav2, None).await.map_err(err)?;
        sleep_secs(uniform(&mut st.rng, 0.5, 1.5)).await;
        Ok(tmp.cookies())
    }

    async fn ensure_browse_page(&self, st: &mut State, api_url: &str) {
        let browse_url = get_referer(api_url);
        if st.visited_pages.contains(&browse_url) || st.session.is_none() {
            return;
        }
        let prefix = ["f2aspr", "f2dnrq"].into_iter().find(|p| api_url.contains(&format!("/{p}/")));
        let s = st.session.as_deref_mut().expect("session");
        tracing::debug!(target: "swim_worker.auth", "ブラウズ画面ナビゲーション: {browse_url}");
        // 1. 画面の最初の GET (URL を直接開くのと同じ: Sec-Fetch-Site none・Referer なし)
        if let Err(e) = Self::req(s, "GET", &browse_url, SESSION_HEADERS, NAV_HEADERS, None).await {
            tracing::warn!(target: "swim_worker.auth", "ブラウズ画面ナビゲーション失敗: {e}");
            return;
        }
        sleep_secs(uniform(&mut st.rng, 0.3, 0.8)).await;
        if let Some(prefix) = prefix {
            let base = format!("{SWIM_PORTAL_URL}/{prefix}");
            let r = browse_url.clone();
            let portal = SWIM_PORTAL_URL.to_string();
            let hdr = |h: H, m: &str| -> Vec<(&str, String)> {
                let mut v: Vec<(&str, String)> = match h {
                    H::Jquery => vec![
                        ("Accept", "application/json, text/javascript, */*; q=0.01".into()),
                        ("X-Requested-With", "XMLHttpRequest".into()),
                        ("Referer", r.clone()),
                    ],
                    H::JqueryText => vec![
                        ("Accept", "text/plain, */*; q=0.01".into()),
                        ("X-Requested-With", "XMLHttpRequest".into()),
                        ("Referer", r.clone()),
                    ],
                    H::Angular => vec![("Origin", portal.clone()), ("Referer", r.clone())],
                    H::AngularRes => vec![("Accept", "*/*".into()), ("Referer", r.clone())],
                    H::Luciadria => vec![("Accept", "application/javascript, application/json".into()), ("Referer", r.clone())],
                };
                if m == "POST" && matches!(h, H::Jquery | H::JqueryText) {
                    v.push(("Origin", portal.clone()));
                }
                v
            };
            let mut browse_count = 0;
            for (method, path, body, h) in spa_init(prefix) {
                let url = format!("{base}/{path}");
                let hv = hdr(h, method);
                let hs: Vec<(&str, &str)> = hv.iter().map(|(k, v)| (*k, v.as_str())).collect();
                let b = body.map(|f| f());
                let s = st.session.as_deref_mut().expect("session");
                if Self::req(s, method, &url, SESSION_HEADERS, &hs, b.as_ref()).await.is_ok() {
                    if PHASE_BREAK_AFTER.iter().any(|m| path.contains(m)) {
                        sleep_secs(uniform(&mut st.rng, 0.3, 1.0)).await;
                    } else {
                        sleep_secs(uniform(&mut st.rng, 0.02, 0.15)).await;
                    }
                }
                // resource/user のあとに画面の追加の GET が 3 回 (jQuery の $.ajax)
                if path == "web/resource/user" && browse_count == 0 {
                    let jv = hdr(H::Jquery, "GET");
                    let js: Vec<(&str, &str)> = jv.iter().map(|(k, v)| (*k, v.as_str())).collect();
                    for _ in 0..3 {
                        let s = st.session.as_deref_mut().expect("session");
                        if Self::req(s, "GET", &browse_url, SESSION_HEADERS, &js, None).await.is_ok() {
                            sleep_secs(uniform(&mut st.rng, 0.02, 0.1)).await;
                        }
                    }
                    browse_count += 1;
                }
            }
        }
        st.visited_pages.insert(browse_url);
        sleep_secs(uniform(&mut st.rng, 1.0, 3.0)).await;
    }

    /// テスト・照合用: 画面の初期化 (サービスの画面を開いたときの要求) だけを行う
    pub async fn open_service_page(&self, api_url: &str) {
        let mut st = self.st.lock().await;
        self.ensure_browse_page(&mut st, api_url).await;
    }

    /// SWIM の API を実行する。HTTP の失敗・401/403 は 1 回だけ再ログインしてやり直す。
    /// capability のテストは `retry_on_auth_error = false` (403 は「未権限」)
    pub async fn execute_api(&self, url: &str, body: &Value, retry_on_auth_error: bool) -> Result<Value, SwimError> {
        validate_swim_url(url)?;
        let mut retried = false;
        loop {
            let mut st = self.st.lock().await;
            if !st.is_ready || st.session.is_none() {
                self.login_locked(&mut st).await?;
            }
            if st.extra_delay > 0.0 {
                tracing::debug!(target: "swim_worker.auth", "応答速度ベース追加遅延: {:.1}秒", st.extra_delay);
                sleep_secs(st.extra_delay).await;
            }
            self.ensure_browse_page(&mut st, url).await;
            let referer = get_referer(url);
            let extra = [("Origin", SWIM_PORTAL_URL), ("Referer", referer.as_str())];
            let start = Instant::now();
            let s = st.session.as_deref_mut().expect("session");
            let resp = match Self::req(s, "POST", url, SESSION_HEADERS, &extra, Some(body)).await {
                Ok(r) => r,
                Err(e) => {
                    if !retried {
                        let d = uniform(&mut st.rng, 5.0, 15.0);
                        tracing::warn!(target: "swim_worker.auth", "API HTTPエラー、{d:.0}秒待機後にリトライ: {e}");
                        sleep_secs(d).await;
                        self.relogin_locked(&mut st, false).await?;
                        retried = true;
                        continue;
                    }
                    return Err(SwimError::Auth(format!("APIエラー: {e}")));
                }
            };
            let elapsed = start.elapsed().as_secs_f64();
            check_response_url(&resp.final_url)?;
            // 応答が遅いときは待ちを足す (サーバーの負荷を下げる)
            if elapsed > SLOW_THRESHOLD_SECS {
                st.extra_delay = (st.extra_delay + 2.0).min(15.0);
                tracing::info!(target: "swim_worker.auth", "SWIM応答遅延検知 ({elapsed:.1}秒)、追加遅延→{:.1}秒", st.extra_delay);
            } else if st.extra_delay > 0.0 {
                st.extra_delay = (st.extra_delay - 0.5).max(0.0);
            }
            if resp.status == 401 || resp.status == 403 {
                if retry_on_auth_error && !retried {
                    let d = uniform(&mut st.rng, 5.0, 15.0);
                    tracing::warn!(target: "swim_worker.auth", "API {}エラー、{d:.0}秒待機後に再ログイン+リトライ", resp.status);
                    sleep_secs(d).await;
                    self.relogin_locked(&mut st, true).await?;
                    retried = true;
                    continue;
                }
                let text: String = String::from_utf8_lossy(&resp.body).chars().take(500).collect();
                return Err(SwimError::Unauthorized {
                    message: format!("API {}エラー (body={text})", resp.status),
                    status: resp.status,
                });
            }
            if resp.status != 200 {
                return Err(SwimError::Auth(format!("APIエラー (status={})", resp.status)));
            }
            // 応答の処理の時間 (ブラウザの描画)
            let d = post_response(&mut st.rng);
            sleep_secs(d).await;
            let data = serde_json::from_slice::<Value>(&resp.body)
                .map_err(|e| SwimError::Auth(format!("APIの応答が JSON ではありません: {e}")))?;
            if st.last_cookie_save.is_none_or(|t| t.elapsed().as_secs_f64() >= COOKIE_SAVE_INTERVAL_SECS) {
                self.save_cookies(&mut st, true);
            }
            return Ok(data);
        }
    }

    /// 認証の要らない公開の GET (お知らせの API など)。毎回新しいセッション
    pub async fn fetch_public_get(&self, url: &str, params: &[(String, String)]) -> Result<Value, SwimError> {
        validate_swim_url(url)?;
        let mut full = url::Url::parse(url).map_err(|e| SwimError::Disallowed(e.to_string()))?;
        if !params.is_empty() {
            full.query_pairs_mut().extend_pairs(params);
        }
        let top_ref = format!("{SWIM_TOP_URL}/");
        let headers = [
            ("Accept", "application/json, text/plain, */*"),
            ("Accept-Language", "ja,en-US;q=0.9,en;q=0.8"),
            ("Referer", top_ref.as_str()),
            ("Sec-Fetch-Dest", "empty"),
            ("Sec-Fetch-Mode", "cors"),
            ("Sec-Fetch-Site", "same-origin"),
        ];
        let mut s = self.factory.new_session();
        let resp = Self::req(s.as_mut(), "GET", full.as_str(), &[], &headers, None)
            .await
            .map_err(|e| SwimError::Auth(format!("公開GET APIエラー: {e}")))?;
        check_response_url(&resp.final_url)?;
        if resp.status != 200 {
            return Err(SwimError::Auth(format!("公開GET APIエラー (status={})", resp.status)));
        }
        serde_json::from_slice(&resp.body).map_err(|e| SwimError::Auth(format!("公開GET APIの応答が JSON ではありません: {e}")))
    }

    async fn relogin_locked(&self, st: &mut State, force: bool) -> Result<(), SwimError> {
        if !force && st.is_ready {
            if let Some(s) = st.session.as_deref_mut() {
                if let Ok(r) = Self::req(s, "GET", SWIM_SESSION_CHECK_URL, SESSION_HEADERS, &[], None).await {
                    if r.status == 200 {
                        return Ok(());
                    }
                }
            }
        }
        st.is_ready = false;
        if force {
            // 403 起因: 保存済みの Cookie も切れている見込みが高いので消す
            if std::fs::remove_file(&self.cookie_file).is_ok() {
                tracing::debug!(target: "swim_worker.auth", "保存済みCookie削除（強制再ログイン）");
            }
        }
        self.login_locked(st).await
    }

    pub async fn close(&self) {
        let mut st = self.st.lock().await;
        if st.session.is_some() {
            self.save_cookies(&mut st, false);
            st.session = None;
        }
        st.is_ready = false;
    }
}

/// 所有者だけが読み書きできるファイルとして書く (セッションの Cookie を含む)
fn write_private(path: &std::path::Path, data: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
        opts.mode(0o600);
        let mut f = opts.open(path)?;
        f.write_all(data)?;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
        Ok(())
    }
    #[cfg(not(unix))]
    {
        let mut f = opts.open(path)?;
        f.write_all(data)
    }
}
