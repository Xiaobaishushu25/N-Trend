//! Sidecar process lifecycle management for Python TqSdk bridge.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Read};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{anyhow, Result};
use serde::Deserialize;

use crate::config::DataSourceConfig;

static SIDECAR_CHILD: Mutex<Option<Child>> = Mutex::new(None);
static ACTIVE_CONFIG: Mutex<Option<DataSourceConfig>> = Mutex::new(None);

const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);
const STARTUP_POLL_INTERVAL: Duration = Duration::from_millis(300);
const STARTUP_PROGRESS_INTERVAL: Duration = Duration::from_secs(5);
const CHILD_LOG_HISTORY_LIMIT: usize = 40;

type SharedChildLogs = Arc<Mutex<VecDeque<String>>>;

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
            tracing::warn!(
                "⚠️ 快期/天勤账号或密码未配置，跳过拉起桥接服务。请在「设置 - 数据」中配置账号密码"
            );
            return Err(anyhow!(
                "快期/天勤账号或密码未配置，请在「设置 - 数据」中填写账号与密码"
            ));
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
                health
                    .pid
                    .map(|v| v.to_string())
                    .unwrap_or_else(|| "未知".into()),
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
            .env("TQ_PASSWORD", &config.tq_password)
            // 管道输出必须禁用 Python 缓冲，否则启动失败时日志可能一直留在子进程内。
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            // CREATE_NO_WINDOW = 0x08000000
            cmd.creation_flags(0x08000000);
        }

        let mut child = cmd
            .spawn()
            .map_err(|e| anyhow!("无法启动 Python 桥接服务子进程: {e}"))?;
        let child_pid = child.id();

        // Python logging 默认写 stderr，TqSdk 的连接提示也可能写 stdout；两路都转发
        // 到 tracing，确保桌面端日志能保留真正的鉴权/网络异常。
        let child_logs = Arc::new(Mutex::new(VecDeque::with_capacity(CHILD_LOG_HISTORY_LIMIT)));
        if let Some(stdout) = child.stdout.take() {
            spawn_output_forwarder(
                stdout,
                "stdout",
                config.tq_password.clone(),
                Arc::clone(&child_logs),
            );
        }
        if let Some(stderr) = child.stderr.take() {
            spawn_output_forwarder(
                stderr,
                "stderr",
                config.tq_password.clone(),
                Arc::clone(&child_logs),
            );
        }

        {
            let mut lock = SIDECAR_CHILD.lock().unwrap();
            *lock = Some(child);
            let mut active = ACTIVE_CONFIG.lock().unwrap();
            *active = Some(config.clone());
        }

        // 5. 轮询等待服务启动并就绪。使用真实截止时间，避免健康请求本身的耗时
        // 叠加后让“100 次轮询”与日志宣称的超时时间不一致。
        let started_at = tokio::time::Instant::now();
        let mut last_progress_at = Duration::ZERO;
        let mut last_health_reason = "桥接 HTTP 服务尚未响应".to_string();
        while started_at.elapsed() < STARTUP_TIMEOUT {
            tokio::time::sleep(STARTUP_POLL_INTERVAL).await;

            // TqApi 初始化若直接失败，Python 进程可能先退出；此时立即报告退出码及
            // 最近子进程日志，而不是继续无意义地等待完整个启动窗口。
            let child_status = {
                let mut lock = SIDECAR_CHILD.lock().unwrap();
                match lock.as_mut() {
                    Some(child) if child.id() == child_pid => child
                        .try_wait()
                        .map_err(|e| anyhow!("检查天勤桥接子进程状态失败: {e}"))?,
                    Some(child) => {
                        return Err(anyhow!(
                            "天勤桥接子进程所有权发生变化（期望 PID {child_pid}，当前 PID {}）",
                            child.id()
                        ));
                    }
                    None => {
                        return Err(anyhow!(
                            "天勤桥接子进程在启动期间已被停止（PID {child_pid}）"
                        ));
                    }
                }
            };
            if let Some(status) = child_status {
                // 给日志转发线程一个很短的机会读完已关闭管道中的尾部内容。
                tokio::time::sleep(Duration::from_millis(50)).await;
                Self::stop();
                let recent = recent_child_log_summary(&child_logs);
                return Err(anyhow!(
                    "天勤桥接子进程提前退出（PID {child_pid}，状态: {status}）。最近日志: {recent}"
                ));
            }

            let health = probe_server(&health_url).await;
            if health.healthy
                && health.service.as_deref() == Some("ntrend-tq-bridge")
                && health.pid == Some(child_pid)
            {
                tracing::info!(
                    "✅ 天勤 Python 桥接服务成功启动并在 127.0.0.1:{} 就绪",
                    port
                );
                return Ok(());
            }

            if health.fatal && health.pid == Some(child_pid) {
                let reason = health
                    .reason
                    .as_deref()
                    .unwrap_or("Python 桥接报告了不可恢复的启动错误")
                    .to_string();
                Self::stop();
                let recent = recent_child_log_summary(&child_logs);
                return Err(anyhow!(
                    "天勤桥接初始化失败（PID {child_pid}）: {reason}。最近日志: {recent}"
                ));
            }

            last_health_reason = health.reason.unwrap_or_else(|| {
                if health.reachable {
                    format!(
                        "健康检查未通过（service={}, pid={}）",
                        health.service.as_deref().unwrap_or("未知"),
                        health
                            .pid
                            .map(|pid| pid.to_string())
                            .unwrap_or_else(|| "未知".to_string())
                    )
                } else {
                    "桥接 HTTP 服务尚未响应".to_string()
                }
            });

            let elapsed = started_at.elapsed();
            if elapsed.saturating_sub(last_progress_at) >= STARTUP_PROGRESS_INTERVAL {
                tracing::info!(
                    "等待天勤桥接服务建立 WebSocket 连接... (已等待 {} 秒/最多 {} 秒，原因: {})",
                    elapsed.as_secs(),
                    STARTUP_TIMEOUT.as_secs(),
                    last_health_reason
                );
                last_progress_at = elapsed;
            }
        }

        Self::stop();
        let recent = recent_child_log_summary(&child_logs);
        Err(anyhow!(
            "天勤桥接服务未能在 {} 秒内就绪，已终止失败的子进程。最后健康状态: {}。最近日志: {}",
            STARTUP_TIMEOUT.as_secs(),
            last_health_reason,
            recent
        ))
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
    fatal: Option<bool>,
    reason: Option<String>,
}

