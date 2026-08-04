use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{anyhow, Context, Result};
use serde::Deserialize;

use crate::analyze::model::{ATR_PERIOD, Bar, DT};

#[derive(Debug, Deserialize)]
struct CsvRow {
    datetime: String,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    #[serde(default)]
    volume: f64,
    #[serde(default)]
    hold: f64,
}

fn parse_dt(s: &str) -> Option<DT> {
    let parts: Vec<&str> = s.split_whitespace().collect();
    if parts.is_empty() {
        return None;
    }

    let mut ymd = [0i32; 3];
    let mut idx = 0;
    for token in parts[0].split(|c: char| c == '-' || c == '/') {
        if idx >= 3 {
            return None;
        }
        ymd[idx] = token.parse().ok()?;
        idx += 1;
    }
    if idx != 3 {
        return None;
    }

    let mut hms = [0i32; 3];
    if parts.len() > 1 {
        let mut i = 0;
        for token in parts[1].split(':') {
            if i >= 3 {
                break;
            }
            hms[i] = token.parse().ok()?;
            i += 1;
        }
    }

    Some(DT {
        year: ymd[0],
        month: ymd[1],
        day: ymd[2],
        hour: hms[0],
        minute: hms[1],
    })
}

pub fn load_csv(path: &str) -> Result<Vec<Bar>> {
    let mut text = fs::read_to_string(path).with_context(|| format!("无法读取 {}", path))?;
    if text.starts_with('\u{feff}') {
        text.remove(0);
    }

    let mut rdr = csv::Reader::from_reader(text.as_bytes());
    let mut bars = Vec::new();

    for (idx, record) in rdr.deserialize::<CsvRow>().enumerate() {
        let row = record.map_err(|e| anyhow!("第{}行: {}", idx + 2, e))?;
        let dt = parse_dt(&row.datetime)
            .ok_or_else(|| anyhow!("第{}行: 时间格式错误 '{}'", idx + 2, row.datetime))?;

        bars.push(Bar {
            dt,
            open: row.open,
            high: row.high,
            low: row.low,
            close: row.close,
            volume: row.volume,
            hold: row.hold,
        });
    }

    if bars.len() < ATR_PERIOD + 2 {
        return Err(anyhow!(
            "至少需要 {} 根K线，实际只有 {} 根",
            ATR_PERIOD + 2,
            bars.len()
        ));
    }

    Ok(bars)
}

pub struct CsvPair {
    pub symbol: String,
    pub path15: PathBuf,
    pub path60: PathBuf,
}

pub fn discover_pairs(dir: &Path) -> Result<Vec<CsvPair>> {
    let mut found: HashMap<String, Vec<(String, Option<PathBuf>, Option<PathBuf>)>> =
        HashMap::new();
    for entry in fs::read_dir(dir).with_context(|| format!("无法读取目录 {}", dir.display()))? {
        let path = entry?.path();
        let Some(name) = path.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        let Some((symbol, suffix, is15)) = split_kline_name(name) else {
            continue;
        };
        let candidates = found.entry(symbol).or_default();
        if candidates.iter().any(|(s, _, _)| *s == suffix) {
            let item = candidates
                .iter_mut()
                .find(|(s, _, _)| *s == suffix)
                .unwrap();
            if is15 {
                if item.1.as_ref().map_or(true, |old| file_is_newer(&path, old)) {
                    item.1 = Some(path);
                }
            } else {
                if item.2.as_ref().map_or(true, |old| file_is_newer(&path, old)) {
                    item.2 = Some(path);
                }
            }
        } else {
            candidates.push((suffix, None, None));
            let item = candidates.last_mut().unwrap();
            if is15 {
                item.1 = Some(path);
            } else {
                item.2 = Some(path);
            }
        }
    }

    let mut pairs = Vec::new();
    for (symbol, candidates) in found {
        let mut best: Option<(String, PathBuf, PathBuf)> = None;
        for (suffix, path15, path60) in candidates {
            if let (Some(path15), Some(path60)) = (path15, path60) {
                let better = best.as_ref().map_or(true, |(_, b15, b60)| {
                    pair_is_better(&path15, &path60, b15, b60)
                });
                if better {
                    best = Some((suffix, path15, path60));
                }
            }
        }
        if let Some((_, path15, path60)) = best {
            pairs.push(CsvPair {
                symbol,
                path15,
                path60,
            });
        }
    }
    pairs.sort_by(|a, b| a.symbol.cmp(&b.symbol));
    Ok(pairs)
}

pub fn infer_symbol(path: &Path) -> Option<String> {
    let name = path.file_name()?.to_str()?;
    split_kline_name(name).map(|(symbol, _, _)| symbol)
}

pub fn file_last_dt(path: &Path) -> Option<DT> {
    let text = fs::read_to_string(path).ok()?;
    let line = text.lines().filter(|l| !l.trim().is_empty()).last()?;
    parse_dt(line.split(',').next()?)
}

fn split_kline_name(name: &str) -> Option<(String, String, bool)> {
    let stem = name.strip_suffix(".csv")?;
    for (marker, is15) in [("_15m", true), ("_60m", false)] {
        if let Some(pos) = stem.find(marker) {
            let symbol = &stem[..pos];
            if !symbol.is_empty() {
                return Some((
                    symbol.to_string(),
                    stem[pos + marker.len()..].to_string(),
                    is15,
                ));
            }
        }
    }
    None
}

fn pair_is_better(a15: &Path, a60: &Path, b15: &Path, b60: &Path) -> bool {
    match (
        file_last_dt(a15),
        file_last_dt(a60),
        file_last_dt(b15),
        file_last_dt(b60),
    ) {
        (Some(x15), Some(x60), Some(y15), Some(y60)) => {
            x15.min(x60) > y15.min(y60)
        }
        _ => {
            let ma = mtime_secs(a15) + mtime_secs(a60);
            let mb = mtime_secs(b15) + mtime_secs(b60);
            ma > mb
        }
    }
}

fn file_is_newer(a: &Path, b: &Path) -> bool {
    match (file_last_dt(a), file_last_dt(b)) {
        (Some(x), Some(y)) => x > y,
        _ => mtime_secs(a) > mtime_secs(b),
    }
}

fn mtime_secs(path: &Path) -> u128 {
    fs::metadata(path)
        .and_then(|m| m.modified())
        .map(|t| t.duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0))
        .unwrap_or(0)
}
