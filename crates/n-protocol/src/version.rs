//! Client and server must be deployed from the same current release.
use crate::{ApiErrorResponse, API_VERSION, CURRENT_SERVER_VERSION};

pub const RELEASE_HEADER: &str = "x-ntrend-version";
pub const API_HEADER: &str = "x-ntrend-api-version";

pub fn require_current_peer(
    peer: &str,
    release: Option<&str>,
    api: Option<&str>,
) -> Result<(), ApiErrorResponse> {
    if release == Some(CURRENT_SERVER_VERSION) && api == Some(API_VERSION) {
        return Ok(());
    }
    Err(ApiErrorResponse::new(
        "UPGRADE_REQUIRED",
        format!(
            "版本不匹配：本端 {CURRENT_SERVER_VERSION} / {API_VERSION}，{peer} {} / {}。请将客户端和服务端同时升级到同一最新版本，禁止使用旧版接口。",
            release.unwrap_or("未声明版本"),
            api.unwrap_or("未声明协议"),
        ),
        "",
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_exact_current_release_and_api_are_accepted() {
        assert!(
            require_current_peer("服务端", Some(CURRENT_SERVER_VERSION), Some(API_VERSION)).is_ok()
        );
        for (release, api) in [
            (None, None),
            (Some("3.0.1"), Some(API_VERSION)),
            (Some("999.0.0"), Some(API_VERSION)),
            (Some(CURRENT_SERVER_VERSION), None),
            (None, Some(API_VERSION)),
            (Some(CURRENT_SERVER_VERSION), Some("v0")),
        ] {
            let err = require_current_peer("服务端", release, api).unwrap_err();
            assert_eq!(err.code, "UPGRADE_REQUIRED");
            assert!(err.message.contains("客户端和服务端同时升级"));
        }
    }
}
