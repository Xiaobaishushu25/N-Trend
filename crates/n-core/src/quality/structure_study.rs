//! Frozen-cohort descriptive study and a v2 review task; no parameter fitting or trade changes.
use super::{
    hash, qualification,
    research::{write_json, AuditRow},
    structure, Rules,
};
use anyhow::{ensure, Context, Result};
use sea_orm::Database;
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap},
    path::{Path, PathBuf},
};

struct Case {
    row: AuditRow,
    payload: Value,
    evidence: Value,
    risk_ticks: Option<f64>,
}
fn metric(c: &Case, path: &str) -> Option<f64> {
    let mut v = &c.evidence["modules"];
    let (module, rest) = path.split_once('.').unwrap();
    v = &v[module]["metrics"];
    for key in rest.split('.') {
        v = &v[key];
    }
    v.as_f64().filter(|x| x.is_finite())
}
// A decimal JSON roundtrip can move an ATR-normalized float by an ulp. The
// underlying market-input hash must still match exactly; only presentation floats
// receive an absolute tolerance, far below a price tick in this payload.
fn payload_equal(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => x
            .as_f64()
            .zip(y.as_f64())
            .is_some_and(|(x, y)| (x - y).abs() <= 1e-10),
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(x, y)| payload_equal(x, y))
        }
        _ => a == b,
    }
}
const METRICS: &[&str] = &[
    "trend_ma.ma20_slope_pre",
    "trend_ma.ma20_slope_a",
    "trend_ma.ma_residence",
    "trend_ma.warning_close_ma_atr",
    "trend_ma.b_ma_crosses",
    "a_process.full.forward_efficiency",
    "a_process.interior.forward_efficiency",
    "a_process.directional_body.largest_share",
    "a_process.closing_records.largest_share",
    "a_process.directional_body.effective_bars",
    "a_process.bars",
    "b_process.bars_without_warning",
    "b_process.reverse_body_bars",
    "b_process.reverse_close_steps",
    "b_process.longest_reverse_close_run",
    "b_process.retracement_of_a_before_warning",
    "b_process.signed_efficiency_from_s1",
    "b_process.max_close_retreat_from_s1_close_atr",
    "b_process.max_range_retreat_from_s1_close_atr",
    "b_process.outside_s1_before_warning_atr",
    "levels.nearest_forward_obstacle_r",
    "warning.s1.opposing_wick_fraction",
    "warning.warning.range_atr",
    "space.rr_to_s1",
];
fn stats(cases: &[&Case], scenario: &str) -> Value {
    let mut statuses = BTreeMap::<String, usize>::new();
    let mut closed = Vec::new();
    let mut entries = 0;
    let mut unpriced = 0;
    for c in cases {
        let e = c.row.evaluations.get(scenario).unwrap_or(&Value::Null);
        let status = e["status"].as_str().unwrap_or("not_evaluated");
        *statuses.entry(status.into()).or_default() += 1;
        if e["entry_ts"].as_str().is_some() {
            entries += 1;
        }
        if status == "closed" {
            if let Some(r) = e["net_r"].as_f64() {
                closed.push((e["exit_ts"].as_str().unwrap_or(""), c.row.event_id, r));
            } else {
                unpriced += 1;
            }
        }
    }
    closed.sort_by(|a, b| a.0.cmp(b.0).then(a.1.cmp(&b.1)));
    let n = closed.len();
    let total = closed.iter().map(|x| x.2).sum::<f64>();
    let wins = closed.iter().filter(|x| x.2 > 0.).count();
    let mut equity = 0_f64;
    let mut peak = 0_f64;
    let mut dd = 0_f64;
    for (_, _, r) in &closed {
        equity += r;
        peak = peak.max(equity);
        dd = dd.max(peak - equity);
    }
    let mean_side = |win: bool| {
        let xs: Vec<_> = closed
            .iter()
            .filter(|x| if win { x.2 > 0. } else { x.2 < 0. })
            .map(|x| x.2)
            .collect();
        (!xs.is_empty()).then(|| xs.iter().sum::<f64>() / xs.len() as f64)
    };
    json!({"candidates":cases.len(),"statuses":statuses,"entries":entries,"closed_priced":n,"closed_unpriced":unpriced,"net_wins":wins,
        "priced_fraction":if cases.is_empty(){None}else{Some(n as f64/cases.len()as f64)},"win_rate":if n==0{None}else{Some(wins as f64/n as f64)},
        "mean_net_r":if n==0{None}else{Some(total/n as f64)},"total_net_r":total,"average_win_r":mean_side(true),"average_loss_r":mean_side(false),"closed_trade_drawdown_r":dd,
        "not_closed_or_unpriced":cases.len()-n})
}
fn both(cases: &[&Case]) -> Value {
    json!({"one_tick":stats(cases,"fixed_one_1tick"),"two_tick":stats(cases,"fixed_one_2tick")})
}
fn nonoverlap<'a>(cases: &[&'a Case]) -> Vec<&'a Case> {
    let mut ordered = cases.to_vec();
    ordered.sort_by(|a, b| {
        a.row
            .warning_ts
            .cmp(&b.row.warning_ts)
            .then(a.row.event_id.cmp(&b.row.event_id))
    });
    let mut end = HashMap::<&str, &str>::new();
    let mut selected = Vec::new();
    for c in ordered {
        if end
            .get(c.row.symbol.as_str())
            .is_some_and(|e| *e >= c.row.s0_ts.as_str())
        {
            continue;
        }
        let last = [
            c.row.warning_ts.as_str(),
            c.row.evaluations["fixed_one_1tick"]["exit_ts"]
                .as_str()
                .unwrap_or(""),
            c.row.evaluations["fixed_one_2tick"]["exit_ts"]
                .as_str()
                .unwrap_or(""),
        ]
        .into_iter()
        .max()
        .unwrap();
        end.insert(&c.row.symbol, last);
        selected.push(c);
    }
    selected
}
fn corr(xs: &[(f64, f64)]) -> Option<f64> {
    if xs.len() < 3 {
        return None;
    }
    let n = xs.len() as f64;
    let a = xs.iter().map(|x| x.0).sum::<f64>() / n;
    let b = xs.iter().map(|x| x.1).sum::<f64>() / n;
    let aa = xs.iter().map(|x| (x.0 - a).powi(2)).sum::<f64>();
    let bb = xs.iter().map(|x| (x.1 - b).powi(2)).sum::<f64>();
    (aa * bb > 0.).then(|| xs.iter().map(|x| (x.0 - a) * (x.1 - b)).sum::<f64>() / (aa * bb).sqrt())
}
fn cuts(cases: &[&Case], key: &str) -> Vec<f64> {
    let mut xs: Vec<_> = cases.iter().filter_map(|c| metric(c, key)).collect();
    xs.sort_by(f64::total_cmp);
    if xs.is_empty() {
        return Vec::new();
    }
    let mut out: Vec<_> = (1..5).map(|i| xs[(xs.len() - 1) * i / 5]).collect();
    out.dedup();
    out
}
fn grouped(cases: &[&Case], key: &str, bounds: &[f64]) -> Value {
    let mut bins = vec![Vec::new(); bounds.len() + 1];
    let mut missing = Vec::new();
    for c in cases {
        if let Some(x) = metric(c, key) {
            let i = bounds.iter().position(|q| x <= *q).unwrap_or(bounds.len());
            bins[i].push(*c);
        } else {
            missing.push(*c);
        }
    }
    json!({"bounds_inclusive_upper":bounds,"bins":bins.iter().enumerate().map(|(i,rs)|json!({"bin":i+1,"lower_exclusive":i.checked_sub(1).map(|j|bounds[j]),"upper_inclusive":bounds.get(i),"stats":both(rs)})).collect::<Vec<_>>(),"missing":both(&missing)})
}
fn ablation_score(c: &Case, removed: &str, rules: &Rules) -> Option<f64> {
    let q = &c.row.quality;
    let parts = [q.trend_score?, q.a_score?, q.b_score?];
    let skip = match removed {
        "trend_ma" => Some(0),
        "a_process" => Some(1),
        "b_process" => Some(2),
        _ => None,
    };
    let m = if let Some(skip) = skip {
        parts
            .iter()
            .zip(rules.morphology_weights)
            .enumerate()
            .filter(|(i, _)| *i != skip)
            .map(|(_, (x, w))| x * w)
            .sum::<f64>()
            / (1. - rules.morphology_weights[skip])
    } else {
        q.morphology_score?
    };
    Some(if removed == "space" {
        m
    } else {
        rules.quality_weights[0] * m + rules.quality_weights[1] * q.space_score?
    })
}
fn ablations(cases: &[&Case], rules: &Rules) -> Value {
    let mut result = serde_json::Map::new();
    for removed in ["none", "trend_ma", "a_process", "b_process", "space"] {
        let mut ranked: Vec<_> = cases
            .iter()
            .filter_map(|c| ablation_score(c, removed, rules).map(|s| (*c, s)))
            .collect();
        ranked.sort_by(|a, b| {
            b.1.total_cmp(&a.1)
                .then(a.0.row.event_id.cmp(&b.0.row.event_id))
        });
        let n = (ranked.len() / 5).max(1).min(ranked.len());
        let threshold = n.checked_sub(1).map(|i| ranked[i].1);
        let top: Vec<_> = ranked
            .iter()
            .filter(|(_, s)| threshold.is_some_and(|t| *s >= t))
            .map(|(c, _)| *c)
            .collect();
        result.insert(removed.into(),json!({"threshold":threshold,"ties_kept":true,"stats":both(&top),"event_ids":top.iter().map(|c|c.row.event_id).collect::<Vec<_>>()}));
    }
    Value::Object(result)
}

