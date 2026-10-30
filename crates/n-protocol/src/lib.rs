pub mod auth;
pub mod config;
pub mod dto;
pub mod error;
pub mod quality;
pub mod ws;
pub mod trader_stream;
pub mod version;

pub use auth::*;
pub use config::*;
pub use dto::*;
pub use error::*;
pub use quality::*;
pub use ws::*;

pub const API_VERSION: &str = "v2";
pub const CURRENT_SERVER_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const REQUIRED_CLIENT_VERSION: &str = CURRENT_SERVER_VERSION;
