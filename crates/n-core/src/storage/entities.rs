//! SeaORM entities for the SQLite database.

pub mod symbols {
    use sea_orm::entity::prelude::*;
    use serde::{Deserialize, Serialize};

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel, Serialize, Deserialize)]
    #[sea_orm(table_name = "symbols")]
    pub struct Model {
        #[sea_orm(primary_key, auto_increment = false)]
        pub code: String,
        pub name: String,
        pub variety: String,
        pub exchange: String,
        pub node: String,
        pub watchlist: bool,
        pub enabled: bool,
        /// 全部品种视图的手动排序索引（拖拽排序落库用；默认按代码序回填）
        pub sort_index: i64,
        /// 最小变动价位（tick）；0 表示未显式设置，查询时用内置默认表
        pub tick_size: f64,
        pub created_at: String,
        pub updated_at: String,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

pub mod klines {
    use sea_orm::entity::prelude::*;
    use serde::{Deserialize, Serialize};

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel, Serialize, Deserialize)]
    #[sea_orm(table_name = "klines")]
    pub struct Model {
        #[sea_orm(primary_key, auto_increment = false)]
        pub symbol: String,
        #[sea_orm(primary_key, auto_increment = false)]
        pub timeframe: String,
        #[sea_orm(primary_key, auto_increment = false)]
        pub ts: String,
        pub open: f64,
        pub high: f64,
        pub low: f64,
        pub close: f64,
        pub volume: f64,
        pub hold: f64,
        pub source: String,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

pub mod scans {
    use sea_orm::entity::prelude::*;
    use serde::{Deserialize, Serialize};

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel, Serialize, Deserialize)]
    #[sea_orm(table_name = "scans")]
    pub struct Model {
        #[sea_orm(primary_key)]
        pub id: i64,
        pub started_at: String,
        pub finished_at: String,
        pub status: String,
        pub scanned: i64,
        pub active_count: i64,
        pub summary: String,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

pub mod signals {
    use sea_orm::entity::prelude::*;
    use serde::{Deserialize, Serialize};

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel, Serialize, Deserialize)]
    #[sea_orm(table_name = "signals")]
    pub struct Model {
        #[sea_orm(primary_key)]
        pub id: i64,
        pub scan_id: i64,
        pub symbol: String,
        pub level: String,
        pub direction: String,
        pub grade: String,
        pub state: String,
        pub category: String,
        pub entry: f64,
        pub stop: f64,
        pub target: f64,
        pub rr: f64,
        pub score: f64,
        pub note: String,
        pub detail: String,
        pub created_at: String,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

pub mod settings {
    use sea_orm::entity::prelude::*;
    use serde::{Deserialize, Serialize};

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel, Serialize, Deserialize)]
    #[sea_orm(table_name = "settings")]
    pub struct Model {
        #[sea_orm(primary_key, auto_increment = false)]
        pub key: String,
        pub value: String,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

/// 品种分组。sort_index 为分组排序预留字段（后续支持手动排序）。
pub mod groups {
    use sea_orm::entity::prelude::*;
    use serde::{Deserialize, Serialize};

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel, Serialize, Deserialize)]
    #[sea_orm(table_name = "groups")]
    pub struct Model {
        #[sea_orm(primary_key)]
        pub id: i64,
        pub name: String,
        pub sort_index: i64,
        pub created_at: String,
        pub updated_at: String,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

/// 品种与分组的关联（多对多）。sort_index 为组内品种排序预留字段。
pub mod symbol_groups {
    use sea_orm::entity::prelude::*;
    use serde::{Deserialize, Serialize};

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel, Serialize, Deserialize)]
    #[sea_orm(table_name = "symbol_groups")]
    pub struct Model {
        #[sea_orm(primary_key, auto_increment = false)]
        pub symbol: String,
        #[sea_orm(primary_key, auto_increment = false)]
        pub group_id: i64,
        pub sort_index: i64,
        pub created_at: String,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

/// 信号结局与首批诊断特征（复盘统计用）。
/// 每个信号一行（signal_id 主键），由扫描后的结局回填任务写入/覆盖。
pub mod signal_outcomes {
    use sea_orm::entity::prelude::*;
    use serde::{Deserialize, Serialize};

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel, Serialize, Deserialize)]
    #[sea_orm(table_name = "signal_outcomes")]
    pub struct Model {
        #[sea_orm(primary_key, auto_increment = false)]
        pub signal_id: i64,
        /// 模拟规则版本（简化出场规则 = 1），规则升级后按版本重新回填
        pub sim_version: i64,
        /// win / loss / no_trigger / open / insufficient_data
        pub outcome: String,
        /// stop / target / no_follow / time_exit / （空）
        pub exit_reason: String,
        /// 模拟回放找到的入场触达时间（快照 trigger_ts 缺失时用于图上补画触发标记）
        pub entry_ts: Option<String>,
        pub exit_ts: Option<String>,
        pub exit_price: Option<f64>,
        pub r_multiple: Option<f64>,
        pub mfe_r: Option<f64>,
        pub mae_r: Option<f64>,
        pub bars_held: Option<i64>,
        /// 触发 bar 成交量 / 前 20 根均量（15m）
        pub vol_ratio: Option<f64>,
        /// 触发 bar 持仓量较前一根增加
        pub oi_increase: Option<bool>,
        /// 60m 连续趋势分 0~5（信号时刻截断计算）
        pub trend60_score: Option<f64>,
        pub updated_at: String,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

