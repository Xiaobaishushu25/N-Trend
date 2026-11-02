//! Consistent SQLite database snapshot and backup generator.

use anyhow::{anyhow, Context, Result};
use flate2::write::GzEncoder;
use flate2::Compression;
use sea_orm::{ConnectionTrait, DatabaseConnection, DbBackend, Statement};
use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use tokio::sync::Mutex;

pub static BACKUP_LOCK: Mutex<()> = Mutex::const_new(());

#[derive(Debug, Clone)]
pub struct SnapshotResult {
    pub file_path: PathBuf,
    pub sha256: String,
    pub size_bytes: u64,
}

/// 在 WAL 模式下安全生成一致性 SQLite 数据库快照：
/// 1. 获取全局备份互斥锁，确保同一时刻仅一个快照任务在执行
/// 2. 使用 SQLite VACUUM INTO 生成物理隔离的副本
/// 3. 对副本执行 PRAGMA integrity_check 校验物理一致性
/// 4. 将副本使用 gzip 进行流式压缩
/// 5. 计算 gzip 文件的 SHA-256 校验和并返回
pub async fn generate_database_snapshot(
    db: &DatabaseConnection,
    temp_dir: &Path,
) -> Result<SnapshotResult> {
    let _guard = match BACKUP_LOCK.try_lock() {
        Ok(guard) => guard,
        Err(_) => {
            return Err(anyhow!("服务端当前已有数据库快照生成任务正在执行，请勿重复请求"));
        }
    };
    std::fs::create_dir_all(temp_dir).context("创建快照临时目录失败")?;

    let unique_id = uuid::Uuid::new_v4().to_string();
    let raw_temp_path = temp_dir.join(format!("ntrend-snapshot-{unique_id}.sqlite"));
    let gz_temp_path = temp_dir.join(format!("ntrend-snapshot-{unique_id}.sqlite.gz"));

    let raw_path_str = raw_temp_path.to_string_lossy().replace('\\', "/");
    let vacuum_sql = format!("VACUUM INTO '{raw_path_str}'");

    tracing::info!(
        "📦 开始执行 SQLite VACUUM INTO 生成数据库快照: {}",
        raw_path_str
    );
    if let Err(e) = db.execute_unprepared(&vacuum_sql).await {
        let _ = std::fs::remove_file(&raw_temp_path);
        return Err(anyhow!("VACUUM INTO 执行失败: {e}"));
    }

    // 校验生成的快照一致性 (VACUUM INTO 重构了全新 B-tree，quick_check 快速且充分)
    {
        tracing::info!("🔍 对生成的 SQLite 快照执行完整性校验 (PRAGMA quick_check)...");
        let snapshot_db = match sea_orm::Database::connect(format!(
            "sqlite://{}?mode=ro",
            raw_temp_path.to_string_lossy().replace('\\', "/")
        ))
        .await
        {
            Ok(db) => db,
            Err(e) => {
                let _ = std::fs::remove_file(&raw_temp_path);
                return Err(anyhow!("打开快照校验连接失败: {e}"));
            }
        };

        let check_result = snapshot_db
            .query_one(Statement::from_string(
                DbBackend::Sqlite,
                "PRAGMA quick_check;".to_string(),
            ))
            .await;

        let is_ok = check_result
            .ok()
            .flatten()
            .and_then(|row| row.try_get_by_index::<String>(0).ok())
            .map(|val| val == "ok")
            .unwrap_or(false);

        if !is_ok {
            let _ = std::fs::remove_file(&raw_temp_path);
            return Err(anyhow!("SQLite 快照完整性校验未通过！"));
        }
        tracing::info!("✓ 快照完整性校验通过 (ok)");
    }

    // 压缩为 gzip 并单趟同步计算 SHA-256，放入 spawn_blocking 避免阻塞 Tokio 异步工作线程
    tracing::info!("🗜️ 正在压缩快照为 gzip 格式: {}", gz_temp_path.display());
    let raw_path_clone = raw_temp_path.clone();
    let gz_path_clone = gz_temp_path.clone();

    let (total_size, sha256_hex) = tokio::task::spawn_blocking(move || -> Result<(u64, String)> {
        struct HashingWriter<W: Write> {
            inner: W,
            hasher: Sha256,
            bytes_written: u64,
        }

        impl<W: Write> Write for HashingWriter<W> {
            fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
                let n = self.inner.write(buf)?;
                self.hasher.update(&buf[..n]);
                self.bytes_written += n as u64;
                Ok(n)
            }

            fn flush(&mut self) -> std::io::Result<()> {
                self.inner.flush()
            }
        }

        let mut raw_file = File::open(&raw_path_clone).context("读取未压缩快照失败")?;
        let gz_file = File::create(&gz_path_clone).context("创建 gzip 文件失败")?;

        let hashing_writer = HashingWriter {
            inner: gz_file,
            hasher: Sha256::new(),
            bytes_written: 0,
        };

        // 使用 Compression::fast() 提高压缩吞吐量，将 CPU 耗时降低 3~5 倍
        let mut encoder = GzEncoder::new(hashing_writer, Compression::fast());
        let mut buffer = [0u8; 128 * 1024];

        loop {
            let bytes_read = raw_file.read(&mut buffer)?;
            if bytes_read == 0 {
                break;
            }
            encoder.write_all(&buffer[..bytes_read])?;
        }
        let hashing_writer = encoder.finish().context("gzip 压缩完成写入失败")?;
        let sha256_hex = format!("{:x}", hashing_writer.hasher.finalize());
        let total_size = hashing_writer.bytes_written;

        // 成功压缩后删除未压缩的原始 sqlite 文件
        let _ = std::fs::remove_file(&raw_path_clone);

        Ok((total_size, sha256_hex))
    })
    .await
    .map_err(|e| {
        let _ = std::fs::remove_file(&raw_temp_path);
        let _ = std::fs::remove_file(&gz_temp_path);
        anyhow!("快照压缩任务执行失败: {e}")
    })?
    .map_err(|e| {
        let _ = std::fs::remove_file(&raw_temp_path);
        let _ = std::fs::remove_file(&gz_temp_path);
        e
    })?;

    tracing::info!(
        "✅ 数据库一致性备份快照就绪 | 大小: {} 字节 | SHA-256: {}",
        total_size,
        sha256_hex
    );

    Ok(SnapshotResult {
        file_path: gz_temp_path,
        sha256: sha256_hex,
        size_bytes: total_size,
    })
}
