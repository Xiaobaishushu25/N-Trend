//! Repository layer over the SeaORM entities.

use anyhow::{anyhow, Context, Result};
use sea_orm::sea_query::OnConflict;
use sea_orm::ConnectionTrait;
use sea_orm::{
    ActiveModelTrait, ColumnTrait, DatabaseConnection, EntityTrait, PaginatorTrait, QueryFilter,
    QueryOrder, QuerySelect, Set,
};

use crate::storage::entities::{
    groups, klines, pattern_events, rollovers, settings, symbol_groups, symbols,
};

pub async fn upsert_klines(db: &DatabaseConnection, rows: Vec<klines::ActiveModel>) -> Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    klines::Entity::insert_many(rows)
        .on_conflict(
            OnConflict::columns([
                klines::Column::Symbol,
                klines::Column::Timeframe,
                klines::Column::Ts,
            ])
            .update_columns([
                klines::Column::Open,
                klines::Column::High,
                klines::Column::Low,
                klines::Column::Close,
                klines::Column::Volume,
                klines::Column::Hold,
                klines::Column::Source,
            ])
            .to_owned(),
        )
        .exec(db)
        .await
        .context("upsert K线失败")?;
    Ok(())
}

pub async fn delete_derived_klines(db: &DatabaseConnection, symbol: &str) -> Result<u64> {
    let res = klines::Entity::delete_many()
        .filter(klines::Column::Symbol.eq(symbol))
        .filter(klines::Column::Source.eq("derived"))
        .exec(db)
        .await
        .context("清理派生K线失败")?;
    Ok(res.rows_affected)
}

pub async fn delete_symbol_klines(db: &DatabaseConnection, symbol: &str) -> Result<()> {
    klines::Entity::delete_many()
        .filter(klines::Column::Symbol.eq(symbol))
        .exec(db)
        .await
        .context("删除品种K线失败")?;
    Ok(())
}

pub async fn delete_symbol_rollovers(db: &DatabaseConnection, symbol: &str) -> Result<()> {
    rollovers::Entity::delete_many()
        .filter(rollovers::Column::Symbol.eq(symbol))
        .exec(db)
        .await
        .context("删除品种换月记录失败")?;
    Ok(())
}

pub async fn delete_symbol_rollover(db: &DatabaseConnection, symbol: &str, ts: &str) -> Result<()> {
    rollovers::Entity::delete_many()
        .filter(rollovers::Column::Symbol.eq(symbol))
        .filter(rollovers::Column::Ts.eq(ts))
        .exec(db)
        .await
        .context("删除单条换月记录失败")?;
    Ok(())
}

pub async fn symbol_rollovers(
    db: &DatabaseConnection,
    symbol: &str,
) -> Result<Vec<rollovers::Model>> {
    Ok(rollovers::Entity::find()
        .filter(rollovers::Column::Symbol.eq(symbol))
        .order_by_asc(rollovers::Column::Ts)
        .all(db)
        .await
        .context("查询换月记录失败")?)
}

/// 全部换月记录：刷新结果时用来判断哪些品种有新确认的换月。
pub async fn all_rollovers(db: &DatabaseConnection) -> Result<Vec<rollovers::Model>> {
    Ok(rollovers::Entity::find()
        .all(db)
        .await
        .context("查询全部换月记录失败")?)
}

/// 批量 upsert 换月记录：同一 symbol+ts 覆盖为最新确认结果。
pub async fn upsert_rollovers(
    db: &DatabaseConnection,
    rows: Vec<rollovers::ActiveModel>,
) -> Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    rollovers::Entity::insert_many(rows)
        .on_conflict(
            OnConflict::columns([rollovers::Column::Symbol, rollovers::Column::Ts])
                .update_columns([
                    rollovers::Column::FromContract,
                    rollovers::Column::ToContract,
                    rollovers::Column::Confirmed,
                    rollovers::Column::UpdatedAt,
                ])
                .to_owned(),
        )
        .exec(db)
        .await
        .context("写入换月记录失败")?;
    Ok(())
}
pub async fn latest_ts(
    db: &DatabaseConnection,
    symbol: &str,
    timeframe: &str,
) -> Result<Option<String>> {
    let row = klines::Entity::find()
        .filter(klines::Column::Symbol.eq(symbol))
        .filter(klines::Column::Timeframe.eq(timeframe))
        .order_by_desc(klines::Column::Ts)
        .one(db)
        .await
        .context("查询最新K线时间失败")?;
    Ok(row.map(|r| r.ts))
}

pub async fn klines(
    db: &DatabaseConnection,
    symbol: &str,
    timeframe: &str,
    limit: Option<usize>,
    end_ts: Option<&str>,
) -> Result<Vec<klines::Model>> {
    let mut q = klines::Entity::find()
        .filter(klines::Column::Symbol.eq(symbol))
        .filter(klines::Column::Timeframe.eq(timeframe));
    if let Some(end) = end_ts {
        q = q.filter(klines::Column::Ts.lte(end));
    }
    // 带 limit 时把取数下推到 SQL（倒序取最后 limit 根再翻回），
    // 避免先把该品种全部K线读进内存再截断。
    let rows = if let Some(limit) = limit {
        let mut rows = q
            .order_by_desc(klines::Column::Ts)
            .limit(limit as u64)
            .all(db)
            .await
            .context("查询K线失败")?;
        rows.reverse();
        rows
    } else {
        q.order_by_asc(klines::Column::Ts)
            .all(db)
            .await
            .context("查询K线失败")?
    };
    Ok(rows)
}

