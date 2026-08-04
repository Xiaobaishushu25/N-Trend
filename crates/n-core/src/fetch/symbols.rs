//! Futures symbol discovery from the Sina node table.

use std::collections::HashMap;
use anyhow::{anyhow, bail, Result};
use regex::Regex;
use serde::Serialize;
use serde_json::Value;

use crate::fetch::SinaClient;

const NODES_JS_URL: &str =
    "http://vip.stock.finance.sina.com.cn/quotes_service/view/js/qihuohangqing.js";
const HQ_DATA_URL: &str = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQFuturesData?page=1&num=1&sort=position&asc=0&node={node}&base={base}";

#[derive(Debug, Clone, Serialize)]
pub struct FuturesSymbol {
    pub code: String,
    pub name: String,
    pub variety: String,
    pub exchange: String,
    pub node: String,
}

#[derive(Debug)]
struct FuturesNode {
    variety: String,
    node: String,
    base: Option<String>,
}

/// 从新浪节点表刷新全部期货品种。
pub async fn refresh(client: &SinaClient) -> Result<Vec<FuturesSymbol>> {
    let text = client.get_text(NODES_JS_URL).await?;
    let nodes = parse_futures_nodes(&text)?;
    if nodes.is_empty() {
        bail!("没有解析到期货节点");
    }

    let mut rows = Vec::new();
    for node in &nodes {
        let mut found = false;
        for base in node_bases(&node.base) {
            for url in hq_data_urls(&node.node, &base) {
                match client.get_text(&url).await {
                    Ok(body) => {
                        if let Some(row) = first_symbol_from_json(&body, node) {
                            rows.push(row);
                            found = true;
                            break;
                        }
                    }
                    Err(_) => continue,
                }
            }
            if found {
                break;
            }
        }
        if !found {
            tracing::warn!("无数据: {} ({})", node.variety, node.node);
        }
    }

    if rows.is_empty() {
        bail!("没有收集到品种");
    }

    rows.sort_by(|a, b| {
        a.exchange
            .cmp(&b.exchange)
            .then(a.code.cmp(&b.code))
            .then(a.variety.cmp(&b.variety))
    });
    Ok(rows)
}

const HQ_NAME_URL: &str = "https://hq.sinajs.cn/list=";
const HQ_REFERER: &str = "https://finance.sina.com.cn";

/// 通过新浪批量行情接口一次获取多个品种的中文名称（每批最多 50 个代码）。
pub async fn fetch_quote_names(
    client: &SinaClient,
    codes: &[String],
) -> Result<HashMap<String, String>> {
    let mut out = HashMap::new();
    for chunk in codes.chunks(50) {
        let joined = chunk
            .iter()
            .map(|c| format!("nf_{c}"))
            .collect::<Vec<_>>()
            .join(",");
        let url = format!("{HQ_NAME_URL}{joined}");
        let text = client.get_text_with_referer(&url, HQ_REFERER).await?;
        for line in text.lines() {
            let Some(eq) = line.find('=') else { continue };
            let prefix = line[..eq].trim();
            let Some(code) = prefix.rsplit('_').next() else { continue };
            if code.is_empty() || code == "hq_str" {
                continue;
            }
            let body = line[eq + 1..]
                .trim()
                .trim_matches(|ch: char| ch == '"' || ch == ';' || ch.is_whitespace());
            if let Some(name) = extract_quote_name(body) {
                out.insert(code.to_string(), name);
            }
        }
    }
    Ok(out)
}

/// 从行情字段中提取中文名称：第一个非数字字段（IF0 等股指格式的名称在末尾）。
fn extract_quote_name(body: &str) -> Option<String> {
    for field in body.split(',') {
        let field = field.trim();
        if field.parse::<f64>().is_err() && field.chars().any(|ch| !ch.is_ascii()) {
            return Some(field.to_string());
        }
    }
    None
}
fn parse_futures_nodes(text: &str) -> Result<Vec<FuturesNode>> {
    let start = text
        .find("ARRFUTURESNODES")
        .ok_or_else(|| anyhow!("未找到 ARRFUTURESNODES"))?;
    let body = &text[start..];
    let end_rel = body
        .find("};")
        .ok_or_else(|| anyhow!("未找到 ARRFUTURESNODES 结尾"))?;
    let object_text = &body[..end_rel + 2];

    let re = Regex::new(
        r"\[\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'(\d+)'(?:\s*,\s*'([^']+)')?\s*\]",
    )?;
    let mut nodes = Vec::new();
    for cap in re.captures_iter(object_text) {
        nodes.push(FuturesNode {
            variety: cap[1].to_string(),
            node: cap[2].to_string(),
            base: cap.get(4).map(|m| m.as_str().to_string()),
        });
    }
    Ok(nodes)
}

