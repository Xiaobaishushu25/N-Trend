use anyhow::{anyhow, Result};
use chrono::{Datelike, NaiveDate, NaiveDateTime};
use sea_orm::{ColumnTrait, Database, DatabaseConnection, EntityTrait, QueryFilter, QueryOrder};
use std::collections::{BTreeMap, HashMap};
use std::env;

use n_core::analyze::analyze_bars_for_version;
use n_core::analyze::dto::PatternDto;
use n_core::analyze::model::{Bar, ATR_PERIOD, DT};
use n_core::analyze::outcome::{self, Outcome, SignalInput};
use n_core::storage::entities::{klines, rollovers};
use n_core::storage::repo;

#[derive(Default, Debug)]
struct Stats {
    total: usize,
    wins: usize,
    losses: usize,
    no_trigger: usize,
    open: usize,
    insufficient: usize,
    rollover: usize,
    invalid: usize,
    r_sum: f64,
    exits: BTreeMap<String, usize>,
}

impl Stats {
    fn record(&mut self, ann: &outcome::SignalAnnotation) {
        self.total += 1;
        let reason = ann.exit_reason.as_str().to_string();
        *self.exits.entry(reason).or_insert(0) += 1;
        match ann.outcome {
            Outcome::Win => {
                self.wins += 1;
                self.r_sum += ann.r_multiple.unwrap_or(0.0);
            }
            Outcome::Loss => {
                self.losses += 1;
                self.r_sum += ann.r_multiple.unwrap_or(0.0);
            }
            Outcome::NoTrigger => self.no_trigger += 1,
            Outcome::Open => self.open += 1,
            Outcome::InsufficientData => self.insufficient += 1,
            Outcome::Rollover => self.rollover += 1,
        }
    }

    fn record_invalid(&mut self) {
        self.total += 1;
        self.invalid += 1;
    }

    fn settled(&self) -> usize {
        self.wins + self.losses
    }

    fn win_rate(&self) -> f64 {
        let settled = self.settled();
        if settled == 0 {
            0.0
        } else {
            self.wins as f64 / settled as f64 * 100.0
        }
    }

