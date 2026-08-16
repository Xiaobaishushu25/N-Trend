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

/// 前向信号事件表。预警K线收盘即创建事件，AB端点/预警K线/入场评分落库后不再回改；
/// 后续行情只推进 state 并记录真实触发与出场。
pub mod pattern_events {
    use sea_orm::entity::prelude::*;
    use serde::{Deserialize, Serialize};

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel, Serialize, Deserialize)]
    #[sea_orm(table_name = "pattern_events")]
    pub struct Model {
        #[sea_orm(primary_key)]
        pub id: i64,
        pub symbol: String,
        pub direction: String,
        pub grade: String,
        pub level: String,
        pub s0_ts: String,
        pub s0_price: f64,
        pub s1_ts: String,
        pub s1_price: f64,
        pub s2_ts: String,
        pub s2_price: f64,
        /// AB 段结构与复盘统计用快照，识别时定死
        pub a_move: f64,
        pub b_move: f64,
        pub a_bars: i64,
        pub b_bars: i64,
        pub retracement: f64,
        /// 预警K线收盘时间（15m）
        pub warning_ts: String,
        /// 实际发现预警的时间（通常等于 warning_ts）
        pub detected_at: String,
        /// strong / wick / cumulative；历史记录可能为 fast / engulf（按强反转兼容）
        pub warning_kind: String,
        pub entry_score: f64,
        /// JSON: {"dim_a": x, "dim_b": y, "dim_warning": z}
        pub entry_score_dims: String,
        pub entry: f64,
        pub stop: f64,
        pub target: f64,
        pub risk: f64,
        pub rr: f64,
        /// pending / triggered / expired / closed
        pub state: String,
        /// 上次推进到哪一根 15m K 线（避免重复扫描已处理行情）
        pub last_advance_ts: Option<String>,
        pub trigger_ts: Option<String>,
        /// 触发所在 15m K线收盘时间
        pub trigger_bar_ts: Option<String>,
        pub trigger_price: Option<f64>,
        pub trigger_score: Option<f64>,
        pub trigger_volume_ratio: Option<f64>,
        pub overshoot_r: Option<f64>,
        pub hold_score: Option<f64>,
        /// JSON 数组，记录触发后每次扫描的持仓评分历史
        pub hold_score_history: String,
        pub outcome: Option<String>,
        pub exit_reason: Option<String>,
        pub exit_ts: Option<String>,
        pub exit_price: Option<f64>,
        pub r_multiple: Option<f64>,
        pub mfe_r: Option<f64>,
        pub mae_r: Option<f64>,
        pub created_at: String,
        pub updated_at: String,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}

/// 连续合约换月记录：5m 断点时间为主键，确认后标记断点后第一根 bar 为换月。
pub mod rollovers {
    use sea_orm::entity::prelude::*;
    use serde::{Deserialize, Serialize};

    #[derive(Clone, Debug, PartialEq, DeriveEntityModel, Serialize, Deserialize)]
    #[sea_orm(table_name = "rollovers")]
    pub struct Model {
        #[sea_orm(primary_key, auto_increment = false)]
        pub symbol: String,
        #[sea_orm(primary_key, auto_increment = false)]
        pub ts: String,
        pub from_contract: String,
        pub to_contract: String,
        pub confirmed: bool,
        pub created_at: String,
        pub updated_at: String,
    }

    #[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
    pub enum Relation {}

    impl ActiveModelBehavior for ActiveModel {}
}
