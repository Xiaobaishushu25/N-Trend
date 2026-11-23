use super::{atomic_json, ExecutionOrder, ExecutionOrdersPage, ExecutionState};
use anyhow::{ensure, Context, Result};
use rusqlite::{params, Connection, OpenFlags, OptionalExtension, Transaction};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::ops::Deref;
use std::path::Path;
use std::sync::{Arc, OnceLock};

const VERSION: u32 = 1;
const DB_NAME: &str = "trader_execution.sqlite3";

#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub(super) struct Orders {
    records: Arc<HashMap<String, ExecutionOrder>>,
    #[serde(skip)]
    dirty: HashSet<String>,
    #[serde(skip)]
    revision: u64,
    #[serde(skip)]
    pub(super) rejections: OnceLock<Arc<super::execution_safety::Rejections>>,
    #[serde(skip)]
    pub(super) progress: OnceLock<Arc<HashMap<String, super::OrderProgress>>>,
}

impl Deref for Orders {
    type Target = HashMap<String, ExecutionOrder>;

    fn deref(&self) -> &Self::Target {
        &self.records
    }
}

impl Orders {
    pub(super) fn revision(&self) -> u64 {
        self.revision
    }

    fn invalidate(&mut self) {
        self.revision = self.revision.wrapping_add(1);
        self.rejections.take();
        self.progress.take();
    }

    pub(super) fn dirty_references(&self) -> Vec<String> {
        self.dirty.iter().cloned().collect()
    }

    pub(super) fn mark_saved(&mut self) {
        self.dirty.clear();
    }

    pub(super) fn insert(&mut self, reference: String, order: ExecutionOrder) {
        self.invalidate();
        self.dirty.insert(reference.clone());
        Arc::make_mut(&mut self.records).insert(reference, order);
    }

    pub(super) fn get_mut(&mut self, reference: &str) -> Option<&mut ExecutionOrder> {
        if !self.records.contains_key(reference) {
            return None;
        }
        self.invalidate();
        self.dirty.insert(reference.to_owned());
        Arc::make_mut(&mut self.records).get_mut(reference)
    }

    #[cfg(test)]
    pub(super) fn values_mut(&mut self) -> impl Iterator<Item = &mut ExecutionOrder> {
        self.invalidate();
        self.dirty.extend(self.records.keys().cloned());
        Arc::make_mut(&mut self.records).values_mut()
    }
}

#[derive(Serialize, Deserialize)]
struct StoreMarker {
    execution_storage_version: u32,
    store_id: String,
    // Older executables must fail to load this file so they cannot trade with
    // an empty order map or silently resume from a stale migration backup.
    orders: (),
}

pub(super) struct ExecutionStore {
    connection: Connection,
    previous: ExecutionState,
}

// This is the durable subset of ExecutionState, excluding the separately
// indexed order table. Maps/sets and vectors use one row per record.
macro_rules! state_fields {
    ($apply:ident) => {
        $apply!(scalar: definition, active_config, paused, strategy_error,
            time_normalization_version, last_ref, account, trading_day, loss_r,
            comparison, p3_runner);
        $apply!(scalar_default: p3_stall);
        $apply!(map: strategy_states, strategy_bars, conditions, positions, closes,
            trade_times, entry_timeouts, last_bars, overnight_snapshots, entry_funding_waits,
            quote_sequences, entry_trigger_sequences, symbol_slots, symbol_release_after,
            entry_decisions);
        $apply!(set: seen_trades, aborted_entries, authorized_entries, recorded_checkpoints);
        $apply!(list: quote_gaps, history);
    };
}

