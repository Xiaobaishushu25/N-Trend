//! SQLite-backed instance registry; all order state remains isolated on disk.
use super::{broker::*, model::ReceivedExecutionQuote, strategy::*, *};
use crate::{
    sea_orm::{ConnectionTrait, DatabaseConnection, DbBackend, Statement},
    storage::repo,
};
use anyhow::{ensure, Context, Result};
use n_protocol::trader_stream::SharedTraderSnapshot;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap},
    path::{Path, PathBuf},
    sync::Arc,
};
use tokio::sync::{Mutex, RwLock};

#[path = "instance_comparison.rs"]
mod comparison;

pub const DEFAULT_INSTANCE: &str = "default";
const REGISTRY_KEY: &str = "trader_instance_registry_v1";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum AccountKind {
    Ctp {
        port: u16,
        broker_id: String,
        user_id: String,
        credential_ref: Option<String>,
    },
    Paper {
        config: PaperConfig,
    },
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccountDefinition {
    pub id: String,
    pub name: String,
    #[serde(flatten)]
    pub kind: AccountKind,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstanceDefinition {
    pub id: String,
    pub name: String,
    pub account_id: String,
    pub created_at: i64,
}
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct Catalog {
    accounts: BTreeMap<String, AccountDefinition>,
    instances: BTreeMap<String, InstanceDefinition>,
}
pub struct InstanceRuntime {
    pub definition: InstanceDefinition,
    pub account: AccountDefinition,
    pub engine: Arc<ExecutionEngine>,
    pub broker: BrokerClient,
    pub control: Mutex<()>,
    pub published: RwLock<SharedTraderSnapshot>,
    pub snapshot: RwLock<Value>,
    pub error: RwLock<Option<String>>,
    pub runtime_error: RwLock<Option<String>>,
    pub quote_tx: tokio::sync::mpsc::Sender<Vec<ReceivedExecutionQuote>>,
    pub quote_rx: Mutex<Option<tokio::sync::mpsc::Receiver<Vec<ReceivedExecutionQuote>>>>,
    pub quote_gap: std::sync::atomic::AtomicBool,
}
pub struct InstanceManager {
    db: DatabaseConnection,
    root: PathBuf,
    catalog: Mutex<Catalog>,
    runtimes: RwLock<BTreeMap<String, Arc<InstanceRuntime>>>,
    // Cache only successful samples; SQLite remains the source of truth.
    equity_sampled_minutes: RwLock<HashMap<String, i64>>,
    pub registry: Arc<StrategyRegistry>,
    comparisons: Arc<comparison::Comparisons>,
}

/// A completion marker is written only after validating and backing up all old files.
pub fn migrate_default(root: &Path) -> Result<PathBuf> {
    let dest = root.join("trader_instances").join(DEFAULT_INSTANCE);
    std::fs::create_dir_all(&dest)?;
    if dest.join("migration_complete.json").exists() {
        return Ok(dest);
    }
    let legacy_lock = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(root.join("trader_execution.lock"))?;
    legacy_lock
        .try_lock()
        .context("旧执行状态正在使用，禁止迁移")?;
    let backup = root.join("legacy-trader-backup");
    std::fs::create_dir_all(&backup)?;
    for name in [
        "trader_config.json",
        "trader_execution.json",
        "strategy_comparison.jsonl",
        "overnight_snapshots.jsonl",
        "overnight_tracking.jsonl",
        "overnight_attribution.jsonl",
    ] {
        let source = root.join(name);
        if !source.exists() {
            continue;
        }
        let bytes = std::fs::read(&source)?;
        if name == "trader_execution.json" {
            ExecutionEngine::validate_saved_state(&bytes).context("旧执行状态损坏，迁移已停止")?;
        }
        if name == "trader_config.json" {
            let config: ExecutionConfig =
                serde_json::from_slice(&bytes).context("旧执行配置损坏，迁移已停止")?;
            ExecutionEngine::validate_config(&config)?;
        }
        let saved = backup.join(name);
        if saved.exists() {
            ensure!(
                std::fs::read(&saved)? == bytes,
                "旧状态与迁移备份不同，须先核对恢复"
            );
        } else {
            std::fs::copy(&source, &saved)?;
        }
        std::fs::copy(&source, dest.join(name))?;
    }
    atomic_write(
        &dest.join("migration_complete.json"),
        &json!({"version":1,"completed_at":chrono::Utc::now().timestamp()}),
    )?;
    Ok(dest)
}
impl InstanceManager {
    pub async fn open(
        db: DatabaseConnection,
        root: PathBuf,
        default: Arc<ExecutionEngine>,
        registry: Arc<StrategyRegistry>,
    ) -> Result<Arc<Self>> {
        db.execute(Statement::from_string(DbBackend::Sqlite,"CREATE TABLE IF NOT EXISTS trader_equity (instance_id TEXT NOT NULL, timestamp INTEGER NOT NULL, equity REAL NOT NULL, margin REAL NOT NULL, fees REAL, quality TEXT, trading_day TEXT, PRIMARY KEY(instance_id,timestamp))".to_string())).await?;
        let mut catalog = match repo::get_setting(&db, REGISTRY_KEY).await? {
            Some(s) => serde_json::from_str::<Catalog>(&s)?,
            None => Catalog::default(),
        };
        if !catalog.instances.contains_key(DEFAULT_INSTANCE) {
            let account_identity = default.account_identity().await;
            let identity = account_identity.as_deref().and_then(|account| account.split_once(':'));
            let (broker, user) = identity.unwrap_or(("", ""));
            catalog.accounts.insert(
                "default".into(),
                AccountDefinition {
                    id: "default".into(),
                    name: "原实盘账户".into(),
                    kind: AccountKind::Ctp {
                        port: 8766,
                        broker_id: broker.into(),
                        user_id: user.into(),
                        credential_ref: None,
                    },
                },
            );
            catalog.instances.insert(
                DEFAULT_INSTANCE.into(),
                InstanceDefinition {
                    id: DEFAULT_INSTANCE.into(),
                    name: "现有 N 字形策略".into(),
                    account_id: "default".into(),
                    created_at: chrono::Utc::now().timestamp(),
                },
            );
        }
        let m = Arc::new(Self {
            db,
            root,
            catalog: Mutex::new(catalog),
            runtimes: RwLock::new(BTreeMap::new()),
            equity_sampled_minutes: RwLock::new(HashMap::new()),
            comparisons: Arc::new(comparison::Comparisons::default()),
            registry,
        });
        let c = m.catalog.lock().await.clone();
        m.persist(&c).await?;
        for d in c.instances.values() {
            let account = c
                .accounts
                .get(&d.account_id)
                .context("策略实例引用了不存在的账户")?
                .clone();
            let runtime = if d.id == DEFAULT_INSTANCE {
                default.bind(&account.id, &d.id).await?;
                let (quote_tx, quote_rx) = tokio::sync::mpsc::channel(64);
                Arc::new(InstanceRuntime {
                    control: Mutex::new(()),
                    quote_tx,
                    quote_rx: Mutex::new(Some(quote_rx)),
                    quote_gap: std::sync::atomic::AtomicBool::new(false),
                    definition: d.clone(),
                    account,
                    engine: default.clone(),
                    broker: default.broker_client(),
                    published: RwLock::default(),
                    snapshot: RwLock::new(Value::Null),
                    error: RwLock::new(None),
                    runtime_error: RwLock::new(None),
                })
            } else {
                match m.load_runtime(d.clone(), account.clone(), None, None).await {
                    Ok(runtime) => runtime,
                    Err(e) => {
                        m.blocked_runtime(
                            d.clone(),
                            account,
                            format!("账户恢复失败，请修复状态后重启: {e:#}"),
                        )
                        .await
                    }
                }
            };
            if let (
                AccountKind::Ctp {
                    broker_id, user_id, ..
                },
                BrokerClient::Ctp(client),
            ) = (&runtime.account.kind, &runtime.broker)
            {
                if !user_id.is_empty() {
                    client.expect_identity(broker_id, user_id);
                }
            }
            m.runtimes.write().await.insert(d.id.clone(), runtime);
        }
        m.validate_catalog(&c)?;
        Ok(m)
    }
    async fn blocked_runtime(
        &self,
        d: InstanceDefinition,
        account: AccountDefinition,
        reason: String,
    ) -> Arc<InstanceRuntime> {
        let broker = BrokerClient::Disabled(reason.clone());
        let config = ExecutionConfig {
            mode: ExecutionMode::MonitorOnly,
            ..Default::default()
        };
        let mut engine = ExecutionEngine::with_config_path(
            broker.clone(),
            config,
            Some(
                self.root
                    .join("trader_instances")
                    .join(&d.id)
                    .join("trader_config.json"),
            ),
        );
        engine.registry = self.registry.clone();
        let engine = Arc::new(engine);
        let _ = engine.pause(true).await;
        let (quote_tx, quote_rx) = tokio::sync::mpsc::channel(64);
        Arc::new(InstanceRuntime {
            definition: d,
            account,
            engine,
            broker,
            control: Mutex::new(()),
            published: RwLock::default(),
            snapshot: RwLock::new(Value::Null),
            error: RwLock::new(Some(reason)),
            runtime_error: RwLock::new(None),
            quote_tx,
            quote_rx: Mutex::new(Some(quote_rx)),
            quote_gap: std::sync::atomic::AtomicBool::new(false),
        })
    }
    fn validate_catalog(&self, c: &Catalog) -> Result<()> {
        let mut identities = std::collections::HashSet::new();
        let mut ports = std::collections::HashSet::new();
        let mut accounts = std::collections::HashSet::new();
        for a in c.accounts.values() {
            Self::valid_id(&a.id)?;
            if let AccountKind::Ctp {
                port,
                broker_id,
                user_id,
                credential_ref,
            } = &a.kind
            {
                ensure!(*port > 0 && ports.insert(*port), "CTP端口重复或无效");
                if !user_id.is_empty() {
                    ensure!(
                        identities.insert((broker_id.clone(), user_id.clone())),
                        "同一实盘账户禁止重复配置"
                    );
                }
                if a.id != "default" {
                    ensure!(
                        !broker_id.is_empty() && !user_id.is_empty() && credential_ref.is_some(),
                        "新增实盘账户必须明确身份和服务端凭据引用"
                    );
                }
                if let Some(r) = credential_ref {
                    Self::valid_id(r)?;
                }
            } else if let AccountKind::Paper { config } = &a.kind {
                config.validate()?;
            }
        }
        for d in c.instances.values() {
            Self::valid_id(&d.id)?;
            ensure!(
                c.accounts.contains_key(&d.account_id) && accounts.insert(d.account_id.clone()),
                "一个账户只能绑定一个策略实例"
            );
        }
        Ok(())
    }
    fn valid_id(s: &str) -> Result<()> {
        ensure!(
            !s.is_empty()
                && s.len() <= 80
                && s.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'),
            "编号只能使用字母、数字、下划线和短横线"
        );
        Ok(())
    }
    async fn persist(&self, c: &Catalog) -> Result<()> {
        self.validate_catalog(c)?;
        repo::set_settings(
            &self.db,
            &HashMap::from([(REGISTRY_KEY.into(), serde_json::to_string(c)?)]),
        )
        .await
    }
    async fn load_runtime(
        &self,
        d: InstanceDefinition,
        account: AccountDefinition,
        config: Option<ExecutionConfig>,
        definition: Option<StrategyDefinition>,
    ) -> Result<Arc<InstanceRuntime>> {
        let dir = self.root.join("trader_instances").join(&d.id);
        std::fs::create_dir_all(&dir)?;
        let broker = match &account.kind {
            AccountKind::Ctp {
                port,
                broker_id,
                user_id,
                ..
            } => {
                let client = CtpTraderClient::for_port(*port);
                client.expect_identity(broker_id, user_id);
                BrokerClient::Ctp(client)
            }
            AccountKind::Paper { config } => BrokerClient::Paper(Arc::new(PaperBroker::open(
                account.id.clone(),
                config.clone(),
                dir.join("paper_account.json"),
            )?)),
        };
        let cfg = match config {
            Some(c) => c,
            None => serde_json::from_slice(&std::fs::read(dir.join("trader_config.json"))?)?,
        };
        ExecutionEngine::validate_config(&cfg)?;
        if !dir.join("trader_config.json").exists() {
            atomic_write(&dir.join("trader_config.json"), &cfg)?;
        }
        let mut engine = ExecutionEngine::with_config_path(
            broker.clone(),
            cfg,
            Some(dir.join("trader_config.json")),
        );
        engine.registry = self.registry.clone();
        let engine = Arc::new(engine);
        engine.bind(&account.id, &d.id).await?;
        if let Some(definition) = definition {
            engine.set_definition(definition).await?;
        }
        let (quote_tx, quote_rx) = tokio::sync::mpsc::channel(64);
        Ok(Arc::new(InstanceRuntime {
            control: Mutex::new(()),
            quote_tx,
            quote_rx: Mutex::new(Some(quote_rx)),
            quote_gap: std::sync::atomic::AtomicBool::new(false),
            definition: d,
            account,
            engine,
            broker,
            published: RwLock::default(),
            snapshot: RwLock::new(Value::Null),
            error: RwLock::new(None),
            runtime_error: RwLock::new(None),
        }))
    }
    pub async fn get(&self, id: &str) -> Result<Arc<InstanceRuntime>> {
        self.runtimes
            .read()
            .await
            .get(id)
            .cloned()
            .ok_or_else(|| anyhow::anyhow!("策略实例不存在: {id}"))
    }
    pub async fn all(&self) -> Vec<Arc<InstanceRuntime>> {
        self.runtimes.read().await.values().cloned().collect()
    }
    pub async fn account(&self, id: &str) -> Result<AccountDefinition> {
        let catalog = self.catalog.lock().await;
        let definition = catalog.instances.get(id).context("实例不存在")?;
        catalog
            .accounts
            .get(&definition.account_id)
            .cloned()
            .context("账户不存在")
    }
    /// Login can update credentials for the same account; it can never rebind
    /// an instance with existing orders or positions to another investor.
    pub async fn set_ctp_credentials(
        &self,
        id: &str,
        broker: &str,
        user: &str,
        reference: &str,
    ) -> Result<()> {
        Self::valid_id(reference)?;
        let runtime = self.get(id).await?;
        let mut catalog = self.catalog.lock().await;
        let account_id = catalog
            .instances
            .get(id)
            .context("实例不存在")?
            .account_id
            .clone();
        let mut next = catalog.clone();
        match &mut next
            .accounts
            .get_mut(&account_id)
            .context("账户不存在")?
            .kind
        {
            AccountKind::Ctp {
                broker_id,
                user_id,
                credential_ref,
                ..
            } => {
                ensure!(
                    broker_id.is_empty() || broker_id == broker,
                    "不能将实例切换为另一个券商，请创建新实例"
                );
                ensure!(
                    user_id.is_empty() || user_id == user,
                    "不能将实例切换为另一个账户，请创建新实例"
                );
                if user_id.is_empty() {
                    ensure!(
                        !runtime.engine.has_unfinished().await,
                        "账户归属未确认且有未完成交易，须先核对"
                    );
                }
                *broker_id = broker.into();
                *user_id = user.into();
                *credential_ref = Some(reference.into());
            }
            AccountKind::Paper { .. } => anyhow::bail!("本地模拟账户不支持 CTP 登录"),
        }
        self.persist(&next).await?;
        *catalog = next;
        if let BrokerClient::Ctp(client) = &runtime.broker {
            client.expect_identity(broker, user);
        }
        Ok(())
    }
    pub async fn rename(&self, id: &str, new_name: String) -> Result<()> {
        let trimmed = new_name.trim();
        ensure!(!trimmed.is_empty() && trimmed.len() <= 160, "实例名称无效");
        let mut c = self.catalog.lock().await;
        let mut next = c.clone();
        let inst = next.instances.get_mut(id).context("实例不存在")?;
        inst.name = trimmed.to_string();
        self.validate_catalog(&next)?;
        self.persist(&next).await?;
        *c = next;
        Ok(())
    }
    pub async fn list(&self) -> Value {
        let mut rows = vec![];
        let (instances, accounts) = {
            let c = self.catalog.lock().await;
            (c.instances.clone(), c.accounts.clone())
        };
        for r in self.all().await {
            let def = instances
                .get(&r.definition.id)
                .cloned()
                .unwrap_or_else(|| r.definition.clone());
            let acc = accounts
                .get(&def.account_id)
                .cloned()
                .unwrap_or_else(|| r.account.clone());
            rows.push(json!({"instance":def,"account":acc,"strategy":r.engine.definition().await,"config":r.engine.get_config().await,"paused":r.engine.paused().await,"error":r.error.read().await.clone().or(r.runtime_error.read().await.clone()).or(r.engine.execution_error().await)}));
        }
        json!({"default_instance_id":DEFAULT_INSTANCE,"instances":rows,"modules":self.registry.catalog()})
    }
    pub async fn create(
        &self,
        name: String,
        account: AccountDefinition,
        strategy: StrategyDefinition,
        config: ExecutionConfig,
    ) -> Result<String> {
        ensure!(!name.trim().is_empty() && name.len() <= 160, "实例名称无效");
        Self::valid_id(&account.id)?;
        self.registry.validate(&strategy)?;
        ExecutionEngine::validate_config(&config)?;
        let mut c = self.catalog.lock().await;
        ensure!(!c.accounts.contains_key(&account.id), "账户编号已存在");
        let id = uuid::Uuid::new_v4().to_string();
        let d = InstanceDefinition {
            id: id.clone(),
            name,
            account_id: account.id.clone(),
            created_at: chrono::Utc::now().timestamp(),
        };
        let mut next = c.clone();
        next.accounts.insert(account.id.clone(), account.clone());
        next.instances.insert(id.clone(), d.clone());
        self.validate_catalog(&next)?;
        if let AccountKind::Ctp {
            credential_ref: Some(reference),
            broker_id,
            user_id,
            ..
        } = &account.kind
        {
            let credentials: Value = serde_json::from_slice(
                &std::fs::read(
                    self.root
                        .join("ctp_credentials")
                        .join(format!("{reference}.json")),
                )
                .context("服务端账户凭据文件不存在")?,
            )?;
            ensure!(
                credentials["BrokerID"].as_str() == Some(broker_id)
                    && credentials["UserID"].as_str() == Some(user_id),
                "凭据中的券商/用户与账户配置不一致"
            );
        }
        let runtime = self
            .load_runtime(d, account, Some(config), Some(strategy))
            .await?;
        runtime.engine.pause(true).await?;
        self.persist(&next).await?;
        *c = next;
        self.runtimes.write().await.insert(id.clone(), runtime);
        Ok(id)
    }
    pub async fn remove(&self, id: &str) -> Result<()> {
        ensure!(id != DEFAULT_INSTANCE, "默认兼容实例不能删除");
        let r = self.get(id).await?;
        ensure!(
            r.engine.paused().await && !r.engine.has_unfinished().await,
            "需先暂停并结束全部条件单、委托和持仓"
        );
        let mut c = self.catalog.lock().await;
        let mut next = c.clone();
        next.instances.remove(id);
        next.accounts.remove(&r.account.id);
        self.persist(&next).await?;
        *c = next;
        self.runtimes.write().await.remove(id);
        Ok(()) // Disk history is deliberately retained.
    }
    pub async fn validate_snapshot(&self, id: &str, snapshot: &Value) -> Result<()> {
        let mut c = self.catalog.lock().await;
        let account_id = c
            .instances
            .get(id)
            .context("实例不存在")?
            .account_id
            .clone();
        let account = c.accounts.get(&account_id).context("账户不存在")?;
        if let AccountKind::Ctp {
            broker_id, user_id, ..
        } = &account.kind
        {
            let h = &snapshot["health"];
            if !h["logged_in"].as_bool().unwrap_or(false) {
                return Ok(());
            }
            let b = h["broker_id"].as_str().unwrap_or("");
            let u = h["user_id"].as_str().unwrap_or("");
            ensure!(!b.is_empty() && !u.is_empty(), "柜台身份缺失");
            if user_id.is_empty() && id == DEFAULT_INSTANCE {
                let mut next = c.clone();
                if let AccountKind::Ctp {
                    broker_id, user_id, ..
                } = &mut next.accounts.get_mut(&account_id).unwrap().kind
                {
                    *broker_id = b.into();
                    *user_id = u.into();
                }
                self.persist(&next).await?;
                *c = next;
            } else {
                ensure!(
                    b == broker_id && u == user_id,
                    "柜台身份与实例账户配置不一致"
                );
            }
            if let BrokerClient::Ctp(client) = &self.get(id).await?.broker {
                client.expect_identity(b, u);
            }
        }
        Ok(())
    }
    pub async fn snapshot(&self, id: &str) -> Result<Value> {
        let r = self.get(id).await?;
        let cached = r.published.read().await.clone();
        if !cached.is_empty() {
            Ok(cached.to_value())
        } else {
            self.build_snapshot(id).await
        }
    }
    pub async fn build_snapshot(&self, id: &str) -> Result<Value> {
        Ok(self
            .build_shared_snapshot(id, &mut execution::ExecutionSnapshotCache::default())
            .await?
            .to_value())
    }

    pub async fn build_shared_snapshot(
        &self,
        id: &str,
        cache: &mut execution::ExecutionSnapshotCache,
    ) -> Result<SharedTraderSnapshot> {
        let r = self.get(id).await?;
        let mut s = r.snapshot.read().await.clone();
        if !s.is_object() {
            s = json!({"health":{},"bridge_online":false});
        }
        s["instance_id"] = json!(id);
        s["account_id"] = json!(r.account.id);
        s["account_kind"] = json!(match r.account.kind {
            AccountKind::Ctp { .. } => "ctp",
            AccountKind::Paper { .. } => "paper",
        });
        let mut snapshot = SharedTraderSnapshot::from_value(s);
        snapshot.extend(r.engine.snapshot_fields(cache, true).await?);
        if let Some(error) = r
            .error
            .read()
            .await
            .clone()
            .or(r.runtime_error.read().await.clone())
        {
            snapshot.insert("execution_error", json!(error));
        }
        Ok(snapshot)
    }
    pub async fn sample_equity(&self, id: &str, s: &Value) -> Result<()> {
        let Some(equity) = s["account"]["balance"].as_f64().filter(|v| v.is_finite()) else {
            return Ok(());
        };
        if s["bridge_online"] == false || !s["health"]["ready"].as_bool().unwrap_or(false) {
            return Ok(());
        }
        let wall = chrono::Utc::now().timestamp();
        if !s["account"]["updated_at"]
            .as_f64()
            .is_some_and(|t| (-1.0..=60.0).contains(&(wall as f64 - t)))
        {
            return Ok(());
        }
        let now = wall / 60 * 60;
        if self.equity_sampled_minutes.read().await.get(id).copied() == Some(now) {
            return Ok(());
        }
        let margin = s["account"]["curr_margin"].as_f64().unwrap_or(0.0);
        let fees = s["account"]["commission"].as_f64();
        self.db.execute(Statement::from_sql_and_values(DbBackend::Sqlite,"INSERT OR IGNORE INTO trader_equity(instance_id,timestamp,equity,margin,fees,quality,trading_day) VALUES(?,?,?,?,?,?,?)",[id.into(),now.into(),equity.into(),margin.into(),fees.into(),if fees.is_some(){"ok"}else{"fees_unconfirmed"}.into(),s["health"]["trading_day"].as_str().unwrap_or("").into()])).await?;
        self.equity_sampled_minutes
            .write()
            .await
            .insert(id.to_owned(), now);
        Ok(())
    }
}
