use super::trader::ApiResponse;
use crate::{
    auth::{AuthDevice, RequireAdmin},
    state::ServerContext,
};
use axum::{
    extract::{Path, Query, State},
    Json,
};
use n_core::trader::{broker::BrokerClient, instances::*, strategy::*, ExecutionConfig};
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::Arc;

fn response(result: anyhow::Result<Value>) -> Json<ApiResponse<Value>> {
    Json(match result {
        Ok(v) => ApiResponse::ok(v),
        Err(e) => ApiResponse::err(e.to_string()),
    })
}
fn manager(ctx: &ServerContext) -> anyhow::Result<&Arc<InstanceManager>> {
    ctx.instances
        .get()
        .ok_or_else(|| anyhow::anyhow!("策略实例管理器不可用"))
}
pub async fn list(
    State(ctx): State<Arc<ServerContext>>,
    _: AuthDevice,
) -> Json<ApiResponse<Value>> {
    response(async { Ok(manager(&ctx)?.list().await) }.await)
}
#[derive(Deserialize)]
pub struct Create {
    name: String,
    account: Option<AccountDefinition>,
    simnow: Option<crate::ctp_login::SimnowLogin>,
    strategy: Option<StrategyDefinition>,
    config: Option<ExecutionConfig>,
    copy_from: Option<String>,
}
pub async fn create(
    State(ctx): State<Arc<ServerContext>>,
    _: RequireAdmin,
    Json(input): Json<Create>,
) -> Json<ApiResponse<Value>> {
    response(
        async {
            let _login_control = ctx.ctp_login_control.lock().await;
            let m = manager(&ctx)?;
            let source = if let Some(id) = &input.copy_from {
                Some(m.get(id).await?)
            } else {
                None
            };
            let strategy = match input.strategy {
                Some(s) => s,
                None => {
                    if let Some(r) = &source {
                        r.engine.definition().await
                    } else {
                        StrategyDefinition::default()
                    }
                }
            };
            let config = match input.config {
                Some(c) => c,
                None => {
                    if let Some(r) = &source {
                        r.engine.get_config().await
                    } else {
                        ExecutionConfig::default()
                    }
                }
            };
            anyhow::ensure!(input.account.is_some() != input.simnow.is_some(), "请选择一种账户配置方式");
            let (account, new_credential) = if let Some(login) = input.simnow {
                let (account, path) = crate::ctp_login::new_account(&ctx, &input.name, &login).await?;
                (account, Some(path))
            } else { (input.account.unwrap(), None) };
            let result = m.create(input.name, account, strategy, config).await;
            match result {
                Ok(id) => Ok(json!({"instance_id":id,"login_status":if new_credential.is_some(){"connecting"}else{"not_requested"}})),
                Err(error) => {
                    if let Some(path) = new_credential { let _ = std::fs::remove_file(path); }
                    Err(error)
                }
            }
        }
        .await,
    )
}
pub async fn connection_settings(
    State(ctx): State<Arc<ServerContext>>,
    _: RequireAdmin,
    Path(id): Path<String>,
) -> Json<ApiResponse<Value>> {
    response(crate::ctp_login::settings(&ctx, &id).await)
}
pub async fn login(
    State(ctx): State<Arc<ServerContext>>,
    _: RequireAdmin,
    Path(id): Path<String>,
    Json(input): Json<crate::ctp_login::SimnowLogin>,
) -> Json<ApiResponse<Value>> {
    response(crate::ctp_login::login(&ctx, &id, input).await)
}
pub async fn snapshot(
    State(ctx): State<Arc<ServerContext>>,
    _: AuthDevice,
    Path(id): Path<String>,
) -> Json<ApiResponse<Value>> {
    response(async { manager(&ctx)?.snapshot(&id).await }.await)
}
#[derive(Deserialize)]
pub struct Page {
    page: Option<usize>,
    page_size: Option<usize>,
}
pub async fn orders(
    State(ctx): State<Arc<ServerContext>>,
    _: AuthDevice,
    Path(id): Path<String>,
    Query(page): Query<Page>,
) -> Json<ApiResponse<Value>> {
    response(
        async {
            let r = manager(&ctx)?.get(&id).await?;
            Ok(serde_json::to_value(
                r.engine
                    .execution_orders_page(page.page.unwrap_or(1), page.page_size.unwrap_or(20))
                    .await,
            )?)
        }
        .await,
    )
}
#[derive(Deserialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum Action {
    Pause,
    Resume,
    Delete,
    Configure {
        config: ExecutionConfig,
    },
    Strategy {
        strategy: StrategyDefinition,
    },
    ConfigureStrategy {
        config: ExecutionConfig,
        strategy: StrategyDefinition,
    },
    Confirm {
        condition_id: String,
    },
    Cancel {
        condition_id: String,
    },
    CancelAll,
    CloseAll,
}
pub async fn action(
    State(ctx): State<Arc<ServerContext>>,
    _: RequireAdmin,
    Path(id): Path<String>,
    Json(action): Json<Action>,
) -> Json<ApiResponse<Value>> {
    response(
        async {
            let m = manager(&ctx)?;
            let r = m.get(&id).await?;
            let _control = r.control.lock().await;
            match action {
                Action::Pause => r.engine.pause(true).await?,
                Action::Resume => {
                    anyhow::ensure!(
                        !matches!(r.broker, BrokerClient::Disabled(_)),
                        "账户恢复失败，修复后须重启服务"
                    );
                    r.engine.registry.validate(&r.engine.definition().await)?;
                    r.engine.pause(false).await?;
                }
                Action::Delete => m.remove(&id).await?,
                Action::ConfigureStrategy { config, strategy } => {
                    r.engine.configure_strategy(strategy, config).await?
                }
                Action::Configure { config } => r.engine.update_config(config).await?,
                Action::Strategy { strategy } => r.engine.set_definition(strategy).await?,
                Action::Confirm { condition_id } => {
                    anyhow::ensure!(
                        r.engine.confirm_open(&condition_id).await?,
                        "条件单不存在或无法确认"
                    );
                }
                Action::Cancel { condition_id } => {
                    anyhow::ensure!(
                        r.engine.cancel_condition(&condition_id).await?,
                        "条件单不存在或无法撤销"
                    );
                }
                Action::CancelAll => {
                    for c in r.engine.list_conditions().await {
                        let _ = r.engine.cancel_condition(&c.id).await?;
                    }
                    if let BrokerClient::Ctp(c) = &r.broker {
                        c.emergency_cancel_all().await?;
                    }
                }
                Action::CloseAll => {
                    r.engine.flatten_positions().await?;
                }
            }
            Ok(json!({"instance_id":id,"accepted":true}))
        }
        .await,
    )
}
#[derive(Deserialize)]
pub struct Interval {
    from: Option<i64>,
    to: Option<i64>,
}
pub async fn comparison(
    State(ctx): State<Arc<ServerContext>>,
    _: AuthDevice,
    Query(q): Query<Interval>,
) -> Json<ApiResponse<Value>> {
    response(
        async {
            let to = q.to.unwrap_or_else(|| chrono::Utc::now().timestamp());
            manager(&ctx)?
                .comparison(q.from.unwrap_or(to - 30 * 86400), to)
                .await
        }
        .await,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::{to_bytes, Body},
        http::{Request, StatusCode},
    };
    use tower::ServiceExt;
    async fn call(
        router: &axum::Router,
        method: &str,
        path: &str,
        token: Option<&str>,
        body: Value,
    ) -> (StatusCode, Value) {
        let mut request = Request::builder()
            .method(method)
            .uri(path)
            .header("content-type", "application/json");
        if let Some(t) = token {
            request = request.header("authorization", format!("Bearer {t}"));
        }
        let response = router
            .clone()
            .oneshot(request.body(Body::from(body.to_string())).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let bytes = to_bytes(response.into_body(), 4 * 1024 * 1024)
            .await
            .unwrap();
        (status, serde_json::from_slice(&bytes).unwrap())
    }
    #[tokio::test]
    async fn http_routes_authenticate_and_legacy_config_always_targets_default() {
        let root =
            std::env::temp_dir().join(format!("ntrend-instance-api-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let db = n_core::storage::connect(&root.join("test.db"))
            .await
            .unwrap();
        for (token, role) in [("admin-test", "admin"), ("read-test", "standard")] {
            n_core::storage::repo::create_device(
                &db,
                token,
                token,
                &crate::auth::hash_token(token),
                role,
            )
            .await
            .unwrap();
        }
        let mut config = n_core::config::Config::default();
        config.data_source.primary_source = "offline".into();
        config.data_source.auto_spawn_bridge = false;
        let services = Arc::new(
            n_core::service::Services::new(db.clone(), config, root.join("config.json"))
                .await
                .unwrap(),
        );
        let ctx = ServerContext::new(
            db.clone(),
            services,
            root.clone(),
            crate::secrets::ServerSecrets::default(),
        );
        let manager = InstanceManager::open(
            db,
            root,
            ctx.execution_engine.clone(),
            ctx.execution_engine.registry.clone(),
        )
        .await
        .unwrap();
        ctx.instances.set(manager).ok().unwrap();
        let router = crate::api::build_router(ctx.clone());
        assert_eq!(
            call(
                &router,
                "GET",
                "/api/v1/trader/instances",
                None,
                Value::Null
            )
            .await
            .0,
            StatusCode::UNAUTHORIZED
        );
        let create = json!({"name":"paper","account":{"id":"paper","name":"paper","kind":"paper","config":{"initial_balance":50000,"contracts":{}}},"copy_from":"default"});
        assert_eq!(
            call(
                &router,
                "POST",
                "/api/v1/trader/instances",
                Some("read-test"),
                create.clone()
            )
            .await
            .0,
            StatusCode::FORBIDDEN
        );
        let (status, result) = call(
            &router,
            "POST",
            "/api/v1/trader/instances",
            Some("admin-test"),
            create,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(result["success"], true);
        let id = result["data"]["instance_id"].as_str().unwrap();
        let (_, snapshot) = call(
            &router,
            "GET",
            &format!("/api/v1/trader/instances/{id}/snapshot"),
            Some("read-test"),
            Value::Null,
        )
        .await;
        assert_eq!(snapshot["data"]["instance_id"], id);
        assert_eq!(snapshot["data"]["account_kind"], "paper");
        assert_eq!(snapshot["data"]["paused"], true);
        let mut config = ctx.execution_engine.get_config().await;
        config.default_large_volume = 7;
        let (_, changed) = call(
            &router,
            "POST",
            "/api/v1/trader/config",
            Some("admin-test"),
            serde_json::to_value(config).unwrap(),
        )
        .await;
        assert_eq!(changed["success"], true);
        assert_eq!(
            ctx.execution_engine.get_config().await.default_large_volume,
            7
        );
        assert_eq!(
            ctx.instances
                .get()
                .unwrap()
                .get(id)
                .await
                .unwrap()
                .engine
                .get_config()
                .await
                .default_large_volume,
            2
        );
        let (_, list) = call(
            &router,
            "GET",
            "/api/v1/trader/instances",
            Some("read-test"),
            Value::Null,
        )
        .await;
        assert_eq!(list["data"]["instances"].as_array().unwrap().len(), 2);
        // No runtime tasks are spawned in this test: account setup and all
        // denied login paths remain entirely offline.
        let setup = json!({"name":"SimNow fixture","simnow":{"user_id":"123456","password":"fixture-password","td_front":"tcp://127.0.0.1:12345","app_id":"simnow_client_test","auth_code":"0000000000000000"}});
        let (_, created) = call(
            &router,
            "POST",
            "/api/v1/trader/instances",
            Some("admin-test"),
            setup.clone(),
        )
        .await;
        assert_eq!(created["success"], true, "{created}");
        let sim_id = created["data"]["instance_id"].as_str().unwrap();
        let path = format!("/api/v1/trader/instances/{sim_id}/connection");
        assert_eq!(
            call(&router, "GET", &path, Some("read-test"), Value::Null)
                .await
                .0,
            StatusCode::FORBIDDEN
        );
        let (_, settings) = call(&router, "GET", &path, Some("admin-test"), Value::Null).await;
        assert_eq!(settings["data"]["password_saved"], true);
        assert!(!settings.to_string().contains("fixture-password"));
        assert!(settings["data"].get("Password").is_none());
        assert!(settings["data"].get("AuthCode").is_none());
        let original = ctx.instances.get().unwrap().account(sim_id).await.unwrap();
        let (_, duplicate) = call(
            &router,
            "POST",
            "/api/v1/trader/instances",
            Some("admin-test"),
            setup.clone(),
        )
        .await;
        assert_eq!(duplicate["success"], false);
        assert_eq!(
            std::fs::read_dir(ctx.data_dir.join("ctp_credentials"))
                .unwrap()
                .count(),
            1
        );
        let login_path = format!("/api/v1/trader/instances/{sim_id}/login");
        assert_eq!(
            call(
                &router,
                "POST",
                &login_path,
                Some("read-test"),
                setup["simnow"].clone()
            )
            .await
            .0,
            StatusCode::FORBIDDEN
        );
        let mut wrong = setup["simnow"].clone();
        wrong["user_id"] = json!("654321");
        assert_eq!(
            call(&router, "POST", &login_path, Some("admin-test"), wrong)
                .await
                .1["success"],
            false
        );
        let (_, paper_login) = call(
            &router,
            "POST",
            &format!("/api/v1/trader/instances/{id}/login"),
            Some("admin-test"),
            setup["simnow"].clone(),
        )
        .await;
        assert_eq!(paper_login["success"], false);
        assert!(ctx.ctp_sidecars.lock().await.is_empty());
        assert_eq!(
            serde_json::to_value(ctx.instances.get().unwrap().account(sim_id).await.unwrap())
                .unwrap(),
            serde_json::to_value(original).unwrap()
        );
        assert!(
            ctx.instances
                .get()
                .unwrap()
                .get(sim_id)
                .await
                .unwrap()
                .engine
                .paused()
                .await
        );
        let (_, catalog) = call(
            &router,
            "GET",
            "/api/v1/trader/instances",
            Some("read-test"),
            Value::Null,
        )
        .await;
        assert!(!catalog.to_string().contains("fixture-password"));
    }
}
