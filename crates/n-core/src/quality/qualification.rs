//! Offline qualification research. Descriptive evidence only: no score, gate or outcome fitting.
use super::{
    hash,
    research::{write_json, AuditRow},
};
use anyhow::{ensure, Context, Result};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    path::Path,
};

pub const VERSION: &str = "qualification-evidence-v1";
pub const DISPLAY: &str = "qualification-single-v1";
const GRADES: [&str; 4] = ["qualified", "marginal", "rejected", "uncertain"];
const TYPES: [&str; 5] = [
    "pullback_n",
    "impulse",
    "consolidation_breakout",
    "other",
    "uncertain",
];
const REASONS: [&str; 6] = ["a", "b", "trend", "space", "warning", "scale"];

#[derive(Clone, Copy)]
struct Candle {
    o: f64,
    h: f64,
    l: f64,
    c: f64,
}
fn average(xs: &[f64]) -> Option<f64> {
    (!xs.is_empty()).then(|| xs.iter().sum::<f64>() / xs.len() as f64)
}
fn ratio(x: f64, y: f64) -> Option<f64> {
    (y > 1e-12).then(|| x / y)
}
fn concentration(xs: &[f64]) -> Value {
    let mut xs = xs.to_vec();
    xs.sort_by(|a, b| b.total_cmp(a));
    let sum: f64 = xs.iter().sum();
    json!({"largest_share":ratio(xs.first().copied().unwrap_or(0.),sum),
        "largest_two_share":ratio(xs.iter().take(2).sum(),sum),
        "effective_bars":ratio(sum*sum,xs.iter().map(|x|x*x).sum()),
        "positive_bars":xs.iter().filter(|x|**x>1e-12).count()})
}
fn overlap(a: Candle, b: Candle) -> Option<f64> {
    ratio(
        (a.h.min(b.h) - a.l.max(b.l)).max(0.),
        (a.h - a.l).min(b.h - b.l),
    )
}
fn path(bs: &[Candle], d: f64) -> Value {
    let ranges: Vec<_> = bs.iter().map(|b| b.h - b.l).collect();
    let overlaps: Vec<_> = bs.windows(2).filter_map(|w| overlap(w[0], w[1])).collect();
    let wick: Vec<_> = bs
        .iter()
        .filter_map(|b| ratio((b.h - b.l) - (b.c - b.o).abs(), b.h - b.l))
        .collect();
    let travel: f64 = bs.windows(2).map(|w| (w[1].c - w[0].c).abs()).sum();
    let net = bs.first().zip(bs.last()).map(|(a, b)| d * (b.c - a.c));
    let mid = bs.len() / 2;
    let half_span = |xs: &[Candle]| -> Option<f64> {
        (!xs.is_empty()).then(|| {
            xs.iter().map(|b| b.h).fold(f64::NEG_INFINITY, f64::max)
                - xs.iter().map(|b| b.l).fold(f64::INFINITY, f64::min)
        })
    };
    json!({"bars":bs.len(),"mean_overlap":average(&overlaps),"mean_wick_fraction":average(&wick),
        "mean_range_atr":average(&ranges),"close_net_atr":net,"close_travel_atr":travel,
        "close_efficiency":net.and_then(|n|ratio(n.abs(),travel)),
        "second_half_range_ratio":if bs.len()>=4 {half_span(&bs[mid..]).zip(half_span(&bs[..mid])).and_then(|(a,b)|ratio(a,b))}else{None}})
}