impl ExecutionStore {
    pub(super) fn open(state_path: &Path) -> Result<(Self, ExecutionState)> {
        let source = match fs::read(state_path) {
            Ok(bytes) => Some(bytes),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => return Err(error.into()),
        };
        let marker = source.as_deref().map(read_marker).transpose()?.flatten();
        let db_path = state_path.with_file_name(DB_NAME);
        if let Some(marker) = marker {
            let connection =
                Connection::open_with_flags(&db_path, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
            configure(&connection)?;
            check_marker(&connection, &marker)?;
            return Self::load(connection);
        }

        let backup = state_path.with_file_name("trader_execution.legacy.json");
        ensure!(
            source.is_some() || !backup.exists(),
            "执行状态标记丢失，保留原始备份并停止迁移"
        );
        let expected: ExecutionState = source
            .as_deref()
            .map(serde_json::from_slice)
            .transpose()?
            .unwrap_or_default();
        let source_hash = source
            .as_deref()
            .map(|bytes| blake3::hash(bytes).to_hex().to_string());
        if let Some(bytes) = &source {
            if backup.exists() {
                ensure!(
                    fs::read(&backup)? == *bytes,
                    "执行状态迁移备份与原文件不一致"
                );
            } else {
                write_backup(&backup, bytes)?;
            }
        }
        let mut connection = Connection::open(&db_path)?;
        configure(&connection)?;
        let initialized: bool = connection.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE name='execution_meta' AND type='table')",
            [], |row| row.get(0),
        )?;
        let store_id = if initialized {
            let (version, id, imported, advanced) = metadata(&connection)?;
            ensure!(
                version == VERSION && imported == source_hash && !advanced,
                "执行状态数据库已独立更新或与原文件不一致，停止迁移"
            );
            id
        } else {
            let id = uuid::Uuid::new_v4().to_string();
            let tx = connection.transaction()?;
            tx.execute_batch(
                "CREATE TABLE execution_meta (id INTEGER PRIMARY KEY CHECK(id=1),
                    version INTEGER NOT NULL, store_id TEXT NOT NULL, source_hash TEXT,
                    advanced INTEGER NOT NULL);
                 CREATE TABLE execution_fields (field TEXT NOT NULL, key TEXT NOT NULL,
                    value BLOB NOT NULL, PRIMARY KEY(field,key)) WITHOUT ROWID;
                 CREATE TABLE execution_orders (reference TEXT PRIMARY KEY,
                    settled INTEGER NOT NULL, trading_day TEXT NOT NULL,
                    ref_number TEXT NOT NULL, wire_ref TEXT NOT NULL, value BLOB NOT NULL);
                 CREATE INDEX execution_orders_page ON execution_orders
                    (trading_day DESC, ref_number DESC, wire_ref DESC, reference DESC);
                 CREATE INDEX execution_orders_unsettled ON execution_orders(reference) WHERE settled=0;",
            )?;
            tx.execute(
                "INSERT INTO execution_meta VALUES(1,?1,?2,?3,0)",
                params![VERSION, id, source_hash],
            )?;
            write_fields(&tx, None, &expected)?;
            write_orders(&tx, &expected.orders, true)?;
            tx.commit()?;
            id
        };
        let (store, state) = Self::load(connection)?;
        verify_import(&expected, &state)?;
        atomic_json(
            state_path,
            &StoreMarker {
                execution_storage_version: VERSION,
                store_id,
                orders: (),
            },
        )?;
        sync_parent(state_path)?;
        Ok((store, state))
    }

    fn load(connection: Connection) -> Result<(Self, ExecutionState)> {
        // The comparison baseline contains no orders or transient state.
        let previous = read_fields(&connection)?;
        let mut state = previous.clone();
        read_orders(&connection, &mut state)?;
        Ok((
            Self {
                connection,
                previous,
            },
            state,
        ))
    }

    pub(super) fn save(&mut self, state: &ExecutionState) -> Result<()> {
        let tx = self.connection.transaction()?;
        let changed = write_fields(&tx, Some(&self.previous), state)?;
        let order_changed = write_orders(&tx, &state.orders, false)?;
        if changed || order_changed {
            tx.execute(
                "UPDATE execution_meta SET advanced=1 WHERE id=1 AND advanced=0",
                [],
            )?;
        }
        tx.commit()?;
        if changed {
            update_baseline(&mut self.previous, state);
        }
        Ok(())
    }

    pub(super) fn page(&self, page: usize, page_size: usize) -> Result<ExecutionOrdersPage> {
        let total =
            self.connection
                .query_row("SELECT COUNT(*) FROM execution_orders", [], |row| {
                    row.get(0)
                })?;
        let offset = page.saturating_sub(1).saturating_mul(page_size);
        let offset = i64::try_from(offset).unwrap_or(i64::MAX);
        let mut select = self.connection.prepare_cached(
            "SELECT value FROM execution_orders
             ORDER BY trading_day DESC, ref_number DESC, wire_ref DESC, reference DESC
             LIMIT ?1 OFFSET ?2",
        )?;
        let mut rows = select.query(params![page_size as i64, offset])?;
        let mut items = Vec::with_capacity(page_size);
        while let Some(row) = rows.next()? {
            items.push(serde_json::from_slice(row.get_ref(0)?.as_blob()?)?);
        }
        Ok(ExecutionOrdersPage {
            items,
            total,
            page,
            page_size,
        })
    }

    #[cfg(test)]
    pub(super) fn set_read_only(&self) {
        self.connection
            .pragma_update(None, "query_only", true)
            .unwrap();
    }
}

