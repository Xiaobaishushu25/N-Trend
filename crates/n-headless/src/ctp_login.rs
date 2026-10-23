//! Client supplied SimNow credentials remain exclusively in the server vault.
use crate::{ctp_sidecar::CtpSidecar, state::ServerContext};
use anyhow::{ensure, Context, Result};
use n_core::trader::instances::{AccountDefinition, AccountKind};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{io::Write, path::Path};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SimnowLogin {
    pub user_id: String,
    #[serde(default)]
    pub password: String,
    pub td_front: String,
    pub app_id: String,
    #[serde(default)]
    pub auth_code: String,
}

impl SimnowLogin {
    pub fn credentials(&self, previous: Option<&Value>) -> Result<Value> {
        let user = self.user_id.trim();
        if let Some(previous) = previous {
            ensure!(
                previous["BrokerID"].as_str() == Some("9999")
                    && previous["UserID"].as_str() == Some(user),
                "已保存凭据的账户身份不一致，禁止覆盖"
            );
        }
        ensure!(
            !user.is_empty() && user.len() <= 16 && user.bytes().all(|b| b.is_ascii_digit()),
            "请填写 SimNow 投资者账号（数字）"
        );
        let address = self.td_front.trim();
        let authority = address
            .strip_prefix("tcp://")
            .context("交易前置格式应为 tcp://主机:端口")?;
        let (host, port) = authority.rsplit_once(':').context("交易前置缺少端口")?;
        ensure!(
            !host.is_empty()
                && host
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-')
                && port.parse::<u16>().is_ok_and(|p| p > 0),
            "交易前置地址或端口无效"
        );
        let password = if self.password.is_empty() {
            previous.and_then(|v| v["Password"].as_str()).unwrap_or("")
        } else {
            &self.password
        };
        let auth = if self.auth_code.is_empty() {
            previous.and_then(|v| v["AuthCode"].as_str()).unwrap_or("")
        } else {
            &self.auth_code
        };
        ensure!(
            !password.is_empty() && password.len() <= 40 && !password.contains(['\n', '\r', '\0']),
            "请填写有效的交易密码（最多40字节）"
        );
        ensure!(
            !self.app_id.trim().is_empty()
                && self.app_id.trim().len() <= 32
                && self.app_id.trim().is_ascii(),
            "AppID 无效"
        );
        ensure!(
            !auth.is_empty()
                && auth.len() <= 16
                && auth.is_ascii()
                && !auth.chars().any(char::is_control),
            "认证码无效（最多16字节）"
        );
        Ok(
            json!({"Environment":"simnow","BrokerID":"9999","UserID":user,"Password":password,"TdFront":address,"AppID":self.app_id.trim(),"AuthCode":auth}),
        )
    }
}

pub fn public_config(value: &Value) -> Value {
    json!({"configured":true,"environment":value["Environment"],"user_id":value["UserID"],"broker_id":value["BrokerID"],"td_front":value["TdFront"],"app_id":value["AppID"],"password_saved":value["Password"].as_str().is_some_and(|s|!s.is_empty()),"auth_code_saved":value["AuthCode"].as_str().is_some_and(|s|!s.is_empty())})
}

pub fn write_credentials(path: &Path, credentials: &Value) -> Result<()> {
    std::fs::create_dir_all(path.parent().context("凭据目录无效")?)?;
    let temp = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| -> Result<()> {
        let mut options = std::fs::OpenOptions::new();
        options.create_new(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&temp)?;
        file.write_all(&serde_json::to_vec(credentials)?)?;
        file.sync_all()?;
        drop(file);
        std::fs::rename(&temp, path)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(temp);
    }
    result.context("保存服务端账户凭据失败")
}

