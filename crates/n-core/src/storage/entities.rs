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

