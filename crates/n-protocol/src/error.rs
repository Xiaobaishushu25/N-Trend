use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ApiErrorResponse {
    pub code: String,
    pub message: String,
    #[serde(alias = "request_id")]
    pub request_id: String,
}

impl ApiErrorResponse {
    pub fn new(code: impl Into<String>, message: impl Into<String>, request_id: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            request_id: request_id.into(),
        }
    }
}

impl std::fmt::Display for ApiErrorResponse {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "[{}] {} (requestId: {})", self.code, self.message, self.request_id)
    }
}

impl std::error::Error for ApiErrorResponse {}
