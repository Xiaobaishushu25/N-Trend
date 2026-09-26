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
    let _guard = BACKUP_LOCK.lock().await;
    std::fs::create_dir_all(temp_dir).context("创建快照临时目录失败")?;

    let unique_id = uuid::Uuid::new_v4().to_string();
    let raw_temp_path = temp_dir.join(format!("ntrend-snapshot-{unique_id}.sqlite"));
    let gz_temp_path = temp_dir.join(format!("ntrend-snapshot-{unique_id}.sqlite.gz"));

    let raw_path_str = raw_temp_path.to_string_lossy().replace('\\', "/");
    let vacuum_sql = format!("VACUUM INTO '{raw_path_str}'");

    tracing::info!("📦 开始执行 SQLite VACUUM INTO 生成数据库快照: {}", raw_path_str);
    db.execute_unprepared(&vacuum_sql)
        .await
        .map_err(|e| anyhow!("VACUUM INTO 执行失败: {e}"))?;

    // 校验生成的快照一致性
    {
        tracing::info!("🔍 对生成的 SQLite 快照执行完整性校验 (PRAGMA integrity_check)...");
        let snapshot_db = crate::storage::connect(&raw_temp_path).await?;
        let check_result = snapshot_db
            .query_one(Statement::from_string(
                DbBackend::Sqlite,
                "PRAGMA integrity_check;".to_string(),
            ))
            .await
            .context("执行 integrity_check 失败")?;

        let is_ok = check_result
            .and_then(|row| row.try_get_by_index::<String>(0).ok())
            .map(|val| val == "ok")
            .unwrap_or(false);

        if !is_ok {
            let _ = std::fs::remove_file(&raw_temp_path);
            return Err(anyhow!("SQLite 快照完整性校验未通过！"));
        }
        tracing::info!("✓ 快照完整性校验通过 (ok)");
    }

    // 压缩为 gzip 并计算 SHA-256
    tracing::info!("🗜️ 正在压缩快照为 gzip 格式: {}", gz_temp_path.display());
    {
        let mut raw_file = File::open(&raw_temp_path).context("读取未压缩快照失败")?;
        let gz_file = File::create(&gz_temp_path).context("创建 gzip 文件失败")?;
        let mut encoder = GzEncoder::new(gz_file, Compression::default());
        let mut buffer = [0u8; 64 * 1024];

        loop {
            let bytes_read = raw_file.read(&mut buffer)?;
            if bytes_read == 0 {
                break;
            }
            encoder.write_all(&buffer[..bytes_read])?;
        }
        encoder.finish().context("gzip 压缩完成写入失败")?;
    }

    // 删除未压缩的临时 sqlite 文件
    let _ = std::fs::remove_file(&raw_temp_path);

    // 计算 gzip 文件大小与 SHA-256 哈希
    let mut gz_read = File::open(&gz_temp_path).context("读取 gzip 快照计算哈希失败")?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 64 * 1024];
    let mut total_size = 0u64;

    loop {
        let n = gz_read.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        total_size += n as u64;
    }

    let sha256_hex = format!("{:x}", hasher.finalize());
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