pub async fn raw_klines(db: &DatabaseConnection, symbol: &str) -> Result<Vec<klines::Model>> {
    let rows = klines::Entity::find()
        .filter(klines::Column::Symbol.eq(symbol))
        .filter(klines::Column::Timeframe.eq("5m"))
        .filter(klines::Column::Source.eq("raw"))
        .order_by_asc(klines::Column::Ts)
        .all(db)
        .await
        .context("查询原始K线失败")?;
    Ok(rows)
}

pub async fn list_symbols(
    db: &DatabaseConnection,
    only_enabled: bool,
) -> Result<Vec<symbols::Model>> {
    // 全部品种按手动排序索引排列（初始由迁移按代码序回填），代码序兜底
    let mut q = symbols::Entity::find()
        .order_by_asc(symbols::Column::SortIndex)
        .order_by_asc(symbols::Column::Code);
    if only_enabled {
        q = q.filter(symbols::Column::Enabled.eq(true));
    }
    Ok(q.all(db).await.context("查询品种失败")?)
}

pub async fn symbol_exists(db: &DatabaseConnection, code: &str) -> Result<bool> {
    let count = symbols::Entity::find()
        .filter(symbols::Column::Code.eq(code))
        .count(db)
        .await
        .context("查询品种失败")?;
    Ok(count > 0)
}

pub async fn upsert_symbols(
    db: &DatabaseConnection,
    rows: Vec<symbols::ActiveModel>,
) -> Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    // 新入库品种按“当前最大 sort_index + 1”追加到全部品种末尾；
    // 已存在的品种走 ON CONFLICT 更新，sort_index 不在更新列里，不会被覆盖。
    let existing_codes: std::collections::HashSet<String> = symbols::Entity::find()
        .all(db)
        .await
        .context("查询已有品种失败")?
        .into_iter()
        .map(|s| s.code)
        .collect();
    let max_sort = symbols::Entity::find()
        .order_by_desc(symbols::Column::SortIndex)
        .one(db)
        .await
        .context("查询品种排序失败")?
        .map_or(0, |s| s.sort_index);
    let mut rows = rows;
    let mut next = max_sort + 1;
    for row in &mut rows {
        let code = row.code.as_ref();
        if !existing_codes.contains(code) {
            row.sort_index = Set(next);
            next += 1;
        }
    }
    symbols::Entity::insert_many(rows)
        .on_conflict(
            OnConflict::columns([symbols::Column::Code])
                .update_columns([
                    symbols::Column::Name,
                    symbols::Column::Variety,
                    symbols::Column::Exchange,
                    symbols::Column::Node,
                ])
                .to_owned(),
        )
        .exec(db)
        .await
        .context("upsert品种失败")?;
    Ok(())
}

pub async fn set_symbol_flags(
    db: &DatabaseConnection,
    code: &str,
    watchlist: bool,
    enabled: bool,
) -> Result<()> {
    let row = symbols::Entity::find()
        .filter(symbols::Column::Code.eq(code))
        .one(db)
        .await
        .context("查询品种失败")?
        .ok_or_else(|| anyhow!("品种不存在: {code}"))?;
    let mut model: symbols::ActiveModel = row.into();
    model.watchlist = Set(watchlist);
    model.enabled = Set(enabled);
    model.updated_at = Set(crate::analyze::time::now_display());
    model.save(db).await.context("更新品种失败")?;
    Ok(())
}

/// 更新品种的最小变动价位（tick）。
pub async fn set_symbol_tick(db: &DatabaseConnection, code: &str, tick: f64) -> Result<()> {
    let row = symbols::Entity::find_by_id(code)
        .one(db)
        .await
        .context("查询品种失败")?
        .ok_or_else(|| anyhow!("品种不存在: {code}"))?;
    let mut model: symbols::ActiveModel = row.into();
    model.tick_size = Set(tick.max(0.0));
    model.updated_at = Set(crate::analyze::time::now_display());
    model.save(db).await.context("更新品种精度失败")?;
    Ok(())
}

pub async fn remove_symbol(db: &DatabaseConnection, code: &str) -> Result<()> {
    // 删除品种的同时清理它在所有分组中的关联
    symbol_groups::Entity::delete_many()
        .filter(symbol_groups::Column::Symbol.eq(code))
        .exec(db)
        .await
        .context("删除品种分组关联失败")?;
    symbols::Entity::delete_many()
        .filter(symbols::Column::Code.eq(code))
        .exec(db)
        .await
        .context("删除品种失败")?;
    Ok(())
}

pub async fn list_groups(db: &DatabaseConnection) -> Result<Vec<groups::Model>> {
    groups::Entity::find()
        .order_by_asc(groups::Column::SortIndex)
        .order_by_asc(groups::Column::Id)
        .all(db)
        .await
        .context("查询分组失败")
}

