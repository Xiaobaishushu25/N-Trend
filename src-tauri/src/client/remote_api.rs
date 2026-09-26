use std::sync::Arc;
use std::time::Duration;
use bytes::Bytes;
use n_protocol::auth::*;
use n_protocol::config::*;
use n_protocol::dto::*;
use n_protocol::error::*;
use reqwest::header::AUTHORIZATION;
use reqwest::{Client, Method, RequestBuilder, StatusCode};
use serde::de::DeserializeOwned;
use serde::Serialize;
use serde_json::Value;

use super::credential_store::CredentialStore;

pub struct RemoteApiClient {
    client: Client,
    credentials: Arc<CredentialStore>,
}

impl RemoteApiClient {
    pub fn new(credentials: Arc<CredentialStore>) -> Self {
        let client = Client::builder()
            .timeout(Duration::from_secs(45))
            .build()
            .unwrap_or_default();
        Self { client, credentials }
    }

    pub fn server_url(&self) -> String {
        self.credentials.get_server_url()
    }

    fn url(&self, path: &str) -> String {
        let base = self.server_url();
        let base = base.trim_end_matches('/');
        let path = path.trim_start_matches('/');
        format!("{base}/api/v1/{path}")
    }

    fn request(&self, method: Method, path: &str) -> RequestBuilder {
        let full_url = self.url(path);
        let mut builder = self.client.request(method, &full_url);

        let token = self.credentials.get_token();
        if !token.is_empty() {
            builder = builder.header(AUTHORIZATION, format!("Bearer {token}"));
        }

        let device_id = self.credentials.get_device_id();
        if !device_id.is_empty() {
            builder = builder.header("X-Device-Id", device_id);
        }

        builder
    }

    async fn execute<T: DeserializeOwned>(&self, rb: RequestBuilder) -> Result<T, ApiErrorResponse> {
        let resp = match rb.send().await {
            Ok(r) => r,
            Err(e) => {
                tracing::error!("❌ [RemoteAPI] 网络请求失败: {e}");
                return Err(ApiErrorResponse::new("NETWORK_ERROR", format!("网络请求失败: {e}"), ""));
            }
        };

        let status = resp.status();
        let url = resp.url().clone();
        if status.is_success() {
            let data = resp.json::<T>().await.map_err(|e| {
                tracing::error!("❌ [RemoteAPI] 解析响应 JSON 失败 ({}): {e}", url);
                ApiErrorResponse::new("PARSE_ERROR", format!("解析服务端响应失败: {e}"), "")
            })?;
            Ok(data)
        } else {
            let text = resp.text().await.unwrap_or_default();
            tracing::error!("❌ [RemoteAPI] 请求失败 [{status}] {url} -> {text}");
            if status == StatusCode::UNAUTHORIZED {
                Err(ApiErrorResponse::new("UNAUTHORIZED", "认证失败或Token已过期", ""))
            } else if status == StatusCode::FORBIDDEN {
                Err(ApiErrorResponse::new("FORBIDDEN", "无权访问此资源", ""))
            } else if status == StatusCode::UPGRADE_REQUIRED {
                Err(ApiErrorResponse::new("UPGRADE_REQUIRED", "客户端版本过低，请升级后继续使用", ""))
            } else {
                match serde_json::from_str::<ApiErrorResponse>(&text) {
                    Ok(err) => Err(err),
                    Err(_) => Err(ApiErrorResponse::new(
                        "SERVER_ERROR",
                        format!("服务端返回错误状态码: {status}"),
                        "",
                    )),
                }
            }
        }
    }

