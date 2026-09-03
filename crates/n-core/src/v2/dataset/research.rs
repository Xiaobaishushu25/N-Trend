//! Stage-2 economic-hypothesis reports.
//!
//! These reports are descriptive only: they never fit a model and never use
//! the final 300 rows.  Regime thresholds are fixed constants by design.

use std::collections::BTreeMap;
use crate::v2::dataset::DatasetRow;
use crate::v2::model::{split_final_holdout, walk_forward_purge_aware};

const TREND_GAP_THRESHOLD: f64 = 0.3;
const ADX_THRESHOLD: f64 = 0.25;

#[derive(Default, Clone, Copy)]
struct Cell { n: usize, wins: usize }
impl Cell {
    fn add(&mut self, row: &DatasetRow) { self.n += 1; self.wins += usize::from(row.label_win == 1); }
    fn wr(self) -> f64 { if self.n == 0 { 0.0 } else { self.wins as f64 / self.n as f64 } }
}

fn trend_regime(row: &DatasetRow) -> Option<&'static str> {
    let gap = row.trend_gap_60?;
    let strength = row.trend_strength_60?;
    if strength < ADX_THRESHOLD { return Some("Range/Weak"); }
    if gap > TREND_GAP_THRESHOLD { Some("Strong Up") }
    else if gap < -TREND_GAP_THRESHOLD { Some("Strong Down") }
    else { Some("Range/Weak") }
}

fn position_bucket(value: f64) -> &'static str {
    if value < 0.0 { "Below 10d low" }
    else if value < 1.0 / 3.0 { "Low" }
    else if value <= 2.0 / 3.0 { "Middle" }
    else if value <= 1.0 { "High" }
    else { "Above 10d high" }
}

fn coarse_position(value: f64) -> &'static str {
    if value < 1.0 / 3.0 { "Low" } else if value <= 2.0 / 3.0 { "Middle" } else { "High" }
}

fn fmt_cell(cell: Cell, prior: f64) -> String {
    if cell.n == 0 { "—".into() } else { format!("{} / {:.1}% / {:.3}", cell.n, cell.wr() * 100.0, prior) }
}

/// Render the fixed-threshold Trend × Direction × Position research report.
/// The first line of every table is based on DEV; the fold section records
/// how many of four causal folds support the same directional relationship.
pub fn render_market_context_research(rows: &[DatasetRow]) -> String {
    let (dev, locked) = split_final_holdout(rows, 300);
    let prior = dev.iter().filter(|r| r.label_win == 1).count() as f64 / dev.len().max(1) as f64;
    let mut trend: BTreeMap<(&str, &str), Cell> = BTreeMap::new();
    let mut position: BTreeMap<(&str, &str), Cell> = BTreeMap::new();
    let mut matrix: BTreeMap<(&str, &str, &str), Cell> = BTreeMap::new();
    for row in dev {
        if let Some(regime) = trend_regime(row) { trend.entry((regime, row.direction.as_str())).or_default().add(row); }
        if let Some(pos) = row.range_position_10d {
            let bucket = position_bucket(pos);
            position.entry((bucket, row.direction.as_str())).or_default().add(row);
            if let Some(regime) = trend_regime(row) { matrix.entry((regime, row.direction.as_str(), coarse_position(pos))).or_default().add(row); }
        }
    }
    let mut out = String::new();
    out.push_str("# Market Context Economic Hypotheses (DEV only)\n\n");
    out.push_str(&format!("Fixed rules: trend gap threshold = {:.1} ATR, ADX threshold = {:.2}; RR excluded.\n\n", TREND_GAP_THRESHOLD, ADX_THRESHOLD));
    out.push_str(&format!("Rows: DEV {} / LOCKED_HISTORICAL_TEST {}. Average DEV baseline P = {:.3}.\n\n", rows.len().saturating_sub(locked.len()), locked.len(), prior));
    out.push_str("`n / WR / baseline P`\n\n");
    out.push_str("## Trend × Direction\n\n| Regime | Long | Short |\n|---|---:|---:|\n");
    for regime in ["Strong Up", "Range/Weak", "Strong Down"] {
        out.push_str(&format!("| {} | {} | {} |\n", regime, fmt_cell(*trend.get(&(regime, "up")).unwrap_or(&Cell::default()), prior), fmt_cell(*trend.get(&(regime, "down")).unwrap_or(&Cell::default()), prior)));
    }
    out.push_str("\n## Position × Direction\n\n| Position | Long | Short |\n|---|---:|---:|\n");
    for bucket in ["Below 10d low", "Low", "Middle", "High", "Above 10d high"] {
        out.push_str(&format!("| {} | {} | {} |\n", bucket, fmt_cell(*position.get(&(bucket, "up")).unwrap_or(&Cell::default()), prior), fmt_cell(*position.get(&(bucket, "down")).unwrap_or(&Cell::default()), prior)));
    }
    out.push_str("\n## Trend × Position\n\n| Trend regime | Low | Middle | High |\n|---|---:|---:|---:|\n");
    for regime in ["Strong Down", "Range/Weak", "Strong Up"] {
        out.push_str(&format!("| {} | {} | {} | {} |\n", regime,
            fmt_cell(*matrix.get(&(regime, "up", "Low")).unwrap_or(&Cell::default()), prior),
            fmt_cell(*matrix.get(&(regime, "up", "Middle")).unwrap_or(&Cell::default()), prior),
            fmt_cell(*matrix.get(&(regime, "up", "High")).unwrap_or(&Cell::default()), prior)));
    }
    out.push_str("\n## Four-fold directional support\n\n");
    let folds = walk_forward_purge_aware(dev, 4);
    let mut h2 = 0usize;
    for fold in folds.iter().filter(|f| f.valid_end > f.valid_start) {
        let slice = &dev[fold.valid_start..fold.valid_end];
        let mut up_low = Cell::default(); let mut up_high = Cell::default();
        let mut down_high = Cell::default(); let mut down_low = Cell::default();
        for row in slice {
            if let Some(p) = row.range_position_10d {
                match (row.direction.as_str(), coarse_position(p)) {
                    ("up", "Low") => up_low.add(row), ("up", "High") => up_high.add(row),
                    ("down", "High") => down_high.add(row), ("down", "Low") => down_low.add(row), _ => {}
                }
            }
        }
        if up_low.n > 0 && up_high.n > 0 && up_low.wr() > up_high.wr() { h2 += 1; }
        if down_high.n > 0 && down_low.n > 0 && down_high.wr() > down_low.wr() { h2 += 1; }
    }
    out.push_str(&format!("- H1 trend-direction support: descriptive table above; fixed thresholds, {} causal folds available.\n- H2 position support: {} directional comparisons supported across folds (max {} for two comparisons × four folds).\n- H3 interaction: inspect the fixed matrix; no model fitting or threshold search was performed.\n", folds.len(), h2, folds.len() * 2));
    out
}
