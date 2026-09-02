//! Market data source trait abstraction.

use std::collections::HashMap;
use anyhow::Result;

use crate::fetch::kline::{Kline, RawKlineResponse};
use crate::fetch::quotes::Quote;
use crate::fetch::symbols::FuturesSymbol;

pub trait MarketDataSource: Send + Sync {
    /// 数据源名称（如 "tqsdk" 或 "sina"）
    fn name(&self) -> &'static str;

    /// 批量拉取实时行情
    fn fetch_quotes(
        &self,
        codes: &[String],
    ) -> impl std::future::Future<Output = Result<HashMap<String, Quote>>> + Send;

    /// 抓取指定品种、指定周期的已定版分钟 K 线（按时间升序）
    fn fetch_minute(
        &self,
        symbol: &str,
        period: &str,
        count: usize,
    ) -> impl std::future::Future<Output = Result<Vec<Kline>>> + Send;

    /// 抓取指定品种、指定周期的原始分钟 K 线（不经过定版延迟过滤，用于探针/影子判定）
    fn fetch_minute_raw(
        &self,
        symbol: &str,
        period: &str,
        count: usize,
    ) -> impl std::future::Future<Output = Result<RawKlineResponse>> + Send;

    /// 搜索合约
    fn search_contracts(
        &self,
        keyword: &str,
    ) -> impl std::future::Future<Output = Result<Vec<FuturesSymbol>>> + Send;

    /// 检查数据源健康状态
    fn is_healthy(&self) -> impl std::future::Future<Output = bool> + Send;
}
