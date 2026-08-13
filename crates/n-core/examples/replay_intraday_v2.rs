use anyhow::{anyhow, Result};
use chrono::{Datelike, NaiveDate, NaiveDateTime};
use sea_orm::{ColumnTrait, Database, DatabaseConnection, EntityTrait, QueryFilter, QueryOrder};
use std::collections::{BTreeMap, HashMap, HashSet};

use n_core::analyze::dto::PatternDto;
use n_core::analyze::model::{Bar, ATR_PERIOD, DT};
use n_core::analyze::{analyze_bars_for_version, AnalysisOutcome};
use n_core::storage::entities::klines;
use n_core::storage::repo;

#[derive(Default)]
struct DayCounts {
    snapshots: usize,
    n_unique: usize,
    box_unique: usize,
    unique_keys: HashSet<String>,
    triggered_keys: HashSet<String>,
    new_warning_keys: HashSet<String>,
    new_warning_rail_keys: HashSet<String>,
    new_trigger_keys: HashSet<String>,
    close_keys: HashSet<String>,
    details: BTreeMap<String, (String, String, String, Option<String>, Option<String>, f64)>,
}

impl DayCounts {
    fn add(
        &mut self,
        key: &str,
        triggered: bool,
        is_box: bool,
        symbol: &str,
        direction: &str,
        level: &str,
        warning_ts: Option<&str>,
        trigger_ts: Option<&str>,
        score: f64,
        day: &str,
        is_close: bool,
    ) {
        self.snapshots += 1;
        if self.unique_keys.insert(key.to_string()) {
            if is_box {
                self.box_unique += 1;
            } else {
                self.n_unique += 1;
            }
            self.details.insert(
                key.to_string(),
                (
                    symbol.to_string(),
                    direction.to_string(),
                    level.to_string(),
                    warning_ts.map(|s| s.to_string()),
                    trigger_ts.map(|s| s.to_string()),
                    score,
                ),
            );
        }
        if let Some(w) = warning_ts {
            if w.starts_with(day) {
                self.new_warning_keys.insert(key.to_string());
                let rail_key = if is_box {
                    key.rsplit_once('|')
                        .map(|(base, _)| base.to_string())
                        .unwrap_or_else(|| key.to_string())
                } else {
                    key.to_string()
                };
                self.new_warning_rail_keys.insert(rail_key);
            }
        }
        if triggered {
            self.triggered_keys.insert(key.to_string());
            if let Some(t) = trigger_ts {
                if t.starts_with(day) {
                    self.new_trigger_keys.insert(key.to_string());
                }
            }
        }
        if is_close {
            self.close_keys.insert(key.to_string());
        }
    }
}

fn parse_ts(s: &str) -> Result<DT> {
    let (date, time) = s.split_once(' ').ok_or_else(|| anyhow!("bad ts {s}"))?;
    let mut dp = date.split('-');
    let year = dp.next().and_then(|x| x.parse().ok()).unwrap_or(0);
    let month = dp.next().and_then(|x| x.parse().ok()).unwrap_or(0);
    let day = dp.next().and_then(|x| x.parse().ok()).unwrap_or(0);
    let mut tp = time.split(':');
    let hour = tp.next().and_then(|x| x.parse().ok()).unwrap_or(0);
    let minute = tp.next().and_then(|x| x.parse().ok()).unwrap_or(0);
    let second = tp.next().and_then(|x| x.parse().ok()).unwrap_or(0);
    Ok(DT {
        year,
        month,
        day,
        hour,
        minute: minute + second / 60,
    })
}

fn bar_time(b: &Bar) -> NaiveDateTime {
    NaiveDate::from_ymd_opt(b.dt.year, b.dt.month as u32, b.dt.day as u32)
        .unwrap()
        .and_hms_opt(b.dt.hour as u32, b.dt.minute as u32, 0)
        .unwrap()
}

fn bar_date(s: &str) -> Option<(i32, u32, u32)> {
    let dt = NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M").ok()?;
    Some((dt.year(), dt.month(), dt.day()))
}

async fn bars_for(db: &DatabaseConnection, symbol: &str, tf: &str) -> Result<Vec<Bar>> {
    let rows = klines::Entity::find()
        .filter(klines::Column::Symbol.eq(symbol))
        .filter(klines::Column::Timeframe.eq(tf))
        .order_by_asc(klines::Column::Ts)
        .all(db)
        .await?;
    rows.iter()
        .map(|r| {
            Ok(Bar {
                dt: parse_ts(&r.ts)?,
                open: r.open,
                high: r.high,
                low: r.low,
                close: r.close,
                volume: r.volume,
                hold: r.hold,
                rollover: false,
            })
        })
        .collect()
}

fn count_signals(out: &AnalysisOutcome, counts: &mut DayCounts, day: &str, is_close: bool) {
    for s in out.detail.signals.iter().filter(|s| s.active) {
        let key = signal_key(&out.detail.symbol, s);
        counts.add(
            &key,
            s.trigger_ts.is_some(),
            s.level == "box",
            &out.detail.symbol,
            &s.direction,
            &s.level,
            s.warning_ts.as_deref(),
            s.trigger_ts.as_deref(),
            s.score,
            day,
            is_close,
        );
    }
}