    fn avg_r(&self) -> f64 {
        let settled = self.settled();
        if settled == 0 {
            0.0
        } else {
            self.r_sum / settled as f64
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

fn mark_rollover(bars: &mut [Bar], rollovers: &[rollovers::Model], timeframe: &str) {
    if bars.is_empty() || rollovers.is_empty() {
        return;
    }
    let is_5m = timeframe == "5m";
    let mut ri = 0usize;
    for bar in bars.iter_mut() {
        while ri < rollovers.len() {
            if !rollovers[ri].confirmed {
                ri += 1;
                continue;
            }
            let ts = &rollovers[ri].ts;
            let bar_start = format!(
                "{:04}-{:02}-{:02} {:02}:{:02}:00",
                bar.dt.year, bar.dt.month, bar.dt.day, bar.dt.hour, bar.dt.minute
            );
            let hit = if is_5m {
                bar_start == *ts
            } else {
                bar_start >= *ts
            };
            if hit {
                bar.rollover = true;
                ri += 1;
            } else if bar_start < *ts {
                break;
            } else {
                ri += 1;
            }
        }
    }
}

async fn bars_for(db: &DatabaseConnection, symbol: &str, tf: &str, mark: bool) -> Result<Vec<Bar>> {
    let rows = klines::Entity::find()
        .filter(klines::Column::Symbol.eq(symbol))
        .filter(klines::Column::Timeframe.eq(tf))
        .order_by_asc(klines::Column::Ts)
        .all(db)
        .await?;
    let mut bars: Vec<Bar> = rows
        .iter()
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
        .collect::<Result<_>>()?;
    if mark {
        let rollovers = repo::symbol_rollovers(db, symbol).await?;
        mark_rollover(&mut bars, &rollovers, tf);
    }
    Ok(bars)
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

fn print_stats(title: &str, s: &Stats) {
    println!(
        "{}: 触发 {} | 胜 {} 负 {} | 胜率 {:.1}% | 平均R {:.3}",
        title,
        s.total,
        s.wins,
        s.losses,
        s.win_rate(),
        s.avg_r()
    );
    println!(
        "  未决: open={} 数据不足={} 未触发={} 换月={} 异常={}",
        s.open, s.insufficient, s.no_trigger, s.rollover, s.invalid
    );
    let parts: Vec<String> = s.exits.iter().map(|(k, v)| format!("{k}={v}")).collect();
    println!("  出场: {}", parts.join(" "));
}

#[tokio::main]
async fn main() -> Result<()> {
    let version = env::var("REPLAY_VERSION").unwrap_or_else(|_| "2".to_string());
    let min_score = env::var("MIN_SCORE")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0.0);
    let db_url = "sqlite://C:/Users/Xbss/AppData/Roaming/com.ntrend.app/ntrend.db?mode=ro";
    let db = Database::connect(db_url).await?;
    let symbols = repo::list_symbols(&db, true).await?;
    let tick_by: HashMap<String, f64> = symbols
        .iter()
        .map(|s| (s.code.clone(), s.tick_size.max(0.0)))
        .collect();
    let days: Vec<String> = if env::var("FULL_HISTORY").as_deref() == Ok("1") {
        let all = klines::Entity::find()
            .filter(klines::Column::Timeframe.eq("15m"))
            .all(&db)
            .await?;
        let mut days: Vec<String> = all
            .iter()
            .map(|r| r.ts.get(..10).unwrap_or("").to_string())
            .collect();
        days.sort();
        days.dedup();
        days
    } else {
        [
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
        ]
        .iter()
        .map(|s| s.to_string())
        .collect()
    };

    let mut all = Stats::default();
    let mut by_day: BTreeMap<String, Stats> =
        days.iter().map(|d| (d.clone(), Stats::default())).collect();
    let mut by_level: BTreeMap<String, Stats> = BTreeMap::new();
    let mut by_dir: BTreeMap<String, Stats> = BTreeMap::new();
    let mut by_score: BTreeMap<String, Stats> = BTreeMap::new();
    let mut by_score_dir: BTreeMap<String, Stats> = BTreeMap::new();
    let mut latest: HashMap<String, (String, PatternDto)> = HashMap::new();
    let mut open_rows: Vec<String> = Vec::new();

    for day in &days {
        for sym in &symbols {
            let bars15 = bars_for(&db, &sym.code, "15m", false).await?;
            let bars60 = bars_for(&db, &sym.code, "60m", false).await?;
            let tick = tick_by.get(&sym.code).copied().unwrap_or(1.0);
            for i in 0..bars15.len() {
                if i + 1 < ATR_PERIOD + 2 {
                    continue;
                }
                let Some((y, m, d)) = bar_date(&bars15[i].dt.to_string()) else {
                    continue;
                };
                let ds = format!("{y:04}-{m:02}-{d:02}");
                if ds != *day {
                    continue;
                }
                let end = bar_time(&bars15[i]);
                let idx60 = bars60.partition_point(|b| bar_time(b) <= end);
                if idx60 < ATR_PERIOD + 2 {
                    continue;
                }
                let outcome = analyze_bars_for_version(
                    &sym.code,
                    &bars15[..=i],
                    &bars60[..idx60],
                    tick,
                    &version,
                )?;
                for s in outcome.detail.signals.iter().filter(|s| s.active) {
                    let Some(t) = s.trigger_ts.as_deref() else {
                        continue;
                    };
                    if !t.starts_with(day.as_str()) {
                        continue;
                    }
                    let key = signal_key(&sym.code, s);
                    latest.insert(key, (sym.code.clone(), s.clone()));
                }
            }
        }
    }

    let mut by_symbol_latest: HashMap<String, Vec<&PatternDto>> = HashMap::new();
    for (_, (symbol, s)) in &latest {
        by_symbol_latest.entry(symbol.clone()).or_default().push(s);
    }
    for (symbol, list) in &by_symbol_latest {
        let bars15_sim = bars_for(&db, symbol, "15m", true).await?;
        let bars60_sim = bars_for(&db, symbol, "60m", true).await?;
        for s in list {
            if s.score < min_score {
                continue;
            }
            let Some(t) = s.trigger_ts.as_deref() else {
                continue;
            };
            let day = t.split(' ').next().unwrap_or("").to_string();
            let level_key = if s.level == "box" {
                "box".to_string()
            } else {
                "n".to_string()
            };
            let created_at = s.warning_ts.clone().unwrap_or_else(|| t.to_string());
            let input = SignalInput {
                symbol: symbol.clone(),
                direction: s.direction.clone(),
                level: s.level.clone(),
                entry: s.entry,
                stop: s.stop,
                target: s.target,
                risk: s.risk,
                created_at,
                warning_ts: s.warning_ts.clone(),
                trigger_ts: s.trigger_ts.clone(),
                s0_ts: Some(s.s0.ts.clone()),
                s1_ts: Some(s.s1.ts.clone()),
                s2_ts: Some(s.s2.ts.clone()),
                a_move: Some(s.a_move),
            };
            if let Some(ann) = outcome::annotate(&input, &bars15_sim, &bars60_sim) {
                by_day.get_mut(&day).unwrap().record(&ann);
                by_level.entry(level_key.clone()).or_default().record(&ann);
                by_dir.entry(s.direction.clone()).or_default().record(&ann);
                let bucket = if s.score < 2.5 {
                    "<2.5".to_string()
                } else if s.score < 3.5 {
                    "2.5-3.5".to_string()
                } else {
                    ">=3.5".to_string()
                };
                by_score.entry(bucket.clone()).or_default().record(&ann);
                let score_dir = format!("{}|{}", bucket, s.direction);
                by_score_dir.entry(score_dir).or_default().record(&ann);
                all.record(&ann);
                if matches!(
                    ann.outcome,
                    Outcome::Open
                        | Outcome::InsufficientData
                        | Outcome::NoTrigger
                        | Outcome::Rollover
                ) {
                    open_rows.push(format!(
                        "{} {} {} warn={} trig={} -> {}",
                        symbol,
                        s.direction,
                        level_key,
                        s.warning_ts.as_deref().unwrap_or("-"),
                        s.trigger_ts.as_deref().unwrap_or("-"),
                        ann.outcome.as_str()
                    ));
                }
            } else {
                by_day.get_mut(&day).unwrap().record_invalid();
                by_level
                    .entry(level_key.clone())
                    .or_default()
                    .record_invalid();
                by_dir
                    .entry(s.direction.clone())
                    .or_default()
                    .record_invalid();
                all.record_invalid();
                open_rows.push(format!(
                    "{} {} {} trig={} -> annotate_none",
                    symbol,
                    s.direction,
                    level_key,
                    s.trigger_ts.as_deref().unwrap_or("-")
                ));
            }
        }
    }

    println!("=== version={version} min_score={min_score} replay ===");
    for day in &days {
        let s = by_day.get(day).unwrap();
        println!(
            "{}  触发 {} | 胜 {} 负 {} | 胜率 {:.1}% | 平均R {:.3}",
            day,
            s.total,
            s.wins,
            s.losses,
            s.win_rate(),
            s.avg_r()
        );
    }
    println!();
    print_stats("n", by_level.get("n").unwrap_or(&Stats::default()));
    print_stats("box", by_level.get("box").unwrap_or(&Stats::default()));
    print_stats("up", by_dir.get("up").unwrap_or(&Stats::default()));
    print_stats("down", by_dir.get("down").unwrap_or(&Stats::default()));
    for bucket in ["<2.5", "2.5-3.5", ">=3.5"] {
        print_stats(bucket, by_score.get(bucket).unwrap_or(&Stats::default()));
    }
    for key in [
        "<2.5|up",
        "<2.5|down",
        "2.5-3.5|up",
        "2.5-3.5|down",
        ">=3.5|up",
        ">=3.5|down",
    ] {
        print_stats(key, by_score_dir.get(key).unwrap_or(&Stats::default()));
    }
    print_stats("total", &all);
    println!();
    println!("未决/换月明细（{}条）:", open_rows.len());
    for r in open_rows {
        println!("  {r}");
    }
    Ok(())
}