/// `warning` is explicit so appended post-warning candles cannot change evidence.
/// Prices in a blind case are already expressed in ATR20(S1), never chart angles.
pub fn diagnose(case: &Value, warning: usize) -> Result<Value> {
    let raw = case["bars"].as_array().context("case bars missing")?;
    ensure!(warning < raw.len(), "warning outside case");
    let bars: Vec<_> = raw[..=warning]
        .iter()
        .map(|r| -> Result<Candle> {
            let n = |i| {
                r[i].as_f64()
                    .filter(|v| v.is_finite())
                    .context("invalid candle")
            };
            let b = Candle {
                o: n(0)?,
                h: n(1)?,
                l: n(2)?,
                c: n(3)?,
            };
            ensure!(
                b.h >= b.o.max(b.c) && b.l <= b.o.min(b.c),
                "invalid OHLC range"
            );
            Ok(b)
        })
        .collect::<Result<_>>()?;
    let point = |i| {
        case["points"][i][0]
            .as_u64()
            .map(|v| v as usize)
            .context("endpoint index")
    };
    let (s0, s1, s2) = (point(0)?, point(1)?, point(2)?);
    ensure!(s0 < s1 && s1 < s2 && s2 <= warning, "unordered endpoints");
    let d = match case["direction"].as_str() {
        Some("up") => 1.,
        Some("down") => -1.,
        _ => anyhow::bail!("invalid direction"),
    };
    let a = &bars[s0..=s1];
    let core = if a.len() > 2 { &a[1..a.len() - 1] } else { &[] };
    let bodies: Vec<_> = a.iter().map(|b| (d * (b.c - b.o)).max(0.)).collect();
    let mut record = d * a[0].c;
    let gains: Vec<_> = a[1..]
        .iter()
        .map(|b| {
            let now = d * b.c;
            let gain = (now - record).max(0.);
            record = record.max(now);
            gain
        })
        .collect();
    let b = &bars[s1 + 1..=s2];
    let pre = &bars[s1 + 1..s2.min(warning - 1) + 1];
    let endpoint = bars[s1];
    let extension = |x: Candle| {
        if d > 0. {
            (endpoint.l - x.l).max(0.)
        } else {
            (x.h - endpoint.h).max(0.)
        }
    };
    let close_extension = |x: Candle| {
        if d > 0. {
            (endpoint.l - x.c).max(0.)
        } else {
            (x.c - endpoint.h).max(0.)
        }
    };
    let first_exit = pre.iter().position(|x| extension(*x) > 1e-9).map(|i| i + 1);
    let amplitude = (case["points"][1][1].as_f64().context("S1 price")?
        - case["points"][0][1].as_f64().context("S0 price")?)
    .abs();
    let wick = if d > 0. {
        endpoint.h - endpoint.o.max(endpoint.c)
    } else {
        endpoint.o.min(endpoint.c) - endpoint.l
    };
    let mut result = json!({"version":VERSION,"warning_index":warning,"s0_index":s0,"s1_index":s1,"s2_index":s2,
        "a":{"bars":a.len(),"interior_bars":core.len(),"legacy_process_window":if core.len()>=4 {"interior"}else{"full_fallback"},
            "amplitude_atr":amplitude,"directional_body":concentration(&bodies),"closing_records":concentration(&gains),
            "full_path":path(a,d),"interior_path":path(core,d),"s1_opposing_wick_fraction":ratio(wick,endpoint.h-endpoint.l)},
        "b":{"bars_including_warning_if_s2":b.len(),"bars_without_warning":pre.len(),
            "outside_s1_atr":b.iter().map(|b|extension(*b)).fold(0.,f64::max),
            "outside_s1_before_warning_atr":pre.iter().map(|b|extension(*b)).fold(0.,f64::max),
            "close_outside_s1_before_warning_atr":pre.iter().map(|b|close_extension(*b)).fold(0.,f64::max),
            "first_range_exit_bar":first_exit,"range_exit_bars":pre.iter().filter(|b|extension(**b)>1e-9).count(),
            "close_exit_bars":pre.iter().filter(|b|close_extension(**b)>1e-9).count(),
            "path_without_warning":path(pre,-d)},
        "background":path(&bars[s0.saturating_sub(20)..s0],d),
        "missing":["tick_not_in_original_blind_payload"],"scoring_effect":"diagnostic_only"});
    let mut interpretation = Vec::new();
    let largest = field(&result, &["a", "directional_body", "largest_share"]);
    interpretation.push(if result["a"]["directional_body"]["largest_share"].is_null() {
        "A段没有可用的顺向实体贡献，无法评价贡献是否分散；需要检查跳空、端点或数据。"
    } else if largest>=0.5 {
        "顺向实体有一半以上集中在单根K。我会先核对它是持续推进还是单次脉冲，不能只因收盘路径效率高就认可A段。"
    } else {"A段顺向实体的贡献相对分散；这只是持续推进的证据之一，还需要看收盘新进展、反向回吐和端点。"});
    interpretation.push(if core.len() < 4 {
        "A段内部少于4根，旧过程指标改用完整段。它与长A的内部窗口口径不同，我会单独核对端点贡献。"
    } else {
        "完整A与内部A均单列诊断；正常转折K与内部推进需要分别观察。"
    });
    interpretation.push(if pre.is_empty(){"预警前没有独立的B段样本，我不会把预警K本身当成回撤已形成或衰减的证据。"}
        else if first_exit.is_none(){"预警前B段区间没有超出S1 K线范围。它可能是内部整理，我暂不把它自动当作标准独立回踩。"}
        else {"预警前B段已出现超出S1范围的区间；还要核对首次出现的时点、持续根数与收盘位移，单根影线超出不足以确认健康回撤。"});
    interpretation.push("判断重叠时，我会同时看区间是否收窄、影线比例和收盘来回路程；这些证据有取舍时保留未知，不把所有重叠都叫作争夺。 ");
    if pre.len() < 4 {
        interpretation.push("预警前B段少于4根，前后半段收敛数据不足，不能宣称已观察到整理收窄。");
    }
    result["interpretation"] = json!(interpretation);
    Ok(result)
}

