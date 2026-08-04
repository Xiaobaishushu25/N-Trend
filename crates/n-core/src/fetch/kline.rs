//! K-line parsing and fetching from the Sina futures minute API.

use anyhow::{anyhow, bail, Context, Result};
use serde::Serialize;
use serde_json::Value;

use crate::fetch::SinaClient;

/// 支持的分钟级别（5m 为持久化基级，15m/60m 供策略校验与手工抓取）。
pub const PERIODS: [(&str, &str); 3] = [("5m", "5"), ("15m", "15"), ("60m", "60")];
pub const DEFAULT_COUNT: usize = 300;
const API_URL: &str = "http://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_{symbol}_{period}_{ts}_=/InnerFuturesNewService.getFewMinLine?symbol={symbol}&type={period}";

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Kline {
    pub datetime: String,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
    pub hold: f64,
}

/// 抓取指定品种、指定分钟级别的最近 count 根K线（已按时间升序去重）。
pub async fn fetch_minute(
    client: &SinaClient,
    symbol: &str,
    period: &str,
    count: usize,
) -> Result<Vec<Kline>> {
    let url = API_URL
        .replace("{symbol}", symbol)
        .replace("{period}", period)
        .replace("{ts}", "0");
    let text = client.get_text(&url).await?;
    let rows = parse_jsonp(&text)?;
    if rows.is_empty() {
        bail!("接口没有返回K线数据");
    }
    Ok(latest_rows(rows, count))
}

pub fn read_symbols(path: &std::path::Path) -> Result<Vec<String>> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("无法读取 {}", path.display()))?;
    let symbols = parse_symbol_list(&text);
    if symbols.is_empty() {
        bail!("{} 中没有找到品种代码", path.display());
    }
    Ok(symbols)
}

pub fn parse_symbol_list(text: &str) -> Vec<String> {
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let mut symbols = Vec::new();

    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with("//") {
            continue;
        }
        let token = line.split_whitespace().next().unwrap_or("");
        let symbol = token
            .trim_matches(|c| c == ',' || c == ';')
            .to_uppercase();
        if symbol.is_empty() || symbols.contains(&symbol) {
            continue;
        }
        symbols.push(symbol);
    }

    symbols
}

fn parse_jsonp(text: &str) -> Result<Vec<Kline>> {
    let eq = text
        .find("=(")
        .or_else(|| text.find('='))
        .ok_or_else(|| anyhow!("意外的 JSONP 响应"))?;
    let mut json_text = text[eq + 1..].trim();
    if let Some(rest) = json_text.strip_prefix('(') {
        json_text = rest.trim();
    }
    json_text = json_text
        .trim_end_matches(|c: char| c == ')' || c == ';' || c.is_whitespace())
        .trim_end();
    let value: Value = serde_json::from_str(json_text).context("解析 JSONP 数据失败")?;
    let items = value
        .as_array()
        .ok_or_else(|| anyhow!("响应不是数组"))?;

    let mut rows = Vec::with_capacity(items.len());
    for item in items {
        rows.push(kline_from_value(item)?);
    }
    Ok(rows)
}

fn kline_from_value(item: &Value) -> Result<Kline> {
    let (dt, open, high, low, close, volume, hold) = if let Some(obj) = item.as_object() {
        let get = |key: &str| -> Result<&str> {
            obj.get(key)
                .and_then(|value| value.as_str())
                .ok_or_else(|| anyhow!("缺少字段 '{}'", key))
        };
        (
            get("d")?,
            get("o")?,
            get("h")?,
            get("l")?,
            get("c")?,
            get("v")?,
            get("p")?,
        )
    } else if let Some(arr) = item.as_array() {
        if arr.len() < 7 {
            bail!("行只有 {} 个字段，需要 7 个", arr.len());
        }
        let get = |index: usize| -> Result<&str> {
            arr[index]
                .as_str()
                .ok_or_else(|| anyhow!("字段 {} 不是文本", index))
        };
        (
            get(0)?,
            get(1)?,
            get(2)?,
            get(3)?,
            get(4)?,
            get(5)?,
            get(6)?,
        )
    } else {
        bail!("意外的K线行");
    };

    Ok(Kline {
        datetime: normalize_datetime(dt)?,
        open: parse_num(open)?,
        high: parse_num(high)?,
        low: parse_num(low)?,
        close: parse_num(close)?,
        volume: parse_num(volume)?,
        hold: parse_num(hold)?,
    })
}