fn signal_key(symbol: &str, s: &PatternDto) -> String {
    if s.level == "box" {
        if let Some(b) = &s.r#box {
            format!(
                "{}|{}|box|{:.6}|{:.6}|{}",
                symbol,
                s.direction,
                b.upper,
                b.lower,
                s.warning_ts.as_deref().unwrap_or("")
            )
        } else {
            format!(
                "{}|{}|box|{:.6}|{:.6}|{}",
                symbol,
                s.direction,
                0.0,
                0.0,
                s.warning_ts.as_deref().unwrap_or("")
            )
        }
    } else {
        format!(
            "{}|{}|{}|{}|{}",
            symbol, s.direction, s.level, s.s1.ts, s.s2.ts
        )
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let db_url = "sqlite://C:/Users/Xbss/AppData/Roaming/com.ntrend.app/ntrend.db?mode=ro";
    let db = Database::connect(db_url).await?;
    let symbols = repo::list_symbols(&db, true).await?;
    let tick_by: HashMap<String, f64> = symbols
        .iter()
        .map(|s| (s.code.clone(), s.tick_size.max(0.0)))
        .collect();
    let days = [
        "2026-07-30",
        "2026-07-31",
        "2026-08-03",
        "2026-08-04",
        "2026-08-05",
        "2026-08-06",
        "2026-08-07",
        "2026-08-10",
        "2026-08-11",
        "2026-08-12",
    ];

    let mut totals_v1 = DayCounts::default();
    let mut totals_v2 = DayCounts::default();
    for day in days {
        let mut v1 = DayCounts::default();
        let mut v2 = DayCounts::default();
        for sym in &symbols {
            let bars15 = bars_for(&db, &sym.code, "15m").await?;
            let bars60 = bars_for(&db, &sym.code, "60m").await?;
            let tick = tick_by.get(&sym.code).copied().unwrap_or(1.0);
            for i in 0..bars15.len() {
                if i + 1 < ATR_PERIOD + 2 {
                    continue;
                }
                let Some((y, m, d)) = bar_date(&bars15[i].dt.to_string()) else {
                    continue;
                };
                let ds = format!("{y:04}-{m:02}-{d:02}");
                if ds != day {
                    continue;
                }
                let end = bar_time(&bars15[i]);
                let idx60 = bars60.partition_point(|b| bar_time(b) <= end);
                if idx60 < ATR_PERIOD + 2 {
                    continue;
                }
                let is_last_bar_of_day =
                    i + 1 == bars15.len() || !bars15[i + 1].dt.to_string().starts_with(day);
                for version in ["1", "2"] {
                    let outcome = analyze_bars_for_version(
                        &sym.code,
                        &bars15[..=i],
                        &bars60[..idx60],
                        tick,
                        version,
                    )?;
                    let counts = if version == "1" { &mut v1 } else { &mut v2 };
                    count_signals(&outcome, counts, day, is_last_bar_of_day);
                }
            }
        }
        println!(
            "{}  1.x snap={} unique={} newW={} newT={} close={} trig={} | 2.0 snap={} unique={} (n={}, box={}) newW={} rail={} newT={} close={} trig={}",
            day,
            v1.snapshots,
            v1.unique_keys.len(),
            v1.new_warning_keys.len(),
            v1.new_trigger_keys.len(),
            v1.close_keys.len(),
            v1.triggered_keys.len(),
            v2.snapshots,
            v2.unique_keys.len(),
            v2.n_unique,
            v2.box_unique,
            v2.new_warning_keys.len(),
            v2.new_warning_rail_keys.len(),
            v2.new_trigger_keys.len(),
            v2.close_keys.len(),
            v2.triggered_keys.len(),
        );
        println!(
            "  1.x close={} newW={} newT={} | 2.0 close={} newW={} rail={} newT={}",
            v1.close_keys.len(),
            v1.new_warning_keys.len(),
            v1.new_trigger_keys.len(),
            v2.close_keys.len(),
            v2.new_warning_keys.len(),
            v2.new_warning_rail_keys.len(),
            v2.new_trigger_keys.len(),
        );
        println!("  2.0 details:");
        for (key, (sym, dir, level, warn, trig, score)) in &v2.details {
            let warn = warn.as_deref().unwrap_or("-");
            let trig = trig.as_deref().unwrap_or("-");
            println!(
                "    {:>4} {:>2} {:<5} warn={} trig={} score={:.2} ({})",
                sym, dir, level, warn, trig, score, key
            );
        }
        merge_counts(&mut totals_v1, &v1);
        merge_counts(&mut totals_v2, &v2);
    }
    println!(
        "total 1.x snap={} unique={} newW={} newT={} close={} trig={} | 2.0 snap={} unique={} newW={} rail={} newT={} close={} trig={}",
        totals_v1.snapshots,
        totals_v1.unique_keys.len(),
        totals_v1.new_warning_keys.len(),
        totals_v1.new_trigger_keys.len(),
        totals_v1.close_keys.len(),
        totals_v1.triggered_keys.len(),
        totals_v2.snapshots,
        totals_v2.unique_keys.len(),
        totals_v2.new_warning_keys.len(),
        totals_v2.new_warning_rail_keys.len(),
        totals_v2.new_trigger_keys.len(),
        totals_v2.close_keys.len(),
        totals_v2.triggered_keys.len(),
    );
    Ok(())
}

fn merge_counts(target: &mut DayCounts, src: &DayCounts) {
    target.snapshots += src.snapshots;
    target.n_unique += src.n_unique;
    target.box_unique += src.box_unique;
    for k in &src.unique_keys {
        target.unique_keys.insert(k.clone());
    }
    for k in &src.triggered_keys {
        target.triggered_keys.insert(k.clone());
    }
    for k in &src.new_warning_keys {
        target.new_warning_keys.insert(k.clone());
    }
    for k in &src.new_warning_rail_keys {
        target.new_warning_rail_keys.insert(k.clone());
    }
    for k in &src.new_trigger_keys {
        target.new_trigger_keys.insert(k.clone());
    }
    for k in &src.close_keys {
        target.close_keys.insert(k.clone());
    }
}
