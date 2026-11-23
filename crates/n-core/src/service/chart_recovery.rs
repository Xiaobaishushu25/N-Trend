use super::*;
use n_protocol::dto::ChartHistoryUpdate;
use tokio::sync::{broadcast, Semaphore};

pub(super) struct ChartRecovery {
    last_started: Mutex<HashMap<String, Instant>>,
    running: Arc<Semaphore>,
    events: broadcast::Sender<ChartHistoryUpdate>,
}

impl Default for ChartRecovery {
    fn default() -> Self {
        Self {
            last_started: Mutex::default(),
            running: Arc::new(Semaphore::new(1)),
            events: broadcast::channel(32).0,
        }
    }
}

impl Services {
    pub fn subscribe_chart_recovery(&self) -> broadcast::Receiver<ChartHistoryUpdate> {
        self.chart_recovery.events.subscribe()
    }

    pub(super) async fn request_chart_recovery(&self, symbol: &str) {
        let mut attempts = self.chart_recovery.last_started.lock().await;
        let now = Instant::now();
        let cooldown = Duration::from_secs(15);
        if attempts
            .get(symbol)
            .is_some_and(|last| now.duration_since(*last) < cooldown)
        {
            return;
        }
        let Ok(permit) = self.chart_recovery.running.clone().try_acquire_owned() else {
            return;
        };
        attempts.retain(|_, time| now.duration_since(*time) < cooldown);
        attempts.insert(symbol.to_owned(), now);

        let symbol = symbol.to_owned();
        let db = self.db.clone();
        let pipeline = self.pipeline.clone();
        let source = self.data_source.clone();
        let fallback = self.chart_fallback_cache.clone();
        let publisher = self.chart_recovery.events.clone();
        tokio::spawn(async move {
            let _permit = permit;
            let work = async {
                let before = market_history_revision(&db, &symbol).await?;
                recover_gaps(&db, &pipeline, &source, &symbol).await?;
                let after = market_history_revision(&db, &symbol).await?;
                Ok::<_, anyhow::Error>((before, after))
            };
            match tokio::time::timeout(Duration::from_secs(18), work).await {
                Ok(Ok((previous_revision, series_revision))) => {
                    let mut cache = fallback.lock().await;
                    if cache
                        .get(&symbol)
                        .is_some_and(|entry| entry.fetched_at <= now)
                    {
                        cache.remove(&symbol);
                    }
                    let _ = publisher.send(ChartHistoryUpdate {
                        symbol,
                        previous_revision,
                        series_revision,
                        full_reload_required: true,
                        bars: vec![],
                    });
                }
                Ok(Err(error)) => tracing::warn!(symbol, %error, "后台图表补库失败"),
                Err(_) => tracing::warn!(symbol, "后台图表补库超时"),
            }
        });
    }
}

async fn recover_gaps(
    db: &DatabaseConnection,
    pipeline: &RawPipeline,
    source: &HybridDataSource,
    symbol: &str,
) -> Result<()> {
    let count = if let Some(latest_ts) = repo::latest_raw_ts(db, symbol).await? {
        let now = Local::now().format("%Y-%m-%d %H:%M:%S").to_string();
        let minutes = ts_gap_minutes(&now, &latest_ts).unwrap_or(0).max(0) as usize;
        (minutes / 5 + 20).clamp(20, 300)
    } else {
        100
    };
    let rows = source.fetch_tq_minute(symbol, "5", count).await?;
    anyhow::ensure!(!rows.is_empty(), "天勤恢复同步未返回已定版K线");
    pipeline.process_missing_raw_batch(symbol, &rows).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tokio::sync::oneshot;

    #[tokio::test]
    async fn local_chart_returns_while_one_recovery_fills_only_missing_bars() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let mut config = Config::default();
        config.data_source.primary_source = "offline".into();
        config.data_source.bridge_port = listener.local_addr().unwrap().port();
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        let services = Services::new(db.clone(), config, "test-chart-recovery.json".into())
            .await
            .unwrap();
        services.config.write().await.data_source.primary_source = "tqsdk".into();
        let row = |minute, price| Kline {
            datetime: format!("2026-01-05 09:{minute:02}:00"),
            open: price,
            high: price,
            low: price,
            close: price,
            volume: 1.,
            hold: 1.,
        };
        repo::upsert_klines(
            &db,
            vec![fetch_to_model("RB0", "5m", "raw", &row(35, 100.))],
        )
        .await
        .unwrap();
        services.chart_fallback_cache.lock().await.insert(
            "RB0".into(),
            ChartFallbackCache {
                bars_5m: Vec::new(),
                status: String::new(),
                message: String::new(),
                fetched_at: Instant::now(),
            },
        );
        let (requested, request_seen) = oneshot::channel();
        let (release, unblock) = oneshot::channel();
        let mock = tokio::spawn(async move {
            let bodies = [
                serde_json::json!({"status":"ok","tq_connected":true,"worker_alive":true}),
                serde_json::json!({"klines":[row(35, 999.), row(40,105.), row(45,106.)]}),
            ];
            for (index, body) in bodies.into_iter().enumerate() {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut request = Vec::new();
                while !request.ends_with(b"\r\n\r\n") {
                    request.push(socket.read_u8().await.unwrap());
                }
                if index == 0 {
                    assert!(request.starts_with(b"GET /health "));
                } else {
                    assert!(request.starts_with(b"GET /api/kline?"));
                }
                if index == 1 {
                    requested.send(()).unwrap();
                    unblock.await.unwrap();
                    let body = body.to_string();
                    socket.write_all(format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).as_bytes()).await.unwrap();
                    return listener;
                }
                let body = body.to_string();
                socket.write_all(format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).as_bytes()).await.unwrap();
            }
            unreachable!()
        });
        assert!(services.data_source.probe_and_activate("test", false).await);
        let mut updates = services.subscribe_chart_recovery();
        let graph = || services.get_chart_klines("RB0", "5m", Some(12), "recovery-test");
        let first = tokio::time::timeout(Duration::from_secs(2), graph())
            .await
            .unwrap()
            .unwrap();
        request_seen.await.unwrap();
        let second = tokio::time::timeout(Duration::from_secs(2), graph())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(first.status, "tqsdk_recovery_pending");
        assert_eq!(second.rows[0].close, 100.);
        release.send(()).unwrap();
        let event = tokio::time::timeout(Duration::from_secs(2), updates.recv())
            .await
            .unwrap()
            .unwrap();
        assert!(event.full_reload_required);
        assert!(event.series_revision > event.previous_revision);
        let recovered = graph().await.unwrap();
        assert_eq!(recovered.status, "tqsdk");
        assert_eq!(
            recovered
                .rows
                .iter()
                .map(|row| row.close)
                .collect::<Vec<_>>(),
            vec![100., 105.]
        );
        let listener = mock.await.unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(50), listener.accept())
                .await
                .is_err()
        );
    }
}