fn node_bases(base: &Option<String>) -> Vec<String> {
    let mut bases = Vec::new();
    if let Some(value) = base {
        if !value.is_empty() {
            bases.push(value.clone());
        }
    }
    if !bases.iter().any(|b| b == "futures") {
        bases.push("futures".to_string());
    }
    bases
}

fn hq_data_urls(node: &str, base: &str) -> Vec<String> {
    let gbk = encode_gbk_query(node);
    let utf8 = percent_encoding::utf8_percent_encode(node, percent_encoding::NON_ALPHANUMERIC)
        .to_string();
    let base = encode_gbk_query(base);

    let gbk_url = HQ_DATA_URL.replace("{node}", &gbk).replace("{base}", &base);
    if gbk == utf8 {
        vec![gbk_url]
    } else {
        let utf8_url = HQ_DATA_URL.replace("{node}", &utf8).replace("{base}", &base);
        vec![gbk_url, utf8_url]
    }
}

fn encode_gbk_query(value: &str) -> String {
    let (bytes, _, _) = encoding_rs::GBK.encode(value);
    percent_encoding::percent_encode(&bytes, percent_encoding::NON_ALPHANUMERIC).to_string()
}

fn first_symbol_from_json(body: &str, node: &FuturesNode) -> Option<FuturesSymbol> {
    let value: Value = serde_json::from_str(body).ok()?;
    let first = if let Some(arr) = value.as_array() {
        arr.first()?
    } else {
        &value
    };
    let obj = first.as_object()?;
    let code = obj.get("symbol")?.as_str()?;
    let name = obj
        .get("name")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let exchange = obj
        .get("exchange")
        .and_then(|v| v.as_str())
        .unwrap_or("");

    Some(FuturesSymbol {
        code: code.to_string(),
        name: name.to_string(),
        variety: node.variety.clone(),
        exchange: exchange.to_string(),
        node: node.node.clone(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_futures_nodes_js() {
        let text = "var ARRFUTURESNODES={data:[['黄金','AU0','1000','futures'],['白银','AG0','1000']]};";
        let nodes = parse_futures_nodes(text).unwrap();
        assert_eq!(nodes.len(), 2);
        assert_eq!(nodes[0].node, "AU0");
        assert_eq!(nodes[0].base.as_deref(), Some("futures"));
        assert_eq!(nodes[1].node, "AG0");
        assert_eq!(nodes[1].base, None);
    }

    #[test]
    fn bases_follow_precedence_and_always_include_futures() {
        assert_eq!(node_bases(&None), vec!["futures"]);
        assert_eq!(node_bases(&Some("futures".to_string())), vec!["futures"]);
        assert_eq!(node_bases(&Some("qh".to_string())), vec!["qh", "futures"]);
    }

    #[test]
    fn extracts_names_from_quote_lines() {
        assert_eq!(
            extract_quote_name("硅铁连续,150000,5770.000,5766.000").as_deref(),
            Some("硅铁连续")
        );
        let if0_body = "4526.400,4538.000,4494.200,0.000,2026-08-03,15:00:00,300,1,4514.968,沪深300指数期货连续";
        assert_eq!(
            extract_quote_name(if0_body).as_deref(),
            Some("沪深300指数期货连续")
        );
    }

    #[test]
    fn parses_first_symbol_json() {
        let body = r#"[{"symbol":"AU0","name":"黄金","exchange":"SHFE"}]"#;
        let node = FuturesNode {
            variety: "黄金".to_string(),
            node: "AU0".to_string(),
            base: None,
        };
        let row = first_symbol_from_json(body, &node).unwrap();
        assert_eq!(row.code, "AU0");
        assert_eq!(row.name, "黄金");
        assert_eq!(row.exchange, "SHFE");
        assert_eq!(row.variety, "黄金");
    }
}



