//! Futures symbol discovery from the Sina node table.

use anyhow::{anyhow, bail, Result};
use regex::Regex;
use serde::Serialize;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use crate::fetch::SinaClient;

const NODES_JS_URL: &str =
    "http://vip.stock.finance.sina.com.cn/quotes_service/view/js/qihuohangqing.js";
const HQ_DATA_URL: &str = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQFuturesData?page=1&num=1&sort=position&asc=0&node={node}&base={base}";
const HQ_ALL_DATA_URL: &str = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQFuturesData?page=1&num=1000&sort=position&asc=0&node={node}&base={base}";

#[derive(Debug, Clone, Serialize)]
pub struct FuturesSymbol {
    pub code: String,
    pub name: String,
    pub variety: String,
    pub exchange: String,
    pub node: String,
}

#[derive(Debug, Clone)]
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
            let Some(code) = prefix.rsplit('_').next() else {
                continue;
            };
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

/// 从行情字段中提取名称：普通合约的名称在第一个字段（如「PVC连续」「PVC2609」）；
/// 股指类（IF0 等）第一个字段是行情数字，名称在末尾的中文字段。
fn extract_quote_name(body: &str) -> Option<String> {
    let fields: Vec<&str> = body
        .split(',')
        .map(|f| f.trim())
        .filter(|f| !f.is_empty())
        .collect();
    let first = *fields.first()?;
    if first.parse::<f64>().is_err() {
        return Some(first.to_string());
    }
    fields
        .into_iter()
        .find(|f| f.chars().any(|ch| !ch.is_ascii()))
        .map(|f| f.to_string())
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

    let re = Regex::new(r"\[\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'(\d+)'(?:\s*,\s*'([^']+)')?\s*\]")?;
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
    let utf8 =
        percent_encoding::utf8_percent_encode(node, percent_encoding::NON_ALPHANUMERIC).to_string();
    let base = encode_gbk_query(base);

    let gbk_url = HQ_DATA_URL.replace("{node}", &gbk).replace("{base}", &base);
    if gbk == utf8 {
        vec![gbk_url]
    } else {
        let utf8_url = HQ_DATA_URL
            .replace("{node}", &utf8)
            .replace("{base}", &base);
        vec![gbk_url, utf8_url]
    }
}