fn normalize_datetime(raw: &str) -> Result<String> {
    let mut parts = raw.split_whitespace();
    let date = parts.next().ok_or_else(|| anyhow!("缺少日期"))?;
    let time = parts.next().unwrap_or("00:00:00");

    let mut date_parts = date.split(|c: char| c == '-' || c == '/');
    let year: i32 = date_parts
        .next()
        .ok_or_else(|| anyhow!("无效日期 '{}'", date))?
        .parse()
        .with_context(|| format!("无效年份 '{}'", date))?;
    let month: i32 = date_parts
        .next()
        .ok_or_else(|| anyhow!("无效日期 '{}'", date))?
        .parse()
        .with_context(|| format!("无效月份 '{}'", date))?;
    let day: i32 = date_parts
        .next()
        .ok_or_else(|| anyhow!("无效日期 '{}'", date))?
        .parse()
        .with_context(|| format!("无效日期 '{}'", date))?;

    let mut time_parts = time.split(':');
    let hour: i32 = time_parts
        .next()
        .ok_or_else(|| anyhow!("无效时间 '{}'", time))?
        .parse()
        .with_context(|| format!("无效小时 '{}'", time))?;
    let minute: i32 = time_parts
        .next()
        .ok_or_else(|| anyhow!("无效时间 '{}'", time))?
        .parse()
        .with_context(|| format!("无效分钟 '{}'", time))?;
    let second: i32 = time_parts
        .next()
        .unwrap_or("0")
        .parse()
        .with_context(|| format!("无效秒 '{}'", time))?;

    Ok(format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
        year, month, day, hour, minute, second
    ))
}

fn parse_num(raw: &str) -> Result<f64> {
    raw.trim()
        .parse::<f64>()
        .with_context(|| format!("无效数字 '{}'", raw))
}

fn latest_rows(mut rows: Vec<Kline>, count: usize) -> Vec<Kline> {
    rows.sort_by(|a, b| a.datetime.cmp(&b.datetime));

    let mut unique: Vec<Kline> = Vec::with_capacity(rows.len());
    for row in rows {
        if unique
            .last()
            .map(|last| last.datetime != row.datetime)
            .unwrap_or(true)
        {
            unique.push(row);
        }
    }

    let start = unique.len().saturating_sub(count);
    unique.split_off(start)
}

/// 把K线写成 CSV（仅用于开发期与旧数据交叉校验）。
pub fn write_csv(path: &std::path::Path, rows: &[Kline]) -> Result<()> {
    let mut writer = csv::Writer::from_path(path)
        .with_context(|| format!("创建 {}", path.display()))?;
    writer.write_record(["datetime", "open", "high", "low", "close", "volume", "hold"])?;

    for row in rows {
        let open = num_text(row.open);
        let high = num_text(row.high);
        let low = num_text(row.low);
        let close = num_text(row.close);
        let volume = num_text(row.volume);
        let hold = num_text(row.hold);
        writer.write_record([
            row.datetime.as_str(),
            open.as_str(),
            high.as_str(),
            low.as_str(),
            close.as_str(),
            volume.as_str(),
            hold.as_str(),
        ])?;
    }

    writer.flush()?;
    Ok(())
}

fn num_text(value: f64) -> String {
    if value.fract() == 0.0 && value.abs() < 1e15 {
        format!("{:.0}", value)
    } else {
        value.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_jsonp_array_rows() {
        let text = r#"var _RB0_15_0_=([["2026-08-02 09:00:00","1","2","0.5","1.5","100","1000"],["2026-08-02 09:05:00","1.5","2.5","1","2","200","1000"]]);"#;
        let rows = parse_jsonp(text).unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].datetime, "2026-08-02 09:00:00");
        assert_eq!(rows[1].close, 2.0);
        assert_eq!(rows[1].volume, 200.0);
    }

    #[test]
    fn latest_rows_dedups_and_keeps_count() {
        let mut rows = vec![
            Kline {
                datetime: "2026-08-02 09:00:00".to_string(),
                open: 1.0,
                high: 2.0,
                low: 0.5,
                close: 1.5,
                volume: 100.0,
                hold: 1000.0,
            },
            Kline {
                datetime: "2026-08-02 09:00:00".to_string(),
                open: 1.1,
                high: 2.1,
                low: 0.6,
                close: 1.6,
                volume: 110.0,
                hold: 1000.0,
            },
            Kline {
                datetime: "2026-08-02 09:05:00".to_string(),
                open: 1.5,
                high: 2.5,
                low: 1.0,
                close: 2.0,
                volume: 200.0,
                hold: 1000.0,
            },
        ];
        let kept = latest_rows(std::mem::take(&mut rows), 1);
        assert_eq!(kept.len(), 1);
        assert_eq!(kept[0].datetime, "2026-08-02 09:05:00");
    }

    #[test]
    fn symbol_list_skips_comments_and_duplicates() {
        let symbols = parse_symbol_list(
            "# 注释\nRB0\nAU0\nRB0\n\nIF0 // 行尾注释\n,AU0,\n",
        );
        assert_eq!(symbols, vec!["RB0", "AU0", "IF0"]);
    }
}
