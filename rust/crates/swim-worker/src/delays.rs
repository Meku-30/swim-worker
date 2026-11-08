//! 待ち時間の分布 (Python 版と同じ。見本 rust/tests/golden/delays.json)
use rand::Rng;
use rand_distr::{Distribution, Exp, LogNormal};

/// 要求の前の待ち時間: 対数正規分布 (中央値 median・p99 p99) を clip_min〜clip_max に切り詰める
/// (consumer.py の _delay_mu・_delay_sigma と _run_api_task)
#[derive(Debug, Clone, Copy)]
pub struct PreTask {
    pub mu: f64,
    pub sigma: f64,
    pub clip_min: f64,
    pub clip_max: f64,
}

impl PreTask {
    pub fn new(median: f64, p99: f64, clip_min: f64, clip_max: f64) -> Self {
        let mu = median.ln();
        Self { mu, sigma: (p99.ln() - mu) / 2.326, clip_min, clip_max }
    }
    pub fn sample(&self, rng: &mut impl Rng) -> f64 {
        let raw = LogNormal::new(self.mu, self.sigma).expect("sigma").sample(rng);
        raw.clamp(self.clip_min, self.clip_max)
    }
}

/// 応答のあとの待ち時間 (auth.py execute_api: expovariate(3.0) + 0.05)
pub fn post_response(rng: &mut impl Rng) -> f64 {
    Exp::new(3.0).expect("rate").sample(rng) + 0.05
}

/// random.uniform(a, b)
pub fn uniform(rng: &mut impl Rng, a: f64, b: f64) -> f64 {
    a + (b - a) * rng.random::<f64>()
}

pub async fn sleep_secs(s: f64) {
    if s > 0.0 {
        tokio::time::sleep(std::time::Duration::from_secs_f64(s)).await;
    }
}
