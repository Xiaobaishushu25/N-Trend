use crate::auth::AuthDevice;
use crate::state::ServerContext;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::Json;
use n_protocol::dto::{ContractSuggestionDto, GroupDto, SymbolDto};
use n_protocol::error::ApiErrorResponse;
use serde::Deserialize;
use std::sync::Arc;

#[derive(Deserialize)]
pub struct AddSymbolReq {
    pub code: String,
}

#[derive(Deserialize)]
pub struct ReorderSymbolsReq {
    pub codes: Vec<String>,
}

#[derive(Deserialize)]
pub struct SetFlagsReq {
    pub code: String,
    pub watchlist: bool,
    pub enabled: bool,
}

#[derive(Deserialize)]
pub struct SetTickReq {
    pub code: String,
    pub tick: f64,
}

#[derive(Deserialize)]
pub struct SetFollowedReq {
    pub code: String,
    pub followed: bool,
}

#[derive(Deserialize)]
pub struct ContractSearchQuery {
    pub keyword: String,
}

#[derive(Deserialize)]
pub struct CreateGroupReq {
    pub name: String,
}

#[derive(Deserialize)]
pub struct RenameGroupReq {
    pub name: String,
}

#[derive(Deserialize)]
pub struct ReorderGroupsReq {
    pub ids: Vec<i64>,
    pub all_position: i64,
}

#[derive(Deserialize)]
pub struct ReorderGroupSymbolsReq {
    pub codes: Vec<String>,
}

pub async fn get_symbols(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<Vec<SymbolDto>>, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::list_symbols(&ctx.db, false).await {
        Ok(rows) => {
            let dtos = rows
                .into_iter()
                .map(|s| SymbolDto {
                    code: s.code,
                    name: s.name,
                    variety: s.variety,
                    exchange: s.exchange,
                    node: s.node,
                    watchlist: s.watchlist,
                    enabled: s.enabled,
                    tick_size: s.tick_size,
                    is_followed: s.is_followed,
                    created_at: s.created_at,
                    updated_at: s.updated_at,
                })
                .collect();
            Ok(Json(dtos))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取品种列表失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn add_symbol(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Json(payload): Json<AddSymbolReq>,
) -> Result<Json<usize>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.add_symbol(&payload.code).await {
        Ok(n) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "symbols")
                .await
                .unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub
                .broadcast(
                    seq,
                    "state.changed",
                    serde_json::json!({ "scope": "symbols", "revision": rev }),
                )
                .await;
            Ok(Json(n))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("BAD_REQUEST", format!("添加品种失败: {e}"), "");
            Err((StatusCode::BAD_REQUEST, Json(err)))
        }
    }
}