fn summarize(cases: &[Case], rules: &Rules) -> Value {
    let all: Vec<_> = cases.iter().collect();
    let basic: Vec<_> = all
        .iter()
        .copied()
        .filter(|c| c.row.old_score >= 2.5 && c.risk_ticks.is_some_and(|t| t + 1e-6 >= 10.))
        .collect();
    let cohorts = [
        ("all", all.clone()),
        ("all_nonoverlap", nonoverlap(&all)),
        ("basic_necessary", basic.clone()),
        ("basic_necessary_nonoverlap", nonoverlap(&basic)),
    ];
    let mut cohort_stats = serde_json::Map::new();
    for (key, rs) in &cohorts {
        cohort_stats.insert((*key).into(),json!({"stats":both(rs),"event_ids":rs.iter().map(|c|c.row.event_id).collect::<Vec<_>>(),"v1_fixed_weight_ablation":ablations(rs,rules)}));
    }
    let mut strata = BTreeMap::<String, Vec<&Case>>::new();
    for c in &all {
        strata
            .entry(format!("symbol:{}", c.row.symbol))
            .or_default()
            .push(c);
        strata
            .entry(format!("month:{}", &c.row.warning_ts[..7]))
            .or_default()
            .push(c);
        let bars = metric(c, "a_process.bars").unwrap_or(0.);
        let key = if bars < 6. {
            "a_length:under6"
        } else if bars <= 15. {
            "a_length:6to15"
        } else {
            "a_length:over15"
        };
        strata.entry(key.into()).or_default().push(c);
        let b = metric(c, "b_process.bars_without_warning").unwrap_or(0.);
        let key = if b <= 1. {
            "b_length:0or1"
        } else if b <= 5. {
            "b_length:2to5"
        } else {
            "b_length:over5"
        };
        strata.entry(key.into()).or_default().push(c);
    }
    let mut groups = serde_json::Map::new();
    for key in METRICS {
        let bounds = cuts(&all, key);
        let mut by_cohort = serde_json::Map::new();
        for (name, rs) in &cohorts {
            by_cohort.insert((*name).into(), grouped(rs, key, &bounds));
        }
        let mut by_strata = serde_json::Map::new();
        for (name, rs) in &strata {
            if rs.len() >= 20 {
                by_strata.insert(name.clone(), grouped(rs, key, &bounds));
            }
        }
        let correlation = |other| {
            corr(
                &all.iter()
                    .filter_map(|c| metric(c, key).zip(metric(c, other)))
                    .collect::<Vec<_>>(),
            )
        };
        groups.insert((*key).into(),json!({"cohorts":by_cohort,"strata_min20":by_strata,"correlation_with_a_bars":correlation("a_process.bars"),"correlation_with_b_bars":correlation("b_process.bars_without_warning")}));
    }
    json!({"version":structure::VERSION,"cohorts":cohort_stats,"metric_groups":groups,
        "strata_baselines":strata.into_iter().map(|(key,rs)|(key,both(&rs))).collect::<BTreeMap<_,_>>(),
        "interpretation":"Historical development, descriptive groups only. Quintile cuts use features from the full frozen cohort, preserve ties and unknowns; no outcome-fitted thresholds. Small strata and correlated tests cannot establish causality. Module display switches are not statistical ablation. V1 ablations remove only existing fixed score components; v2 levels/endpoint evidence has no score to remove.",
        "nonoverlap_policy":"Earliest warning then event id, per symbol; reject structures whose S0 is at/before prior selected max(warning, scenario1 exit, scenario2 exit). Select once per cohort before metric grouping, not separately per bin. Conservative retrospective overlap control, not a portfolio backtest.",
        "execution":"Reuse frozen fixed-one-lot 5m conservative replay, 1/2 tick slippage and 2.5 tick roundtrip proxy fee; small-size 1R target, not S1 hit rate. Basic subset applies only old score>=2.5 and original risk_ticks+1e-6>=10 (same numerical tolerance as replay), not full account admission. Unknown/nonexecuted records are not losses."})
}
fn f(v: &Value) -> String {
    v.as_f64()
        .map(|n| format!("{n:.3}"))
        .unwrap_or_else(|| "—".into())
}
fn percent(v: &Value) -> String {
    v.as_f64()
        .map(|n| format!("{:.1}%", n * 100.))
        .unwrap_or_else(|| "—".into())
}
fn report(summary: &Value, task: &Value) -> String {
    let mut md=String::from("# 模块化结构诊断v2与历史分组\n\n本次先恢复A段方向效率与旧过程证据，再增加B段回撤尝试，保留趋势、MA20、前高前低、端点预警和空间。六模块独立存储，不新增评分、合格阈值或交易门槛。\n\n## 已有20例对照\n\n原始判断与看解读记录按v1原样归档，新版复核单独记账，不自动迁移为v2判断。表中的“内部顺向效率”是完整内部窗口，不使用短A回退。\n\n|新例|原形态 / 综合|完整 / 内部顺向效率|预警前B根数|反向实体 / 收盘步数|从S1极值回撤ATR|超出S1整根ATR|\n|---|---|---|---:|---|---:|---:|\n");
    for (i, id) in task["new_queue"].as_array().unwrap().iter().enumerate() {
        let c = &task["cases"][id.as_str().unwrap()];
        let m = &c["diagnostics"]["modules"];
        let a = &m["a_process"]["metrics"];
        let b = &m["b_process"]["metrics"];
        let l = task["previous_case_assessments"]
            .as_array()
            .unwrap()
            .iter()
            .find(|l| l["case_id"] == *id);
        let label = |key: &str| l.and_then(|l| l[key].as_str()).unwrap_or("未标");
        md += &format!(
            "|{}|{} / {}|{} / {}|{}|{} / {}|{}|{}|\n",
            i + 1,
            label("morphology_qualification"),
            label("opportunity_qualification"),
            percent(&a["full"]["forward_efficiency"]),
            percent(&a["interior"]["forward_efficiency"]),
            b["bars_without_warning"],
            b["reverse_body_bars"],
            b["reverse_close_steps"],
            f(&b["max_retreat_from_s1_extreme_atr"]),
            f(&b["outside_s1_before_warning_atr"])
        );
    }
    md+="\n第5例内部逆行以带方向效率为负、顺向效率0表达，零路程仍未知。第8例在S1范围内有5次反向收盘推进；第10例预警前有2根B但仅1次反向推进，不能概括成连续多根反向，新增收盘回撤纪录也应另看；不以出范围或固定根数替代回撤过程。端点长影线只观测，不新增扣分。原标签是研究输入，不是盈利正确答案。\n\n## 固定候选集合的历史基线\n\n|集合|候选|完成可定价|净胜率|平均净R|2 tick平均净R|\n|---|---:|---:|---:|---:|---:|\n";
    for (key, label) in [
        ("all", "完整诊断"),
        ("all_nonoverlap", "完整诊断去重叠"),
        ("basic_necessary", "旧分与风险必要条件"),
        ("basic_necessary_nonoverlap", "必要条件去重叠"),
    ] {
        let x = &summary["cohorts"][key]["stats"];
        let s = &x["one_tick"];
        md += &format!(
            "|{}|{}|{}|{}|{}|{}|\n",
            label,
            s["candidates"],
            s["closed_priced"],
            percent(&s["win_rate"]),
            f(&s["mean_net_r"]),
            f(&x["two_tick"]["mean_net_r"])
        );
    }
    md+="\n必要条件仅旧分≥2.5且初始风险≥10 tick（风险比较沿用回放的1e-6数值容差），不等同完整交易准入。未触发、失效、未成交、路径不明与缺数据单列在summary.json；净胜率分母只含完成且可定价记录。使用原冻结5分钟一手回放与研究费用，1R执行目标不等于到S1命中。去重叠只是保守的历史重复控制，不是组合账户回测。\n\n## 无拟合分组（必要条件且去重叠）\n\n分组边界来自全体完整样本的特征分位数，相同值不拆开；各集合、品种、月份和根数层使用同一边界。空组和缺失保留，不筛选更好看的组。\n\n|指标|组|上界|候选|完成可定价|净胜率|平均净R|2 tick平均净R|\n|---|---:|---:|---:|---:|---:|---:|---:|\n";
    for key in [
        "a_process.interior.forward_efficiency",
        "a_process.closing_records.largest_share",
        "b_process.reverse_close_steps",
        "b_process.signed_efficiency_from_s1",
        "trend_ma.ma20_slope_a",
        "levels.nearest_forward_obstacle_r",
        "warning.s1.opposing_wick_fraction",
    ] {
        for b in summary["metric_groups"][key]["cohorts"]["basic_necessary_nonoverlap"]["bins"]
            .as_array()
            .unwrap()
        {
            let s = &b["stats"]["one_tick"];
            md += &format!(
                "|{}|{}|{}|{}|{}|{}|{}|{}|\n",
                key,
                b["bin"],
                f(&b["upper_inclusive"]),
                s["candidates"],
                s["closed_priced"],
                percent(&s["win_rate"]),
                f(&s["mean_net_r"]),
                f(&b["stats"]["two_tick"]["mean_net_r"])
            );
        }
    }
    md+="\n## 现有v1固定权重消融（必要条件且去重叠）\n\n只移除既有趋势/A/B/空间分项，剩余权重按原规则归一，取前20%且保留同分，不调权重。前高前低与端点在v2还没有评分，不能把其分组冒充删模块消融。\n\n|移除项|选中候选|完成可定价|净胜率|平均净R|2 tick平均净R|\n|---|---:|---:|---:|---:|---:|\n";
    for key in ["none", "trend_ma", "a_process", "b_process", "space"] {
        let x = &summary["cohorts"]["basic_necessary_nonoverlap"]["v1_fixed_weight_ablation"][key]
            ["stats"];
        let s = &x["one_tick"];
        md += &format!(
            "|{}|{}|{}|{}|{}|{}|\n",
            key,
            s["candidates"],
            s["closed_priced"],
            percent(&s["win_rate"]),
            f(&s["mean_net_r"]),
            f(&x["two_tick"]["mean_net_r"])
        );
    }
    md+="\n## 解释边界\n\n历史已参与开发，不是未来未见验证；本次不据分组结果定阈值。大量相关分组、小样本、品种与根数混杂均限制结论。summary.json保留24个指标的全样本/必要条件/去重叠对照、至少20例的品种月份与A/B根数层、缺失组及根数相关性；这仍不能证明独立增量收益。前高前低使用S0前40根窗口极值与左右各2根严格拐点，并在S0之前确认，不冒充完整关键位或人工区域。趋势与MA20继续保留，不因为本轮标签少提就删除。\n";
    md
}