fn configure(connection: &Connection) -> Result<()> {
    connection.busy_timeout(std::time::Duration::from_secs(1))?;
    connection.pragma_update(None, "synchronous", "FULL")?;
    connection.pragma_update(None, "journal_mode", "WAL")?;
    Ok(())
}

fn metadata(connection: &Connection) -> Result<(u32, String, Option<String>, bool)> {
    Ok(connection.query_row(
        "SELECT version,store_id,source_hash,advanced FROM execution_meta WHERE id=1",
        [],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
    )?)
}

fn read_marker(bytes: &[u8]) -> Result<Option<StoreMarker>> {
    #[derive(Deserialize)]
    struct Probe {
        execution_storage_version: Option<u32>,
    }
    if serde_json::from_slice::<Probe>(bytes)?
        .execution_storage_version
        .is_some()
    {
        return Ok(Some(serde_json::from_slice(bytes)?));
    }
    Ok(None)
}

fn check_marker(connection: &Connection, marker: &StoreMarker) -> Result<()> {
    let (version, id, _, _) = metadata(connection)?;
    ensure!(
        version == VERSION && version == marker.execution_storage_version && id == marker.store_id,
        "执行状态标记与数据库不匹配"
    );
    Ok(())
}

fn write_backup(path: &Path, bytes: &[u8]) -> Result<()> {
    use std::io::Write;
    let temporary = path.with_extension("tmp");
    let mut file = fs::File::create(&temporary)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    drop(file);
    fs::rename(temporary, path)?;
    sync_parent(path)
}

fn sync_parent(_path: &Path) -> Result<()> {
    #[cfg(unix)]
    fs::File::open(
        _path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new(".")),
    )?
    .sync_all()?;
    Ok(())
}