/// 创建分组：名称唯一，sort_index 取当前最大值 + 1（为后续排序预留）。
pub async fn create_group(db: &DatabaseConnection, name: &str) -> Result<groups::Model> {
    let name = name.trim();
    if name.is_empty() {
        return Err(anyhow!("分组名称不能为空"));
    }
    let exists = groups::Entity::find()
        .filter(groups::Column::Name.eq(name))
        .count(db)
        .await
        .context("查询分组失败")?
        > 0;
    if exists {
        return Err(anyhow!("分组「{name}」已存在"));
    }
    let max_sort = groups::Entity::find()
        .order_by_desc(groups::Column::SortIndex)
        .one(db)
        .await
        .context("查询分组失败")?
        .map_or(0, |g| g.sort_index);
    let now = crate::analyze::time::now_display();
    let model = groups::ActiveModel {
        id: Default::default(),
        name: Set(name.to_string()),
        sort_index: Set(max_sort + 1),
        created_at: Set(now.clone()),
        updated_at: Set(now),
    };
    groups::Entity::insert(model)
        .exec_with_returning(db)
        .await
        .context("创建分组失败")
}

pub async fn rename_group(db: &DatabaseConnection, id: i64, name: &str) -> Result<()> {
    let name = name.trim();
    if name.is_empty() {
        return Err(anyhow!("分组名称不能为空"));
    }
    let row = groups::Entity::find_by_id(id)
        .one(db)
        .await
        .context("查询分组失败")?
        .ok_or_else(|| anyhow!("分组不存在"))?;
    if row.name != name {
        let exists = groups::Entity::find()
            .filter(groups::Column::Name.eq(name))
            .filter(groups::Column::Id.ne(id))
            .count(db)
            .await
            .context("查询分组失败")?
            > 0;
        if exists {
            return Err(anyhow!("分组「{name}」已存在"));
        }
    }
    let mut model: groups::ActiveModel = row.into();
    model.name = Set(name.to_string());
    model.updated_at = Set(crate::analyze::time::now_display());
    model.save(db).await.context("重命名分组失败")?;
    Ok(())
}

pub async fn delete_group(db: &DatabaseConnection, id: i64) -> Result<()> {
    symbol_groups::Entity::delete_many()
        .filter(symbol_groups::Column::GroupId.eq(id))
        .exec(db)
        .await
        .context("删除分组关联失败")?;
    groups::Entity::delete_many()
        .filter(groups::Column::Id.eq(id))
        .exec(db)
        .await
        .context("删除分组失败")?;
    Ok(())
}

/// 分组内的品种（按组内排序索引、再按代码排序）。
pub async fn group_symbols(db: &DatabaseConnection, group_id: i64) -> Result<Vec<symbols::Model>> {
    let members = symbol_groups::Entity::find()
        .filter(symbol_groups::Column::GroupId.eq(group_id))
        .order_by_asc(symbol_groups::Column::SortIndex)
        .order_by_asc(symbol_groups::Column::Symbol)
        .all(db)
        .await
        .context("查询分组品种失败")?;
    if members.is_empty() {
        return Ok(Vec::new());
    }
    let codes: Vec<String> = members.iter().map(|m| m.symbol.clone()).collect();
    let syms = symbols::Entity::find()
        .filter(symbols::Column::Code.is_in(codes))
        .all(db)
        .await
        .context("查询分组品种失败")?;
    let by_code: std::collections::HashMap<String, symbols::Model> =
        syms.into_iter().map(|s| (s.code.clone(), s)).collect();
    Ok(members
        .into_iter()
        .filter_map(|m| by_code.get(&m.symbol).cloned())
        .collect())
}

/// 品种当前所属的全部分组（供右键菜单里标记“已在某组”）。
pub async fn symbol_groups(db: &DatabaseConnection, symbol: &str) -> Result<Vec<groups::Model>> {
    let members = symbol_groups::Entity::find()
        .filter(symbol_groups::Column::Symbol.eq(symbol))
        .all(db)
        .await
        .context("查询品种分组失败")?;
    if members.is_empty() {
        return Ok(Vec::new());
    }
    let ids: Vec<i64> = members.iter().map(|m| m.group_id).collect();
    groups::Entity::find()
        .filter(groups::Column::Id.is_in(ids))
        .order_by_asc(groups::Column::SortIndex)
        .order_by_asc(groups::Column::Id)
        .all(db)
        .await
        .context("查询分组失败")
}

