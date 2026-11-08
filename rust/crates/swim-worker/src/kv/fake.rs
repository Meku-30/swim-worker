//! テスト用のメモリの上の Kv。操作ごとに 1 回だけ失敗を差し込める。
//! 時間は tokio の時計 (テストでは `tokio::time::pause` で進める)
use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};
use std::sync::Mutex;
use std::time::Duration;

use async_trait::async_trait;
use tokio::time::Instant;

use super::{Kv, KvError};

/// 値と、期限 (置いた時刻, 秒)
type Entry = (Vec<u8>, Option<(Instant, u64)>);

#[derive(Default)]
struct State {
    strings: HashMap<String, Entry>,
    lists: HashMap<String, VecDeque<String>>,
    sets: HashMap<String, BTreeSet<String>>,
    hashes: HashMap<String, BTreeMap<String, String>>,
    fail: HashMap<&'static str, VecDeque<KvError>>,
    calls: Vec<String>,
}

#[derive(Default)]
pub struct FakeKv {
    st: Mutex<State>,
}

impl FakeKv {
    fn st(&self) -> std::sync::MutexGuard<'_, State> {
        self.st.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// 次の `op` (メソッド名) を 1 回だけ失敗させる
    pub fn fail_next(&self, op: &'static str, e: KvError) {
        self.st().fail.entry(op).or_default().push_back(e);
    }

    fn check(&self, op: &'static str, detail: String) -> Result<(), KvError> {
        let mut st = self.st();
        st.calls.push(format!("{op} {detail}"));
        match st.fail.get_mut(op).and_then(|q| q.pop_front()) {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }

    /// 呼ばれた操作の記録 ("setex results:w:t1" など)
    pub fn calls(&self) -> Vec<String> {
        self.st().calls.clone()
    }

    pub fn push(&self, list: &str, v: &str) {
        self.st().lists.entry(list.into()).or_default().push_back(v.into());
    }

    pub fn list_len(&self, list: &str) -> usize {
        self.st().lists.get(list).map_or(0, VecDeque::len)
    }

    pub fn put(&self, key: &str, v: &str) {
        self.st().strings.insert(key.into(), (v.as_bytes().to_vec(), None));
    }

    pub fn get_bytes(&self, key: &str) -> Option<Vec<u8>> {
        self.expire();
        self.st().strings.get(key).map(|(v, _)| v.clone())
    }

    /// 置いたときの TTL (秒)。期限なしなら None
    pub fn ttl_of(&self, key: &str) -> Option<u64> {
        self.st().strings.get(key).and_then(|(_, e)| e.map(|(_, s)| s))
    }

    pub fn hget(&self, h: &str, f: &str) -> Option<String> {
        self.st().hashes.get(h).and_then(|m| m.get(f).cloned())
    }

    pub fn set_members(&self, set: &str, members: &[&str]) {
        self.st().sets.insert(set.into(), members.iter().map(|s| s.to_string()).collect());
    }

    pub fn has_key(&self, key: &str) -> bool {
        self.expire();
        let st = self.st();
        st.strings.contains_key(key) || st.lists.get(key).is_some_and(|l| !l.is_empty()) || st.sets.contains_key(key)
    }

    fn expire(&self) {
        let now = Instant::now();
        self.st().strings.retain(|_, (_, e)| e.is_none_or(|(at, s)| now < at + Duration::from_secs(s)));
    }
}

#[async_trait]
impl Kv for FakeKv {
    async fn ping(&self) -> Result<(), KvError> {
        self.check("ping", String::new())
    }
    async fn blpop(&self, key: &str, timeout_secs: f64) -> Result<Option<String>, KvError> {
        self.check("blpop", key.into())?;
        let deadline = Instant::now() + Duration::from_secs_f64(timeout_secs);
        loop {
            if let Some(v) = self.st().lists.get_mut(key).and_then(VecDeque::pop_front) {
                return Ok(Some(v));
            }
            if Instant::now() >= deadline {
                return Ok(None);
            }
            tokio::time::sleep(Duration::from_millis(10).min(deadline - Instant::now())).await;
        }
    }
    async fn get(&self, key: &str) -> Result<Option<String>, KvError> {
        self.check("get", key.into())?;
        Ok(self.get_bytes(key).map(|v| String::from_utf8_lossy(&v).into_owned()))
    }
    async fn set_nx_ex(&self, key: &str, value: &str, secs: u64) -> Result<bool, KvError> {
        self.check("set_nx_ex", key.into())?;
        self.expire();
        let mut st = self.st();
        if st.strings.contains_key(key) {
            return Ok(false);
        }
        st.strings.insert(key.into(), (value.as_bytes().to_vec(), Some((Instant::now(), secs))));
        Ok(true)
    }
    async fn setex(&self, key: &str, secs: u64, value: &[u8]) -> Result<(), KvError> {
        self.check("setex", key.into())?;
        self.st().strings.insert(key.into(), (value.to_vec(), Some((Instant::now(), secs))));
        Ok(())
    }
    async fn del(&self, key: &str) -> Result<i64, KvError> {
        self.check("del", key.into())?;
        let mut st = self.st();
        let n = st.strings.remove(key).is_some() as i64
            + st.lists.remove(key).is_some() as i64
            + st.sets.remove(key).is_some() as i64
            + st.hashes.remove(key).is_some() as i64;
        Ok(n)
    }
    async fn sismember(&self, set: &str, member: &str) -> Result<bool, KvError> {
        self.check("sismember", format!("{set} {member}"))?;
        Ok(self.st().sets.get(set).is_some_and(|s| s.contains(member)))
    }
    async fn sismember_many(&self, pairs: &[(String, String)]) -> Result<Vec<Option<bool>>, KvError> {
        self.check("sismember_many", format!("{}", pairs.len()))?;
        let st = self.st();
        Ok(pairs.iter().map(|(s, m)| Some(st.sets.get(s).is_some_and(|x| x.contains(m)))).collect())
    }
    async fn sadd(&self, set: &str, member: &str) -> Result<(), KvError> {
        self.check("sadd", format!("{set} {member}"))?;
        self.st().sets.entry(set.into()).or_default().insert(member.into());
        Ok(())
    }
    async fn smembers(&self, set: &str) -> Result<Vec<String>, KvError> {
        self.check("smembers", set.into())?;
        Ok(self.st().sets.get(set).map(|s| s.iter().cloned().collect()).unwrap_or_default())
    }
    async fn hset(&self, hash: &str, field: &str, value: &str) -> Result<(), KvError> {
        self.check("hset", format!("{hash} {field}"))?;
        self.st().hashes.entry(hash.into()).or_default().insert(field.into(), value.into());
        Ok(())
    }
}
