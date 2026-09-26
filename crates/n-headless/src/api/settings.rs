use crate::auth::{AuthDevice, RequireAdmin};
use crate::state::ServerContext;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::Json;
use n_protocol::config::{
    AppConfigDto, ConfigApplyResult, DataSourceConfigDto, EmailSettingsDto, FetchConfigDto,
    LogConfigDto, NotifyConfigDto, PrecloseConfigDto, QuoteConfigDto, SchedulerConfigDto,
    ServerSettingsDto, ServerSettingsUpdate,
};
use n_protocol::error::ApiErrorResponse;
use std::sync::Arc;

pub async fn get_server_settings(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<ServerSettingsDto>, (StatusCode, Json<ApiErrorResponse>)> {
    let cfg = ctx.services.config().await;
    let secrets = ctx.secrets.read().await;
    let rev = n_core::storage::repo::get_revision(&ctx.db, "settings")
        .await
        .unwrap_or(1);

    let dto = ServerSettingsDto {
        config_revision: rev,
        app_config: AppConfigDto {
            auto_start_scheduler: cfg.app_config.auto_start_scheduler,
            logic_version: cfg.app_config.logic_version,
        },
        scheduler: SchedulerConfigDto {
            refresh_interval_secs: cfg.scheduler.refresh_interval_secs,
            scan_interval_secs: cfg.scheduler.scan_interval_secs,
            trading_only: cfg.scheduler.trading_only,
        },
        fetch: FetchConfigDto {
            request_interval_ms: cfg.fetch.request_interval_ms,
            minutely_budget: cfg.fetch.minutely_budget,
            backfill_count: cfg.fetch.backfill_count,
            incremental_count: cfg.fetch.incremental_count,
        },
        quote: QuoteConfigDto {
            poll_interval_ms: cfg.quote.poll_interval_ms,
            request_interval_ms: cfg.quote.request_interval_ms,
            minutely_budget: cfg.quote.minutely_budget,
        },
        notify: NotifyConfigDto {
            in_app_new_pattern: cfg.notify.in_app_new_pattern,
            new_pattern_min_score: cfg.notify.new_pattern_min_score,
            in_app_entry_trigger: cfg.notify.in_app_entry_trigger,
            system_entry_trigger: cfg.notify.system_entry_trigger,
        },
        preclose: PrecloseConfigDto {
            schema_version: cfg.preclose.schema_version,
            enabled: cfg.preclose.enabled,
            lead_secs: cfg.preclose.lead_secs,
            horizon_minutes: cfg.preclose.horizon_minutes,
            in_app_notify: cfg.preclose.in_app_notify,
        },
        log: LogConfigDto {
            level: cfg.log.level,
        },
        data_source: DataSourceConfigDto {
            primary_source: cfg.data_source.primary_source,
            fallback_enabled: cfg.data_source.fallback_enabled,
            tq_account: cfg.data_source.tq_account,
            tq_password_configured: !secrets.tq_password.is_empty(),
            bridge_port: cfg.data_source.bridge_port,
            auto_spawn_bridge: cfg.data_source.auto_spawn_bridge,
            python_path: cfg.data_source.python_path,
        },
        email: EmailSettingsDto {
            enabled: cfg.email.enabled,
            to: cfg.email.to,
            from: cfg.email.from,
            smtp_host: cfg.email.smtp_host,
            smtp_port: cfg.email.smtp_port,
            smtp_user: cfg.email.smtp_user,
            smtp_password_configured: !secrets.smtp_password.is_empty(),
        },
    };

    Ok(Json(dto))
}

pub async fn update_server_settings(
    State(ctx): State<Arc<ServerContext>>,
    RequireAdmin(_): RequireAdmin,
    Json(update): Json<ServerSettingsUpdate>,
) -> Result<Json<ConfigApplyResult>, (StatusCode, Json<ApiErrorResponse>)> {
    let current_rev = n_core::storage::repo::get_revision(&ctx.db, "settings")
        .await
        .unwrap_or(1);
    if update.config_revision != current_rev {
        let err = ApiErrorResponse::new(
            "CONFLICT",
            format!(
                "配置版本已过期 (当前版本: {}, 提交版本: {})，请刷新后重试",
                current_rev, update.config_revision
            ),
            "",
        );
        return Err((StatusCode::CONFLICT, Json(err)));
    }

    let mut applied = Vec::new();
    let mut restart_required = Vec::new();
    let rejected = Vec::new();

    // 1. 处理密码等秘密配置
    let mut secrets_changed = false;
    {
        let mut secrets = ctx.secrets.write().await;
        if let Some(ds) = &update.data_source {
            if let Some(pwd) = &ds.tq_password {
                if !pwd.trim().is_empty() {
                    secrets.tq_password = pwd.trim().to_string();
                    secrets_changed = true;
                    applied.push("tq_password".to_string());
                }
            }
        }
        if let Some(em) = &update.email {
            if let Some(pwd) = &em.smtp_password {
                if !pwd.trim().is_empty() {
                    secrets.smtp_password = pwd.trim().to_string();
                    secrets_changed = true;
                    applied.push("smtp_password".to_string());
                }
            }
        }
        if secrets_changed {
            if let Err(e) = secrets.save(&ctx.data_dir) {
                tracing::error!("保存密钥文件失败: {e}");
            }
        }
    }

    // 2. 更新通用配置
    let mut current_cfg = ctx.services.config().await;
    if let Some(app) = update.app_config {
        current_cfg.app_config.auto_start_scheduler = app.auto_start_scheduler;
        current_cfg.app_config.logic_version = app.logic_version;
        applied.push("app_config".to_string());
    }
    if let Some(sch) = update.scheduler {
        current_cfg.scheduler.refresh_interval_secs = sch.refresh_interval_secs;
        current_cfg.scheduler.scan_interval_secs = sch.scan_interval_secs;
        current_cfg.scheduler.trading_only = sch.trading_only;
        applied.push("scheduler".to_string());
    }
    if let Some(fetch) = update.fetch {
        current_cfg.fetch.request_interval_ms = fetch.request_interval_ms;
        current_cfg.fetch.minutely_budget = fetch.minutely_budget;
        current_cfg.fetch.backfill_count = fetch.backfill_count;
        current_cfg.fetch.incremental_count = fetch.incremental_count;
        applied.push("fetch".to_string());
    }
    if let Some(q) = update.quote {
        current_cfg.quote.poll_interval_ms = q.poll_interval_ms;
        current_cfg.quote.request_interval_ms = q.request_interval_ms;
        current_cfg.quote.minutely_budget = q.minutely_budget;
        applied.push("quote".to_string());
    }
    if let Some(not) = update.notify {
        current_cfg.notify.in_app_new_pattern = not.in_app_new_pattern;
        current_cfg.notify.new_pattern_min_score = not.new_pattern_min_score;
        current_cfg.notify.in_app_entry_trigger = not.in_app_entry_trigger;
        current_cfg.notify.system_entry_trigger = not.system_entry_trigger;
        applied.push("notify".to_string());
    }
    if let Some(pre) = update.preclose {
        current_cfg.preclose.schema_version = pre.schema_version;
        current_cfg.preclose.enabled = pre.enabled;
        current_cfg.preclose.lead_secs = pre.lead_secs;
        current_cfg.preclose.horizon_minutes = pre.horizon_minutes;
        current_cfg.preclose.in_app_notify = pre.in_app_notify;
        applied.push("preclose".to_string());
    }
    if let Some(log) = update.log {
        if log.level != current_cfg.log.level {
            current_cfg.log.level = log.level;
            restart_required.push("log.level (需重启服务)".to_string());
        }
    }
    if let Some(ds) = update.data_source {
        current_cfg.data_source.primary_source = ds.primary_source;
        current_cfg.data_source.fallback_enabled = ds.fallback_enabled;
        current_cfg.data_source.tq_account = ds.tq_account;
        if let Some(pwd) = &ds.tq_password {
            if !pwd.trim().is_empty() {
                current_cfg.data_source.tq_password = pwd.trim().to_string();
            }
        }
        current_cfg.data_source.bridge_port = ds.bridge_port;
        current_cfg.data_source.auto_spawn_bridge = ds.auto_spawn_bridge;
        current_cfg.data_source.python_path = ds.python_path;
        applied.push("data_source".to_string());
    }
    if let Some(em) = update.email {
        current_cfg.email.enabled = em.enabled;
        current_cfg.email.to = em.to;
        current_cfg.email.from = em.from;
        current_cfg.email.smtp_host = em.smtp_host;
        current_cfg.email.smtp_port = em.smtp_port;
        current_cfg.email.smtp_user = em.smtp_user;
        if let Some(pwd) = &em.smtp_password {
            if !pwd.trim().is_empty() {
                current_cfg.email.smtp_password = pwd.trim().to_string();
            }
        }
        applied.push("email".to_string());
    }

    if let Err(e) = ctx.services.apply_config(current_cfg).await {
        let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("持久化配置失败: {e}"), "");
        return Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)));
    }

    let new_rev = n_core::storage::repo::bump_revision(&ctx.db, "settings")
        .await
        .unwrap_or(current_rev + 1);

    let seq = ctx.next_event_seq();
    ctx.realtime_hub
        .broadcast(
            seq,
            "state.changed",
            serde_json::json!({ "scope": "settings", "revision": new_rev }),
        )
        .await;

    Ok(Json(ConfigApplyResult {
        applied,
        restart_required,
        rejected,
        new_revision: new_rev,
    }))
}

#[derive(serde::Deserialize)]
pub struct SetLastGroupReq {
    pub group_id: Option<i64>,
}

#[derive(serde::Deserialize)]
pub struct SetTimeframesReq {
    pub timeframes: Vec<String>,
}

pub async fn set_last_group(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Json(payload): Json<SetLastGroupReq>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.set_last_group(payload.group_id).await {
        Ok(_) => Ok(StatusCode::OK),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("设置最后选中的分组失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn set_timeframes(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Json(payload): Json<SetTimeframesReq>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.set_timeframes(payload.timeframes).await {
        Ok(_) => Ok(StatusCode::OK),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("设置周期配置失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn reset_config(
    State(ctx): State<Arc<ServerContext>>,
    RequireAdmin(_): RequireAdmin,
) -> Result<Json<ServerSettingsDto>, (StatusCode, Json<ApiErrorResponse>)> {
    if let Err(e) = ctx.services.reset_config().await {
        let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("重置配置失败: {e}"), "");
        return Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)));
    }
    get_server_settings(
        State(ctx),
        AuthDevice {
            id: String::new(),
            role: "admin".to_string(),
            name: String::new(),
        },
    )
    .await
}