/// 把品种加入分组（幂等）。sort_index 取该组内当前最大值 + 1。
pub async fn add_symbol_to_group(
    db: &DatabaseConnection,
    symbol: &str,
    group_id: i64,
) -> Result<()> {
    if !symbol_exists(db, symbol).await? {
        return Err(anyhow!("品种不存在: {symbol}"));
    }
    if groups::Entity::find_by_id(group_id)
        .one(db)
        .await
        .context("查询分组失败")?
        .is_none()
    {
        return Err(anyhow!("分组不存在"));
    }
    let exists = symbol_groups::Entity::find()
        .filter(symbol_groups::Column::Symbol.eq(symbol))
        .filter(symbol_groups::Column::GroupId.eq(group_id))
        .count(db)
        .await
        .context("查询分组品种失败")?
        > 0;
    if exists {
        return Ok(());
    }
    let max_sort = symbol_groups::Entity::find()
        .filter(symbol_groups::Column::GroupId.eq(group_id))
        .order_by_desc(symbol_groups::Column::SortIndex)
        .one(db)
        .await
        .context("查询分组品种失败")?
        .map_or(0, |m| m.sort_index);
    let model = symbol_groups::ActiveModel {
        symbol: Set(symbol.to_string()),
        group_id: Set(group_id),
        sort_index: Set(max_sort + 1),
        created_at: Set(crate::analyze::time::now_display()),
    };
    symbol_groups::Entity::insert(model)
        .exec(db)
        .await
        .context("加入分组失败")?;
    Ok(())
}

/// 把品种从指定分组移除。
pub async fn remove_symbol_from_group(
    db: &DatabaseConnection,
    symbol: &str,
    group_id: i64,
) -> Result<()> {
    symbol_groups::Entity::delete_many()
        .filter(symbol_groups::Column::Symbol.eq(symbol))
        .filter(symbol_groups::Column::GroupId.eq(group_id))
        .exec(db)
        .await
        .context("从分组移除失败")?;
    Ok(())
}

/// 批量重排分组：按传入的 id 顺序重写 groups.sort_index（供管理分组拖拽排序落库）。
pub async fn reorder_groups(db: &DatabaseConnection, ids: &[i64]) -> Result<()> {
    for (idx, id) in ids.iter().enumerate() {
        let row = groups::Entity::find_by_id(*id)
            .one(db)
            .await
            .context("查询分组失败")?;
        let Some(row) = row else {
            continue;
        };
        let mut model: groups::ActiveModel = row.into();
        model.sort_index = Set(idx as i64);
        model.save(db).await.context("更新分组排序失败")?;
    }
    Ok(())
}

/// 批量重排组内品种：按传入的代码顺序重写 sort_index（供拖拽排序落库）。
pub async fn reorder_group_symbols(
    db: &DatabaseConnection,
    group_id: i64,
    codes: &[String],
) -> Result<()> {
    for (idx, code) in codes.iter().enumerate() {
        let row = symbol_groups::Entity::find()
            .filter(symbol_groups::Column::Symbol.eq(code))
            .filter(symbol_groups::Column::GroupId.eq(group_id))
            .one(db)
            .await
            .context("查询分组品种失败")?;
        let Some(row) = row else {
            continue;
        };
        let mut model: symbol_groups::ActiveModel = row.into();
        model.sort_index = Set(idx as i64);
        model.save(db).await.context("更新组内品种排序失败")?;
    }
    Ok(())
}

/// 批量重排全部品种：按传入的代码顺序重写 symbols.sort_index（供全部视图拖拽排序落库）。
pub async fn reorder_symbols(db: &DatabaseConnection, codes: &[String]) -> Result<()> {
    for (idx, code) in codes.iter().enumerate() {
        let row = symbols::Entity::find_by_id(code)
            .one(db)
            .await
            .context("查询品种失败")?;
        let Some(row) = row else {
            continue;
        };
        let mut model: symbols::ActiveModel = row.into();
        model.sort_index = Set(idx as i64);
        model.save(db).await.context("更新品种排序失败")?;
    }
    Ok(())
}

/// 写入一条前向识别出的信号事件。
pub async fn insert_pattern_event(
    db: &DatabaseConnection,
    row: pattern_events::ActiveModel,
) -> Result<i64> {
    let res = pattern_events::Entity::insert(row)
        .exec(db)
        .await
        .context("写入信号事件失败")?;
    Ok(res.last_insert_id)
}

/// 全部信号事件（复盘统计用）。
pub async fn all_pattern_events(db: &DatabaseConnection) -> Result<Vec<pattern_events::Model>> {
    Ok(pattern_events::Entity::find()
        .order_by_asc(pattern_events::Column::Id)
        .all(db)
        .await
        .context("查询信号事件失败")?)
}

/// 按 id 查询单条信号事件。
pub async fn pattern_event_by_id(
    db: &DatabaseConnection,
    id: i64,
) -> Result<Option<pattern_events::Model>> {
    Ok(pattern_events::Entity::find_by_id(id)
        .one(db)
        .await
        .context("查询信号事件失败")?)
}

/// 某品种指定状态的信号事件。
pub async fn pattern_events_by_symbol(
    db: &DatabaseConnection,
    symbol: &str,
    state: Option<&str>,
) -> Result<Vec<pattern_events::Model>> {
    let mut q = pattern_events::Entity::find()
        .filter(pattern_events::Column::Symbol.eq(symbol))
        .order_by_asc(pattern_events::Column::WarningTs);
    if let Some(state) = state {
        q = q.filter(pattern_events::Column::State.eq(state));
    }
    Ok(q.all(db).await.context("查询信号事件失败")?)
}

