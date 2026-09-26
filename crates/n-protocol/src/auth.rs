use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum DeviceRole {
    Admin,
    Standard,
}

impl DeviceRole {
    pub fn as_str(&self) -> &'static str {
        match self {
            DeviceRole::Admin => "admin",
            DeviceRole::Standard => "standard",
        }
    }

    pub fn from_str(s: &str) -> Self {
        match s.to_lowercase().as_str() {
            "admin" => DeviceRole::Admin,
            _ => DeviceRole::Standard,
        }
    }

    pub fn is_admin(&self) -> bool {
        matches!(self, DeviceRole::Admin)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct DeviceRegisterRequest {
    #[serde(alias = "deviceName", alias = "device_name")]
    pub device_name: String,
    #[serde(alias = "adminKey", alias = "admin_key")]
    pub admin_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct DeviceRegisterResponse {
    #[serde(alias = "deviceId", alias = "device_id")]
    pub device_id: String,
    pub token: String,
    pub role: String,
    #[serde(alias = "serverTime", alias = "server_time")]
    pub server_time: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct DeviceItemDto {
    pub id: String,
    pub name: String,
    pub role: String,
    #[serde(alias = "createdAt", alias = "created_at")]
    pub created_at: String,
    #[serde(default, alias = "lastSeenAt", alias = "last_seen_at")]
    pub last_seen_at: Option<String>,
    #[serde(default, alias = "revokedAt", alias = "revoked_at")]
    pub revoked_at: Option<String>,
}
