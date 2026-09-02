//! Sidecar process lifecycle management for Python TqSdk bridge.

use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

use anyhow::{anyhow, Result};
use serde::Deserialize;

use crate::config::DataSourceConfig;

static SIDECAR_CHILD: Mutex<Option<Child>> = Mutex::new(None);
static ACTIVE_CONFIG: Mutex<Option<DataSourceConfig>> = Mutex::new(None);

pub struct SidecarManager;

impl SidecarManager {
    /// 检查并启动 Python Sidecar 桥接服务
    pub async fn start(config: &DataSourceConfig) -> Result<()> {
        Self::start_with_policy(config, true).await
    }

    async fn start_with_policy(
        config: &DataSourceConfig,
        allow_existing_external: bool,
    ) -> Result<()> {
        if config.primary_source != "tqsdk" || !config.auto_spawn_bridge {
            return Ok(());
        }

        if config.tq_account.trim().is_empty() || config.tq_password.is_empty() {
            tracing::warn!("⚠️ 快期/天勤账号或密码未配置，跳过拉起桥接服务。请在「设置 - 数据」中配置账号密码");
            return Err(anyhow!("快期/天勤账号或密码未配置，请在「设置 - 数据」中填写账号与密码"));
        }

        let port = config.bridge_port;
        let health_url = format!("http://127.0.0.1:{port}/health");

        // 1. 检查当前是否已有配置完全一致的运行中桥接服务
        let is_running_same_config = {
            let active = ACTIVE_CONFIG.lock().unwrap();
            active.as_ref().map_or(false, |c| {
                c.bridge_port == config.bridge_port
                    && c.tq_account == config.tq_account
                    && c.tq_password == config.tq_password
                    && c.python_path == config.python_path
            })
        };

        let owned_pid = SIDECAR_CHILD.lock().unwrap().as_ref().map(Child::id);
        let health = probe_server(&health_url).await;
        if health.healthy && health.service.as_deref() == Some("ntrend-tq-bridge") {
            if owned_pid.is_none() || owned_pid == health.pid {
                if is_running_same_config || owned_pid.is_none() {
                    if owned_pid.is_none() && !allow_existing_external {
                        return Err(anyhow!(
                            "端口 {port} 上存在非当前程序拥有的桥接进程，无法确认新账号配置已生效，请先停止该进程"
                        ));
                    }
                    tracing::info!(
                        "✅ 天勤 Python 桥接服务已在 127.0.0.1:{} 运行中 (PID: {})",
                        port,
                        health.pid.unwrap_or_default()
                    );
                    return Ok(());
                }
            }
        }
        // 端口有响应但并非当前拥有的健康实例时，禁止继续 spawn 后假报“启动成功”。
        // 这正是旧版在遗留进程占用 8765 时出现 500/心跳超时的来源。
        if health.reachable && owned_pid != health.pid {
            return Err(anyhow!(
                "端口 {port} 已被非当前桥接实例占用 (PID: {}, service: {}, reason: {})",
                health.pid.map(|v| v.to_string()).unwrap_or_else(|| "未知".into()),
                health.service.as_deref().unwrap_or("未知"),
                health.reason.as_deref().unwrap_or("无")
            ));
        }

        // 如果配置变更或已死，先终止可能遗留的旧子进程
        Self::stop();

        if !config.auto_spawn_bridge {
            tracing::info!("配置已禁用自动拉起桥接服务，跳过拉起");
            return Ok(());
        }

        // 2. 定位 Python 可执行程序
        let python_bin = find_python_executable(config.python_path.as_deref())?;
        tracing::info!("使用 Python 环境: {}", python_bin.display());

        // 3. 定位 tq_bridge.py 脚本文件
        let script_path = find_bridge_script()?;
        tracing::info!("定位到桥接脚本: {}", script_path.display());

        // 4. 启动子进程
        tracing::info!(
            "正在启动天勤 Python 桥接服务 (端口: {}, 账号: {})...",
            port,
            config.tq_account
        );

        let mut cmd = Command::new(python_bin);
        cmd.arg(&script_path)
            .arg("--port")
            .arg(port.to_string())
            .arg("--parent-pid")
            .arg(std::process::id().to_string())
            // 凭据放入子进程环境，避免出现在系统进程命令行中。
            .env("TQ_ACCOUNT", &config.tq_account)
            .env("TQ_PASSWORD", &config.tq_password);

        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            // CREATE_NO_WINDOW = 0x08000000
            cmd.creation_flags(0x08000000);
        }

        let child = cmd
            .spawn()
            .map_err(|e| anyhow!("无法启动 Python 桥接服务子进程: {e}"))?;
        let child_pid = child.id();

