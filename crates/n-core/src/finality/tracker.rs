//! State machine tracking finality progress for a single bar.

use chrono::Local;

use super::model::{BarFingerprint, FinalityTrial, SessionType};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProbeResult {
    pub is_revision: bool,
    pub became_candidate_final: bool,
    pub became_false_final: bool,
    pub same_count: usize,
}

#[derive(Debug, Clone)]
pub struct BarFinalityTracker {
    pub symbol: String,
    pub bar_ts: String,
    pub session_type: SessionType,
    pub first_seen_at: Option<String>,
    pub first_seen_delay_ms: Option<i64>,
    pub last_fingerprint: Option<BarFingerprint>,
    pub same_count: usize,
    pub candidate_final_at: Option<String>,
    pub candidate_delay_ms: Option<i64>,
    pub candidate_fingerprint: Option<String>,
    pub revision_count: usize,
    pub last_revision_at: Option<String>,
    pub last_revision_delay_ms: Option<i64>,
    pub false_final: bool,
    pub final_fingerprint: Option<String>,
    pub probe_count: usize,
    pub created_at: String,
}

impl BarFinalityTracker {
    pub fn new(symbol: impl Into<String>, bar_ts: impl Into<String>, session_type: SessionType) -> Self {
        let now = Local::now().format("%Y-%m-%d %H:%M:%S%.3f").to_string();
        Self {
            symbol: symbol.into(),
            bar_ts: bar_ts.into(),
            session_type,
            first_seen_at: None,
            first_seen_delay_ms: None,
            last_fingerprint: None,
            same_count: 0,
            candidate_final_at: None,
            candidate_delay_ms: None,
            candidate_fingerprint: None,
            revision_count: 0,
            last_revision_at: None,
            last_revision_delay_ms: None,
            false_final: false,
            final_fingerprint: None,
            probe_count: 0,
            created_at: now,
        }
    }

    /// 录入一次探针观测值，推进状态机并返回本次变更事件。
    pub fn record_probe(&mut self, now_str: &str, elapsed_ms: i64, fp: BarFingerprint) -> ProbeResult {
        self.probe_count += 1;
        if self.first_seen_at.is_none() {
            self.first_seen_at = Some(now_str.to_string());
            self.first_seen_delay_ms = Some(elapsed_ms);
        }

        let mut is_revision = false;
        let mut became_candidate_final = false;
        let mut became_false_final = false;

        match &self.last_fingerprint {
            None => {
                self.last_fingerprint = Some(fp.clone());
                self.same_count = 1;
            }
            Some(last) if last == &fp => {
                self.same_count += 1;
                // 首次达成连续 3 次完全相同：定为 Candidate Final
                // 首次确立后时间戳永久冻结，绝不被后续重复一致覆盖
                if self.same_count == 3 && self.candidate_final_at.is_none() {
                    self.candidate_final_at = Some(now_str.to_string());
                    self.candidate_delay_ms = Some(elapsed_ms);
                    self.candidate_fingerprint = Some(fp.signature());
                    became_candidate_final = true;
                }
            }
            Some(_) => {
                is_revision = true;
                self.revision_count += 1;
                self.last_revision_at = Some(now_str.to_string());
                self.last_revision_delay_ms = Some(elapsed_ms);

                // 若此前已达到 Candidate Final，但随后又发生修改 -> 记录 False Final!
                if self.candidate_final_at.is_some() && !self.false_final {
                    self.false_final = true;
                    became_false_final = true;
                }

                self.last_fingerprint = Some(fp.clone());
                self.same_count = 1; // 重置稳定计数
            }
        }

        self.final_fingerprint = Some(fp.signature());

        ProbeResult {
            is_revision,
            became_candidate_final,
            became_false_final,
            same_count: self.same_count,
        }
    }