fn hq_all_data_urls(node: &str, base: &str) -> Vec<String> {
    let gbk = encode_gbk_query(node);
    let utf8 =
        percent_encoding::utf8_percent_encode(node, percent_encoding::NON_ALPHANUMERIC).to_string();
    let base = encode_gbk_query(base);

    let gbk_url = HQ_ALL_DATA_URL
        .replace("{node}", &gbk)
        .replace("{base}", &base);
    if gbk == utf8 {
        vec![gbk_url]
    } else {
        let utf8_url = HQ_ALL_DATA_URL
            .replace("{node}", &utf8)
            .replace("{base}", &base);
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
    let name = obj.get("name").and_then(|v| v.as_str()).unwrap_or("");
    let exchange = obj.get("exchange").and_then(|v| v.as_str()).unwrap_or("");

    Some(FuturesSymbol {
        code: code.to_string(),
        name: name.to_string(),
        variety: node.variety.clone(),
        exchange: exchange.to_string(),
        node: node.node.clone(),
    })
}

/// 解析行情列表 JSON（数组或单个对象），返回该节点下的全部合约。
fn symbols_from_json(body: &str, node: &FuturesNode) -> Option<Vec<FuturesSymbol>> {
    let value: Value = serde_json::from_str(body).ok()?;
    let items = match value {
        Value::Array(arr) => arr,
        other => vec![other],
    };
    let mut rows = Vec::with_capacity(items.len());
    for item in items {
        let obj = item.as_object()?;
        let code = obj.get("symbol")?.as_str()?;
        let name = obj.get("name").and_then(|v| v.as_str()).unwrap_or("");
        let exchange = obj.get("exchange").and_then(|v| v.as_str()).unwrap_or("");
        rows.push(FuturesSymbol {
            code: code.to_string(),
            name: name.to_string(),
            variety: node.variety.clone(),
            exchange: exchange.to_string(),
            node: node.node.clone(),
        });
    }
    Some(rows)
}

/// 节点表在进程内只解析一次（静态数据）。
static NODE_CACHE: OnceLock<Mutex<Option<Vec<FuturesNode>>>> = OnceLock::new();
/// 各节点的全部合约列表缓存，5 分钟过期（合约随交易日变化，不宜长期缓存）。
static CONTRACT_CACHE: OnceLock<Mutex<HashMap<String, (Instant, Vec<FuturesSymbol>)>>> =
    OnceLock::new();
const CONTRACT_CACHE_TTL: Duration = Duration::from_secs(300);

/// 按前缀搜索新浪期货合约（如 RB → RB0、RB2609、RB2608…），供标题栏搜索提示使用。
/// 先匹配节点表（代码前缀或中文名），再拉取该节点全部合约并按前缀过滤。
pub async fn search_contracts(client: &SinaClient, keyword: &str) -> Result<Vec<FuturesSymbol>> {
    let kw = keyword.trim().to_uppercase();
    if kw.is_empty() {
        return Ok(Vec::new());
    }
    // 节点表接口不可达或没有命中时，回退到行情接口批量探测常见月份合约
    if let Ok(rows) = search_contracts_via_nodes(client, &kw).await {
        if !rows.is_empty() {
            return Ok(rows);
        }
    }
    fallback_contract_search(client, &kw).await
}

async fn search_contracts_via_nodes(client: &SinaClient, kw: &str) -> Result<Vec<FuturesSymbol>> {
    // 先取缓存并立刻释放锁，避免 MutexGuard 跨 await
    let cached = {
        let guard = NODE_CACHE.get_or_init(|| Mutex::new(None)).lock().unwrap();
        match guard.as_ref() {
            Some(nodes) => Some(nodes.clone()),
            None => None,
        }
    };
    let nodes = match cached {
        Some(nodes) => nodes,
        None => {
            let text = client.get_text(NODES_JS_URL).await?;
            let nodes = parse_futures_nodes(&text)?;
            *NODE_CACHE.get_or_init(|| Mutex::new(None)).lock().unwrap() = Some(nodes.clone());
            nodes
        }
    };

    // 节点匹配：代码前缀（RB / RB2 都能命中 RB0 节点）或中文名包含
    let matched: Vec<&FuturesNode> = nodes
        .iter()
        .filter(|n| {
            let base = n.node.trim_end_matches(|c: char| c.is_ascii_digit());
            let code_hit = !base.is_empty() && (base.starts_with(&kw) || kw.starts_with(base));
            let name_hit = !kw.is_ascii() && n.variety.contains(&kw);
            code_hit || name_hit
        })
        .take(3)
        .collect();
    if matched.is_empty() {
        return Ok(Vec::new());
    }

    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for node in matched {
        let contracts = fetch_node_contracts(client, node).await?;
        for row in contracts {
            let code_hit = row.code.starts_with(&kw);
            let name_hit = !kw.is_ascii() && row.name.contains(&kw);
            if (code_hit || name_hit) && seen.insert(row.code.clone()) {
                out.push(row);
            }
        }
    }
    // 主力/连续合约（代码短）在前，其余按月排序；最多返回 15 条
    out.sort_by(|a, b| a.code.len().cmp(&b.code.len()).then(a.code.cmp(&b.code)));
    out.truncate(15);
    Ok(out)
}

/// 兜底方案：节点表/合约列表接口不可达时，用行情接口（hq.sinajs.cn，应用日常验证过的主机）
/// 批量探测「前缀 + 连续合约 + 当年/次年各月」合约，如 RB0、RB2501…RB2612。
async fn fallback_contract_search(client: &SinaClient, kw: &str) -> Result<Vec<FuturesSymbol>> {
    if kw.is_empty() || !kw.is_ascii() || !kw.chars().all(|c| c.is_ascii_alphanumeric()) {
        return Ok(Vec::new());
    }
    let year = crate::analyze::time::now_parts().0;
    let mut codes: Vec<String> = vec![format!("{kw}0")];
    for y in [year, year + 1] {
        for m in 1..=12u16 {
            codes.push(format!("{kw}{:02}{:02}", y % 100, m));
        }
    }
    let names = fetch_quote_names(client, &codes).await?;
    let mut out: Vec<FuturesSymbol> = names
        .into_iter()
        .filter(|(code, _)| code.starts_with(kw))
        .map(|(code, name)| FuturesSymbol {
            code,
            name,
            variety: String::new(),
            exchange: String::new(),
            node: String::new(),
        })
        .collect();
    out.sort_by(|a, b| a.code.len().cmp(&b.code.len()).then(a.code.cmp(&b.code)));
    out.truncate(15);
    Ok(out)
}

async fn fetch_node_contracts(
    client: &SinaClient,
    node: &FuturesNode,
) -> Result<Vec<FuturesSymbol>> {
    let cache = CONTRACT_CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    {
        let guard = cache.lock().unwrap();
        if let Some((at, rows)) = guard.get(&node.node) {
            if at.elapsed() < CONTRACT_CACHE_TTL {
                return Ok(rows.clone());
            }
        }
    }
    let mut rows = Vec::new();
    for base in node_bases(&node.base) {
        for url in hq_all_data_urls(&node.node, &base) {
            match client.get_text(&url).await {
                Ok(body) => {
                    if let Some(found) = symbols_from_json(&body, node) {
                        rows = found;
                        break;
                    }
                }
                Err(_) => continue,
            }
        }
        if !rows.is_empty() {
            break;
        }
    }
    if rows.is_empty() {
        return Ok(Vec::new());
    }
    cache
        .lock()
        .unwrap()
        .insert(node.node.clone(), (Instant::now(), rows.clone()));
    Ok(rows)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_futures_nodes_js() {
        let text =
            "var ARRFUTURESNODES={data:[['黄金','AU0','1000','futures'],['白银','AG0','1000']]};";
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
        // 纯字母数字的合约名（如 PVC2609）也必须能取到，而不是捡到末尾的“连”字
        assert_eq!(
            extract_quote_name("PVC2609,6500.000,6600.000,6400.000,0.000,2026-08-08,15:00:00")
                .as_deref(),
            Some("PVC2609")
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