pub async fn remove_symbol(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(code): Path<String>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.remove_symbol(&code).await {
        Ok(_) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "symbols")
                .await
                .unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub
                .broadcast(
                    seq,
                    "state.changed",
                    serde_json::json!({ "scope": "symbols", "revision": rev }),
                )
                .await;
            Ok(StatusCode::NO_CONTENT)
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("删除品种失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn reorder_symbols(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Json(payload): Json<ReorderSymbolsReq>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::reorder_symbols(&ctx.db, &payload.codes).await {
        Ok(_) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "symbols")
                .await
                .unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub
                .broadcast(
                    seq,
                    "state.changed",
                    serde_json::json!({ "scope": "symbols", "revision": rev }),
                )
                .await;
            Ok(StatusCode::OK)
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("排序品种失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn set_symbol_flags(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Json(payload): Json<SetFlagsReq>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::set_symbol_flags(&ctx.db, &payload.code, payload.watchlist, payload.enabled).await {
        Ok(_) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "symbols").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "symbols", "revision": rev })).await;
            Ok(StatusCode::OK)
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("更新标记失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn set_symbol_tick(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Json(payload): Json<SetTickReq>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::set_symbol_tick(&ctx.db, &payload.code, payload.tick).await {
        Ok(_) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "symbols").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "symbols", "revision": rev })).await;
            Ok(StatusCode::OK)
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("更新最小价位失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn set_symbol_followed(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Json(payload): Json<SetFollowedReq>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::set_symbol_followed(&ctx.db, &payload.code, payload.followed).await {
        Ok(_) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "symbols").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "symbols", "revision": rev })).await;
            Ok(StatusCode::OK)
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("更新关注状态失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn refresh_symbol_list(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<usize>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.refresh_symbol_list().await {
        Ok(n) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "symbols").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "symbols", "revision": rev })).await;
            Ok(Json(n))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("刷新品种列表失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn enrich_symbol_names(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<usize>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.enrich_existing_symbols().await {
        Ok(n) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "symbols").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "symbols", "revision": rev })).await;
            Ok(Json(n))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("补全品种信息失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn search_contracts(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Query(q): Query<ContractSearchQuery>,
) -> Result<Json<Vec<ContractSuggestionDto>>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.search_contracts(&q.keyword).await {
        Ok(list) => {
            let dtos = list
                .into_iter()
                .map(|s| ContractSuggestionDto {
                    code: s.code,
                    name: s.name,
                    variety: s.variety,
                    exchange: s.exchange,
                    node: s.node,
                })
                .collect();
            Ok(Json(dtos))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("搜索合约失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

// ---------------------------------------------------------------------------
// Groups
// ---------------------------------------------------------------------------

pub async fn list_groups(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<Vec<GroupDto>>, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::list_groups(&ctx.db).await {
        Ok(groups) => {
            let dtos = groups
                .into_iter()
                .map(|g| GroupDto {
                    id: g.id,
                    name: g.name,
                    sort_index: g.sort_index,
                })
                .collect();
            Ok(Json(dtos))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取分组列表失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn create_group(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Json(payload): Json<CreateGroupReq>,
) -> Result<Json<GroupDto>, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::create_group(&ctx.db, &payload.name).await {
        Ok(g) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "groups").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "groups", "revision": rev })).await;
            Ok(Json(GroupDto {
                id: g.id,
                name: g.name,
                sort_index: g.sort_index,
            }))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("创建分组失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn rename_group(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(id): Path<i64>,
    Json(payload): Json<RenameGroupReq>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::rename_group(&ctx.db, id, &payload.name).await {
        Ok(_) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "groups").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "groups", "revision": rev })).await;
            Ok(StatusCode::OK)
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("重命名分组失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn delete_group(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(id): Path<i64>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::delete_group(&ctx.db, id).await {
        Ok(_) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "groups").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "groups", "revision": rev })).await;
            Ok(StatusCode::NO_CONTENT)
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("删除分组失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn reorder_groups(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Json(payload): Json<ReorderGroupsReq>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    if let Err(e) = n_core::storage::repo::reorder_groups(&ctx.db, &payload.ids).await {
        let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("排序列分组失败: {e}"), "");
        return Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)));
    }
    let mut map = std::collections::HashMap::new();
    map.insert("group_all_position".to_string(), payload.all_position.to_string());
    let _ = n_core::storage::repo::set_settings(&ctx.db, &map).await;

    let rev = n_core::storage::repo::bump_revision(&ctx.db, "groups").await.unwrap_or(1);
    let seq = ctx.next_event_seq();
    ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "groups", "revision": rev })).await;
    Ok(StatusCode::OK)
}

pub async fn get_group_all_position(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<i64>, (StatusCode, Json<ApiErrorResponse>)> {
    let pos = n_core::storage::repo::get_setting(&ctx.db, "group_all_position")
        .await
        .unwrap_or_default()
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(0);
    Ok(Json(pos))
}

pub async fn get_group_symbols(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(id): Path<i64>,
) -> Result<Json<Vec<SymbolDto>>, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::group_symbols(&ctx.db, id).await {
        Ok(symbols) => {
            let dtos = symbols
                .into_iter()
                .map(|s| SymbolDto {
                    code: s.code,
                    name: s.name,
                    variety: s.variety,
                    exchange: s.exchange,
                    node: s.node,
                    watchlist: s.watchlist,
                    enabled: s.enabled,
                    tick_size: s.tick_size,
                    is_followed: s.is_followed,
                    created_at: s.created_at,
                    updated_at: s.updated_at,
                })
                .collect();
            Ok(Json(dtos))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取分组成员失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn add_symbol_to_group(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(id): Path<i64>,
    Json(payload): Json<AddSymbolReq>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::add_symbol_to_group(&ctx.db, &payload.code, id).await {
        Ok(_) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "groups").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "groups", "revision": rev })).await;
            Ok(StatusCode::OK)
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("添加品种到分组失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn remove_symbol_from_group(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path((id, code)): Path<(i64, String)>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::remove_symbol_from_group(&ctx.db, &code, id).await {
        Ok(_) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "groups").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "groups", "revision": rev })).await;
            Ok(StatusCode::NO_CONTENT)
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("移出品种失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn reorder_group_symbols(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(id): Path<i64>,
    Json(payload): Json<ReorderGroupSymbolsReq>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::reorder_group_symbols(&ctx.db, id, &payload.codes).await {
        Ok(_) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "groups").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "groups", "revision": rev })).await;
            Ok(StatusCode::OK)
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("分组品种重排序失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_symbol_groups(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(code): Path<String>,
) -> Result<Json<Vec<GroupDto>>, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::symbol_groups(&ctx.db, &code).await {
        Ok(groups) => {
            let dtos = groups
                .into_iter()
                .map(|g| GroupDto {
                    id: g.id,
                    name: g.name,
                    sort_index: g.sort_index,
                })
                .collect();
            Ok(Json(dtos))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取品种所属分组失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}
