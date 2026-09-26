pub mod auth;
pub mod config;
pub mod dto;
pub mod error;
pub mod ws;

pub use auth::*;
pub use config::*;
pub use dto::*;
pub use error::*;
pub use ws::*;

pub const API_VERSION: &str = "v1";
pub const CURRENT_SERVER_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const MIN_CLIENT_VERSION: &str = "3.0.0";
