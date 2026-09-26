use anyhow::{anyhow, Result};
use std::fs::File;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

pub struct ProcessLock {
    path: PathBuf,
}

impl ProcessLock {
    pub fn acquire(data_dir: &Path) -> Result<Self> {
        let lock_path = data_dir.join("ntrend.lock");
        let my_pid = std::process::id();

        if lock_path.exists() {
            if let Ok(mut f) = File::open(&lock_path) {
                let mut content = String::new();
                if f.read_to_string(&mut content).is_ok() {
                    if let Ok(prev_pid) = content.trim().parse::<u32>() {
                        if prev_pid != my_pid && is_process_running(prev_pid) {
                            return Err(anyhow!(
                                "ntrend 服务端已有实例在运行中 (PID: {})！\n若确认无其他实例运行，请手动删除 {}",
                                prev_pid,
                                lock_path.display()
                            ));
                        }
                    }
                }
            }
        }

        let mut f = File::create(&lock_path)?;
        write!(f, "{}", my_pid)?;
        f.sync_all().ok();

        tracing::info!("🔒 已成功获取进程排他锁 (PID: {})", my_pid);

        Ok(Self { path: lock_path })
    }
}

impl Drop for ProcessLock {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
        tracing::info!("🔓 进程排他锁已释放");
    }
}

#[cfg(windows)]
fn is_process_running(pid: u32) -> bool {
    use std::process::Command;
    // 使用 tasklist 快速检测 pid 是否存在
    if let Ok(output) = Command::new("tasklist")
        .args(["/FI", &format!("PID eq {}", pid), "/NH"])
        .output()
    {
        let out_str = String::from_utf8_lossy(&output.stdout);
        out_str.contains(&pid.to_string())
    } else {
        false
    }
}

#[cfg(not(windows))]
fn is_process_running(pid: u32) -> bool {
    // Unix: kill(pid, 0) == 0
    let res = unsafe { libc::kill(pid as libc::pid_t, 0) };
    res == 0
}