pub async fn new_account(
    ctx: &ServerContext,
    name: &str,
    login: &SimnowLogin,
) -> Result<(AccountDefinition, std::path::PathBuf)> {
    let credentials = login.credentials(None)?;
    let manager = ctx.instances.get().context("实例管理器不可用")?;
    let used = manager
        .all()
        .await
        .into_iter()
        .filter_map(|r| match r.account.kind {
            AccountKind::Ctp { port, .. } => Some(port),
            _ => None,
        })
        .collect::<std::collections::HashSet<_>>();
    let port = (8767..=8999)
        .find(|p| !used.contains(p) && std::net::TcpListener::bind(("127.0.0.1", *p)).is_ok())
        .context("没有可用的账户桥接端口")?;
    let id = format!("simnow-{}", uuid::Uuid::new_v4());
    let path = ctx
        .data_dir
        .join("ctp_credentials")
        .join(format!("{id}.json"));
    write_credentials(&path, &credentials)?;
    Ok((
        AccountDefinition {
            id: id.clone(),
            name: name.into(),
            kind: AccountKind::Ctp {
                port,
                broker_id: "9999".into(),
                user_id: login.user_id.trim().into(),
                credential_ref: Some(id),
            },
        },
        path,
    ))
}

pub async fn settings(ctx: &ServerContext, id: &str) -> Result<Value> {
    let manager = ctx.instances.get().context("实例管理器不可用")?;
    match manager.account(id).await?.kind {
        AccountKind::Ctp {
            credential_ref: Some(reference),
            ..
        } => {
            let path = ctx
                .data_dir
                .join("ctp_credentials")
                .join(format!("{reference}.json"));
            let value: Value =
                serde_json::from_slice(&std::fs::read(path).context("服务端凭据不可读取")?)
                    .context("服务端凭据格式无效")?;
            Ok(public_config(&value))
        }
        AccountKind::Ctp {
            broker_id, user_id, ..
        } => Ok(json!({"configured":false,"broker_id":broker_id,"user_id":user_id})),
        _ => anyhow::bail!("本地模拟账户无需登录"),
    }
}

