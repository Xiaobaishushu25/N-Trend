//! The backend owns its CTP child; a pre-existing bridge is reused, never killed.
use anyhow::{Context, Result};
use n_core::trader::CtpTraderClient;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncRead, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

const SERVICE: &str = "ntrend-ctp-trader";

pub struct CtpSidecar {
    stop: Option<oneshot::Sender<()>>,
    task: Option<JoinHandle<()>>,
    reconnect: mpsc::Sender<(Launch, oneshot::Sender<Result<()>>)>,
}

#[derive(Clone)]
struct Launch {
    python: PathBuf,
    script: PathBuf,
    port: u16,
    config: Option<PathBuf>,
    flow_dir: Option<PathBuf>,
    identity: Option<(String, String)>,
    poll: Duration,
    probe_timeout: Duration,
}

impl CtpSidecar {
    pub fn start(custom_python: Option<&str>) -> Result<Self> {
        let script = find_script().context("找不到 CTP 桥接脚本")?;
        let python = find_python(custom_python).context("找不到 CTP Python 解释器")?;
        Ok(Self::launch(Launch {
            python,
            script,
            port: 8766,
            config: None,
            flow_dir: None,
            identity: None,
            poll: Duration::from_secs(5),
            probe_timeout: Duration::from_secs(1),
        }))
    }

    pub fn start_account(
        custom_python: Option<&str>,
        port: u16,
        config: PathBuf,
        flow_dir: PathBuf,
        broker: String,
        user: String,
    ) -> Result<Self> {
        Ok(Self::launch(Self::account_launch(
            custom_python,
            port,
            config,
            flow_dir,
            broker,
            user,
        )?))
    }

    fn account_launch(
        custom_python: Option<&str>,
        port: u16,
        config: PathBuf,
        flow_dir: PathBuf,
        broker: String,
        user: String,
    ) -> Result<Launch> {
        let value: serde_json::Value = serde_json::from_slice(&std::fs::read(&config)?)?;
        anyhow::ensure!(
            value["BrokerID"].as_str() == Some(&broker) && value["UserID"].as_str() == Some(&user),
            "CTP凭据身份不匹配"
        );
        Ok(Launch {
            python: find_python(custom_python)?,
            script: find_script()?,
            port,
            config: Some(config),
            flow_dir: Some(flow_dir),
            identity: Some((broker, user)),
            poll: Duration::from_secs(5),
            probe_timeout: Duration::from_secs(1),
        })
    }

    pub async fn reconnect_account(
        &self,
        custom_python: Option<&str>,
        port: u16,
        config: PathBuf,
        flow_dir: PathBuf,
        broker: String,
        user: String,
    ) -> Result<()> {
        let launch = Self::account_launch(custom_python, port, config, flow_dir, broker, user)?;
        let (tx, rx) = oneshot::channel();
        self.reconnect
            .send((launch, tx))
            .await
            .context("CTP连接管理任务已停止")?;
        rx.await.context("CTP重连任务已停止")?
    }

    fn launch(launch: Launch) -> Self {
        let (stop, receiver) = oneshot::channel();
        let (reconnect, requests) = mpsc::channel(4);
        Self {
            stop: Some(stop),
            reconnect,
            task: Some(tokio::spawn(supervise(launch, receiver, requests))),
        }
    }

    pub async fn stop(mut self) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        if let Some(mut task) = self.task.take() {
            if tokio::time::timeout(Duration::from_secs(6), &mut task)
                .await
                .is_err()
            {
                task.abort(); // kill_on_drop also covers cancellation during a probe.
                let _ = task.await;
            }
        }
    }
}