#[derive(Debug, Default)]
struct HealthProbe {
    reachable: bool,
    healthy: bool,
    service: Option<String>,
    pid: Option<u32>,
    fatal: bool,
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
                    fatal: body.fatal.unwrap_or(false),
                    reason: body.reason,
                },
                Err(e) => HealthProbe {
                    reachable: true,
                    reason: Some(format!("健康响应无法解析: {e}")),
                    ..HealthProbe::default()
                },
            }
        }
        Err(e) => HealthProbe {
            reason: Some(format!("健康检查请求失败: {e}")),
            ..HealthProbe::default()
        },
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ChildLogLevel {
    Info,
    Warn,
    Error,
}

fn classify_child_log(line: &str) -> ChildLogLevel {
    let upper = line.to_ascii_uppercase();
    if upper.contains(" - ERROR - ")
        || upper.contains("[ERROR]")
        || upper.starts_with("ERROR")
        || upper.contains("TRACEBACK")
        || upper.contains("FAILED TO")
    {
        ChildLogLevel::Error
    } else if upper.contains(" - WARNING - ")
        || upper.contains("[WARNING]")
        || upper.contains("[WARN]")
        || upper.starts_with("WARNING")
        || upper.starts_with("WARN")
    {
        ChildLogLevel::Warn
    } else {
        ChildLogLevel::Info
    }
}

fn redact_child_log(line: &str, password: &str) -> String {
    if password.is_empty() {
        line.to_string()
    } else {
        line.replace(password, "***")
    }
}

fn spawn_output_forwarder<R>(
    reader: R,
    stream: &'static str,
    password: String,
    history: SharedChildLogs,
) where
    R: Read + Send + 'static,
{
    let thread_name = format!("tq-bridge-{stream}");
    let spawn_result = std::thread::Builder::new()
        .name(thread_name)
        .spawn(move || {
            for result in BufReader::new(reader).lines() {
                let line = match result {
                    Ok(line) => redact_child_log(&line, &password),
                    Err(error) => {
                        tracing::warn!("读取天勤桥接 {stream} 失败: {error}");
                        break;
                    }
                };
                if line.trim().is_empty() {
                    continue;
                }

                if let Ok(mut logs) = history.lock() {
                    if logs.len() == CHILD_LOG_HISTORY_LIMIT {
                        logs.pop_front();
                    }
                    logs.push_back(format!("{stream}: {line}"));
                }

                match classify_child_log(&line) {
                    ChildLogLevel::Info => tracing::info!("[天勤桥接/{stream}] {line}"),
                    ChildLogLevel::Warn => tracing::warn!("[天勤桥接/{stream}] {line}"),
                    ChildLogLevel::Error => tracing::error!("[天勤桥接/{stream}] {line}"),
                }
            }
        });

    if let Err(error) = spawn_result {
        tracing::warn!("无法启动天勤桥接 {stream} 日志转发线程: {error}");
    }
}

fn recent_child_log_summary(history: &SharedChildLogs) -> String {
    let Ok(logs) = history.lock() else {
        return "日志缓存不可用".to_string();
    };
    if logs.is_empty() {
        return "无子进程输出".to_string();
    }

    let mut recent = logs.iter().rev().take(5).cloned().collect::<Vec<_>>();
    recent.reverse();
    recent.join(" | ")
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

#[cfg(test)]
mod tests {
    use super::{classify_child_log, redact_child_log, ChildLogLevel};

    #[test]
    fn classifies_python_log_levels() {
        assert_eq!(
            classify_child_log("2026-09-07 - INFO - connected"),
            ChildLogLevel::Info
        );
        assert_eq!(
            classify_child_log("2026-09-07 - WARNING - retrying"),
            ChildLogLevel::Warn
        );
        assert_eq!(
            classify_child_log("2026-09-07 - ERROR - auth failed"),
            ChildLogLevel::Error
        );
        assert_eq!(
            classify_child_log("Failed to initialize TqApi: invalid account"),
            ChildLogLevel::Error
        );
    }

    #[test]
    fn redacts_password_from_child_output() {
        assert_eq!(
            redact_child_log("login failed for secret-123", "secret-123"),
            "login failed for ***"
        );
        assert_eq!(redact_child_log("ordinary output", ""), "ordinary output");
    }
}
