//! Real child processes with a standard-library fake HTTP bridge, never CTP.
use super::*;
use std::path::Path;
use tokio::io::AsyncWriteExt;

struct Fixture {
    root: PathBuf,
    launch: Launch,
}

impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("ntrend-ctp-child-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        let script = root.join("fake.py");
        std::fs::write(
            &script,
            r#"
import argparse, http.server, json, os, pathlib
p = argparse.ArgumentParser()
p.add_argument('--port', type=int)
p.add_argument('--parent-pid', type=int)
a = p.parse_args()
with pathlib.Path(__file__).with_suffix('.starts').open('a') as f:
    f.write(str(os.getpid()) + ' ' + str(a.parent_pid) + '\n')
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/crash':
            os._exit(7)
        data = dict(service='ntrend-ctp-trader', pid=os.getpid(), uptime=1.0,
                    healthy=False, ready=False, connected=False, authenticated=False,
                    logged_in=False, trading_day=None, broker_id=None, user_id=None)
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(('127.0.0.1', a.port), Handler).serve_forever()
"#,
        )
        .unwrap();
        let port = std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        Self {
            root,
            launch: Launch {
                python: find_python(None)
                    .expect("Python is required for fake bridge lifecycle tests"),
                script,
                port,
                poll: Duration::from_millis(60),
                probe_timeout: Duration::from_millis(150),
            },
        }
    }

    fn starts(&self) -> Vec<(u32, u32)> {
        std::fs::read_to_string(self.launch.script.with_extension("starts"))
            .unwrap_or_default()
            .lines()
            .filter_map(|line| {
                let mut values = line.split_whitespace();
                Some((values.next()?.parse().ok()?, values.next()?.parse().ok()?))
            })
            .collect()
    }

    async fn running(&self, minimum_starts: usize) -> u32 {
        let client = CtpTraderClient::for_port(self.launch.port);
        let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
        loop {
            if let Listener::Bridge { pid, .. } = listener(&self.launch, &client).await {
                if self.starts().len() >= minimum_starts && self.starts().last().unwrap().0 == pid {
                    return pid;
                }
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "fake bridge did not become available"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    async fn stopped(&self) {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        while tokio::net::TcpStream::connect(("127.0.0.1", self.launch.port))
            .await
            .is_ok()
        {
            assert!(
                tokio::time::Instant::now() < deadline,
                "owned fake bridge was orphaned"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

#[tokio::test]
async fn starts_once_while_counter_offline_recovers_child_crash_and_stops_with_backend() {
    let fixture = Fixture::new();
    let supervisor = CtpSidecar::launch(fixture.launch.clone());
    let first = fixture.running(1).await;
    assert_eq!(fixture.starts()[0].1, std::process::id());
    tokio::time::sleep(Duration::from_millis(250)).await;
    assert_eq!(
        fixture.starts().len(),
        1,
        "ready=false must not cause a login/restart loop"
    );
    let mut socket = tokio::net::TcpStream::connect(("127.0.0.1", fixture.launch.port))
        .await
        .unwrap();
    socket
        .write_all(b"GET /crash HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .await
        .unwrap();
    let second = fixture.running(2).await;
    assert_ne!(first, second);
    supervisor.stop().await;
    fixture.stopped().await;
    assert_eq!(fixture.starts().len(), 2);
}

#[tokio::test]
async fn existing_bridge_is_reused_and_never_stopped_by_a_non_owner() {
    let fixture = Fixture::new();
    let mut external = Command::new(&fixture.launch.python)
        .arg(&fixture.launch.script)
        .args(["--port", &fixture.launch.port.to_string()])
        .args(["--parent-pid", &std::process::id().to_string()])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let pid = fixture.running(1).await;
    let supervisor = CtpSidecar::launch(fixture.launch.clone());
    tokio::time::sleep(Duration::from_millis(250)).await;
    supervisor.stop().await;
    assert_eq!(fixture.running(1).await, pid);
    assert_eq!(fixture.starts().len(), 1);
    assert!(external.try_wait().unwrap().is_none());
    external.kill().await.unwrap();
    fixture.stopped().await;
}

#[tokio::test]
async fn unrelated_listener_prevents_a_second_trader() {
    let fixture = Fixture::new();
    let occupied = tokio::net::TcpListener::bind(("127.0.0.1", fixture.launch.port))
        .await
        .unwrap();
    let supervisor = CtpSidecar::launch(fixture.launch.clone());
    tokio::time::sleep(Duration::from_millis(400)).await;
    supervisor.stop().await;
    assert!(fixture.starts().is_empty());
    assert!(
        tokio::net::TcpStream::connect(occupied.local_addr().unwrap())
            .await
            .is_ok()
    );
}

#[tokio::test]
async fn dropping_startup_guard_also_cleans_up_an_owned_child() {
    let fixture = Fixture::new();
    let supervisor = CtpSidecar::launch(fixture.launch.clone());
    fixture.running(1).await;
    drop(supervisor);
    fixture.stopped().await;
}

#[test]
fn deployed_script_is_discoverable() {
    assert!(Path::new(&find_script().unwrap()).is_file());
}