/// 更新单条信号事件（推进触发/持仓/出场状态）。
pub async fn update_pattern_event(
    db: &DatabaseConnection,
    row: pattern_events::Model,
) -> Result<()> {
    let res = db
        .execute(sea_orm::Statement::from_sql_and_values(
            sea_orm::DatabaseBackend::Sqlite,
            r#"
                UPDATE pattern_events SET
                    symbol = ?1, direction = ?2, grade = ?3, level = ?4,
                    s0_ts = ?5, s0_price = ?6, s1_ts = ?7, s1_price = ?8,
                    s2_ts = ?9, s2_price = ?10, a_move = ?11, b_move = ?12,
                    a_bars = ?13, b_bars = ?14, retracement = ?15,
                    warning_ts = ?16, detected_at = ?17, warning_kind = ?18,
                    entry_score = ?19, entry_score_dims = ?20, entry = ?21,
                    stop = ?22, target = ?23, risk = ?24, rr = ?25,
                    state = ?26, last_advance_ts = ?27, trigger_ts = ?28,
                    trigger_bar_ts = ?29, trigger_price = ?30, trigger_score = ?31,
                    trigger_volume_ratio = ?32, overshoot_r = ?33, hold_score = ?34,
                    hold_score_history = ?35, outcome = ?36, exit_reason = ?37,
                    exit_ts = ?38, exit_price = ?39, r_multiple = ?40, mfe_r = ?41,
                    mae_r = ?42, created_at = ?43, updated_at = ?44
                WHERE id = ?45
                "#,
            vec![
                row.symbol.into(),
                row.direction.into(),
                row.grade.into(),
                row.level.into(),
                row.s0_ts.into(),
                row.s0_price.into(),
                row.s1_ts.into(),
                row.s1_price.into(),
                row.s2_ts.into(),
                row.s2_price.into(),
                row.a_move.into(),
                row.b_move.into(),
                row.a_bars.into(),
                row.b_bars.into(),
                row.retracement.into(),
                row.warning_ts.into(),
                row.detected_at.into(),
                row.warning_kind.into(),
                row.entry_score.into(),
                row.entry_score_dims.into(),
                row.entry.into(),
                row.stop.into(),
                row.target.into(),
                row.risk.into(),
                row.rr.into(),
                row.state.into(),
                row.last_advance_ts.into(),
                row.trigger_ts.into(),
                row.trigger_bar_ts.into(),
                row.trigger_price.into(),
                row.trigger_score.into(),
                row.trigger_volume_ratio.into(),
                row.overshoot_r.into(),
                row.hold_score.into(),
                row.hold_score_history.into(),
                row.outcome.into(),
                row.exit_reason.into(),
                row.exit_ts.into(),
                row.exit_price.into(),
                row.r_multiple.into(),
                row.mfe_r.into(),
                row.mae_r.into(),
                row.created_at.into(),
                row.updated_at.into(),
                row.id.into(),
            ],
        ))
        .await
        .context("更新信号事件失败")?;
    if res.rows_affected() != 1 {
        return Err(anyhow!(
            "更新信号事件失败: 影响行数 {}",
            res.rows_affected()
        ));
    }
    Ok(())
}

/// 重建复盘数据时清空事件表。
pub async fn clear_pattern_events(db: &DatabaseConnection) -> Result<()> {
    pattern_events::Entity::delete_many()
        .exec(db)
        .await
        .context("清空信号事件失败")?;
    db.execute_unprepared("UPDATE sqlite_sequence SET seq = 0 WHERE name = 'pattern_events'")
        .await
        .context("重置信号事件编号失败")?;
    Ok(())
}

/// 删除历史上落盘的快速路径预警记录（2026-08-16 起不再生成该类型）。
pub async fn delete_fast_pattern_events(db: &DatabaseConnection) -> Result<u64> {
    let res = pattern_events::Entity::delete_many()
        .filter(pattern_events::Column::WarningKind.eq("fast"))
        .exec(db)
        .await
        .context("清理快速路径信号失败")?;
    Ok(res.rows_affected)
}

/// 按 id 删除单条信号事件（重复信号清理用）。
pub async fn delete_pattern_event(db: &DatabaseConnection, id: i64) -> Result<()> {
    let res = pattern_events::Entity::delete_by_id(id)
        .exec(db)
        .await
        .context("删除信号事件失败")?;
    if res.rows_affected != 1 {
        return Err(anyhow!("删除信号事件失败: 影响行数 {}", res.rows_affected));
    }
    Ok(())
}

/// 按 (symbol, direction, warning_ts) 查已有事件，避免同一预警K线重复建事件。
pub async fn pattern_event_by_warning(
    db: &DatabaseConnection,
    symbol: &str,
    direction: &str,
    warning_ts: &str,
) -> Result<Option<pattern_events::Model>> {
    Ok(pattern_events::Entity::find()
        .filter(pattern_events::Column::Symbol.eq(symbol))
        .filter(pattern_events::Column::Direction.eq(direction))
        .filter(pattern_events::Column::WarningTs.eq(warning_ts))
        .one(db)
        .await
        .context("查询信号事件失败")?)
}

