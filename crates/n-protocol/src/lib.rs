pub mod auth;
pub mod config;
pub mod dto;
pub mod error;
pub mod quality;
pub mod review;
pub mod trader_stream;
pub mod version;
pub mod ws;

pub use auth::*;
pub use config::*;
pub use dto::*;
pub use error::*;
pub use quality::*;
pub use ws::*;

pub const API_VERSION: &str = "v2";
pub const CURRENT_SERVER_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const REQUIRED_CLIENT_VERSION: &str = CURRENT_SERVER_VERSION;

/// Daily maintenance window in China Standard Time (16:30–19:30).
pub fn automatic_backup_window(now: chrono::DateTime<chrono::Utc>) -> bool {
    use chrono::Timelike;
    let local = now + chrono::Duration::hours(8);
    (16 * 60 + 30..19 * 60 + 30).contains(&(local.hour() * 60 + local.minute()))
}

#[cfg(test)]
mod tests {
    use super::automatic_backup_window;
    use chrono::{TimeZone, Utc};

    #[test]
    fn backup_window_is_anchored_to_shanghai_time_and_excludes_trading_hours() {
        let allowed = |hour, minute| {
            automatic_backup_window(Utc.with_ymd_and_hms(2026, 11, 20, hour, minute, 0).unwrap())
        };
        assert!(!allowed(1, 45));
        assert!(!allowed(8, 29));
        assert!(allowed(8, 30));
        assert!(allowed(11, 29));
        assert!(!allowed(11, 30));
        assert!(!allowed(13, 0));
    }
}
