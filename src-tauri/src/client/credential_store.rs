use std::path::{Path, PathBuf};
use std::sync::RwLock;
use serde::{Deserialize, Serialize};

const KEYRING_SERVICE: &str = "ntrend_client";
const KEYRING_USER: &str = "device_token";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthRecord {
    pub server_url: String,
    pub device_id: String,
    pub device_token: String,
    pub device_role: String,
    pub device_name: String,
}

impl Default for AuthRecord {
    fn default() -> Self {
        Self {
            server_url: "http://127.0.0.1:8081".to_string(),
            device_id: String::new(),
            device_token: String::new(),
            device_role: "pc_admin".to_string(),
            device_name: "Desktop PC".to_string(),
        }
    }
}

pub struct CredentialStore {
    auth_file_path: PathBuf,
    record: RwLock<AuthRecord>,
}

impl CredentialStore {
    pub fn new(data_dir: &Path) -> Self {
        let auth_file_path = data_dir.join("client_auth.json");
        let mut record = AuthRecord::default();

        // 1. Try reading from file first
        if let Ok(content) = std::fs::read_to_string(&auth_file_path) {
            if let Ok(saved) = serde_json::from_str::<AuthRecord>(&content) {
                record = saved;
            }
        }

        // 2. Try loading token from OS Keyring (preferred for token security)
        if let Ok(entry) = keyring::Entry::new(KEYRING_SERVICE, KEYRING_USER) {
            if let Ok(secret) = entry.get_password() {
                if !secret.trim().is_empty() {
                    record.device_token = secret;
                }
            }
        }

        Self {
            auth_file_path,
            record: RwLock::new(record),
        }
    }

    pub fn get_record(&self) -> AuthRecord {
        self.record.read().unwrap().clone()
    }

    pub fn get_token(&self) -> String {
        self.record.read().unwrap().device_token.clone()
    }

    pub fn get_server_url(&self) -> String {
        self.record.read().unwrap().server_url.clone()
    }

    pub fn get_device_id(&self) -> String {
        self.record.read().unwrap().device_id.clone()
    }

    pub fn update_auth(&self, record: AuthRecord) -> Result<(), String> {
        // 1. Try saving token to OS Keyring
        if let Ok(entry) = keyring::Entry::new(KEYRING_SERVICE, KEYRING_USER) {
            if record.device_token.is_empty() {
                let _ = entry.delete_password();
            } else {
                let _ = entry.set_password(&record.device_token);
            }
        }

        // 2. Save record to client_auth.json file
        let json = serde_json::to_string_pretty(&record).map_err(|e| e.to_string())?;
        std::fs::write(&self.auth_file_path, json).map_err(|e| e.to_string())?;

        // 3. Update memory
        *self.record.write().unwrap() = record;
        Ok(())
    }

    pub fn clear_token(&self) -> Result<(), String> {
        let mut rec = self.get_record();
        rec.device_token.clear();
        self.update_auth(rec)
    }
}
