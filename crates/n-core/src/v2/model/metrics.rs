use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Metrics {
    pub n: usize,
    pub brier: f64,
    pub logloss: f64,
    pub auc: f64,
    pub accuracy: f64,
    pub baseline_brier: f64,
    pub baseline_logloss: f64,
    pub top20_lift: f64,
    pub constant_win_rate: f64,
    pub calibration: Vec<CalibrationBucket>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CalibrationBucket {
    pub bin: usize,
    pub count: usize,
    pub avg_p: f64,
    pub avg_y: f64,
}

pub fn brier_score(y_true: &[i32], p_pred: &[f64]) -> f64 {
    if y_true.is_empty() { return 0.0; }
    y_true.iter().zip(p_pred.iter()).map(|(y, p)| { let d = *p - *y as f64; d*d }).sum::<f64>() / y_true.len() as f64
}

pub fn logloss(y_true: &[i32], p_pred: &[f64]) -> f64 {
    if y_true.is_empty() { return 0.0; }
    let eps = 1e-15;
    y_true.iter().zip(p_pred.iter()).map(|(y, p)| {
        let pc = p.clamp(eps, 1.0 - eps);
        if *y == 1 { -pc.ln() } else { -(1.0 - pc).ln() }
    }).sum::<f64>() / y_true.len() as f64
}

pub fn accuracy(y_true: &[i32], p_pred: &[f64]) -> f64 {
    if y_true.is_empty() { return 0.0; }
    let correct = y_true.iter().zip(p_pred.iter()).filter(|(y, p)| (**p >= 0.5) == (**y == 1)).count();
    correct as f64 / y_true.len() as f64
}

pub fn auc(y_true: &[i32], p_pred: &[f64]) -> f64 {
    let n = y_true.len();
    if n == 0 { return 0.5; }
    let pos: Vec<f64> = y_true.iter().zip(p_pred.iter()).filter(|(y,_)| **y==1).map(|(_,p)| *p).collect();
    let neg: Vec<f64> = y_true.iter().zip(p_pred.iter()).filter(|(y,_)| **y==0).map(|(_,p)| *p).collect();
    if pos.is_empty() || neg.is_empty() { return 0.5; }
    let mut wins = 0.0;
    let mut total = 0.0;
    for pp in &pos {
        for np in &neg {
            total += 1.0;
            if pp > np { wins += 1.0; } else if (pp - np).abs() < 1e-12 { wins += 0.5; }
        }
    }
    f64::max(0.0, f64::min(1.0, wins / total))
}

pub fn calibration(y_true: &[i32], p_pred: &[f64], n_bins: usize) -> Vec<CalibrationBucket> {
    let mut buckets = vec![(0usize, 0.0, 0.0); n_bins];
    for (y,p) in y_true.iter().zip(p_pred.iter()) {
        let mut b = (p * n_bins as f64).floor() as usize;
        if b >= n_bins { b = n_bins-1; }
        buckets[b].0 += 1;
        buckets[b].1 += *p;
        buckets[b].2 += *y as f64;
    }
    buckets.into_iter().enumerate().map(|(i,(c,sum_p,sum_y))| {
        CalibrationBucket { bin: i, count: c, avg_p: if c>0 { sum_p / c as f64 } else { 0.0 }, avg_y: if c>0 { sum_y / c as f64 } else { 0.0 } }
    }).collect()
}

pub fn top20_lift(y_true: &[i32], p_pred: &[f64]) -> f64 {
    if y_true.is_empty() { return 1.0; }
    let overall = y_true.iter().filter(|y| **y==1).count() as f64 / y_true.len() as f64;
    if overall <= 1e-9 { return 1.0; }
    let mut pairs: Vec<(f64,i32)> = y_true.iter().zip(p_pred.iter()).map(|(y,p)| (*p,*y)).collect();
    pairs.sort_by(|a,b| b.0.partial_cmp(&a.0).unwrap());
    let k = (pairs.len() as f64 * 0.2).ceil() as usize;
    if k == 0 { return 1.0; }
    let top_wins = pairs[..k].iter().filter(|(_,y)| *y==1).count() as f64 / k as f64;
    top_wins / overall
}

pub fn compute_metrics(y_true: &[i32], p_pred: &[f64]) -> Metrics {
    let n = y_true.len();
    let brier = brier_score(y_true, p_pred);
    let ll = logloss(y_true, p_pred);
    let auc_v = auc(y_true, p_pred);
    let acc = accuracy(y_true, p_pred);
    let win_rate = if n>0 { y_true.iter().filter(|y| **y==1).count() as f64 / n as f64 } else { 0.5 };
    let const_pred = vec![win_rate; n];
    let baseline_brier = brier_score(y_true, &const_pred);
    let baseline_logloss = logloss(y_true, &const_pred);
    let lift = top20_lift(y_true, p_pred);
    let cal = calibration(y_true, p_pred, 10);
    Metrics { n, brier, logloss: ll, auc: auc_v, accuracy: acc, baseline_brier, baseline_logloss, top20_lift: lift, constant_win_rate: win_rate, calibration: cal }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn brier_perfect_is_zero() { assert!((brier_score(&[1,0], &[1.0,0.0]) - 0.0).abs() < 1e-9); }
    #[test]
    fn auc_perfect_is_one() { assert!((auc(&[1,1,0,0], &[0.9,0.8,0.3,0.2]) - 1.0).abs() < 1e-9); }
    #[test]
    fn auc_random_half() { let a = auc(&[1,0,1,0], &[0.5,0.5,0.5,0.5]); assert!((a-0.5).abs() < 1e-9); }
    #[test]
    fn logloss_clamped() { let ll = logloss(&[1], &[0.0]); assert!(ll < 40.0); }
    #[test]
    fn top20_lift_computes() {
        let y = vec![1,1,1,0,0,0,0,0,0,0];
        let p = vec![0.9,0.8,0.7,0.6,0.5,0.4,0.3,0.2,0.1,0.05];
        let lift = top20_lift(&y, &p);
        assert!(lift > 2.0);
    }
}