    /// 转换为可持久化的试验汇总结构。
    pub fn to_trial(&self, completed: bool, updated_at: &str) -> FinalityTrial {
        FinalityTrial {
            id: None,
            symbol: self.symbol.clone(),
            bar_ts: self.bar_ts.clone(),
            session_type: self.session_type.as_str().to_string(),
            first_seen_at: self.first_seen_at.clone(),
            first_seen_delay_ms: self.first_seen_delay_ms,
            candidate_final_at: self.candidate_final_at.clone(),
            candidate_delay_ms: self.candidate_delay_ms,
            revision_count: self.revision_count as i32,
            last_revision_at: self.last_revision_at.clone(),
            last_revision_delay_ms: self.last_revision_delay_ms,
            false_final: self.false_final,
            candidate_fingerprint: self.candidate_fingerprint.clone(),
            final_fingerprint: self.final_fingerprint.clone(),
            probe_count: self.probe_count as i32,
            completed,
            created_at: self.created_at.clone(),
            updated_at: updated_at.to_string(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fp(val: &str) -> BarFingerprint {
        BarFingerprint::new("2026-08-28 10:45:00", val, val, val, val, "100", "1000")
    }

    #[test]
    fn test_candidate_final_normal_flow() {
        let mut tracker = BarFinalityTracker::new("RB0", "2026-08-28 10:45:00", SessionType::Normal);
        let res1 = tracker.record_probe("10:45:00", 0, fp("100"));
        assert_eq!(res1.same_count, 1);
        assert!(!res1.became_candidate_final);

        let res2 = tracker.record_probe("10:45:05", 5000, fp("100"));
        assert_eq!(res2.same_count, 2);
        assert!(!res2.became_candidate_final);

        let res3 = tracker.record_probe("10:45:10", 10000, fp("100"));
        assert_eq!(res3.same_count, 3);
        assert!(res3.became_candidate_final);
        assert_eq!(tracker.candidate_delay_ms, Some(10000));
        assert_eq!(tracker.candidate_final_at.as_deref(), Some("10:45:10"));

        // 后续继续保持相同，不应再次触发 became_candidate_final，且时间不被覆盖
        let res4 = tracker.record_probe("10:45:15", 15000, fp("100"));
        assert_eq!(res4.same_count, 4);
        assert!(!res4.became_candidate_final);
        assert_eq!(tracker.candidate_delay_ms, Some(10000));
        assert!(!tracker.false_final);
    }

    #[test]
    fn test_candidate_final_late_revision_triggers_false_final() {
        let mut tracker = BarFinalityTracker::new("CJ0", "2026-08-28 11:30:00", SessionType::Close1130);
        tracker.record_probe("11:30:00", 0, fp("100"));
        tracker.record_probe("11:30:05", 5000, fp("100"));
        let res3 = tracker.record_probe("11:30:10", 10000, fp("100"));
        assert!(res3.became_candidate_final);
        assert!(!tracker.false_final);

        // 继续保持到 11:30:20
        tracker.record_probe("11:30:15", 15000, fp("100"));
        tracker.record_probe("11:30:20", 20000, fp("100"));

        // 11:30:25 突发新浪晚修改！
        let res_late = tracker.record_probe("11:30:25", 25000, fp("105"));
        assert!(res_late.is_revision);
        assert!(res_late.became_false_final);
        assert!(tracker.false_final);
        assert_eq!(tracker.candidate_delay_ms, Some(10000));
        assert_eq!(tracker.last_revision_delay_ms, Some(25000));
        assert_eq!(tracker.revision_count, 1);
    }

    #[test]
    fn test_early_revision_then_stabilize() {
        let mut tracker = BarFinalityTracker::new("PB0", "2026-08-28 11:30:00", SessionType::Close1130);
        tracker.record_probe("11:30:00", 0, fp("100"));
        // 5秒时变动
        let res2 = tracker.record_probe("11:30:05", 5000, fp("102"));
        assert!(res2.is_revision);
        assert_eq!(res2.same_count, 1);
        assert!(!res2.became_candidate_final);
        assert!(!tracker.false_final);

        tracker.record_probe("11:30:10", 10000, fp("102"));
        let res4 = tracker.record_probe("11:30:15", 15000, fp("102"));
        assert!(res4.became_candidate_final);
        assert_eq!(tracker.candidate_delay_ms, Some(15000));
        assert!(!tracker.false_final);
        assert_eq!(tracker.revision_count, 1);
        assert_eq!(tracker.last_revision_delay_ms, Some(5000));
    }
}
