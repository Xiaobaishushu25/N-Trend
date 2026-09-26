use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum StateChangeScope {
    Symbols,
    Groups,
    ManualLevels,
    Signals,
    Settings,
    Notifications,
}

impl StateChangeScope {
    pub fn as_str(&self) -> &'static str {
        match self {
            StateChangeScope::Symbols => "symbols",
            StateChangeScope::Groups => "groups",
            StateChangeScope::ManualLevels => "manual_levels",
            StateChangeScope::Signals => "signals",
            StateChangeScope::Settings => "settings",
            StateChangeScope::Notifications => "notifications",
        }
    }

    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "symbols" => Some(StateChangeScope::Symbols),
            "groups" => Some(StateChangeScope::Groups),
            "manual_levels" => Some(StateChangeScope::ManualLevels),
            "signals" => Some(StateChangeScope::Signals),
            "settings" => Some(StateChangeScope::Settings),
            "notifications" => Some(StateChangeScope::Notifications),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum WsClientMessage {
    Subscribe {
        symbols: Vec<String>,
        timeframes: Vec<String>,
    },
    Unsubscribe {
        symbols: Vec<String>,
        timeframes: Vec<String>,
    },
    Ping {
        client_time: i64,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum WsServerMessage {
    Event {
        event_seq: u64,
        topic: String,
        payload: serde_json::Value,
    },
    Subscribed {
        symbols: Vec<String>,
        timeframes: Vec<String>,
    },
    Pong {
        client_time: i64,
        server_time: i64,
    },
    Error {
        code: String,
        message: String,
    },
}

impl WsServerMessage {
    pub fn new_event(event_seq: u64, topic: impl Into<String>, payload: impl Serialize) -> Self {
        Self::Event {
            event_seq,
            topic: topic.into(),
            payload: serde_json::to_value(payload).unwrap_or(serde_json::Value::Null),
        }
    }
}