        {
            let mut lock = SIDECAR_CHILD.lock().unwrap();
            *lock = Some(child);
            let mut active = ACTIVE_CONFIG.lock().unwrap();
            *active = Some(config.clone());
        }

        // 5. 轮询等待服务启动并就绪 (TqApi 冷启动在弱网/鉴权排队时可达 15-20s，30s 兜底)
        for i in 1..=100 {
            tokio::time::sleep(Duration::from_millis(300)).await;
            let health = probe_server(&health_url).await;
            if health.healthy
                && health.service.as_deref() == Some("ntrend-tq-bridge")
                && health.pid == Some(child_pid)
            {
                tracing::info!("✅ 天勤 Python 桥接服务成功启动并在 127.0.0.1:{} 就绪", port);
                return Ok(());
            }
            if i % 5 == 0 {
                tracing::info!("等待天勤桥接服务建立 WebSocket 连接... ({}/100)", i);
            }
        }

        Self::stop();
        Err(anyhow!("天勤桥接服务未能在 30 秒内就绪，已终止失败的子进程"))
    }

    /// 原子化重启桥接服务（先安全终止旧进程，再根据新配置拉起）
    pub async fn restart(config: &DataSourceConfig) -> Result<()> {
        Self::stop();
        tokio::time::sleep(Duration::from_millis(300)).await;
        Self::start_with_policy(config, false).await
    }

    /// 停止子进程
    pub fn stop() {
        let mut lock = SIDECAR_CHILD.lock().unwrap();
        if let Some(mut child) = lock.take() {
            tracing::info!("正在停止天勤 Python 桥接服务子进程...");
            let _ = child.kill();
            let _ = child.wait();
            tracing::info!("天勤 Python 桥接服务已关闭");
        }
        let mut active = ACTIVE_CONFIG.lock().unwrap();
        *active = None;
    }
}

#[derive(Debug, Default, Deserialize)]
struct HealthBody {
    service: Option<String>,
    pid: Option<u32>,
    status: Option<String>,
    reason: Option<String>,
}

#[derive(Debug, Default)]
struct HealthProbe {
    reachable: bool,
    healthy: bool,
    service: Option<String>,
    pid: Option<u32>,
    reason: Option<String>,
}

async fn probe_server(url: &str) -> HealthProbe {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_millis(600))
        .build()
        .unwrap_or_default();
    match client.get(url).send().await {
        Ok(resp) => {
            let success = resp.status().is_success();
            match resp.json::<HealthBody>().await {
                Ok(body) => HealthProbe {
                    reachable: true,
                    healthy: success && body.status.as_deref() == Some("ok"),
                    service: body.service,
                    pid: body.pid,
                    reason: body.reason,
                },
                Err(e) => HealthProbe {
                    reachable: true,
                    reason: Some(format!("健康响应无法解析: {e}")),
                    ..HealthProbe::default()
                },
            }
        }
        Err(_) => HealthProbe::default(),
    }
}

fn find_python_executable(custom: Option<&str>) -> Result<PathBuf> {
    if let Some(p) = custom {
        let path = PathBuf::from(p);
        if path.exists() {
            return Ok(path);
        }
    }

    // 搜索系统常见路径
    let candidates = [
        r"C:\Users\Xbss\AppData\Local\Python\bin\python.exe",
        r"C:\Users\Xbss\AppData\Local\Python\pythoncore-3.14-64\python.exe",
        "python.exe",
        "python",
        "py.exe",
        "py",
    ];

    for c in candidates {
        let p = PathBuf::from(c);
        if p.exists() {
            return Ok(p);
        }
        if Command::new(c).arg("--version").output().is_ok() {
            return Ok(PathBuf::from(c));
        }
    }

    Ok(PathBuf::from("python"))
}

fn find_bridge_script() -> Result<PathBuf> {
    let candidates = [
        PathBuf::from("python-bridge/tq_bridge.py"),
        PathBuf::from("../python-bridge/tq_bridge.py"),
        PathBuf::from("../../python-bridge/tq_bridge.py"),
    ];

    for c in &candidates {
        if c.exists() {
            return Ok(std::fs::canonicalize(c).unwrap_or_else(|_| c.clone()));
        }
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let p1 = dir.join("python-bridge").join("tq_bridge.py");
            if p1.exists() {
                return Ok(p1);
            }
            let p2 = dir.join("tq_bridge.py");
            if p2.exists() {
                return Ok(p2);
            }
        }
    }

    Ok(PathBuf::from("python-bridge/tq_bridge.py"))
}