pub async fn login(ctx: &ServerContext, id: &str, input: SimnowLogin) -> Result<Value> {
    let _serial = ctx.ctp_login_control.lock().await;
    let manager = ctx.instances.get().context("实例管理器不可用")?;
    let runtime = manager.get(id).await?;
    ensure!(
        !matches!(
            runtime.broker,
            n_core::trader::broker::BrokerClient::Disabled(_)
        ),
        "账户恢复失败，请修复状态后重启服务"
    );
    let _control = runtime.control.lock().await;
    let account = manager.account(id).await?;
    let AccountKind::Ctp {
        port,
        ref broker_id,
        ref user_id,
        ref credential_ref,
    } = account.kind
    else {
        anyhow::bail!("本地模拟账户无需登录")
    };
    ensure!(
        broker_id.is_empty() || broker_id == "9999",
        "此入口只支持 SimNow 账户"
    );
    ensure!(
        user_id.is_empty() || user_id == input.user_id.trim(),
        "不能更换实例绑定的账户，请创建新实例"
    );
    for other in manager.all().await {
        if other.definition.id != id {
            if let AccountKind::Ctp {
                broker_id, user_id, ..
            } = manager.account(&other.definition.id).await?.kind
            {
                ensure!(
                    broker_id != "9999" || user_id != input.user_id.trim(),
                    "同一 SimNow 账户已经绑定其他实例"
                );
            }
        }
    }
    if user_id.is_empty() {
        ensure!(
            !runtime.engine.has_unfinished().await,
            "账户归属未确认且有未完成交易，须先核对"
        );
        if let n_core::trader::broker::BrokerClient::Ctp(client) = &runtime.broker {
            if let Ok(health) = client.health().await {
                ensure!(
                    health
                        .user_id
                        .as_deref()
                        .is_none_or(|u| u.is_empty() || u == input.user_id.trim()),
                    "现有桥接属于另一账户，请创建新实例"
                );
            }
        }
    }
    let reference = credential_ref
        .clone()
        .unwrap_or_else(|| format!("client-{}", account.id));
    let path = ctx
        .data_dir
        .join("ctp_credentials")
        .join(format!("{reference}.json"));
    let previous = if path.exists() {
        Some(serde_json::from_slice::<Value>(&std::fs::read(&path)?).context("原凭据格式无效")?)
    } else {
        None
    };
    let credentials = input.credentials(previous.as_ref())?;
    let python = ctx.services.config().await.data_source.python_path;
    // The supervisor refuses to replace externally owned bridges. A rejected
    // restart restores the previous credential file.
    let mut sidecars = ctx.ctp_sidecars.lock().await;
    write_credentials(&path, &credentials)?;
    let flow = ctx
        .data_dir
        .join("trader_instances")
        .join(id)
        .join("flow_ctp");
    let result = if let Some(sidecar) = sidecars.get(id) {
        sidecar
            .reconnect_account(
                python.as_deref(),
                port,
                path.clone(),
                flow,
                "9999".into(),
                input.user_id.trim().into(),
            )
            .await
    } else {
        CtpSidecar::start_account(
            python.as_deref(),
            port,
            path.clone(),
            flow,
            "9999".into(),
            input.user_id.trim().into(),
        )
        .map(|s| {
            sidecars.insert(id.into(), s);
        })
    };
    if let Err(error) = result {
        if let Some(previous) = previous {
            write_credentials(&path, &previous)?;
        } else {
            std::fs::remove_file(&path)?;
        }
        return Err(error);
    }
    if let Err(error) = manager
        .set_ctp_credentials(id, input.user_id.trim(), &reference)
        .await
    {
        runtime.engine.pause(true).await?;
        if let Some(sidecar) = sidecars.remove(id) {
            sidecar.stop().await;
        }
        if let Some(previous) = previous {
            write_credentials(&path, &previous)?;
        } else {
            std::fs::remove_file(&path)?;
        }
        return Err(error);
    }
    // Credentials accepted is distinct from a successful counter login. The
    // next bridge snapshot drives login/settlement/reconciliation status.
    let pending = json!({"bridge_online":false,"health":{"connected":false,"authenticated":false,"logged_in":false,"ready":false},"account":null,"broker_positions":[]});
    runtime.engine.reconcile(&pending).await?;
    *runtime.snapshot.write().await = pending;
    *runtime.published.write().await = Value::Null;
    *runtime.error.write().await = None;
    Ok(json!({"instance_id":id,"accepted":true,"status":"connecting"}))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn input() -> SimnowLogin {
        SimnowLogin {
            user_id: "123456".into(),
            password: "fixture-password".into(),
            td_front: "tcp://127.0.0.1:12345".into(),
            app_id: "simnow_client_test".into(),
            auth_code: "0000000000000000".into(),
        }
    }
    #[test]
    fn public_settings_never_return_credentials_and_blank_password_reuses_same_identity() {
        let mut login = input();
        let old = login.credentials(None).unwrap();
        let public = public_config(&old);
        assert!(public.get("Password").is_none() && public.get("AuthCode").is_none());
        assert!(!public.to_string().contains("fixture-password"));
        login.password.clear();
        login.auth_code.clear();
        assert_eq!(login.credentials(Some(&old)).unwrap(), old);
        assert!(login.credentials(None).is_err());
        login.user_id = "654321".into();
        assert!(login.credentials(Some(&old)).is_err());
    }
    #[test]
    fn invalid_front_or_fields_are_rejected_before_saving() {
        for front in [
            "http://localhost:8081",
            "tcp://localhost:0",
            "tcp://localhost:99999",
            "tcp://localhost:1234/path",
            "tcp://user@localhost:1234",
        ] {
            let mut login = input();
            login.td_front = front.into();
            assert!(login.credentials(None).is_err());
        }
        let mut login = input();
        login.password = "x".repeat(41);
        assert!(login.credentials(None).is_err());
    }
    #[test]
    fn credential_file_replacement_is_atomic_and_has_no_leftover_temp_file() {
        let root = std::env::temp_dir().join(format!("ntrend-vault-test-{}", uuid::Uuid::new_v4()));
        let path = root.join("account.json");
        let original = input().credentials(None).unwrap();
        write_credentials(&path, &original).unwrap();
        let mut replacement = original;
        replacement["Password"] = json!("replacement-fixture");
        write_credentials(&path, &replacement).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&std::fs::read(&path).unwrap()).unwrap(),
            replacement
        );
        assert_eq!(std::fs::read_dir(&root).unwrap().count(), 1);
        std::fs::remove_dir_all(root).unwrap();
    }
}