pub async fn run(args: &[String], out: &Path) -> Result<()> {
    let arg = |key: &str| {
        args.windows(2)
            .find(|p| p[0] == key)
            .map(|p| PathBuf::from(&p[1]))
            .with_context(|| format!("{key} required"))
    };
    let task_path = arg("--task")?;
    let previous = qualification::load_task(&task_path)?;
    ensure!(
        previous["version"] == qualification::VERSION,
        "v2 study starts from a frozen v1 task"
    );
    let labels_path = arg("--labels")?;
    let labels: Value = serde_json::from_slice(&std::fs::read(&labels_path)?)?;
    qualification::validate_assessments(&previous, &labels)?;
    let parent = task_path.parent().context("task parent")?;
    // The legacy paired labels and references must be checked too, not only new assessments.
    let validation = out.join("input-validation");
    std::fs::create_dir(&validation)?;
    super::blind::import(
        &[
            "--manifest".into(),
            parent.join("blind-manifest.json").to_string_lossy().into(),
            "--labels".into(),
            labels_path.to_string_lossy().into(),
        ],
        &validation,
    )?;
    let audit_path = arg("--audit")?;
    let audit_raw = std::fs::read(&audit_path)?;
    let audits: Vec<AuditRow> = serde_json::from_slice(&audit_raw)?;
    let source_path = arg("--source-manifest")?;
    let source_raw = std::fs::read(&source_path)?;
    let source: Value = serde_json::from_slice(&source_raw)?;
    let rules: Rules = serde_json::from_value(source["rules"].clone())?;
    rules.validate()?;
    ensure!(
        source["parameter_hash"] == rules.hash(),
        "source rules hash mismatch"
    );
    ensure!(
        source["cost"]["roundtrip_cost_ticks"] == 2.5
            && source["cost"]["slippage_ticks"] == json!([1, 2]),
        "unsupported frozen execution assumptions"
    );
    let db_path = arg("--db")?;
    let db_hash = hash(&std::fs::read(&db_path)?);
    ensure!(
        source["database_sha256"] == db_hash,
        "snapshot hash mismatch"
    );
    let db = Database::connect(format!(
        "sqlite://{}?mode=ro",
        db_path.to_string_lossy().replace('\\', "/")
    ))
    .await?;
    let events = crate::storage::repo::all_pattern_events(&db).await?;
    let symbols = crate::storage::repo::list_symbols(&db, false).await?;
    ensure!(
        audits.len() == events.len(),
        "audit event coverage mismatch"
    );
    let by_id: HashMap<_, _> = events.iter().map(|e| (e.id, e)).collect();
    ensure!(by_id.len() == events.len(), "duplicate source event");
    let mut cases = Vec::new();
    let mut status = BTreeMap::<String, usize>::new();
    let mut by_symbol = BTreeMap::<&str, Vec<&AuditRow>>::new();
    let mut ids = std::collections::HashSet::new();
    for row in &audits {
        ensure!(ids.insert(row.event_id), "duplicate audit event");
        let event = by_id.get(&row.event_id).context("audit event missing")?;
        ensure!(
            super::event_key(event) == row.quality.event_key,
            "audit event identity mismatch"
        );
        ensure!(
            row.symbol == event.symbol
                && row.direction == event.direction
                && row.warning_ts == event.warning_ts
                && row.s0_ts == event.s0_ts
                && row.s1_ts == event.s1_ts
                && (row.old_score - event.entry_score).abs() < 1e-9,
            "audit metadata mismatch for {}",
            row.event_id
        );
        *status.entry(row.quality.status.clone()).or_default() += 1;
        if row.quality.status == "ok" {
            by_symbol.entry(&row.symbol).or_default().push(row);
        }
    }
    for (symbol, rows) in by_symbol {
        let bars = super::research::bars_from_rows(
            &crate::storage::repo::klines(&db, symbol, "15m", None, None).await?,
            &crate::storage::repo::symbol_rollovers(&db, symbol).await?,
        );
        let sym = symbols
            .iter()
            .find(|s| s.code == symbol)
            .context("missing symbol metadata")?;
        let tick = crate::precision::effective_tick(sym.tick_size, &sym.code, &sym.variety);
        for row in rows {
            let event = by_id[&row.event_id];
            let fresh = super::evaluate(
                event,
                &bars,
                tick,
                &row.warning_ts,
                "historical_recompute",
                &rules,
            );
            ensure!(
                fresh.status == "ok"
                    && fresh.input_hash == row.quality.input_hash
                    && fresh.parameter_hash == row.quality.parameter_hash,
                "audit input mismatch for {}",
                row.event_id
            );
            ensure!(
                fresh.features.len() == row.quality.features.len()
                    && fresh
                        .features
                        .iter()
                        .all(|(k, v)| match (v, row.quality.features.get(k)) {
                            (Some(x), Some(Some(y))) => (x - y).abs() < 1e-9,
                            (None, Some(None)) => true,
                            _ => false,
                        }),
                "frozen feature mismatch for {}",
                row.event_id
            );
            for (name, x, y) in [
                ("trend", fresh.trend_score, row.quality.trend_score),
                ("a", fresh.a_score, row.quality.a_score),
                ("b", fresh.b_score, row.quality.b_score),
                ("space", fresh.space_score, row.quality.space_score),
                (
                    "morphology",
                    fresh.morphology_score,
                    row.quality.morphology_score,
                ),
                ("quality", fresh.quality_score, row.quality.quality_score),
            ] {
                ensure!(
                    x.zip(y).is_some_and(|(x, y)| (x - y).abs() < 1e-9),
                    "frozen {name} score mismatch for {}",
                    row.event_id
                );
            }
            let payload = super::blind::case(event, &bars, &fresh, &rules);
            let warning = payload["bars"].as_array().unwrap().len() - 1;
            let tick_atr = tick / fresh.features["atr_s1"].unwrap();
            let evidence = structure::diagnose(&payload, warning, Some(row), Some(tick_atr))?;
            cases.push(Case {
                row: row.clone(),
                risk_ticks: Some((event.entry - event.stop).abs() / tick),
                payload,
                evidence,
            });
        }
    }
    db.close().await?;
    cases.sort_by(|a, b| {
        a.row
            .warning_ts
            .cmp(&b.row.warning_ts)
            .then(a.row.event_id.cmp(&b.row.event_id))
    });
    let by_case: HashMap<_, _> = cases
        .iter()
        .map(|c| (c.payload["case_id"].as_str().unwrap(), c))
        .collect();
    let mut task = previous.clone();
    for (id, c) in task["cases"].as_object_mut().context("task cases")? {
        let full = by_case
            .get(id.as_str())
            .context("review case not in frozen full cohort")?;
        ensure!(
            full.payload["input_hash"] == c["input_hash"]
                && payload_equal(&full.payload["bars"], &c["bars"])
                && payload_equal(&full.payload["points"], &c["points"]),
            "review payload mismatch for {id}"
        );
        c["diagnostics"] = full.evidence.clone();
    }
    task["version"] = json!(structure::VERSION);
    task["display_version"] = json!(structure::DISPLAY);
    task["previous_task_hash"] = previous["task_hash"].clone();
    task["previous_case_assessments"] = labels["case_assessments"].clone();
    task["previous_case_drafts"] = labels["case_drafts"].clone();
    task["legacy_labels"] = labels["labels"].clone();
    task["source_feedback_sha256"] = json!(hash(&std::fs::read(&labels_path)?));
    task["method"]=json!("Same frozen 60 development cases; modular v2 reinterpretation, not a new blind sample. V1 assessments are readonly history; v2 assessments start empty. No scores or outcomes in review payload.");
    qualification::seal(&mut task)?;
    write_json(&out.join("qualification-task.json"), &task)?;
    for name in ["blind-manifest.json", "assistant-notes.json"] {
        std::fs::copy(parent.join(name), out.join(name))?;
    }
    std::fs::copy(&task_path, out.join("input-v1-task.json"))?;
    std::fs::copy(&labels_path, out.join("input-v1-labels.json"))?;
    let safe = serde_json::to_string(&task)?
        .replace('<', "\\u003c")
        .replace('>', "\\u003e")
        .replace('&', "\\u0026");
    std::fs::write(
        out.join("qualification-review.html"),
        include_str!("qualification-review.html").replace("__QUALIFICATION_PAYLOAD__", &safe),
    )?;
    let summary = summarize(&cases, &rules);
    write_json(&out.join("summary.json"), &summary)?;
    write_json(&out.join("evidence.json"),&cases.iter().map(|c|json!({"event_id":c.row.event_id,"symbol":c.row.symbol,"direction":c.row.direction,"warning_ts":c.row.warning_ts,"input_hash":c.row.quality.input_hash,"evidence":c.evidence})).collect::<Vec<_>>())?;
    let md = report(&summary, &task);
    std::fs::write(out.join("report.md"), &md)?;
    let escaped = md
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;");
    std::fs::write(out.join("report.html"),format!("<!doctype html><meta charset=\"utf-8\"><title>结构诊断v2研究</title><style>body{{font:15px/1.7 system-ui;max-width:1250px;margin:30px auto;padding:20px;color:#23324a}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}</style><p><a href=\"qualification-review.html\">打开模块化评价页</a> · <a href=\"summary.json\">完整历史统计</a></p><pre>{escaped}</pre>"))?;
    write_json(
        &out.join("manifest.json"),
        &json!({"version":structure::VERSION,"display_version":structure::DISPLAY,"created_at":chrono::Utc::now().to_rfc3339(),"source_database_sha256":db_hash,"source_manifest_sha256":hash(&source_raw),"source_audit_sha256":hash(&audit_raw),"source_feedback_sha256":task["source_feedback_sha256"],"source_task_hash":previous["task_hash"],"task_hash":task["task_hash"],"modules":structure::MODULES,"source_status_coverage":status,"diagnosed_cases":cases.len(),"source_rows":audits.len(),"recomputed_hashes_and_legacy_features_verified":cases.len(),"review_cases":task["cases"].as_object().unwrap().len(),"rules_unchanged":true,"source_database_readonly":true,"new_module_scores":false,"future_validation":false,"cost":source["cost"],"source_code_sha256":hash(&[include_bytes!("structure.rs").as_slice(),include_bytes!("structure_study.rs").as_slice(),include_bytes!("qualification-review.html").as_slice()].concat())}),
    )?;
    println!(
        "v2 modules: {} full / {} review cases; {}",
        cases.len(),
        task["cases"].as_object().unwrap().len(),
        out.join("qualification-review.html").display()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unknown_and_no_trade_are_not_losses_and_cost_scenarios_remain_separate() {
        let row = |id, status, r| {
            let mut row=AuditRow{event_id:id,symbol:"X".into(),direction:"up".into(),grade:"".into(),level:"".into(),warning_ts:"2026-01-01".into(),s0_ts:"2025-12-31".into(),s1_ts:"".into(),old_score:3.,old_dims:Value::Null,quality:serde_json::from_value(json!({"event_id":id,"event_key":"k","warning_ts":"","timeframe":"15m","feature_version":"v","rules_version":"v","parameter_hash":"h","input_hash":"i","origin":"test","status":"ok","features":{},"contributions":[],"explanations":[],"flags":[]})).unwrap(),legacy_recomputed:None,legacy_max_error:None,evaluations:BTreeMap::new()};
            row.evaluations.insert(
                "fixed_one_1tick".into(),
                json!({"status":status,"net_r":r,"exit_ts":"2026-01-02"}),
            );
            Case {
                row,
                payload: Value::Null,
                evidence: json!({"modules":{"a_process":{"metrics":{"bars":3}}}}),
                risk_ticks: Some(20.),
            }
        };
        let a = row(1, "closed", Some(1.));
        let b = row(2, "invalidated", None);
        let c = row(3, "ambiguous", None);
        let d = row(4, "closed", Some(-1.));
        let s = stats(&[&a, &b, &c, &d], "fixed_one_1tick");
        assert_eq!(s["closed_priced"], 2);
        assert_eq!(s["win_rate"], 0.5);
        assert_eq!(s["mean_net_r"], 0.);
        assert_eq!(s["not_closed_or_unpriced"], 2);
        let unavailable = stats(&[&a, &b, &c, &d], "fixed_one_2tick");
        assert_eq!(unavailable["closed_priced"], 0);
        assert!(unavailable["mean_net_r"].is_null());
        assert_eq!(cuts(&[&a, &b, &c, &d], "a_process.bars"), vec![3.]);
        let bins = grouped(&[&a, &b, &c, &d], "a_process.bars", &[3.]);
        assert_eq!(bins["bins"][0]["stats"]["one_tick"]["candidates"], 4);
        assert_eq!(bins["bins"][1]["stats"]["one_tick"]["candidates"], 0);
        assert!(payload_equal(
            &json!([1.0, null]),
            &json!([1.0000000000000002, null])
        ));
        assert!(!payload_equal(&json!([1.0, null]), &json!([1.00001, null])));
    }
}
