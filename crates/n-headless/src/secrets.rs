use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::fs::File;
use std::io::Write;
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ServerSecrets {
    #[serde(default)]
    pub admin_key: String,
    #[serde(default)]
    pub tq_password: String,
    #[serde(default)]
    pub smtp_password: String,
}

impl ServerSecrets {
    pub fn load_or_create(data_dir: &Path) -> Result<Self> {
        let path = data_dir.join("secrets.json");
        let mut secrets = if path.exists() {
            let content = std::fs::read_to_string(&path)
                .with_context(|| format!("读取密钥文件 {} 失败", path.display()))?;
            serde_json::from_str::<ServerSecrets>(&content).unwrap_or_default()
        } else {
            ServerSecrets::default()
        };

        // 环境变量 NTREND_ADMIN_KEY 优先
        if let Ok(env_key) = std::env::var("NTREND_ADMIN_KEY") {
            if !env_key.trim().is_empty() {
                secrets.admin_key = env_key.trim().to_string();
            }
        }

        // 若首次运行且无管理密钥，自动生成一个随机 32 字符的 AdminKey
        if secrets.admin_key.trim().is_empty() {
            let generated: String = uuid::Uuid::new_v4().simple().to_string();
            secrets.admin_key = generated;
            tracing::warn!(
                "🔑 [安全提示] 首次启动未检测到管理密钥，已自动生成 NTREND_ADMIN_KEY: {}",
                secrets.admin_key
            );
            secrets.save(data_dir)?;
        }

        Ok(secrets)
    }

    pub fn save(&self, data_dir: &Path) -> Result<()> {
        let path = data_dir.join("secrets.json");
        let tmp_path = data_dir.join(format!("secrets.json.tmp.{}", uuid::Uuid::new_v4()));

        let json = serde_json::to_string_pretty(self)?;
        {
            let mut file = File::create(&tmp_path)?;
            file.write_all(json.as_bytes())?;
            file.sync_all()?;
        }

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&tmp_path, std::fs::Permissions::from_mode(0o600));
        }

        std::fs::rename(&tmp_path, &path).context("原子替换 secrets.json 失败")?;
        Ok(())
    }

    pub fn admin_key_matches(&self, key: &str) -> bool {
        !self.admin_key.is_empty() && self.admin_key.trim() == key.trim()
    }
}