pub(super) fn export(state_path: &Path) -> Result<serde_json::Value> {
    let bytes = fs::read(state_path)?;
    let Some(marker) = read_marker(&bytes)? else {
        return Ok(serde_json::from_slice(&bytes)?);
    };
    let mut connection = Connection::open_with_flags(
        state_path.with_file_name(DB_NAME),
        OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    connection.busy_timeout(std::time::Duration::from_secs(3))?;
    let snapshot = connection.transaction()?;
    check_marker(&snapshot, &marker)?;
    Ok(serde_json::to_value(read_state(&snapshot)?)?)
}

fn read_state(connection: &Connection) -> Result<ExecutionState> {
    let mut state = read_fields(connection)?;
    read_orders(connection, &mut state)?;
    Ok(state)
}

fn read_orders(connection: &Connection, state: &mut ExecutionState) -> Result<()> {
    let mut select = connection.prepare("SELECT reference,value FROM execution_orders")?;
    let mut rows = select.query([])?;
    while let Some(row) = rows.next()? {
        let reference: String = row.get(0)?;
        let order = serde_json::from_slice(row.get_ref(1)?.as_blob()?)
            .with_context(|| format!("读取委托 {reference} 失败"))?;
        Arc::make_mut(&mut state.orders.records).insert(reference, order);
    }
    Ok(())
}

fn read_fields(connection: &Connection) -> Result<ExecutionState> {
    let mut state = ExecutionState::default();
    macro_rules! load {
        (scalar: $($field:ident),+) => {$(
            state.$field = read_scalar(connection, stringify!($field))?;
        )+};
        (scalar_default: $($field:ident),+) => {$(
            state.$field = read_optional_scalar(connection, stringify!($field))?.unwrap_or_default();
        )+};
        ($kind:ident: $($field:ident),+) => {$(
            state.$field = read_records(connection, stringify!($field), ReadRecord::$kind)?;
        )+};
    }
    state_fields!(load);
    Ok(state)
}

fn read_scalar<T: DeserializeOwned>(db: &Connection, field: &str) -> Result<T> {
    read_optional_scalar(db, field)?.with_context(|| format!("缺少执行状态 {field}"))
}

fn read_optional_scalar<T: DeserializeOwned>(db: &Connection, field: &str) -> Result<Option<T>> {
    let bytes: Option<Vec<u8>> = db
        .query_row(
            "SELECT value FROM execution_fields WHERE field=?1 AND key=''",
            [field],
            |row| row.get(0),
        )
        .optional()
        .with_context(|| format!("读取执行状态 {field}"))?;
    bytes.map(|bytes| serde_json::from_slice(&bytes).with_context(|| format!("解析执行状态 {field}"))).transpose()
}

// Each collection has its own representation in SQLite and decodes directly
// into the corresponding Rust collection.
struct ReadRecord;
impl ReadRecord {
    fn map<T: DeserializeOwned>(key: String, value: &[u8]) -> Result<(String, T)> {
        Ok((key, serde_json::from_slice(value)?))
    }
    fn set(key: String, _value: &[u8]) -> Result<String> {
        Ok(key)
    }
    fn list<T: DeserializeOwned>(_key: String, value: &[u8]) -> Result<T> {
        Ok(serde_json::from_slice(value)?)
    }
}

fn read_records<T, C: FromIterator<T>>(
    db: &Connection,
    field: &str,
    decode: impl Fn(String, &[u8]) -> Result<T>,
) -> Result<C> {
    let mut select =
        db.prepare_cached("SELECT key,value FROM execution_fields WHERE field=?1 ORDER BY key")?;
    let mut rows = select.query([field])?;
    std::iter::from_fn(|| match rows.next() {
        Ok(Some(row)) => Some((|| decode(row.get(0)?, row.get_ref(1)?.as_blob()?))()),
        Ok(None) => None,
        Err(error) => Some(Err(error.into())),
    })
    .collect()
}

fn write_fields(
    tx: &Transaction,
    previous: Option<&ExecutionState>,
    state: &ExecutionState,
) -> Result<bool> {
    let mut fields = FieldChanges::new(tx)?;
    macro_rules! write {
        (scalar_default: $($field:ident),+) => { write!(scalar: $($field),+); };
        ($kind:ident: $($field:ident),+) => {$(
            fields.$kind(stringify!($field), previous.map(|p| &p.$field), &state.$field)?;
        )+};
    }
    state_fields!(write);
    Ok(fields.changed)
}

fn update_baseline(previous: &mut ExecutionState, state: &ExecutionState) {
    macro_rules! update {
        ($kind:ident: $($field:ident),+) => {$(
            if previous.$field != state.$field {
                previous.$field.clone_from(&state.$field);
            }
        )+};
    }
    state_fields!(update);
}

fn verify_import(expected: &ExecutionState, actual: &ExecutionState) -> Result<()> {
    macro_rules! verify {
        ($kind:ident: $($field:ident),+) => {$(
            ensure!(expected.$field == actual.$field,
                "执行状态迁移校验失败 ({})，原文件保留", stringify!($field));
        )+};
    }
    state_fields!(verify);
    ensure!(
        expected.orders.len() == actual.orders.len(),
        "迁移前后委托数量不一致"
    );
    for (reference, expected) in expected.orders.iter() {
        let actual = actual.orders.get(reference).context("迁移后委托缺失")?;
        ensure!(
            serde_json::to_value(expected)? == serde_json::to_value(actual)?,
            "委托 {reference} 迁移校验失败"
        );
    }
    Ok(())
}

struct FieldChanges<'db> {
    put: rusqlite::CachedStatement<'db>,
    delete: rusqlite::CachedStatement<'db>,
    changed: bool,
}

impl<'db> FieldChanges<'db> {
    fn new(tx: &'db Transaction) -> Result<Self> {
        Ok(Self {
            put: tx.prepare_cached(
                "INSERT INTO execution_fields(field,key,value) VALUES(?1,?2,?3)
                 ON CONFLICT(field,key) DO UPDATE SET value=excluded.value",
            )?,
            delete: tx.prepare_cached("DELETE FROM execution_fields WHERE field=?1 AND key=?2")?,
            changed: false,
        })
    }

    fn put(&mut self, field: &str, key: &str, value: &impl Serialize) -> Result<()> {
        self.put
            .execute(params![field, key, serde_json::to_vec(value)?])?;
        self.changed = true;
        Ok(())
    }

    fn remove(&mut self, field: &str, key: &str) -> Result<()> {
        self.delete.execute(params![field, key])?;
        self.changed = true;
        Ok(())
    }

    fn scalar<T: Serialize + PartialEq>(
        &mut self,
        field: &str,
        old: Option<&T>,
        value: &T,
    ) -> Result<()> {
        if old != Some(value) {
            self.put(field, "", value)?;
        }
        Ok(())
    }