/// 按品种 + 方向查所有未了结的 pending 事件，供上层按预警K线距离与入场价差
/// 做相似预警抑制。不再要求 s0/s1 同源或入场价完全相同。
pub async fn pending_pattern_events_by_symbol_direction(
    db: &DatabaseConnection,
    symbol: &str,
    direction: &str,
) -> Result<Vec<pattern_events::Model>> {
    Ok(pattern_events::Entity::find()
        .filter(pattern_events::Column::Symbol.eq(symbol))
        .filter(pattern_events::Column::Direction.eq(direction))
        .filter(pattern_events::Column::State.eq("pending"))
        .all(db)
        .await
        .context("查询待触发信号事件失败")?)
}

pub async fn all_settings(
    db: &DatabaseConnection,
) -> Result<std::collections::HashMap<String, String>> {
    let rows = settings::Entity::find()
        .all(db)
        .await
        .context("读取设置失败")?;
    Ok(rows.into_iter().map(|r| (r.key, r.value)).collect())
}

pub async fn get_setting(db: &DatabaseConnection, key: &str) -> Result<Option<String>> {
    let row = settings::Entity::find_by_id(key)
        .one(db)
        .await
        .context("读取设置失败")?;
    Ok(row.map(|r| r.value))
}

pub async fn set_settings(
    db: &DatabaseConnection,
    map: &std::collections::HashMap<String, String>,
) -> Result<()> {
    let rows: Vec<settings::ActiveModel> = map
        .iter()
        .map(|(key, value)| settings::ActiveModel {
            key: Set(key.clone()),
            value: Set(value.clone()),
        })
        .collect();
    if rows.is_empty() {
        return Ok(());
    }
    settings::Entity::insert_many(rows)
        .on_conflict(
            OnConflict::columns([settings::Column::Key])
                .update_columns([settings::Column::Value])
                .to_owned(),
        )
        .exec(db)
        .await
        .context("写入设置失败")?;
    Ok(())
}