fn field(v: &Value, keys: &[&str]) -> f64 {
    keys.iter().fold(v, |a, k| &a[*k]).as_f64().unwrap_or(0.)
}
pub(super) fn seal(task: &mut Value) -> Result<()> {
    task["task_hash"] = json!("0".repeat(64));
    task["task_hash"] = json!(hash(&serde_json::to_vec_pretty(task)?));
    Ok(())
}
pub fn load_task(path: &Path) -> Result<Value> {
    let raw = std::fs::read_to_string(path)?;
    let task: Value = serde_json::from_str(&raw)?;
    let claimed = task["task_hash"].as_str().context("task hash missing")?;
    ensure!(
        claimed.len() == 64
            && hash(
                raw.replacen(
                    &format!("\"task_hash\": \"{claimed}\""),
                    &format!("\"task_hash\": \"{}\"", "0".repeat(64)),
                    1
                )
                .as_bytes()
            ) == claimed,
        "qualification task changed"
    );
    ensure!(
        (task["version"] == VERSION && task["display_version"] == DISPLAY)
            || (task["version"] == super::structure::VERSION
                && task["display_version"] == super::structure::DISPLAY),
        "qualification version mismatch"
    );
    Ok(task)
}
fn choice(value: &Value, allowed: &[&str], optional: bool) -> bool {
    value
        .as_str()
        .is_some_and(|s| allowed.contains(&s) || (optional && s.is_empty()))
}
pub fn validate_assessments(task: &Value, labels: &Value) -> Result<()> {
    ensure!(
        labels["qualification_task_hash"] == task["task_hash"]
            && labels["assessment_version"] == task["version"]
            && labels["assessment_display_version"] == task["display_version"],
        "qualification task/version mismatch"
    );
    ensure!(
        labels["manifest_hash"] == task["base_manifest_hash"],
        "qualification base mismatch"
    );
    let mut seen = HashSet::new();
    let completed = labels["case_assessments"]
        .as_array()
        .context("case assessments array")?;
    let empty = Vec::new();
    let drafts = labels
        .get("case_drafts")
        .map(|v| v.as_array().context("case drafts array"))
        .transpose()?
        .unwrap_or(&empty);
    for (label, partial) in completed
        .iter()
        .map(|l| (l, false))
        .chain(drafts.iter().map(|l| (l, true)))
    {
        let id = label["case_id"].as_str().context("assessment identity")?;
        ensure!(seen.insert((id, partial)), "duplicate case assessment");
        let case = task["cases"]
            .get(id)
            .context("unknown qualification case")?;
        ensure!(
            label["input_hash"] == case["input_hash"],
            "assessment input hash mismatch"
        );
        let check = |form: &Value, optional: bool| -> Result<()> {
            for key in ["morphology_qualification", "opportunity_qualification"] {
                ensure!(
                    choice(&form[key], &GRADES, optional),
                    "invalid qualification choice"
                );
            }
            ensure!(
                choice(&form["pattern_type"], &TYPES, optional),
                "invalid pattern type"
            );
            ensure!(
                form["note"]
                    .as_str()
                    .is_some_and(|s| s.chars().count() <= 2000),
                "invalid assessment note"
            );
            ensure!(
                form["reasons"]
                    .as_array()
                    .is_some_and(|xs| xs.iter().all(|x| choice(x, &REASONS, false))),
                "invalid assessment reasons"
            );
            Ok(())
        };
        check(label, partial)?;
        let reference = &label["assistant_reference"];
        ensure!(
            reference["task_hash"] == task["task_hash"]
                && reference["version"] == labels["assessment_version"],
            "assessment reference mismatch"
        );
        match reference["viewed"]
            .as_bool()
            .context("assessment viewed flag")?
        {
            true => {
                check(&reference["before"], true)?;
                ensure!(
                    reference["opened_at"]
                        .as_str()
                        .is_some_and(|s| chrono::DateTime::parse_from_rfc3339(s).is_ok()),
                    "assessment reference time"
                );
            }
            false => {}
        }
    }
    Ok(())
}

