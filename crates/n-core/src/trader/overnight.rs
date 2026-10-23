use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;

use chrono::{DateTime, Days, Local, Timelike};
use serde::{Deserialize, Serialize};

use super::execution::ManagedPosition;
use super::model::ExecutionQuote;
use crate::session::calendar::classify_night_session;
use crate::session::spec::NightSessionType;
use crate::session::trading_calendar::{has_night_session_tonight, is_trading_day};

/// 收盘前检查点类型
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CheckpointType {
    /// 日盘收盘前（15:00 前）
    DayClose,
    /// 夜盘收盘前（23:00 / 23:30 / 01:00 / 02:30 前）
    NightClose,
}

/// 跨时段/隔夜影子跟踪快照
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OvernightSnapshot {
    /// 唯一标识: format!("{}:{}:{:?}", position_id, trading_day, checkpoint_type)
    pub id: String,
    pub position_id: String,
    pub symbol: String,
    pub instrument_id: String,
    pub direction: String, // "up" / "down"
    pub volume: i32,
    pub fill_price: f64,
    pub current_stop_price: f64,
    pub risk: f64,
    pub open_ts: String,
    pub trading_day: String,

    // --- T-2m 快照指标 ---
    pub checkpoint_type: CheckpointType,
    pub checkpoint_ts: String,
    pub checkpoint_last: f64,
    pub checkpoint_bid: f64,
    pub checkpoint_ask: f64,
    /// 假设立即以对价平仓的价格（多头取 Bid，空头取 Ask）
    pub hypo_exit_price: f64,
    /// 假设平仓的盈亏点数
    pub hypo_pnl_pts: f64,
    /// 假设平仓的 R 倍数
    pub hypo_pnl_r: f64,

    // --- T=0 收盘漂移指标（可选） ---
    pub session_close_price: Option<f64>,
    pub late_drift_pts: Option<f64>,

    // --- 下一时段开盘跳空指标（待回填） ---
    pub next_open_ts: Option<String>,
    pub next_open_price: Option<f64>,
    pub gap_pts: Option<f64>,
    pub gap_r: Option<f64>,
    pub gap_penetrated_stop: Option<bool>,

    // --- 实盘最终自然出场数据（待回填） ---
    pub actual_exit_ts: Option<String>,
    pub actual_exit_price: Option<f64>,
    pub actual_exit_reason: Option<String>,
    pub actual_realized_pnl_pts: Option<f64>,
    pub actual_realized_pnl_r: Option<f64>,

    // --- 核心归因指标 ---
    /// 隔夜实际超额收益点数 (actual - hypo)
    pub overnight_delta_pts: Option<f64>,
    /// 隔夜实际超额收益 R 倍数 (actual_r - hypo_r)
    pub overnight_delta_r: Option<f64>,
    /// 记录生成时间
    pub created_at: i64,
}