    async fn execute_empty(&self, rb: RequestBuilder) -> Result<(), ApiErrorResponse> {
        let resp = match rb.send().await {
            Ok(r) => r,
            Err(e) => {
                tracing::error!("❌ [RemoteAPI] 网络请求失败: {e}");
                return Err(ApiErrorResponse::new("NETWORK_ERROR", format!("网络请求失败: {e}"), ""));
            }
        };

        let status = resp.status();
        let url = resp.url().clone();
        if status.is_success() {
            Ok(())
        } else {
            let text = resp.text().await.unwrap_or_default();
            tracing::error!("❌ [RemoteAPI] 请求失败 [{status}] {url} -> {text}");
            if status == StatusCode::UNAUTHORIZED {
                Err(ApiErrorResponse::new("UNAUTHORIZED", "认证失败或Token已过期", ""))
            } else if status == StatusCode::FORBIDDEN {
                Err(ApiErrorResponse::new("FORBIDDEN", "无权访问此资源", ""))
            } else {
                match serde_json::from_str::<ApiErrorResponse>(&text) {
                    Ok(err) => Err(err),
                    Err(_) => Err(ApiErrorResponse::new(
                        "SERVER_ERROR",
                        format!("服务端返回错误状态码: {status}"),
                        "",
                    )),
                }
            }
        }
    }

    // ── Meta & Status ──