impl Drop for CtpSidecar {
    fn drop(&mut self) {
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}

#[derive(Debug, PartialEq)]
enum Listener {
    Bridge { pid: u32, ready: bool },
    Occupied,
    Free,
}

async fn listener(launch: &Launch, client: &CtpTraderClient) -> Listener {
    if let Ok(Ok(health)) = tokio::time::timeout(launch.probe_timeout, client.health()).await {
        if launch.identity.as_ref().is_some_and(|(b, u)| {
            health.broker_id.as_ref() != Some(b) || health.user_id.as_ref() != Some(u)
        }) {
            return Listener::Occupied;
        }
        if health.service == SERVICE {
            if let Some(pid) = health.pid.filter(|pid| *pid > 0) {
                return Listener::Bridge {
                    pid,
                    ready: health.ready,
                };
            }
        }
    }
    match std::net::TcpListener::bind(("127.0.0.1", launch.port)) {
        Ok(probe) => {
            drop(probe);
            Listener::Free
        }
        // A busy/unresponsive or unknown listener must never trigger a second trader.
        _ => Listener::Occupied,
    }
}

async fn supervise(
    mut launch: Launch,
    mut stop: oneshot::Receiver<()>,
    mut requests: mpsc::Receiver<(Launch, oneshot::Sender<Result<()>>)>,
) {
    let client = CtpTraderClient::for_port(launch.port);
    let mut child: Option<Child> = None;
    let mut last = None;
    let mut interval = tokio::time::interval(launch.poll);
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            biased;
            _ = &mut stop => break,
            Some((next, reply)) = requests.recv() => {
                let state = listener(&launch, &client).await;
                let owned_pid = child.as_ref().and_then(Child::id);
                let refused = next.port != launch.port || match state {
                    Listener::Bridge { pid, .. } => owned_pid != Some(pid),
                    Listener::Occupied => true,
                    Listener::Free => false,
                };
                if refused {
                    let _ = reply.send(Err(anyhow::anyhow!("该端口由外部或未确认的桥接占用，请先停止外部桥接后重试；其他账户不受影响")));
                    continue;
                }
                if let Some(mut owned) = child.take() { stop_owned(&mut owned).await; }
                launch = next;
                last = None;
                interval.reset_immediately();
                let _ = reply.send(Ok(()));
                continue;
            },
            _ = interval.tick() => {},
        }
        if let Some(owned) = child.as_mut() {
            match owned.try_wait() {
                Ok(Some(status)) => {
                    tracing::warn!("CTP 子进程已退出 ({status})，重新检查端口并尝试恢复");
                    child = None;
                    last = None;
                }
                Ok(None) => {}
                Err(error) => {
                    tracing::warn!("检查 CTP 子进程失败: {error}，保留当前进程等待下次检查");
                    continue;
                }
            }
        }
        let state = listener(&launch, &client).await;
        if last.as_ref() != Some(&state) {
            match &state {
                Listener::Bridge { pid, ready } => {
                    if child.as_ref().and_then(Child::id) == Some(*pid) {
                        tracing::info!("CTP 子进程运行中: pid={pid}, ready={ready}");
                    } else {
                        tracing::info!("复用现有 CTP 桥接: pid={pid}, ready={ready}；不接管其退出");
                    }
                }
                Listener::Occupied => tracing::warn!(
                    "CTP 端口 {} 存在未确认的监听服务，暂不启动第二个交易进程",
                    launch.port
                ),
                Listener::Free => {}
            }
        }
        if state == Listener::Free && child.is_none() {
            let mut command = Command::new(&launch.python);
            command
                .arg(&launch.script)
                .args(["--port", &launch.port.to_string()])
                .args(["--parent-pid", &std::process::id().to_string()])
                .env("PYTHONUNBUFFERED", "1")
                .env("PYTHONIOENCODING", "utf-8")
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .kill_on_drop(true);
            if let Some(config) = &launch.config {
                command.arg("--config").arg(config);
            }
            if let Some(flow) = &launch.flow_dir {
                command.arg("--flow-dir").arg(flow);
            }
            #[cfg(windows)]
            command.creation_flags(0x08000000);
            match command.spawn() {
                Ok(mut spawned) => {
                    tracing::info!(
                        "后端已启动 CTP 桥接子进程: pid={:?}, port={}",
                        spawned.id(),
                        launch.port
                    );
                    if let Some(stdout) = spawned.stdout.take() {
                        forward(stdout);
                    }
                    if let Some(stderr) = spawned.stderr.take() {
                        forward(stderr);
                    }
                    child = Some(spawned);
                }
                Err(error) => tracing::warn!("启动 CTP 桥接失败，稍后重试: {error}"),
            }
        }
        last = Some(state);
    }
    if let Some(mut child) = child {
        stop_owned(&mut child).await;
    }
}

#[cfg(test)]
#[path = "ctp_sidecar_tests.rs"]
mod tests;

fn forward(reader: impl AsyncRead + Unpin + Send + 'static) {
    tokio::spawn(async move {
        let mut lines = BufReader::new(reader).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            if line.trim().is_empty() {
                continue;
            }
            let lower = line.to_lowercase();
            if lower.contains("password") || lower.contains("密码") {
                tracing::warn!("[CTP桥接] 认证相关输出已隐藏");
                continue;
            }
            let line = line.chars().take(2048).collect::<String>();
            if line.contains("[ERROR]") || line.contains("Traceback") {
                tracing::warn!("[CTP桥接] {line}");
            } else {
                tracing::info!("[CTP桥接] {line}");
            }
        }
    });
}

async fn stop_owned(child: &mut Child) {
    #[cfg(unix)]
    if let Some(pid) = child.id() {
        let _ = Command::new("kill")
            .args(["-INT", &pid.to_string()])
            .status()
            .await;
    }
    #[cfg(windows)]
    let _ = child.start_kill();
    if tokio::time::timeout(Duration::from_secs(3), child.wait())
        .await
        .is_err()
    {
        let _ = child.kill().await;
    }
    tracing::info!("后端拥有的 CTP 子进程已停止");
}

fn find_python(custom: Option<&str>) -> Result<PathBuf> {
    for program in custom.into_iter().chain(["python3", "python", "py"]) {
        let Ok(output) = std::process::Command::new(program)
            .args(["-c", "import sys; print(sys.executable)"])
            .env("PYTHONIOENCODING", "utf-8")
            .output()
        else {
            continue;
        };
        if output.status.success() {
            let executable = PathBuf::from(String::from_utf8(output.stdout)?.trim());
            if executable.is_file() {
                // Bypass Windows launchers so the owned PID is the Python PID.
                return Ok(executable);
            }
        }
    }
    anyhow::bail!("需要安装 Python 和 openctp_ctp，或配置数据源的 Python 路径")
}

fn find_script() -> Result<PathBuf> {
    let mut candidates = vec![
        PathBuf::from("python-bridge/ctp_trader.py"),
        PathBuf::from("../python-bridge/ctp_trader.py"),
        PathBuf::from("../../python-bridge/ctp_trader.py"),
    ];
    if let Some(directory) = std::env::current_exe()?.parent() {
        candidates.push(directory.join("python-bridge/ctp_trader.py"));
        candidates.push(directory.join("ctp_trader.py"));
    }
    for path in candidates {
        if path.is_file() {
            return Ok(path.canonicalize()?);
        }
    }
    anyhow::bail!("python-bridge/ctp_trader.py 不存在")
}