impl OvernightSnapshot {
    /// 从当前持仓与盘口行情构建收盘前快照
    pub fn create(
        pos: &ManagedPosition,
        quote: &ExecutionQuote,
        checkpoint_type: CheckpointType,
        trading_day: &str,
    ) -> Self {
        let is_up = pos.direction == "up";
        // 多头以买一（Bid）平仓，空头以卖一（Ask）平仓；无买卖一价时降级为最新价（Last）
        let hypo_exit_price = if is_up {
            if quote.bid > 0.0 {
                quote.bid
            } else {
                quote.last
            }
        } else {
            if quote.ask > 0.0 {
                quote.ask
            } else {
                quote.last
            }
        };

        let sign = if is_up { 1.0 } else { -1.0 };
        let hypo_pnl_pts = (hypo_exit_price - pos.fill_price) * sign;
        let risk = pos.risk.max(1e-6);
        let hypo_pnl_r = hypo_pnl_pts / risk;
        let now = chrono::Local::now();
        let checkpoint_ts = now.format("%Y-%m-%d %H:%M:%S").to_string();
        let id = format!("{}:{}:{:?}", pos.id, trading_day, checkpoint_type);

        Self {
            id,
            position_id: pos.id.clone(),
            symbol: pos.symbol.clone(),
            instrument_id: pos.instrument_id.clone(),
            direction: pos.direction.clone(),
            volume: pos.volume,
            fill_price: pos.fill_price,
            current_stop_price: pos.current_stop_price,
            risk: pos.risk,
            open_ts: pos.open_ts.clone(),
            trading_day: trading_day.to_string(),
            checkpoint_type,
            checkpoint_ts,
            checkpoint_last: quote.last,
            checkpoint_bid: quote.bid,
            checkpoint_ask: quote.ask,
            hypo_exit_price,
            hypo_pnl_pts,
            hypo_pnl_r,
            session_close_price: None,
            late_drift_pts: None,
            next_open_ts: None,
            next_open_price: None,
            gap_pts: None,
            gap_r: None,
            gap_penetrated_stop: None,
            actual_exit_ts: None,
            actual_exit_price: None,
            actual_exit_reason: None,
            actual_realized_pnl_pts: None,
            actual_realized_pnl_r: None,
            overnight_delta_pts: None,
            overnight_delta_r: None,
            created_at: now.timestamp(),
        }
    }

    /// 回填收盘价（观察最后 2 分钟价格漂移）
    pub fn update_close_price(&mut self, close_price: f64) {
        if close_price > 0.0 {
            let sign = if self.direction == "up" { 1.0 } else { -1.0 };
            self.session_close_price = Some(close_price);
            self.late_drift_pts = Some((close_price - self.hypo_exit_price) * sign);
        }
    }

    /// 回填下一时段开盘跳空
    pub fn update_next_open(&mut self, open_price: f64, open_ts: &str) {
        if open_price > 0.0 && self.next_open_price.is_none() {
            let sign = if self.direction == "up" { 1.0 } else { -1.0 };
            let gap_pts = (open_price - self.hypo_exit_price) * sign;
            let risk = self.risk.max(1e-6);
            let gap_r = gap_pts / risk;

            // 检查开盘价是否击穿保护止损
            let stop_penetrated = if self.direction == "up" {
                open_price <= self.current_stop_price
            } else {
                open_price >= self.current_stop_price
            };

            self.next_open_ts = Some(open_ts.to_string());
            self.next_open_price = Some(open_price);
            self.gap_pts = Some(gap_pts);
            self.gap_r = Some(gap_r);
            self.gap_penetrated_stop = Some(stop_penetrated);
        }
    }

    /// 回填实盘最终出场数据并完成归因计算
    pub fn finalize_actual_exit(
        &mut self,
        exit_price: f64,
        exit_ts: &str,
        exit_reason: &str,
        realized_pnl_pts: f64,
        realized_pnl_r: f64,
    ) {
        self.actual_exit_price = Some(exit_price);
        self.actual_exit_ts = Some(exit_ts.to_string());
        self.actual_exit_reason = Some(exit_reason.to_string());
        self.actual_realized_pnl_pts = Some(realized_pnl_pts);
        self.actual_realized_pnl_r = Some(realized_pnl_r);

        // 计算差额: 实际持仓收益 - 假设平仓收益
        let delta_pts = realized_pnl_pts - self.hypo_pnl_pts;
        let delta_r = realized_pnl_r - self.hypo_pnl_r;
        self.overnight_delta_pts = Some(delta_pts);
        self.overnight_delta_r = Some(delta_r);
    }
}

/// 追加一条快照记录到指定的 JSONL 文件中
pub fn append_snapshot_jsonl(path: &Path, snapshot: &OvernightSnapshot) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    let line = serde_json::to_string(snapshot)?;
    writeln!(file, "{}", line)?;
    file.flush()?;
    Ok(())
}

