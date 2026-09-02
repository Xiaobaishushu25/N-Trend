//! End-to-end integration test for HybridDataSource (TianQin primary + Sina fallback)

use std::sync::Arc;
use tokio::sync::RwLock;

use n_core::config::Config;
use n_core::fetch::{HybridDataSource, MarketDataSource, SinaClient, TqBridgeClient};
use n_core::process::SidecarManager;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    println!(">>> 1. 启动并连接天勤 Python 桥接服务...");
    let config = Config::default();
    SidecarManager::start(&config.data_source).await?;

    let config_arc = Arc::new(RwLock::new(config.clone()));
    let tq_client = TqBridgeClient::with_port(config.data_source.bridge_port);
    let sina_client = SinaClient::new();
    let data_source = HybridDataSource::new(tq_client, sina_client, config_arc);

    println!(">>> 2. 检查数据源健康状态: {}", data_source.is_healthy().await);
    println!(">>> 当前激活数据源名称: {}", data_source.name());

    println!("\n>>> 3. 测试批量实时行情请求 (RB0, IF0, AU0, MA0)...");
    let codes = vec![
        "RB0".to_string(),
        "IF0".to_string(),
        "AU0".to_string(),
        "MA0".to_string(),
    ];
    let quotes = data_source.fetch_quotes(&codes).await?;
    for (_code, q) in &quotes {
        println!(
            "   [行情] {} ({}) | 最新: {:.2} | 昨结: {:.2} | 涨跌: {:+.2}%",
            q.code,
            q.name,
            q.latest,
            q.prev_settle,
            q.change_pct.unwrap_or(0.0)
        );
    }
    assert_eq!(quotes.len(), 4, "应成功获取全部 4 个品种行情");

    println!("\n>>> 4. 测试 5 分钟 K 线拉取 (RB0, 10 根)...");
    let klines = data_source.fetch_minute("RB0", "5", 10).await?;
    println!("   成功获取 {} 根 K 线", klines.len());
    for k in klines.iter().rev().take(3) {
        println!(
            "   [K线] {} | O:{:.1} H:{:.1} L:{:.1} C:{:.1} V:{:.0} 持仓:{:.0}",
            k.datetime, k.open, k.high, k.low, k.close, k.volume, k.hold
        );
    }
    assert!(!klines.is_empty(), "K线列表不应为空");

    println!("\n>>> 5. 测试合约搜索 (关键字: RB)...");
    let results = data_source.search_contracts("RB").await?;
    println!("   搜索结果数量: {}", results.len());
    for r in &results {
        println!("   [合约] {} | {} | 交易所: {}", r.code, r.name, r.exchange);
    }

    println!("\n>>> 6. 关闭 Python 桥接服务...");
    SidecarManager::stop();
    println!(">>> ✅ 所有端到端联调测试全部顺利通过！");

    Ok(())
}
