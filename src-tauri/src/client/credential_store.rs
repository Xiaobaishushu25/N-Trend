use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::RwLock;

const KEYRING_SERVICE: &str = "ntrend_client";

fn keyring_service(data_dir: &Path) -> String {
    if data_dir
        .to_string_lossy()
        .contains("com.ntrend.app.research")
    {
        format!("{KEYRING_SERVICE}:research")
    } else {
        KEYRING_SERVICE.to_string()
    }
}
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
    keyring_service: String,
    record: RwLock<AuthRecord>,
}

impl CredentialStore {
    pub fn new(data_dir: &Path) -> Self {
        let auth_file_path = data_dir.join("client_auth.json");
        let mut record = AuthRecord::default();
        if data_dir
            .to_string_lossy()
            .contains("com.ntrend.app.research")
        {
            record.server_url = "http://127.0.0.1:18081".into();
        }
        let keyring_service = keyring_service(data_dir);

        // 1. Try reading from file first
        if let Ok(content) = std::fs::read_to_string(&auth_file_path) {
            if let Ok(saved) = serde_json::from_str::<AuthRecord>(&content) {
                record = saved;
            }
        }

        // 2. Try loading token from OS Keyring (preferred for token security)
        if let Ok(entry) = keyring::Entry::new(&keyring_service, KEYRING_USER) {
            if let Ok(secret) = entry.get_password() {
                if !secret.trim().is_empty() {
                    record.device_token = secret;
                }
            }
        }

        Self {
            auth_file_path,
            keyring_service,
            record: RwLock::new(record),
        }
    }

    pub fn reload_from_disk(&self) {
        if let Ok(content) = std::fs::read_to_string(&self.auth_file_path) {
            if let Ok(saved) = serde_json::from_str::<AuthRecord>(&content) {
                *self.record.write().unwrap() = saved;
            }
        }
    }

    pub fn is_research(&self) -> bool {
        self.keyring_service.ends_with(":research")
    }

    pub fn get_record(&self) -> AuthRecord {
        self.reload_from_disk();
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
        let keyring_service = &self.keyring_service;
        // 1. Try saving token to OS Keyring
        if let Ok(entry) = keyring::Entry::new(&keyring_service, KEYRING_USER) {
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
