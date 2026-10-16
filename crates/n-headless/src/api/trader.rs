use std::sync::Arc;
use axum::extract::{Json, State};
use axum::response::IntoResponse;
use serde::{Deserialize, Serialize};

use n_core::trader::{CtpTraderClient, ExecutionConfig};
use crate::state::ServerContext;

#[derive(Debug, Serialize)]
pub struct ApiResponse<T> {
    pub success: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<T>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl<T> ApiResponse<T> {
    pub fn ok(data: T) -> Self {
        Self {
            success: true,
            data: Some(data),
            error: None,
        }
    }

    pub fn err(msg: impl Into<String>) -> Self {
        Self {
            success: false,
            data: None,
            error: Some(msg.into()),
        }
    }
}

pub async fn get_trader_status(
    State(ctx): State<Arc<ServerContext>>,
) -> impl IntoResponse {
    let client = CtpTraderClient::for_port(8766);
    let health = match client.health().await {
        Ok(h) => h,
        Err(e) => {
            return Json(ApiResponse::<serde_json::Value>::err(format!(
                "CTP 桥接服务未响应或未启动: {}",
                e
            )));
        }
    };
    let config = ctx.execution_engine.get_config().await;
    let res = serde_json::json!({
        "health": health,
        "config": config,
    });
    Json(ApiResponse::ok(res))
}

pub async fn get_trader_account(
    State(_ctx): State<Arc<ServerContext>>,
) -> impl IntoResponse {
    let client = CtpTraderClient::for_port(8766);
    match client.account().await {
        Ok(acc) => Json(ApiResponse::ok(acc)),
        Err(e) => Json(ApiResponse::err(format!("获取账户资金失败: {}", e))),
    }
}

pub async fn get_trader_positions(
    State(ctx): State<Arc<ServerContext>>,
) -> impl IntoResponse {
    let client = CtpTraderClient::for_port(8766);
    let broker_positions = client.positions().await.unwrap_or_default();
    let managed_positions = ctx.execution_engine.list_positions().await;
    let res = serde_json::json!({
        "broker_positions": broker_positions,
        "managed_positions": managed_positions,
    });
    Json(ApiResponse::ok(res))
}

pub async fn get_condition_orders(
    State(ctx): State<Arc<ServerContext>>,
) -> impl IntoResponse {
    let conditions = ctx.execution_engine.list_conditions().await;
    Json(ApiResponse::ok(conditions))
}

#[derive(Debug, Deserialize)]
pub struct CancelConditionReq {
    pub id: String,
}

pub async fn cancel_condition_order(
    State(ctx): State<Arc<ServerContext>>,
    Json(payload): Json<CancelConditionReq>,
) -> impl IntoResponse {
    match ctx.execution_engine.cancel_condition(&payload.id).await {
        Ok(true) => Json(ApiResponse::ok("条件单已成功撤销")),
        Ok(false) => Json(ApiResponse::err("条件单未找到或已无法撤销")),
        Err(e) => Json(ApiResponse::err(format!("撤单失败: {}", e))),
    }
}

#[derive(Debug, Deserialize)]
pub struct ConfirmConditionReq {
    pub id: String,
}

pub async fn confirm_condition_order(
    State(ctx): State<Arc<ServerContext>>,
    Json(payload): Json<ConfirmConditionReq>,
) -> impl IntoResponse {
    match ctx.execution_engine.confirm_open(&payload.id).await {
        Ok(true) => Json(ApiResponse::ok("开仓确认成功，已向柜台发送委托")),
        Ok(false) => Json(ApiResponse::err("未找到该待确认条件单或状态已过期")),
        Err(e) => Json(ApiResponse::err(format!("确认开仓异常: {}", e))),
    }
}

pub async fn update_trader_config(
    State(ctx): State<Arc<ServerContext>>,
    Json(cfg): Json<ExecutionConfig>,
) -> impl IntoResponse {
    ctx.execution_engine.update_config(cfg.clone()).await;
    Json(ApiResponse::ok(cfg))
}

pub async fn emergency_cancel_all(
    State(_ctx): State<Arc<ServerContext>>,
) -> impl IntoResponse {
    let client = CtpTraderClient::for_port(8766);
    match client.emergency_cancel_all().await {
        Ok(res) => Json(ApiResponse::ok(res)),
        Err(e) => Json(ApiResponse::err(format!("一键全撤失败: {}", e))),
    }
}

pub async fn emergency_close_all(
    State(_ctx): State<Arc<ServerContext>>,
) -> impl IntoResponse {
    let client = CtpTraderClient::for_port(8766);
    match client.emergency_close_all().await {
        Ok(res) => Json(ApiResponse::ok(res)),
        Err(e) => Json(ApiResponse::err(format!("一键全平失败: {}", e))),
    }
}

pub async fn get_trade_history(
    State(ctx): State<Arc<ServerContext>>,
) -> impl IntoResponse {
    let history = ctx.execution_engine.get_trade_history().await;
    Json(ApiResponse::ok(history))
}

pub async fn get_trade_statistics(
    State(ctx): State<Arc<ServerContext>>,
) -> impl IntoResponse {
    let stats = ctx.execution_engine.get_trade_statistics().await;
    Json(ApiResponse::ok(stats))
}