/// 检查给定时刻是否落在该品种的收盘前 N 分钟打点窗口内（默认 pre_close_minutes = 2）
///
/// 返回 `Some(CheckpointType)` 表示当前时刻属于对应时段的收盘前打点时刻。
pub fn check_pre_close_checkpoint(
    symbol: &str,
    now: &DateTime<Local>,
    pre_close_minutes: u32,
) -> Option<CheckpointType> {
    let date = now.date_naive();
    let (h, m) = (now.hour(), now.minute());
    let now_mins = h * 60 + m;

    // 1. 日盘收盘判定：15:00 收盘
    // 当 pre_close_minutes = 2 时，目标分钟为 14:58 (14*60 + 58 = 898)
    let day_close_mins = 15 * 60;
    if day_close_mins >= pre_close_minutes && now_mins == day_close_mins - pre_close_minutes {
        if is_trading_day(&date) {
            return Some(CheckpointType::DayClose);
        }
    }

    // 2. 夜盘收盘判定：依据品种的 NightSessionType 确定
    let night_type = classify_night_session(symbol);
    if night_type == NightSessionType::None {
        return None;
    }

    let (close_h, close_m) = match night_type {
        NightSessionType::None => return None,
        NightSessionType::Close2300 => (23, 0),
        NightSessionType::Close2330 => (23, 30),
        NightSessionType::Close0100 => (1, 0),
        NightSessionType::Close0230 => (2, 30),
    };

    // 计算夜盘收盘前的目标时间 (时, 分)
    let close_total_mins = close_h * 60 + close_m;
    let target_mins = if close_total_mins >= pre_close_minutes {
        close_total_mins - pre_close_minutes
    } else {
        // 跨日前（例如 00:00 前 N 分钟，这里 01:00 减 2 分钟不会为负）
        close_total_mins + 24 * 60 - pre_close_minutes
    };

    if now_mins != target_mins {
        return None;
    }

    // 校验夜盘有效性
    if h >= 20 {
        // 当天晚上 20 点后（如 22:58 / 23:28）
        if has_night_session_tonight(&date) {
            return Some(CheckpointType::NightClose);
        }
    } else if h < 4 {
        // 次日凌晨（如 00:58 / 02:28），夜盘起始于前一天晚上
        let prev_date = date - Days::new(1);
        if has_night_session_tonight(&prev_date) {
            return Some(CheckpointType::NightClose);
        }
    }

    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn local_dt(y: i32, m: u32, d: u32, h: u32, min: u32) -> DateTime<Local> {
        Local.with_ymd_and_hms(y, m, d, h, min, 0).unwrap()
    }

    #[test]
    fn test_day_close_checkpoint_trigger() {
        // 2026-09-24 是交易日 (周四)
        // 14:58 是 15:00 收盘前 2 分钟
        let t_hit = local_dt(2026, 9, 24, 14, 58);
        assert_eq!(
            check_pre_close_checkpoint("RB", &t_hit, 2),
            Some(CheckpointType::DayClose)
        );

        // 14:57 不是打点时间
        let t_miss = local_dt(2026, 9, 24, 14, 57);
        assert_eq!(check_pre_close_checkpoint("RB", &t_miss, 2), None);

        // 15:00 不是打点时间
        let t_close = local_dt(2026, 9, 24, 15, 0);
        assert_eq!(check_pre_close_checkpoint("RB", &t_close, 2), None);
    }

    #[test]
    fn test_night_close_2300_trigger() {
        // RB 默认 23:00 收盘，提前 2 分钟为 22:58
        let t_hit = local_dt(2026, 9, 24, 22, 58);
        assert_eq!(
            check_pre_close_checkpoint("RB", &t_hit, 2),
            Some(CheckpointType::NightClose)
        );

        // 22:59 不是
        let t_miss = local_dt(2026, 9, 24, 22, 59);
        assert_eq!(check_pre_close_checkpoint("RB", &t_miss, 2), None);
    }

    #[test]
    fn test_night_close_various_types() {
        // CU 是 01:00 收盘，提前 2 分钟为 00:58
        // 假设 2026-09-24 晚上开启夜盘，跨日为 2026-09-25 00:58
        let t_cu = local_dt(2026, 9, 25, 0, 58);
        assert_eq!(
            check_pre_close_checkpoint("CU", &t_cu, 2),
            Some(CheckpointType::NightClose)
        );

        // AU 是 02:30 收盘，提前 2 分钟为 02:28
        let t_au = local_dt(2026, 9, 25, 2, 28);
        assert_eq!(
            check_pre_close_checkpoint("AU", &t_au, 2),
            Some(CheckpointType::NightClose)
        );

        // 无夜盘品种（如 CJ 红枣），在 22:58 不应触发夜盘打点
        assert_eq!(check_pre_close_checkpoint("CJ", &t_hit_rb_night(), 2), None);
    }

    fn t_hit_rb_night() -> DateTime<Local> {
        Local.with_ymd_and_hms(2026, 9, 24, 22, 58, 0).unwrap()
    }

    #[test]
    fn test_snapshot_calculations() {
        let pos = ManagedPosition {
            origin: Default::default(),
            id: "pos_123".into(),
            condition_order_id: "cond_123".into(),
            symbol: "RB".into(),
            instrument_id: "rb2501".into(),
            direction: "up".into(), // 多头
            tier: crate::trader::PositionTier::Small,
            fill_price: 3500.0,
            initial_stop_price: 3480.0,
            target_price: 3540.0,
            risk: 20.0,
            volume: 1,
            open_ts: "2026-09-24 14:00:00".into(),
            bars_held: 4,
            max_mfe_r: 1.0,
            max_mae_r: 0.0,
            trailing_grade: 0,
            current_stop_price: 3480.0,
            lot1_closed: false,
            exit_reason: None,
            exit_price: None,
            is_closed: false,
            realized_pnl_pts: None,
            r_multiple: None,
            event_id: None,
            planned_entry_price: None,
            score: None,
            warning_bar_ts: None,
            trigger_ts: None,
            order_insert_ts: None,
        };

        let quote = ExecutionQuote {
            instrument_id: "rb2501".into(),
            exchange_id: "SHFE".into(),
            underlying_symbol: "RB".into(),
            last: 3515.0,
            bid: 3514.0, // 多头对价平仓成交价
            ask: 3516.0,
            tick: 1.0,
            lower: 3300.0,
            upper: 3700.0,
            timestamp: chrono::Utc::now().timestamp(),
        };

        let mut snap =
            OvernightSnapshot::create(&pos, &quote, CheckpointType::DayClose, "2026-09-24");
        // 多头以 Bid 平仓: 3514 - 3500 = +14 点
        assert_eq!(snap.hypo_exit_price, 3514.0);
        assert_eq!(snap.hypo_pnl_pts, 14.0);
        assert_eq!(snap.hypo_pnl_r, 14.0 / 20.0); // 0.7R

        // 模拟下一时段跳空高开到 3530
        snap.update_next_open(3530.0, "2026-09-24 21:00:00");
        assert_eq!(snap.next_open_price, Some(3530.0));
        assert_eq!(snap.gap_pts, Some(16.0)); // 3530 - 3514 = +16 点跳空获利
        assert_eq!(snap.gap_penetrated_stop, Some(false));

        // 模拟最终在次日以 3540 止盈出场 (+40 点, +2.0R)
        snap.finalize_actual_exit(3540.0, "2026-09-25 09:15:00", "target_1r", 40.0, 2.0);
        // 隔夜超额收益 = 40.0 - 14.0 = +26.0 点
        assert_eq!(snap.overnight_delta_pts, Some(26.0));
        assert_eq!(snap.overnight_delta_r, Some(1.3)); // 2.0 - 0.7 = +1.3R
    }
}
