//! Research is an execution boundary, not a configurable trading mode.
use axum::http::Method;
use n_core::config::Config;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RuntimeProfile {
    Production,
    Research,
}
impl RuntimeProfile {
    pub fn parse(args: &[String]) -> anyhow::Result<Self> {
        let mut result = Self::Production;
        for (i, arg) in args.iter().enumerate() {
            let value = if arg == "--profile" {
                Some(
                    args.get(i + 1)
                        .ok_or_else(|| anyhow::anyhow!("--profile needs a value"))?
                        .as_str(),
                )
            } else {
                arg.strip_prefix("--profile=")
            };
            if let Some(value) = value {
                result = match value {
                    "research" => Self::Research,
                    "production" => Self::Production,
                    _ => anyhow::bail!("unknown runtime profile: {value}"),
                };
            }
        }
        Ok(result)
    }
    pub fn is_research(self) -> bool {
        self == Self::Research
    }
    pub fn config(self, config: &mut Config) {
        if self.is_research() {
            config.app_config.auto_start_scheduler = false;
            config.data_source.primary_source = "offline".into();
            config.data_source.auto_spawn_bridge = false;
            config.data_source.tq_password.clear();
            config.email.enabled = false;
            config.email.smtp_password.clear();
        }
    }
    pub fn allows(self, method: &Method, path: &str) -> bool {
        if !self.is_research() {
            return true;
        }
        // Reads capable of contacting external bridges are blocked too.
        if path.starts_with("/api/v1/trader")
            || path.starts_with("/api/v1/quotes")
            || path.starts_with("/api/v1/contracts")
        {
            return false;
        }
        if *method == Method::GET || *method == Method::HEAD || *method == Method::OPTIONS {
            return true;
        }
        matches!(
            path,
            "/api/v1/devices/register"
                | "/api/v1/annotations"
                | "/api/v1/decisions"
                | "/api/v1/actions/scan"
                | "/api/v1/actions/scan-fast"
                | "/api/v1/review/outcomes/refresh"
        )
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn imported_production_config_cannot_enable_execution() {
        let profile = RuntimeProfile::Research;
        let mut c = Config::default();
        c.data_source.primary_source = "tqsdk".into();
        c.data_source.auto_spawn_bridge = true;
        c.email.enabled = true;
        profile.config(&mut c);
        assert_eq!(c.data_source.primary_source, "offline");
        assert!(!c.data_source.auto_spawn_bridge);
        assert!(!c.email.enabled);
        for path in [
            "/api/v1/trader/emergency/close-all",
            "/api/v1/trader/condition/confirm",
            "/api/v1/admin/bridge/restart",
            "/api/v1/actions/refresh",
            "/api/v1/settings/server",
        ] {
            assert!(!profile.allows(&Method::POST, path));
        }
        assert!(!profile.allows(&Method::GET, "/api/v1/trader/account"));
        assert!(profile.allows(&Method::POST, "/api/v1/actions/scan"));
        assert!(RuntimeProfile::parse(&["--profile=typo".into()]).is_err());
    }
}

#[cfg(test)]
mod http_tests {
    use super::*;
    use tower::ServiceExt;
    #[tokio::test]
    async fn research_router_blocks_bridge_and_trading_without_touching_a_counter() {
        let db = n_core::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        let mut cfg = Config::default();
        RuntimeProfile::Research.config(&mut cfg);
        let service = std::sync::Arc::new(
            n_core::service::Services::new(
                db.clone(),
                cfg,
                std::env::temp_dir().join("ntrend-research-no-config.json"),
            )
            .await
            .unwrap(),
        );
        let ctx = crate::state::ServerContext::with_profile(
            db,
            service,
            std::env::temp_dir(),
            crate::secrets::ServerSecrets::default(),
            RuntimeProfile::Research,
        );
        let router = crate::api::build_router(ctx);
        for path in [
            "/api/v1/trader/emergency/close-all",
            "/api/v1/trader/condition/confirm",
            "/api/v1/admin/bridge/restart",
            "/api/v1/actions/refresh",
            "/api/v1/settings/server",
        ] {
            let response = router
                .clone()
                .oneshot(
                    axum::http::Request::post(path)
                        .body(axum::body::Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), axum::http::StatusCode::FORBIDDEN);
        }
    }
}
