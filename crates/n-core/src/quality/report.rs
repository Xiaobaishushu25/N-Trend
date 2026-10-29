use super::{
    research::{write_json, AuditRow},
    Rules,
};
use anyhow::Result;
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap},
    path::Path,
};

fn stats(rows: &[&AuditRow], scenario: &str) -> Value {
    let mut statuses = BTreeMap::<String, usize>::new();
    let mut values = Vec::new();
    let mut settled = Vec::new();
    for r in rows {
        let ev = &r.evaluations[scenario];
        let status = ev["status"].as_str().unwrap_or("not_evaluated");
        *statuses.entry(status.into()).or_default() += 1;
        if status == "closed" {
            if let Some(net) = ev["net_r"].as_f64() {
                values.push(net);
                settled.push((ev["exit_ts"].as_str().unwrap_or(""), net));
            }
        }
    }
    settled.sort_by_key(|x| x.0);
    let mut eq: f64 = 0.;
    let mut peak: f64 = 0.;
    let mut dd: f64 = 0.;
    for (_, r) in settled {
        eq += r;
        peak = peak.max(eq);
        dd = dd.max(peak - eq);
    }
    json!({"candidates":rows.len(),"statuses":statuses,"closed_priced":values.len(),"net_r":values.iter().sum::<f64>(),"mean_net_r":if values.is_empty(){None}else{Some(values.iter().sum::<f64>()/values.len()as f64)},"closed_trade_drawdown_r":dd,"win_rate":if values.is_empty(){None}else{Some(values.iter().filter(|r|**r>0.).count()as f64/values.len()as f64)}})
}
fn value(r: &AuditRow, k: &str) -> f64 {
    match k {
        "old" => r.old_score,
        "morphology" => r.quality.morphology_score.unwrap_or(0.),
        _ => r.quality.quality_score.unwrap_or(0.),
    }
}
fn distribution(mut xs: Vec<f64>) -> Value {
    xs.sort_by(f64::total_cmp);
    if xs.is_empty() {
        return Value::Null;
    }
    let n = xs.len();
    let quantile = |p: f64| xs[((n - 1) as f64 * p).round() as usize];
    let mut counts = HashMap::new();
    for x in &xs {
        *counts.entry(format!("{x:.2}")).or_insert(0usize) += 1;
    }
    json!({"n":n,"min":xs[0],"p25":quantile(0.25),"median":quantile(0.5),"p75":quantile(0.75),"max":xs[n-1],"iqr":quantile(0.75)-quantile(0.25),"tied_rows_2dp":counts.values().filter(|n|**n>1).sum::<usize>()})
}
fn corr(xs: &[(f64, f64)]) -> Option<f64> {
    if xs.len() < 2 {
        return None;
    }
    let a = xs.iter().map(|x| x.0).sum::<f64>() / xs.len() as f64;
    let b = xs.iter().map(|x| x.1).sum::<f64>() / xs.len() as f64;
    let aa = xs.iter().map(|x| (x.0 - a).powi(2)).sum::<f64>();
    let bb = xs.iter().map(|x| (x.1 - b).powi(2)).sum::<f64>();
    (aa * bb > 0.).then(|| xs.iter().map(|x| (x.0 - a) * (x.1 - b)).sum::<f64>() / (aa * bb).sqrt())
}
pub fn write(out: &Path, rows: &[AuditRow], rules: &Rules) -> Result<()> {
    let valid: Vec<_> = rows.iter().filter(|r| r.quality.status == "ok").collect();
    let mut statuses = BTreeMap::new();
    for r in rows {
        *statuses.entry(&r.quality.status).or_insert(0usize) += 1;
    }
    let mut ranking = BTreeMap::new();
    let mut migration = HashMap::<i64, Value>::new();
    let mut comparisons = BTreeMap::new();
    for key in ["old", "morphology", "quality"] {
        let mut ordered = valid.clone();
        ordered.sort_by(|a, b| {
            value(b, key)
                .total_cmp(&value(a, key))
                .then(a.event_id.cmp(&b.event_id))
        });
        for (rank, row) in ordered.iter().enumerate() {
            migration
                .entry(row.event_id)
                .or_insert_with(|| json!({"event_id":row.event_id,"symbol":row.symbol}))
                [format!("{key}_rank")] = json!(rank + 1);
        }
        ranking.insert(
            key,
            distribution(ordered.iter().map(|r| value(r, key)).collect()),
        );
        let n = (ordered.len() / 5).max(1).min(ordered.len());
        let mut top = ordered[..n].to_vec();
        top.sort_by(|a, b| a.warning_ts.cmp(&b.warning_ts));
        // Same-symbol overlapping structures are not counted as independent bets.
        let mut ends = HashMap::<String, String>::new();
        let independent: Vec<_> = top
            .iter()
            .copied()
            .filter(|r| {
                if ends.get(&r.symbol).is_some_and(|end| *end >= r.s0_ts) {
                    return false;
                }
                let exit = r.evaluations["fixed_one_1tick"]["exit_ts"]
                    .as_str()
                    .unwrap_or(&r.warning_ts);
                ends.insert(r.symbol.clone(), exit.into());
                true
            })
            .collect();
        comparisons.insert(key,json!({"top20_percent":stats(&top,"fixed_one_1tick"),"top20_2tick":stats(&top,"fixed_one_2tick"),"top20_nonoverlap":stats(&independent,"fixed_one_1tick")}));
    }
    let mut ablation = BTreeMap::new();
    for removed in ["trend", "a", "b", "space"] {
        let mut ordered: Vec<_> = valid
            .iter()
            .map(|r| {
                let q = &r.quality;
                let parts = [
                    q.trend_score.unwrap(),
                    q.a_score.unwrap(),
                    q.b_score.unwrap(),
                ];
                let skip = match removed {
                    "trend" => Some(0),
                    "a" => Some(1),
                    "b" => Some(2),
                    _ => None,
                };
                let m = if let Some(skip) = skip {
                    parts
                        .iter()
                        .zip(rules.morphology_weights)
                        .enumerate()
                        .filter(|(i, _)| *i != skip)
                        .map(|(_, (s, w))| s * w)
                        .sum::<f64>()
                        / (1. - rules.morphology_weights[skip])
                } else {
                    q.morphology_score.unwrap()
                };
                let score = if removed == "space" {
                    m
                } else {
                    rules.quality_weights[0] * m + rules.quality_weights[1] * q.space_score.unwrap()
                };
                (*r, score)
            })
            .collect();
        ordered.sort_by(|a, b| b.1.total_cmp(&a.1));
        ablation.insert(removed,json!({"four_case_order":ordered.iter().filter(|(r,_)|[7068,7034,7084,7086].contains(&r.event_id)).map(|(r,s)|json!({"id":r.event_id,"score":s})).collect::<Vec<_>>(),"top20_stats":stats(&ordered.iter().take((ordered.len()/5).max(1)).map(|x|x.0).collect::<Vec<_>>(),"fixed_one_1tick")}));
    }
    let names = ["trend", "a", "b", "space"];
    let mut correlations = BTreeMap::new();
    for i in 0..4 {
        for j in i + 1..4 {
            let pairs = valid
                .iter()
                .map(|r| {
                    let q = &r.quality;
                    let v = [
                        q.trend_score.unwrap(),
                        q.a_score.unwrap(),
                        q.b_score.unwrap(),
                        q.space_score.unwrap(),
                    ];
                    (v[i], v[j])
                })
                .collect::<Vec<_>>();
            correlations.insert(format!("{}:{}", names[i], names[j]), corr(&pairs));
        }
    }
    let mut groups = BTreeMap::<String, Vec<&AuditRow>>::new();
    for r in &valid {
        groups
            .entry(format!("symbol:{}", r.symbol))
            .or_default()
            .push(r);
        groups
            .entry(format!("month:{}", &r.warning_ts[..7]))
            .or_default()
            .push(r);
    }
    let groups:BTreeMap<_,_>=groups.into_iter().map(|(k,rs)|{let mut top=rs.clone();top.sort_by(|a,b|value(b,"quality").total_cmp(&value(a,"quality")));let take=(top.len()/5).max(1);(k,json!({"all":stats(&rs,"fixed_one_1tick"),"quality_top20":stats(&top[..take],"fixed_one_1tick")}))}).collect();
    let legacy_mismatches:Vec<_>=rows.iter().filter(|r|r.legacy_max_error.is_some_and(|e|e>1e-6)).map(|r|json!({"event_id":r.event_id,"source_status":r.quality.status,"source_input_hash":r.quality.input_hash,"source_status_reasons":r.quality.explanations,"max_error":r.legacy_max_error,"persisted":r.old_dims,"recomputed":r.legacy_recomputed})).collect();
    let reference = [7068, 7034, 7084, 7086];
    let mut deviations = Vec::new();
    for pair in reference.windows(2) {
        let preferred = valid.iter().find(|r| r.event_id == pair[0]);
        let other = valid.iter().find(|r| r.event_id == pair[1]);
        if let (Some(a), Some(b)) = (preferred, other) {
            if a.quality.quality_score < b.quality.quality_score {
                let qa = &a.quality;
                let qb = &b.quality;
                deviations.push(json!({"preferred":a.event_id,"other":b.event_id,"trend_delta":rules.quality_weights[0]*rules.morphology_weights[0]*(qa.trend_score.unwrap()-qb.trend_score.unwrap()),"a_delta":rules.quality_weights[0]*rules.morphology_weights[1]*(qa.a_score.unwrap()-qb.a_score.unwrap()),"b_delta":rules.quality_weights[0]*rules.morphology_weights[2]*(qa.b_score.unwrap()-qb.b_score.unwrap()),"space_delta":rules.quality_weights[1]*(qa.space_score.unwrap()-qb.space_score.unwrap()),"quality_delta":qa.quality_score.unwrap()-qb.quality_score.unwrap()}));
            }
        }
    }
    let normalized = json!({"old_on_100_scale":distribution(valid.iter().map(|r|r.old_score*20.).collect()),"new_quality":distribution(valid.iter().map(|r|r.quality.quality_score.unwrap()).collect()),"note":"Only compare distributions on matching scales; a larger numerical span alone is not evidence of better ranking."});
    let summary = json!({"events":rows.len(),"legacy_recomputed":rows.iter().filter(|r|r.legacy_recomputed.is_some()).count(),"reference_order_deviations":deviations,"normalized_distributions":normalized,"coverage":statuses,"distributions":ranking,"comparisons":comparisons,"baseline":stats(&valid,"fixed_one_1tick"),"groups":groups,"ablations":ablation,"component_correlations":correlations,"legacy_recompute_mismatches":legacy_mismatches,"legacy_boundary_diagnostic":{"amplitude_just_below_1_5":0.999999,"amplitude_at_1_5":0.5,"legacy_unchanged":true,"new_mapping":"continuous smoothstep"},"validation":"historical development; overlapping results are not independent prospective evidence","cost_note":"fixed one lot, same 5m conservative policy; 1/2 tick slippage plus explicit roundtrip proxy commission; no actual historical contract/fee proof"});
    write_json(&out.join("summary.json"), &summary)?;
    let mut changes = migration.into_values().collect::<Vec<_>>();
    changes.sort_by_key(|x| x["quality_rank"].as_u64());
    write_json(&out.join("rank-migration.json"), &changes)?;
    let cases = rows
        .iter()
        .filter(|r| [7068, 7034, 7084, 7086].contains(&r.event_id))
        .collect::<Vec<_>>();
    let mut md=format!("# 信号质量规则基线报告\n\n- 规则：{}\n- 参数哈希：{}\n- 全部事件：{}；可完整评价：{}\n- 新分仅用于并行展示，旧候选选择、通知和交易评分保持不变。\n- 历史快照重构，不代表预警当时的数据版本，也不是未来收益证明。\n\n|编号|品种|旧分|形态|趋势|A段|B段|空间|综合|旧分复现最大误差|\n|---|---|---:|---:|---:|---:|---:|---:|---:|---:|\n",rules.version,rules.hash(),rows.len(),valid.len());
    for r in &cases {
        let q = &r.quality;
        let fmt = |v: Option<f64>| v.map(|x| format!("{x:.3}")).unwrap_or_else(|| "—".into());
        md += &format!(
            "|{}|{}|{:.4}|{}|{}|{}|{}|{}|{}|{}|\n",
            r.event_id,
            r.symbol,
            r.old_score,
            fmt(q.morphology_score),
            fmt(q.trend_score),
            fmt(q.a_score),
            fmt(q.b_score),
            fmt(q.space_score),
            fmt(q.quality_score),
            fmt(r.legacy_max_error)
        );
    }
    md+=&format!("\n旧评分可重算{}条；其中{}条与历史落盘不一致。偏差记录保留数据状态、行情哈希及新旧分项，见summary.json。新评分完整性检查通过样本中的旧分不一致数为{}。\n\n",rows.iter().filter(|r|r.legacy_recomputed.is_some()).count(),legacy_mismatches.len(),valid.iter().filter(|r|r.legacy_max_error.is_some_and(|e|e>1e-6)).count());
    let mut ordered = cases.clone();
    ordered.sort_by(|a, b| value(b, "quality").total_cmp(&value(a, "quality")));
    md+=&format!("\n实际综合排序：{}。\n\n参考排序：7068 > 7034 > 7084 > 7086。规则未按编号调参；差异按分项解释保留。\n",ordered.iter().map(|r|r.event_id.to_string()).collect::<Vec<_>>().join(" > "));
    for d in &deviations {
        md+=&format!("\n排序偏差：参考偏好 {} 优于 {}，但规则综合差值 {:.3}；其中趋势贡献差 {:.3}、A段 {:.3}、B段 {:.3}、空间 {:.3}。这是待盲评校准的规则偏差，不通过编号修正。\n",d["preferred"],d["other"],d["quality_delta"].as_f64().unwrap(),d["trend_delta"].as_f64().unwrap(),d["a_delta"].as_f64().unwrap(),d["b_delta"].as_f64().unwrap(),d["space_delta"].as_f64().unwrap());
    }
    for r in &cases {
        md += &format!("\n## {} {}\n\n", r.event_id, r.symbol);
        for reason in &r.quality.explanations {
            md += &format!("- {reason}\n");
        }
    }
    md+="\n## 全库诊断与收益对照\n\n详见 summary.json：覆盖、分数分布、排名迁移、相关性、四组消融、固定一手扣费结果与月份/品种分组。未知路径及未定价记录单列。\n\n旧幅度函数在1.5 ATR处存在跳变；旧公式保留，新规则使用连续映射。盲评工具见 blind-review.html。\n";
    std::fs::write(out.join("report.md"), &md)?;
    let html=format!("<!doctype html><meta charset=\"utf-8\"><title>信号形态质量诊断</title><style>body{{font:16px/1.7 system-ui;max-width:1200px;margin:36px auto;padding:20px;background:#f4f6fa;color:#243247}}pre{{white-space:pre-wrap;background:white;padding:28px;border-radius:14px}}a{{color:#2658cc}}</style><h1>信号形态质量诊断</h1><p><a href=\"blind-review.html\">打开离线盲评</a> · <a href=\"summary.json\">全库统计与消融</a> · <a href=\"audit.json\">逐条原始因子</a> · <a href=\"rank-migration.json\">排名迁移</a></p><pre>{}</pre>",escape(&md));
    std::fs::write(out.join("report.html"), html)?;
    Ok(())
}
fn escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}
