//! Sina real-time futures quotes via `hq.sinajs.cn/list=`.
//!
//! 商品期货与股指期货的返回字段排布不同，按代码前缀区分：
//! - 商品（RB0/AU0/…）：名称,时间,开,高,低,昨收,买价,卖价,最新价,结算价,昨结算,…
//! - 股指（IF0/IH0/IC0/IM0）：开,高,低,最新价,成交量,成交额,持仓量,…,昨收,昨结算,买一价,…,日期,时间,…,名称
//! 字段位置已用新浪板块行情 JSON（Market_Center.getHQFuturesData）交叉验证。

use std::collections::HashMap;

use anyhow::Result;
use serde::Serialize;

use crate::fetch::{RequestPriority, SinaClient};

const HQ_QUOTE_URL: &str = "https://hq.sinajs.cn/list=";
const HQ_REFERER: &str = "https://finance.sina.com.cn";
/// 每批最多携带的品种数（新浪行情接口的保守批量上限）。
const BATCH_SIZE: usize = 50;

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Quote {
    pub code: String,
    pub name: String,
    pub latest: f64,
    pub prev_settle: f64,
    /// 相对昨结算的涨跌幅（百分比，如 1.28 表示 +1.28%）。
    pub change_pct: Option<f64>,
}

/// 批量拉取实时行情：每个品种对应 `nf_{code}`，每批最多 50 个代码（默认按 P1 实时行情优先级调度）。
/// 单条解析失败只跳过该品种，不影响整批结果。
pub async fn fetch_quotes(client: &SinaClient, codes: &[String]) -> Result<HashMap<String, Quote>> {
    fetch_quotes_with_priority(client, codes, RequestPriority::P1).await
}

/// 支持指定优先级的批量实时行情拉取。
pub async fn fetch_quotes_with_priority(
    client: &SinaClient,
    codes: &[String],
    priority: RequestPriority,
) -> Result<HashMap<String, Quote>> {
    let mut out = HashMap::new();
    for chunk in codes.chunks(BATCH_SIZE) {
        let joined = chunk
            .iter()
            .map(|c| format!("nf_{c}"))
            .collect::<Vec<_>>()
            .join(",");
        let url = format!("{HQ_QUOTE_URL}{joined}");
        let text = client
            .get_text_with_referer_and_priority(&url, HQ_REFERER, priority)
            .await?;
        for line in text.lines() {
            if let Some((code, quote)) = parse_quote_line(line) {
                out.insert(code, quote);
            }
        }
    }
    Ok(out)
}

fn parse_quote_line(line: &str) -> Option<(String, Quote)> {
    let eq = line.find('=')?;
    let prefix = line[..eq].trim();
    let code = prefix.rsplit('_').next()?;
    if code.is_empty() || code == "hq_str" {
        return None;
    }
    let body = line[eq + 1..]
        .trim()
        .trim_matches(|ch: char| ch == '"' || ch == ';' || ch.is_whitespace());
    let quote = parse_quote_body(code, body)?;
    Some((code.to_string(), quote))
}

/// 解析单条行情 body；空串（无该品种/停牌）返回 None。
fn parse_quote_body(code: &str, body: &str) -> Option<Quote> {
    if body.is_empty() {
        return None;
    }
    let fields: Vec<&str> = body.split(',').collect();
    let is_index = matches!(&code[..code.len().min(2)], "IF" | "IH" | "IC" | "IM");
    let (latest, prev_settle, name) = if is_index {
        // 开,高,低,最新,量,额,持仓,…,昨收,昨结算,…,买一,…,日期,时间,…,名称
        if fields.len() < 15 {
            return None;
        }
        let latest = fields[3].parse().ok()?;
        let prev_settle = fields[14].parse().ok()?;
        let name = fields.last().copied().unwrap_or("").to_string();
        (latest, prev_settle, name)
    } else {
        // 名称,时间,开,高,低,昨收,买价,卖价,最新价,结算价,昨结算,…
        if fields.len() < 11 {
            return None;
        }
        let latest = fields[8].parse().ok()?;
        let prev_settle = fields[10].parse().ok()?;
        let name = fields[0].to_string();
        (latest, prev_settle, name)
    };
    let change_pct = if prev_settle > 0.0 {
        Some((latest - prev_settle) / prev_settle * 100.0)
    } else {
        None
    };
    Some(Quote {
        code: code.to_string(),
        name,
        latest,
        prev_settle,
        change_pct,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn approx(a: f64, b: f64) -> bool {
        (a - b).abs() < 1e-6
    }

    #[test]
    fn parses_commodity_quote() {
        // 2026-08-05 实测返回
        let line = r#"var hq_str_nf_RB0="螺纹钢连续,150000,2990.000,2999.000,2968.000,2990.000,2989.000,2990.000,2990.000,2985.000,2983.000,115,90,2375618.000,797857,沪,螺纹钢,2026-08-05,1,,,,,,,,,2985.221,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0";"#;
        let (code, q) = parse_quote_line(line).unwrap();
        assert_eq!(code, "RB0");
        assert_eq!(q.name, "螺纹钢连续");
        assert_eq!(q.latest, 2990.0);
        assert_eq!(q.prev_settle, 2983.0);
        assert!(approx(
            q.change_pct.unwrap(),
            (2990.0 - 2983.0) / 2983.0 * 100.0
        ));
    }

    #[test]
    fn parses_index_quote() {
        // 2026-08-05 实测返回（与板块 JSON 的 trade/presettlement 一致）
        let line = r#"var hq_str_nf_IF0="4520.600,4641.200,4520.000,4621.000,76442,351262341.400,156390.000,4621.000,0.000,5018.800,4106.400,0.000,0.000,4552.800,4562.600,151539.000,4619.400,4,0.000,0,0.000,0,0.000,0,0.000,0,4621.000,2,0.000,0,0.000,0,0.000,0,0.000,0,2026-08-05,15:00:00,100,1,,,,,,,,,4595.148,沪深300指数期货连续";"#;
        let (code, q) = parse_quote_line(line).unwrap();
        assert_eq!(code, "IF0");
        assert_eq!(q.name, "沪深300指数期货连续");
        assert_eq!(q.latest, 4621.0);
        assert_eq!(q.prev_settle, 4562.6);
        assert!(approx(
            q.change_pct.unwrap(),
            (4621.0 - 4562.6) / 4562.6 * 100.0
        ));
    }

    #[test]
    fn empty_body_is_skipped() {
        let line = r#"var hq_str_nf_RB0="";"#;
        assert!(parse_quote_line(line).is_none());
    }

    #[test]
    fn change_pct_is_none_when_no_prev_settle() {
        let q = parse_quote_body("RB0", "螺纹钢连续,150000,1,1,1,1,1,1,1,1,0").unwrap();
        assert_eq!(q.latest, 1.0);
        assert_eq!(q.prev_settle, 0.0);
        assert!(q.change_pct.is_none());
    }
}