    fn map<T: Serialize + PartialEq>(
        &mut self,
        field: &str,
        old: Option<&HashMap<String, T>>,
        map: &HashMap<String, T>,
    ) -> Result<()> {
        for (key, value) in map {
            if old.and_then(|old| old.get(key)) != Some(value) {
                self.put(field, key, value)?;
            }
        }
        for key in old
            .into_iter()
            .flat_map(HashMap::keys)
            .filter(|key| !map.contains_key(*key))
        {
            self.remove(field, key)?;
        }
        Ok(())
    }

    fn set(
        &mut self,
        field: &str,
        old: Option<&HashSet<String>>,
        set: &HashSet<String>,
    ) -> Result<()> {
        for key in set
            .iter()
            .filter(|key| old.is_none_or(|old| !old.contains(*key)))
        {
            self.put(field, key, &())?;
        }
        for key in old.into_iter().flatten().filter(|key| !set.contains(*key)) {
            self.remove(field, key)?;
        }
        Ok(())
    }

    fn list<T: Serialize + PartialEq>(
        &mut self,
        field: &str,
        old: Option<&Vec<T>>,
        list: &[T],
    ) -> Result<()> {
        for (index, value) in list.iter().enumerate() {
            if old.and_then(|old| old.get(index)) != Some(value) {
                self.put(field, &format!("{index:020}"), value)?;
            }
        }
        for index in list.len()..old.map_or(0, Vec::len) {
            self.remove(field, &format!("{index:020}"))?;
        }
        Ok(())
    }
}

fn write_orders(tx: &Transaction, orders: &Orders, import: bool) -> Result<bool> {
    if !import && orders.dirty.is_empty() {
        return Ok(false);
    }
    let mut put = tx.prepare_cached(
        "INSERT INTO execution_orders(reference,settled,trading_day,ref_number,wire_ref,value)
         VALUES(?1,?2,?3,?4,?5,?6)
         ON CONFLICT(reference) DO UPDATE SET settled=excluded.settled,trading_day=excluded.trading_day,
             ref_number=excluded.ref_number,wire_ref=excluded.wire_ref,value=excluded.value
         WHERE execution_orders.value <> excluded.value",
    )?;
    let mut changed = false;
    let mut save = |key: &str, order: &ExecutionOrder| -> Result<()> {
        let reference = order.request.order_ref.as_deref().unwrap_or_default();
        let number = reference.parse::<u64>().unwrap_or(0);
        changed |= put.execute(params![
            key,
            order.settled(),
            order.trading_day,
            format!("{number:020}"),
            reference,
            serde_json::to_vec(order)?
        ])? != 0;
        Ok(())
    };
    if import {
        for (reference, order) in orders.iter() {
            save(reference, order)?;
        }
    } else {
        for reference in &orders.dirty {
            save(reference, &orders[reference])?;
        }
    }
    Ok(changed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resumes_interrupted_import_and_refuses_to_boot_without_database() {
        let dir = std::env::temp_dir().join(format!("ntrend_execution_{}", uuid::Uuid::new_v4()));
        fs::create_dir(&dir).unwrap();
        let path = dir.join("trader_execution.json");
        let old = br#"{"account":"9999:12345","last_ref":42,"seen_trades":["a","b","c"]}"#;
        fs::write(&path, old).unwrap();
        fs::create_dir(path.with_extension("tmp")).unwrap();
        assert!(ExecutionStore::open(&path).is_err());
        assert_eq!(fs::read(&path).unwrap(), old);
        assert_eq!(
            fs::read(path.with_file_name("trader_execution.legacy.json")).unwrap(),
            old
        );
        fs::remove_dir(path.with_extension("tmp")).unwrap();

        let (mut store, mut state) = ExecutionStore::open(&path).unwrap();
        assert_eq!(state.last_ref, 42);
        assert_eq!(state.seen_trades.len(), 3);
        assert!(serde_json::from_slice::<ExecutionState>(&fs::read(&path).unwrap()).is_err());
        state.last_ref = 43;
        store.save(&mut state).unwrap();
        assert_eq!(export(&path).unwrap()["last_ref"], 43);
        drop(store);

        let db_path = path.with_file_name(DB_NAME);
        fs::remove_file(&db_path).unwrap();
        assert!(ExecutionStore::open(&path).is_err());
        assert!(!db_path.exists());
        fs::remove_file(path.with_file_name("trader_execution.legacy.json")).unwrap();
        fs::remove_file(path).unwrap();
        fs::remove_dir(dir).unwrap();
    }
}