    pub async fn get_meta(&self) -> Result<MetaDto, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "meta")).await
    }

    pub async fn get_server_status(&self) -> Result<ServerStatusDto, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "status")).await
    }

    // ── Devices ──

    pub async fn list_devices(&self) -> Result<Vec<DeviceItemDto>, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "devices")).await
    }

    pub async fn revoke_device(&self, device_id: &str) -> Result<(), ApiErrorResponse> {
        self.execute_empty(self.request(Method::DELETE, &format!("devices/{device_id}"))).await
    }

    pub async fn update_device_role(&self, device_id: &str, role: &str) -> Result<(), ApiErrorResponse> {
        #[derive(Serialize)]
        struct RoleReq<'a> {
            role: &'a str,
        }
        self.execute_empty(
            self.request(Method::PUT, &format!("devices/{device_id}/role"))
                .json(&RoleReq { role }),
        )
        .await
    }

    pub async fn pair_device(&self, req: &DeviceRegisterRequest) -> Result<DeviceRegisterResponse, ApiErrorResponse> {
        self.execute(self.request(Method::POST, "devices/register").json(req)).await
    }

    // ── Symbols & Groups ──

    pub async fn get_symbols(&self) -> Result<Vec<SymbolDto>, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "symbols")).await
    }

    pub async fn list_groups(&self) -> Result<Vec<GroupDto>, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "groups")).await
    }

    pub async fn create_group(&self, name: &str) -> Result<GroupDto, ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            name: &'a str,
        }
        self.execute(self.request(Method::POST, "groups").json(&Body { name })).await
    }

    pub async fn rename_group(&self, id: i64, name: &str) -> Result<(), ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            name: &'a str,
        }
        self.execute_empty(self.request(Method::PUT, &format!("groups/{id}")).json(&Body { name })).await
    }

    pub async fn delete_group(&self, id: i64) -> Result<(), ApiErrorResponse> {
        self.execute_empty(self.request(Method::DELETE, &format!("groups/{id}"))).await
    }

    pub async fn get_group_symbols(&self, group_id: i64) -> Result<Vec<SymbolDto>, ApiErrorResponse> {
        self.execute(self.request(Method::GET, &format!("groups/{group_id}/symbols"))).await
    }

    pub async fn list_symbol_groups(&self, symbol: &str) -> Result<Vec<GroupDto>, ApiErrorResponse> {
        self.execute(self.request(Method::GET, &format!("symbols/{symbol}/groups"))).await
    }

    pub async fn add_symbol_to_group(&self, group_id: i64, symbol: &str) -> Result<(), ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            code: &'a str,
        }
        self.execute_empty(self.request(Method::POST, &format!("groups/{group_id}/symbols")).json(&Body { code: symbol })).await
    }

    pub async fn remove_symbol_from_group(&self, group_id: i64, symbol: &str) -> Result<(), ApiErrorResponse> {
        self.execute_empty(self.request(Method::DELETE, &format!("groups/{group_id}/symbols/{symbol}"))).await
    }

    pub async fn reorder_groups(&self, ids: &[i64], all_position: i64) -> Result<(), ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            ids: &'a [i64],
            all_position: i64,
        }
        self.execute_empty(self.request(Method::POST, "groups/reorder").json(&Body { ids, all_position })).await
    }

    pub async fn get_group_all_position(&self) -> Result<i64, ApiErrorResponse> {
        let r: i64 = self.execute(self.request(Method::GET, "groups/all-position")).await?;
        Ok(r)
    }

    pub async fn reorder_group_symbols(&self, group_id: i64, codes: &[String]) -> Result<(), ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            codes: &'a [String],
        }
        self.execute_empty(self.request(Method::POST, &format!("groups/{group_id}/reorder-symbols")).json(&Body { codes })).await
    }

    pub async fn reorder_symbols(&self, codes: &[String]) -> Result<(), ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            codes: &'a [String],
        }
        self.execute_empty(self.request(Method::POST, "symbols/reorder").json(&Body { codes })).await
    }

    pub async fn add_symbol(&self, code: &str) -> Result<usize, ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            code: &'a str,
        }
        let r: usize = self.execute(self.request(Method::POST, "symbols").json(&Body { code })).await?;
        Ok(r)
    }

    pub async fn search_contracts(&self, keyword: &str) -> Result<Vec<ContractSuggestionDto>, ApiErrorResponse> {
        self.execute(self.request(Method::GET, &format!("contracts/search?keyword={keyword}"))).await
    }

    pub async fn remove_symbol(&self, code: &str) -> Result<(), ApiErrorResponse> {
        self.execute_empty(self.request(Method::DELETE, &format!("symbols/{code}"))).await
    }

    pub async fn set_symbol_flags(&self, code: &str, watchlist: bool, enabled: bool) -> Result<(), ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            code: &'a str,
            watchlist: bool,
            enabled: bool,
        }
        self.execute_empty(self.request(Method::POST, "symbols/flags").json(&Body { code, watchlist, enabled })).await
    }

    pub async fn set_symbol_tick(&self, code: &str, tick: f64) -> Result<(), ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            code: &'a str,
            tick: f64,
        }
        self.execute_empty(self.request(Method::POST, "symbols/tick").json(&Body { code, tick })).await
    }

    pub async fn set_symbol_followed(&self, code: &str, followed: bool) -> Result<(), ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            code: &'a str,
            followed: bool,
        }
        self.execute_empty(self.request(Method::POST, "symbols/followed").json(&Body { code, followed })).await
    }

    pub async fn enrich_symbol_names(&self) -> Result<usize, ApiErrorResponse> {
        let r: usize = self.execute(self.request(Method::POST, "symbols/enrich")).await?;
        Ok(r)
    }

    pub async fn refresh_symbol_list(&self) -> Result<usize, ApiErrorResponse> {
        let r: usize = self.execute(self.request(Method::POST, "symbols/refresh")).await?;
        Ok(r)
    }

    // ── Klines & Quotes ──

    pub async fn get_klines(&self, symbol: &str, timeframe: &str, limit: Option<usize>, before: Option<i64>) -> Result<Vec<KlineDto>, ApiErrorResponse> {
        let mut path = format!("klines/{symbol}/{timeframe}?");
        if let Some(l) = limit {
            path.push_str(&format!("limit={l}&"));
        }
        if let Some(b) = before {
            path.push_str(&format!("before={b}&"));
        }
        self.execute(self.request(Method::GET, &path)).await
    }

    pub async fn get_chart_klines(&self, symbol: &str, timeframe: &str, limit: Option<usize>, before: Option<i64>) -> Result<ChartKlineResponse, ApiErrorResponse> {
        let mut path = format!("chart-klines/{symbol}/{timeframe}?");
        if let Some(l) = limit {
            path.push_str(&format!("limit={l}&"));
        }
        if let Some(b) = before {
            path.push_str(&format!("before={b}&"));
        }
        self.execute(self.request(Method::GET, &path)).await
    }

    pub async fn get_trend_series(&self, symbol: &str, timeframe: &str, limit: Option<usize>) -> Result<Vec<TrendPointDto>, ApiErrorResponse> {
        let mut path = format!("trend-series/{symbol}/{timeframe}?");
        if let Some(l) = limit {
            path.push_str(&format!("limit={l}"));
        }
        self.execute(self.request(Method::GET, &path)).await
    }

    pub async fn get_market_snapshot(&self) -> Result<Vec<MarketSnapshot>, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "quotes")).await
    }

    // ── Manual Levels ──

    pub async fn list_manual_levels(&self, symbol: Option<&str>, timeframe: Option<&str>, active_only: bool) -> Result<Vec<ManualLevelDto>, ApiErrorResponse> {
        let mut path = format!("manual-levels?active_only={active_only}&");
        if let Some(s) = symbol {
            path.push_str(&format!("symbol={s}&"));
        }
        if let Some(tf) = timeframe {
            path.push_str(&format!("timeframe={tf}&"));
        }
        self.execute(self.request(Method::GET, &path)).await
    }

    pub async fn create_manual_level(&self, input: &ManualLevelInput) -> Result<ManualLevelDto, ApiErrorResponse> {
        self.execute(self.request(Method::POST, "manual-levels").json(input)).await
    }

    pub async fn update_manual_level(&self, id: i64, input: &ManualLevelInput) -> Result<ManualLevelDto, ApiErrorResponse> {
        self.execute(self.request(Method::PUT, &format!("manual-levels/{id}")).json(input)).await
    }

    pub async fn set_manual_level_monitoring(&self, id: i64, enabled: bool) -> Result<ManualLevelDto, ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body {
            enabled: bool,
        }
        self.execute(self.request(Method::POST, &format!("manual-levels/{id}/monitoring")).json(&Body { enabled })).await
    }

    pub async fn archive_manual_level(&self, id: i64) -> Result<(), ApiErrorResponse> {
        self.execute_empty(self.request(Method::POST, &format!("manual-levels/{id}/archive"))).await
    }

    pub async fn delete_manual_level(&self, id: i64) -> Result<(), ApiErrorResponse> {
        self.execute_empty(self.request(Method::DELETE, &format!("manual-levels/{id}"))).await
    }

    pub async fn get_manual_level_events(&self, id: i64) -> Result<Vec<ManualLevelEventDto>, ApiErrorResponse> {
        self.execute(self.request(Method::GET, &format!("manual-levels/{id}/events"))).await
    }

    pub async fn get_active_events(&self) -> Result<Value, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "signals/active")).await
    }

    // ── Actions ──

    pub async fn refresh_data_now(&self) -> Result<RefreshStats, ApiErrorResponse> {
        self.execute(self.request(Method::POST, "actions/refresh")).await
    }

    pub async fn run_scan_now(&self) -> Result<Value, ApiErrorResponse> {
        self.execute(self.request(Method::POST, "actions/scan")).await
    }

    pub async fn run_scan_fast_now(&self) -> Result<Value, ApiErrorResponse> {
        self.execute(self.request(Method::POST, "actions/scan-fast")).await
    }

    pub async fn rebuild_events_now(&self) -> Result<Value, ApiErrorResponse> {
        self.execute(self.request(Method::POST, "actions/rebuild-events")).await
    }

    pub async fn refresh_outcomes_now(&self) -> Result<OutcomeRefresh, ApiErrorResponse> {
        self.execute(self.request(Method::POST, "review/outcomes/refresh")).await
    }

    // ── Signals & Outcomes ──

    pub async fn get_review_stats(&self, dimension: &str, scope: &str, version: Option<&str>, score_min: Option<f64>, score_max: Option<f64>) -> Result<Value, ApiErrorResponse> {
        let mut path = format!("review/stats?dimension={dimension}&scope={scope}&");
        if let Some(v) = version {
            path.push_str(&format!("version={v}&"));
        }
        if let Some(s) = score_min {
            path.push_str(&format!("score_min={s}&"));
        }
        if let Some(s) = score_max {
            path.push_str(&format!("score_max={s}&"));
        }
        self.execute(self.request(Method::GET, &path)).await
    }

    pub async fn get_recent_outcomes(&self, limit: Option<usize>, filters: Option<&RecentOutcomeFilters>) -> Result<Value, ApiErrorResponse> {
        let mut path = "review/outcomes?".to_string();
        if let Some(l) = limit {
            path.push_str(&format!("limit={l}&"));
        }
        if let Some(f) = filters {
            if let Some(s) = &f.symbol { path.push_str(&format!("symbol={s}&")); }
            if let Some(v) = &f.version { path.push_str(&format!("version={v}&")); }
            if let Some(d) = &f.direction { path.push_str(&format!("direction={d}&")); }
            if let Some(lv) = &f.level { path.push_str(&format!("level={lv}&")); }
            if let Some(g) = &f.grade { path.push_str(&format!("grade={g}&")); }
            if let Some(s) = f.score_min { path.push_str(&format!("score_min={s}&")); }
            if let Some(s) = f.score_max { path.push_str(&format!("score_max={s}&")); }
            if let Some(o) = &f.outcome { path.push_str(&format!("outcome={o}&")); }
        }
        self.execute(self.request(Method::GET, &path)).await
    }

    pub async fn get_review_signal(&self, event_id: i64) -> Result<Value, ApiErrorResponse> {
        self.execute(self.request(Method::GET, &format!("review/signal/{event_id}"))).await
    }

    pub async fn get_signal_user_data(&self, event_id: i64) -> Result<SignalUserData, ApiErrorResponse> {
        self.execute(self.request(Method::GET, &format!("annotations/{event_id}"))).await
    }

    pub async fn add_signal_annotation(&self, event_id: i64, content: &str) -> Result<SignalAnnotationDto, ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            event_id: i64,
            content: &'a str,
        }
        self.execute(self.request(Method::POST, "annotations").json(&Body { event_id, content })).await
    }

    pub async fn delete_signal_annotation(&self, id: i64) -> Result<(), ApiErrorResponse> {
        self.execute_empty(self.request(Method::DELETE, &format!("annotations/{id}"))).await
    }

    pub async fn set_signal_decision(&self, event_id: i64, opened: bool) -> Result<SignalDecisionDto, ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body {
            event_id: i64,
            opened: bool,
        }
        self.execute(self.request(Method::POST, "decisions").json(&Body { event_id, opened })).await
    }

    pub async fn get_v2_models(&self) -> Result<Vec<V2ModelRow>, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "v2/models")).await
    }

    pub async fn get_v2_dataset_report(&self) -> Result<V2ReportBundle, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "v2/report")).await
    }

    pub async fn get_v2_predictions(&self, model_id: Option<&str>) -> Result<Vec<V2PredictionRow>, ApiErrorResponse> {
        let mut path = "v2/predictions?".to_string();
        if let Some(m) = model_id {
            path.push_str(&format!("model_id={m}"));
        }
        self.execute(self.request(Method::GET, &path)).await
    }

    pub async fn set_v2_model_status(&self, model_id: &str, status: &str) -> Result<(), ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            model_id: &'a str,
            status: &'a str,
        }
        self.execute_empty(self.request(Method::POST, "v2/models/status").json(&Body { model_id, status })).await
    }

    pub async fn backfill_v2_predictions(&self) -> Result<serde_json::Value, ApiErrorResponse> {
        self.execute(self.request(Method::POST, "v2/predictions/backfill")).await
    }

    pub async fn get_active_preclose_signals(&self) -> Result<Value, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "signals/preclose")).await
    }

    pub async fn get_active_preclose_candidates(&self) -> Result<Value, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "signals/preclose-candidates")).await
    }

    pub async fn get_preclose_candidates(&self, _limit: Option<usize>) -> Result<Value, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "signals/preclose-candidates")).await
    }

    pub async fn get_preclose_signals(&self, _limit: Option<usize>) -> Result<Value, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "signals/preclose")).await
    }

    // ── Notifications ──

    pub async fn get_notifications(&self, limit: Option<usize>, _unread_only: bool) -> Result<Vec<NotificationHistoryItem>, ApiErrorResponse> {
        let mut path = "notifications?".to_string();
        if let Some(l) = limit {
            path.push_str(&format!("limit={l}"));
        }
        self.execute(self.request(Method::GET, &path)).await
    }

    pub async fn record_notification(&self, item: &NewNotificationHistoryItem) -> Result<NotificationHistoryItem, ApiErrorResponse> {
        self.execute(self.request(Method::POST, "notifications").json(item)).await
    }

    pub async fn mark_notifications_read(&self, up_to_id: Option<u64>) -> Result<(), ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body {
            up_to_id: Option<u64>,
        }
        self.execute_empty(self.request(Method::POST, "notifications/read").json(&Body { up_to_id })).await
    }

    pub async fn clear_notifications(&self) -> Result<(), ApiErrorResponse> {
        self.execute_empty(self.request(Method::DELETE, "notifications")).await
    }

    // ── Settings ──

    pub async fn get_server_settings(&self) -> Result<ServerSettingsDto, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "settings/server")).await
    }

    pub async fn update_server_settings(&self, req: &ServerSettingsUpdate) -> Result<ConfigApplyResult, ApiErrorResponse> {
        self.execute(self.request(Method::PUT, "settings/server").json(req)).await
    }

    pub async fn set_last_group(&self, group_id: Option<i64>) -> Result<(), ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body {
            group_id: Option<i64>,
        }
        self.execute_empty(self.request(Method::POST, "settings/last-group").json(&Body { group_id })).await
    }

    pub async fn set_timeframes(&self, timeframes: &[String]) -> Result<(), ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body<'a> {
            timeframes: &'a [String],
        }
        self.execute_empty(self.request(Method::POST, "settings/timeframes").json(&Body { timeframes })).await
    }

    pub async fn reset_config(&self) -> Result<ServerSettingsDto, ApiErrorResponse> {
        self.execute(self.request(Method::POST, "settings/reset")).await
    }

    // ── Admin ──

    pub async fn get_scheduler_status(&self) -> Result<SchedulerStatus, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "admin/runtime/status")).await
    }

    pub async fn set_scheduler_running(&self, running: bool) -> Result<SchedulerStatus, ApiErrorResponse> {
        #[derive(Serialize)]
        struct Body {
            running: bool,
        }
        self.execute(self.request(Method::POST, "admin/runtime/scheduler").json(&Body { running })).await
    }

    pub async fn restart_server(&self) -> Result<(), ApiErrorResponse> {
        self.execute_empty(self.request(Method::POST, "admin/runtime/restart")).await
    }

    pub async fn restart_bridge(&self) -> Result<(), ApiErrorResponse> {
        self.execute_empty(self.request(Method::POST, "admin/bridge/restart")).await
    }

    pub async fn download_database_backup(&self) -> Result<(Bytes, String), ApiErrorResponse> {
        let resp = self.request(Method::GET, "admin/database-backup")
            .send()
            .await
            .map_err(|e| ApiErrorResponse::new("BACKUP_DOWNLOAD_FAILED", format!("下载备份失败: {e}"), ""))?;

        if !resp.status().is_success() {
            return Err(ApiErrorResponse::new("BACKUP_ERROR", format!("备份接口返回错误: {}", resp.status()), ""));
        }

        let sha256 = resp.headers()
            .get("X-Backup-Sha256")
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default()
            .to_string();

        let bytes = resp.bytes().await.map_err(|e| {
            ApiErrorResponse::new("BACKUP_READ_FAILED", format!("读取备份流失败: {e}"), "")
        })?;

        Ok((bytes, sha256))
    }

    // ── Integrity ──

    pub async fn check_symbol_integrity(&self, symbol: &str) -> Result<serde_json::Value, ApiErrorResponse> {
        self.execute(self.request(Method::GET, &format!("integrity?symbol={symbol}"))).await
    }

    pub async fn check_all_symbols_integrity(&self) -> Result<serde_json::Value, ApiErrorResponse> {
        self.execute(self.request(Method::GET, "integrity")).await
    }

    pub async fn repair_symbol_integrity(&self, symbol: &str) -> Result<serde_json::Value, ApiErrorResponse> {
        self.execute(self.request(Method::POST, &format!("integrity/repair?symbol={symbol}"))).await
    }
}