/// 删除设置表中指定键（迁移配置到 JSON 后清理旧配置键用）。
pub async fn delete_settings(db: &DatabaseConnection, keys: &[String]) -> Result<()> {
    if keys.is_empty() {
        return Ok(());
    }
    settings::Entity::delete_many()
        .filter(settings::Column::Key.is_in(keys.iter().map(|s| s.as_str())))
        .exec(db)
        .await
        .context("删除设置键失败")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use sea_orm::DatabaseConnection;

    use super::*;

    async fn test_db() -> DatabaseConnection {
        crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .expect("in-memory db")
    }

    #[tokio::test]
    async fn upsert_and_query_klines() {
        let db = test_db().await;
        let row = |ts: &str, close: f64| klines::ActiveModel {
            symbol: Set("RB0".to_string()),
            timeframe: Set("5m".to_string()),
            ts: Set(ts.to_string()),
            open: Set(1.0),
            high: Set(2.0),
            low: Set(0.5),
            close: Set(close),
            volume: Set(10.0),
            hold: Set(100.0),
            source: Set("raw".to_string()),
        };
        upsert_klines(&db, vec![row("2026-08-03 09:00:00", 1.5)])
            .await
            .unwrap();
        // 相同主键再次 upsert，close 被覆盖
        upsert_klines(&db, vec![row("2026-08-03 09:00:00", 1.8)])
            .await
            .unwrap();
        upsert_klines(&db, vec![row("2026-08-03 09:05:00", 1.9)])
            .await
            .unwrap();

        let rows = klines(&db, "RB0", "5m", None, None).await.unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].close, 1.8);
        assert_eq!(rows[1].ts, "2026-08-03 09:05:00");

        let limited = klines(&db, "RB0", "5m", Some(1), None).await.unwrap();
        assert_eq!(limited.len(), 1);
        assert_eq!(limited[0].ts, "2026-08-03 09:05:00");
    }

    #[tokio::test]
    async fn pattern_events_roundtrip_and_dedup_by_warning() {
        let db = test_db().await;
        let row =
            |warning_ts: &str, state: &str, score: f64, kind: &str| pattern_events::ActiveModel {
                id: sea_orm::NotSet,
                symbol: Set("BU0".to_string()),
                direction: Set("up".to_string()),
                grade: Set("A级".to_string()),
                level: Set("fine".to_string()),
                s0_ts: Set("2026-08-14 09:15".to_string()),
                s0_price: Set(4128.0),
                s1_ts: Set("2026-08-14 09:30".to_string()),
                s1_price: Set(4150.0),
                s2_ts: Set("2026-08-14 09:45".to_string()),
                s2_price: Set(4137.0),
                a_move: Set(22.0),
                b_move: Set(13.0),
                a_bars: Set(1),
                b_bars: Set(1),
                retracement: Set(0.59),
                warning_ts: Set(warning_ts.to_string()),
                detected_at: Set(warning_ts.to_string()),
                warning_kind: Set(kind.to_string()),
                entry_score: Set(score),
                entry_score_dims: Set(r#"{"dim_a":3.8,"dim_b":3.4,"dim_warning":3.5}"#.to_string()),
                entry: Set(4162.0),
                stop: Set(4137.0),
                target: Set(4216.0),
                risk: Set(25.0),
                rr: Set(2.16),
                state: Set(state.to_string()),
                last_advance_ts: Set(None),
                trigger_ts: Set(None),
                trigger_bar_ts: Set(None),
                trigger_price: Set(None),
                trigger_score: Set(None),
                trigger_volume_ratio: Set(None),
                overshoot_r: Set(None),
                hold_score: Set(None),
                hold_score_history: Set("[]".to_string()),
                outcome: Set(None),
                exit_reason: Set(None),
                exit_ts: Set(None),
                exit_price: Set(None),
                r_multiple: Set(None),
                mfe_r: Set(None),
                mae_r: Set(None),
                created_at: Set("2026-08-14 11:30".to_string()),
                updated_at: Set("2026-08-14 11:30".to_string()),
            };

        let id = insert_pattern_event(&db, row("2026-08-14 11:30", "pending", 3.6, "wick"))
            .await
            .unwrap();
        assert_eq!(
            pattern_event_by_warning(&db, "BU0", "up", "2026-08-14 11:30")
                .await
                .unwrap()
                .unwrap()
                .id,
            id
        );
        let events = all_pattern_events(&db).await.unwrap();
        assert_eq!(events.len(), 1);
        assert!((events[0].entry_score - 3.6).abs() < 1e-9);

        let mut model = pattern_event_by_id(&db, id).await.unwrap().unwrap();
        model.state = "triggered".to_string();
        model.trigger_ts = Some("2026-08-14 13:45".to_string());
        model.trigger_price = Some(4162.0);
        model.hold_score = Some(3.8);
        model.updated_at = "2026-08-14 13:45".to_string();
        update_pattern_event(&db, model).await.unwrap();

        let updated = pattern_event_by_id(&db, id).await.unwrap().unwrap();
        assert_eq!(updated.state, "triggered");
        assert_eq!(updated.trigger_ts.as_deref(), Some("2026-08-14 13:45"));
        assert_eq!(updated.hold_score, Some(3.8));

        clear_pattern_events(&db).await.unwrap();
        assert!(all_pattern_events(&db).await.unwrap().is_empty());
        let next_id = insert_pattern_event(&db, row("2026-08-14 12:00", "pending", 3.6, "wick"))
            .await
            .unwrap();
        assert_eq!(next_id, 1);
    }

    #[tokio::test]
    async fn delete_fast_pattern_events_removes_only_fast_kind() {
        let db = test_db().await;
        let row = |warning_ts: &str, kind: &str| pattern_events::ActiveModel {
            id: sea_orm::NotSet,
            symbol: Set("BU0".to_string()),
            direction: Set("up".to_string()),
            grade: Set("A级".to_string()),
            level: Set("fine".to_string()),
            s0_ts: Set("2026-08-14 09:15".to_string()),
            s0_price: Set(4128.0),
            s1_ts: Set("2026-08-14 09:30".to_string()),
            s1_price: Set(4150.0),
            s2_ts: Set("2026-08-14 09:45".to_string()),
            s2_price: Set(4137.0),
            a_move: Set(22.0),
            b_move: Set(13.0),
            a_bars: Set(1),
            b_bars: Set(1),
            retracement: Set(0.59),
            warning_ts: Set(warning_ts.to_string()),
            detected_at: Set(warning_ts.to_string()),
            warning_kind: Set(kind.to_string()),
            entry_score: Set(3.6),
            entry_score_dims: Set(r#"{"dim_a":3.8,"dim_b":3.4,"dim_warning":3.5}"#.to_string()),
            entry: Set(4162.0),
            stop: Set(4137.0),
            target: Set(4216.0),
            risk: Set(25.0),
            rr: Set(2.16),
            state: Set("pending".to_string()),
            last_advance_ts: Set(None),
            trigger_ts: Set(None),
            trigger_bar_ts: Set(None),
            trigger_price: Set(None),
            trigger_score: Set(None),
            trigger_volume_ratio: Set(None),
            overshoot_r: Set(None),
            hold_score: Set(None),
            hold_score_history: Set("[]".to_string()),
            outcome: Set(None),
            exit_reason: Set(None),
            exit_ts: Set(None),
            exit_price: Set(None),
            r_multiple: Set(None),
            mfe_r: Set(None),
            mae_r: Set(None),
            created_at: Set("2026-08-14 11:30".to_string()),
            updated_at: Set("2026-08-14 11:30".to_string()),
        };

        insert_pattern_event(&db, row("2026-08-14 11:30", "fast"))
            .await
            .unwrap();
        insert_pattern_event(&db, row("2026-08-14 12:00", "strong"))
            .await
            .unwrap();

        assert_eq!(delete_fast_pattern_events(&db).await.unwrap(), 1);
        let events = all_pattern_events(&db).await.unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].warning_kind, "strong");
    }

    #[tokio::test]
    async fn pending_pattern_events_by_symbol_direction_lists_pending() {
        let db = test_db().await;
        let row = |warning_ts: &str, entry: f64, state: &str| pattern_events::ActiveModel {
            id: sea_orm::NotSet,
            symbol: Set("UR0".to_string()),
            direction: Set("up".to_string()),
            grade: Set("C级".to_string()),
            level: Set("fine".to_string()),
            s0_ts: Set("2026-08-13 09:30".to_string()),
            s0_price: Set(1660.0),
            s1_ts: Set("2026-08-13 13:45".to_string()),
            s1_price: Set(1685.0),
            s2_ts: Set("2026-08-13 14:00".to_string()),
            s2_price: Set(1668.0),
            a_move: Set(25.0),
            b_move: Set(17.0),
            a_bars: Set(4),
            b_bars: Set(2),
            retracement: Set(0.68),
            warning_ts: Set(warning_ts.to_string()),
            detected_at: Set(warning_ts.to_string()),
            warning_kind: Set("wick".to_string()),
            entry_score: Set(2.9),
            entry_score_dims: Set(r#"{"dim_a":3.2,"dim_b":2.8,"dim_warning":3.4}"#.to_string()),
            entry: Set(entry),
            stop: Set(1658.0),
            target: Set(1685.0),
            risk: Set(17.0),
            rr: Set(1.0),
            state: Set(state.to_string()),
            last_advance_ts: Set(None),
            trigger_ts: Set(None),
            trigger_bar_ts: Set(None),
            trigger_price: Set(None),
            trigger_score: Set(None),
            trigger_volume_ratio: Set(None),
            overshoot_r: Set(None),
            hold_score: Set(None),
            hold_score_history: Set("[]".to_string()),
            outcome: Set(None),
            exit_reason: Set(None),
            exit_ts: Set(None),
            exit_price: Set(None),
            r_multiple: Set(None),
            mfe_r: Set(None),
            mae_r: Set(None),
            created_at: Set(warning_ts.to_string()),
            updated_at: Set(warning_ts.to_string()),
        };

        let first = insert_pattern_event(&db, row("2026-08-13 09:30", 1675.0, "pending"))
            .await
            .unwrap();
        let second = insert_pattern_event(&db, row("2026-08-13 09:45", 1676.0, "pending"))
            .await
            .unwrap();
        insert_pattern_event(&db, row("2026-08-13 10:00", 1676.0, "triggered"))
            .await
            .unwrap();
        insert_pattern_event(&db, row("2026-08-13 10:15", 1676.0, "down"))
            .await
            .unwrap();

        let pending = pending_pattern_events_by_symbol_direction(&db, "UR0", "up")
            .await
            .unwrap();
        let ids: Vec<i64> = pending.iter().map(|e| e.id).collect();
        assert!(ids.contains(&first));
        assert!(ids.contains(&second));

        // 已离开 pending 的事件不再参与相似预警抑制
        let mut model = pattern_event_by_id(&db, first).await.unwrap().unwrap();
        model.state = "triggered".to_string();
        model.trigger_ts = Some("2026-08-14 10:00".to_string());
        update_pattern_event(&db, model).await.unwrap();
        let pending = pending_pattern_events_by_symbol_direction(&db, "UR0", "up")
            .await
            .unwrap();
        assert!(!pending.iter().any(|e| e.id == first));
    }

    #[tokio::test]
    async fn settings_roundtrip() {
        let db = test_db().await;
        let mut map = std::collections::HashMap::new();
        map.insert("a".to_string(), "1".to_string());
        map.insert("b".to_string(), "2".to_string());
        set_settings(&db, &map).await.unwrap();
        map.insert("a".to_string(), "9".to_string());
        set_settings(&db, &map).await.unwrap();
        let all = all_settings(&db).await.unwrap();
        assert_eq!(all.get("a").map(String::as_str), Some("9"));
        assert_eq!(all.get("b").map(String::as_str), Some("2"));
    }

    #[tokio::test]
    async fn rollovers_roundtrip_and_delete() {
        let db = test_db().await;
        let row = |from: &str, to: &str, confirmed: bool| rollovers::ActiveModel {
            symbol: Set("BU0".to_string()),
            ts: Set("2026-08-05 21:05:00".to_string()),
            from_contract: Set(from.to_string()),
            to_contract: Set(to.to_string()),
            confirmed: Set(confirmed),
            created_at: Set("2026-08-05 21:10:00".to_string()),
            updated_at: Set("2026-08-05 21:10:00".to_string()),
        };
        upsert_rollovers(
            &db,
            vec![
                row("BU2609", "BU2610", false),
                row("BU2609", "BU2610", true),
            ],
        )
        .await
        .unwrap();

        let rows = symbol_rollovers(&db, "BU0").await.unwrap();
        assert_eq!(rows.len(), 1);
        assert!(rows[0].confirmed);
        assert_eq!(rows[0].from_contract, "BU2609");

        let all = all_rollovers(&db).await.unwrap();
        assert_eq!(all.len(), 1);
        assert_eq!(all[0].symbol, "BU0");

        delete_symbol_rollovers(&db, "BU0").await.unwrap();
        assert!(symbol_rollovers(&db, "BU0").await.unwrap().is_empty());
    }
}