pub fn export(args: &[String], out: &Path) -> Result<()> {
    let arg = |key: &str| {
        args.windows(2)
            .find(|x| x[0] == key)
            .map(|x| x[1].clone())
            .with_context(|| format!("{key} required"))
    };
    let source = std::path::PathBuf::from(arg("--manifest")?);
    let manifest = super::blind::load_manifest(&source)?;
    // Reuse the legacy identity/reference validator; never reinterpret its choices as absolute labels.
    super::blind::import(args, out)?;
    let feedback: Value = serde_json::from_slice(&std::fs::read(arg("--labels")?)?)?;
    let rows: Vec<AuditRow> = serde_json::from_slice(&std::fs::read(arg("--audit")?)?)?;
    let mut ordered: Vec<_> = rows.iter().filter(|r| r.quality.status == "ok").collect();
    ordered.sort_by(|a, b| a.symbol.cmp(&b.symbol).then(a.s0_ts.cmp(&b.s0_ts)));
    let mut clusters = HashMap::new();
    let (mut symbol, mut end, mut cluster) = (String::new(), String::new(), String::new());
    for r in ordered {
        if symbol != r.symbol || r.s0_ts > end {
            symbol = r.symbol.clone();
            end = r.warning_ts.clone();
            cluster = format!("{}|{}", r.symbol, r.s0_ts);
        } else if r.warning_ts > end {
            end = r.warning_ts.clone();
        }
        clusters.insert(r.quality.input_hash.clone(), cluster.clone());
    }
    let pairs = manifest["pairs"].as_array().context("pairs")?;
    let known: HashSet<_> = feedback["labels"]
        .as_array()
        .context("labels")?
        .iter()
        .filter_map(|l| l["pair_id"].as_str())
        .collect();
    let mut consumed = HashSet::new();
    let mut revisit = Vec::new();
    let mut cases = serde_json::Map::new();
    let mut context = serde_json::Map::new();
    for pair in pairs {
        for side in ["left", "right"] {
            let c = &pair[side];
            let id = c["case_id"].as_str().context("case id")?;
            let h = c["input_hash"].as_str().context("input hash")?;
            ensure!(clusters.contains_key(h), "audit missing qualification case");
            let mut c = c.clone();
            let w = c["bars"].as_array().context("bars")?.len() - 1;
            c["diagnostics"] = diagnose(&c, w)?;
            c["warning_index"] = json!(w);
            cases.insert(id.into(), c);
            context.insert(
                id.into(),
                json!({"pair_id":pair["pair_id"],"side":side,"split":pair["split"]}),
            );
            if known.contains(pair["pair_id"].as_str().unwrap_or("")) {
                revisit.push(id.to_string());
                consumed.insert(clusters[h].clone());
            }
        }
    }
    for r in &rows {
        if [7068, 7034, 7084, 7086].contains(&r.event_id) {
            if let Some(k) = clusters.get(&r.quality.input_hash) {
                consumed.insert(k.clone());
            }
        }
    }
    let reviewed_clusters = consumed.clone();
    let mut fresh = Vec::new();
    let mut sampling = serde_json::Map::new();
    for group in [
        "concentrated_a",
        "limited_b",
        "overlap_context",
        "reference_sample",
    ] {
        let mut available: Vec<_> = cases
            .iter()
            .filter(|(id, c)| {
                context[*id]["split"] == "development"
                    && !consumed.contains(&clusters[c["input_hash"].as_str().unwrap()])
            })
            .map(|(id, c)| (id.clone(), c.clone()))
            .collect();
        available.sort_by(|(ia, a), (ib, b)| {
            let score = |c: &Value| match group {
                "concentrated_a" => field(
                    &c["diagnostics"],
                    &["a", "directional_body", "largest_share"],
                ),
                "limited_b" => -field(&c["diagnostics"], &["b", "outside_s1_before_warning_atr"]),
                "overlap_context" => field(
                    &c["diagnostics"],
                    &["b", "path_without_warning", "mean_overlap"],
                ),
                _ => 0.,
            };
            score(b).total_cmp(&score(a)).then(
                hash(format!("20261029-qualification|{ia}").as_bytes())
                    .cmp(&hash(format!("20261029-qualification|{ib}").as_bytes())),
            )
        });
        let mut n = 0;
        for (id, c) in available {
            let key = &clusters[c["input_hash"].as_str().unwrap()];
            if consumed.insert(key.clone()) {
                fresh.push(id.clone());
                sampling.insert(id, json!(group));
                n += 1;
                if n == 5 {
                    break;
                }
            }
        }
    }
    let selected: HashSet<_> = fresh.iter().chain(revisit.iter()).cloned().collect();
    cases.retain(|id, _| selected.contains(id));
    context.retain(|id, _| selected.contains(id));
    let identities:Vec<_>=pairs.iter().map(|p|json!({"pair_id":p["pair_id"],"left_case_id":p["left"]["case_id"],"left_input_hash":p["left"]["input_hash"],"right_case_id":p["right"]["case_id"],"right_input_hash":p["right"]["input_hash"]})).collect();
    let original_notes = super::assistant_review::load(
        &source
            .parent()
            .context("manifest parent")?
            .join("assistant-notes.json"),
        &manifest,
    )?;
    let mut task = json!({"version":VERSION,"display_version":DISPLAY,"base_manifest_hash":manifest["manifest_hash"],"base_display_version":manifest["display_version"],
        "source_feedback_sha256":hash(&std::fs::read(arg("--labels")?)?),"legacy_reference_version":original_notes["version"],"legacy_reference_hash":original_notes["notes_hash"],
        "cases":cases,"context":context,"new_queue":fresh,"revisit_queue":revisit,"sampling":sampling,"pair_identities":identities,
        "legacy_labels":feedback["labels"],"seed":"20261029-qualification","requested_new_cases":20,
        "retired_reviewed_clusters":reviewed_clusters.len(),"new_cases_are_development_only":true,
        "method":"Evidence-guided subsample of the prior score-selected task; no further outcome or score ranking. Previously discussed wave clusters and known examples excluded. Development diagnosis, not a random market sample or prospective validation."});
    seal(&mut task)?;
    write_json(&out.join("qualification-task.json"), &task)?;
    std::fs::copy(&source, out.join("blind-manifest.json"))?;
    std::fs::copy(
        source
            .parent()
            .context("manifest parent")?
            .join("assistant-notes.json"),
        out.join("assistant-notes.json"),
    )?;
    let payload = serde_json::to_string(&task)?
        .replace('<', "\\u003c")
        .replace('>', "\\u003e")
        .replace('&', "\\u0026");
    std::fs::write(
        out.join("qualification-review.html"),
        include_str!("qualification-review.html").replace("__QUALIFICATION_PAYLOAD__", &payload),
    )?;
    println!(
        "Qualification page: {} ({} new / {} revisit)",
        out.join("qualification-review.html").display(),
        task["new_queue"].as_array().unwrap().len(),
        task["revisit_queue"].as_array().unwrap().len()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn sample() -> Value {
        json!({"case_id":"sample","input_hash":"input","direction":"up",
        "bars":[[0,1,0,1,null],[1,2,1,2,null],[2,3,2,3,null],[3,4,3,4,null],
            [4,5,3,4,null],[4,4,3,3,null],[3,4,3,3,null],[3,4,2,3,null],[3,5,3,4,null]],
        "points":[[0,0],[4,5],[8,2]],"rr":1,"entry":4,"stop":2,"target":5})
    }
    #[test]
    fn warning_is_excluded_and_future_candles_are_irrelevant() {
        let c = sample();
        let d = diagnose(&c, 8).unwrap();
        assert_eq!(d["b"]["bars_without_warning"], 3);
        assert_eq!(d["b"]["first_range_exit_bar"], 3);
        assert_eq!(d["b"]["range_exit_bars"], 1);
        assert_eq!(d["b"]["outside_s1_before_warning_atr"], 1.);
        assert!(d["b"]["path_without_warning"]["second_half_range_ratio"].is_null());
        let mut changed = c.clone();
        changed["bars"]
            .as_array_mut()
            .unwrap()
            .push(json!([99, 100, -100, 0, null]));
        assert_eq!(d, diagnose(&changed, 8).unwrap());
        changed["bars"][8] = json!([3, 90, -90, 4, null]);
        assert_eq!(
            d["b"]["path_without_warning"],
            diagnose(&changed, 8).unwrap()["b"]["path_without_warning"]
        );
    }
    #[test]
    fn contribution_distinguishes_many_bars_from_one_impulse_and_zero_is_unknown() {
        let mut c = sample();
        let before = diagnose(&c, 8).unwrap();
        assert_eq!(before["a"]["directional_body"]["effective_bars"], 4.);
        for i in 0..4 {
            c["bars"][i] = json!([0, 4, 0, if i == 3 { 4 } else { 0 }, null]);
        }
        let after = diagnose(&c, 8).unwrap();
        assert_eq!(after["a"]["directional_body"]["effective_bars"], 1.);
        assert_eq!(after["a"]["directional_body"]["largest_share"], 1.);
        for b in c["bars"].as_array_mut().unwrap() {
            *b = json!([0, 0, 0, 0, null]);
        }
        let zero = diagnose(&c, 8).unwrap();
        assert!(zero["a"]["directional_body"]["effective_bars"].is_null());
        assert!(zero["b"]["path_without_warning"]["mean_overlap"].is_null());
    }
    #[test]
    fn evidence_is_direction_symmetric_and_translation_invariant() {
        let c = sample();
        let d = diagnose(&c, 8).unwrap();
        let mut mirrored = c.clone();
        mirrored["direction"] = json!("down");
        for b in mirrored["bars"].as_array_mut().unwrap() {
            *b = json!([
                -b[0].as_f64().unwrap(),
                -b[2].as_f64().unwrap(),
                -b[1].as_f64().unwrap(),
                -b[3].as_f64().unwrap(),
                null
            ]);
        }
        for p in mirrored["points"].as_array_mut().unwrap() {
            p[1] = json!(-p[1].as_f64().unwrap());
        }
        assert_eq!(d, diagnose(&mirrored, 8).unwrap());
        let mut translated = c;
        for b in translated["bars"].as_array_mut().unwrap() {
            for i in 0..4 {
                b[i] = json!(b[i].as_f64().unwrap() + 100.);
            }
        }
        for p in translated["points"].as_array_mut().unwrap() {
            p[1] = json!(p[1].as_f64().unwrap() + 100.);
        }
        assert_eq!(d, diagnose(&translated, 8).unwrap());
    }
    #[test]
    fn assessment_validation_separates_drafts_from_completed_and_checks_identity() {
        let task = json!({"task_hash":"task","base_manifest_hash":"base","version":VERSION,"display_version":DISPLAY,"cases":{"sample":sample()}});
        let form = json!({"case_id":"sample","input_hash":"input","morphology_qualification":"qualified","opportunity_qualification":"marginal","pattern_type":"pullback_n","reasons":["a","b"],"note":"两边都好不同于无法判断","assistant_reference":{"viewed":false,"version":VERSION,"task_hash":"task"}});
        let mut labels = json!({"manifest_hash":"base","qualification_task_hash":"task","assessment_version":VERSION,"assessment_display_version":DISPLAY,"case_assessments":[form.clone()],"case_drafts":[]});
        validate_assessments(&task, &labels).unwrap();
        labels["case_assessments"][0]["pattern_type"] = json!("");
        assert!(validate_assessments(&task, &labels).is_err());
        labels["case_drafts"] = labels["case_assessments"].clone();
        labels["case_assessments"] = json!([]);
        validate_assessments(&task, &labels).unwrap();
        labels["case_drafts"][0]["input_hash"] = json!("foreign");
        assert!(validate_assessments(&task, &labels).is_err());
        labels["case_drafts"] = json!([]);
        labels["case_assessments"] = json!([form.clone(), form]);
        assert!(validate_assessments(&task, &labels).is_err());
    }

    #[test]
    fn overlap_is_described_with_wicks_and_close_travel_not_a_blanket_rejection() {
        let calm = [
            Candle {
                o: 1.,
                h: 1.2,
                l: 0.9,
                c: 1.1,
            },
            Candle {
                o: 1.1,
                h: 1.3,
                l: 1.,
                c: 1.2,
            },
            Candle {
                o: 1.2,
                h: 1.4,
                l: 1.1,
                c: 1.3,
            },
        ];
        let contested = [
            Candle {
                o: 1.1,
                h: 2.,
                l: 0.,
                c: 1.,
            },
            Candle {
                o: 1.,
                h: 2.,
                l: 0.,
                c: 1.2,
            },
            Candle {
                o: 1.2,
                h: 2.,
                l: 0.,
                c: 1.,
            },
        ];
        let a = path(&calm, 1.);
        let b = path(&contested, 1.);
        assert!(a["mean_overlap"].as_f64().unwrap() > 0.5);
        assert!(
            a["mean_wick_fraction"].as_f64().unwrap() < b["mean_wick_fraction"].as_f64().unwrap()
        );
        assert!(a["close_efficiency"].as_f64().unwrap() > b["close_efficiency"].as_f64().unwrap());
        assert!(a.get("qualification").is_none());
    }
}
