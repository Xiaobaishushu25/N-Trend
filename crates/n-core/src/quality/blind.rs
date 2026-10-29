//! Offline blind task export. Payload contains no scores, outcomes or future candles.
use super::{
    hash,
    research::{write_json, AuditRow},
    Bar, Rules, SignalQualityDto,
};
use anyhow::{ensure, Context, Result};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    path::Path,
};

pub fn case(
    e: &crate::storage::entities::pattern_events::Model,
    bars: &[Bar],
    q: &SignalQualityDto,
    _rules: &Rules,
) -> Value {
    let all: Vec<_> = bars
        .iter()
        .filter(|b| b.dt.to_bar_ts() <= e.warning_ts)
        .cloned()
        .collect();
    let s0 = all
        .iter()
        .position(|b| b.dt.to_bar_ts() == e.s0_ts)
        .unwrap();
    let lo = s0.saturating_sub(40);
    let atr = q.features["atr_s1"].unwrap();
    let norm = |x: f64| (x - e.s0_price) / atr;
    let ma = crate::analyze::indicators::ma_series(&all, 20);
    let case_id = hash(format!("{}|{}", q.event_key, q.input_hash).as_bytes());
    json!({"case_id":case_id,"input_hash":q.input_hash,"display_version":"blind-quality-v1","direction":e.direction,"bars":all.iter().enumerate().skip(lo).map(|(i,b)|json!([norm(b.open),norm(b.high),norm(b.low),norm(b.close),ma[i].map(norm)])).collect::<Vec<_>>(),"points":[[s0-lo,norm(e.s0_price)],[all.iter().position(|b|b.dt.to_bar_ts()==e.s1_ts).unwrap()-lo,norm(e.s1_price)],[all.iter().position(|b|b.dt.to_bar_ts()==e.s2_ts).unwrap()-lo,norm(e.s2_price)]],"entry":norm(e.entry),"stop":norm(e.stop),"target":norm(e.s1_price),"rr":q.features["rr_to_s1"]})
}
pub fn export(out: &Path, rows: &[AuditRow], cases: &HashMap<i64, Value>) -> Result<()> {
    let seed = "20261029";
    // Interval clusters, per symbol and across both directions. No shared wave crosses splits.
    let mut ordered: Vec<_> = rows.iter().filter(|r| r.quality.status == "ok").collect();
    ordered.sort_by(|a, b| a.symbol.cmp(&b.symbol).then(a.s0_ts.cmp(&b.s0_ts)));
    let mut cluster = HashMap::new();
    let mut groups = HashMap::<String, Vec<i64>>::new();
    let mut symbol = "";
    let mut end = String::new();
    let mut key = String::new();
    for r in &ordered {
        if symbol != r.symbol || r.s0_ts > end {
            symbol = &r.symbol;
            key = format!("{}|{}", r.symbol, r.s0_ts);
            end = r.warning_ts.clone();
        } else if r.warning_ts > end {
            end = r.warning_ts.clone();
        }
        cluster.insert(r.event_id, key.clone());
        groups.entry(key.clone()).or_default().push(r.event_id);
    }
    let split: HashMap<_, _> = groups
        .iter()
        .map(|(k, ids)| {
            let known = ids.iter().any(|i| [7068, 7034, 7084, 7086].contains(i));
            let n = u64::from_str_radix(&hash(format!("{seed}|{k}").as_bytes())[..8], 16).unwrap();
            (
                k.clone(),
                if !known && n % 5 == 0 {
                    "holdout"
                } else {
                    "development"
                },
            )
        })
        .collect();
    ordered.sort_by_key(|r| hash(format!("{seed}|{}", r.quality.event_key).as_bytes()));
    let mut used = HashSet::new();
    let mut pairs = Vec::new();
    let mut counts = HashMap::new();
    for category in [
        "old_close",
        "same_grade_process",
        "trend_difference",
        "retrace_or_space",
    ] {
        let mut n = 0;
        for a in &ordered {
            if n >= 50 {
                break;
            }
            if used.contains(&a.event_id) {
                continue;
            }
            let other = ordered.iter().find(|b| {
                if a.event_id == b.event_id
                    || used.contains(&b.event_id)
                    || cluster[&a.event_id] == cluster[&b.event_id]
                    || split[&cluster[&a.event_id]] != split[&cluster[&b.event_id]]
                {
                    return false;
                }
                let qa = &a.quality;
                let qb = &b.quality;
                match category {
                    "old_close" => {
                        (a.old_score - b.old_score).abs() <= 0.15
                            && (qa.quality_score.unwrap() - qb.quality_score.unwrap()).abs() >= 10.
                    }
                    "same_grade_process" => {
                        a.grade == b.grade
                            && a.direction == b.direction
                            && (qa.a_score.unwrap() - qb.a_score.unwrap()).abs() >= 15.
                    }
                    "trend_difference" => {
                        a.direction == b.direction
                            && (qa.trend_score.unwrap() - qb.trend_score.unwrap()).abs() >= 20.
                    }
                    _ => {
                        (qa.b_score.unwrap() - qb.b_score.unwrap()).abs() >= 20.
                            || (qa.space_score.unwrap() - qb.space_score.unwrap()).abs() >= 25.
                    }
                }
            });
            if let Some(b) = other {
                used.insert(a.event_id);
                used.insert(b.event_id);
                n += 1;
                let swap = hash(
                    format!("{seed}|{}|{}", a.quality.event_key, b.quality.event_key).as_bytes(),
                )
                .as_bytes()[0]
                    % 2
                    == 0;
                let (left, right) = if swap { (*b, *a) } else { (*a, *b) };
                let pair_id = hash(
                    format!("{}|{}", left.quality.event_key, right.quality.event_key).as_bytes(),
                );
                pairs.push(json!({"pair_id":pair_id,"category":category,"split":split[&cluster[&a.event_id]],"left":cases[&left.event_id],"right":cases[&right.event_id]}));
            }
        }
        counts.insert(category, n);
    }
    let mut manifest = json!({"display_version":"blind-quality-v1","seed":seed,"pairs":pairs,"counts":counts,"requested_pairs":200,"actual_pairs":pairs.len()});
    manifest["manifest_hash"] = json!("0".repeat(64));
    let id = hash(&serde_json::to_vec_pretty(&manifest)?);
    manifest["manifest_hash"] = json!(id);
    write_json(&out.join("blind-manifest.json"), &manifest)?;
    let notes = super::assistant_review::build(&manifest, rows)?;
    super::assistant_review::write_page(out, &manifest, &notes)?;
    Ok(())
}
pub fn import(args: &[String], out: &Path) -> Result<()> {
    let arg = |name: &str| {
        args.windows(2)
            .find(|p| p[0] == name)
            .map(|p| p[1].clone())
            .with_context(|| format!("{name} required"))
    };
    let manifest_path = std::path::PathBuf::from(arg("--manifest")?);
    let manifest = load_manifest(&manifest_path)?;
    let claimed = manifest["manifest_hash"]
        .as_str()
        .context("manifest hash missing")?;
    let labels: Value = serde_json::from_slice(&std::fs::read(arg("--labels")?)?)?;
    ensure!(
        labels["manifest_hash"] == claimed
            && labels["display_version"] == manifest["display_version"],
        "labels belong to another data/display version"
    );
    let pairs: HashMap<_, _> = manifest["pairs"]
        .as_array()
        .context("manifest pairs")?
        .iter()
        .map(|p| (p["pair_id"].as_str().unwrap_or(""), p))
        .collect();
    let references = if labels["labels"]
        .as_array()
        .context("labels array")?
        .iter()
        .any(|label| label.get("assistant_reference").is_some())
    {
        let path = manifest_path
            .parent()
            .context("manifest parent")?
            .join("assistant-notes.json");
        Some(super::assistant_review::load(&path, &manifest)?)
    } else {
        None
    };
    let mut seen = HashSet::new();
    let mut joined = Vec::new();
    for label in labels["labels"].as_array().context("labels array")? {
        let id = label["pair_id"].as_str().context("pair_id missing")?;
        ensure!(seen.insert(id), "duplicate preference");
        let pair = pairs.get(id).context("unknown pair")?;
        for side in ["left", "right"] {
            ensure!(
                label[format!("{side}_case_id")] == pair[side]["case_id"]
                    && label[format!("{side}_input_hash")] == pair[side]["input_hash"],
                "case identity/hash mismatch"
            );
        }
        for field in ["morphology", "opportunity"] {
            ensure!(
                ["left", "right", "tie", "uncertain"]
                    .contains(&label[field].as_str().unwrap_or("")),
                "invalid preference choice"
            );
        }
        ensure!(
            label["reasons"].as_array().is_some_and(|rs| rs
                .iter()
                .all(|r| ["trend", "a", "b", "space"].contains(&r.as_str().unwrap_or("")))),
            "invalid reason codes"
        );
        ensure!(
            label["note"]
                .as_str()
                .is_some_and(|s| s.chars().count() <= 2000),
            "invalid note"
        );
        let mut group = "legacy_unrecorded";
        if let Some(reference) = label.get("assistant_reference") {
            ensure!(
                reference.is_object(),
                "invalid assistant reference metadata"
            );
            let notes = references.as_ref().context("assistant notes missing")?;
            ensure!(
                reference["notes_hash"] == notes["notes_hash"]
                    && reference["version"] == notes["version"],
                "assistant reference version mismatch"
            );
            let viewed = reference["viewed"]
                .as_bool()
                .context("reference viewed flag required")?;
            group = if viewed {
                "reference_viewed"
            } else {
                "no_reference_opened"
            };
            if viewed {
                let before = &reference["before"];
                for field in ["morphology", "opportunity"] {
                    ensure!(
                        ["", "left", "right", "tie", "uncertain"]
                            .contains(&before[field].as_str().unwrap_or("invalid")),
                        "invalid pre-reference choice"
                    );
                }
                ensure!(
                    before["note"]
                        .as_str()
                        .is_some_and(|s| s.chars().count() <= 2000),
                    "invalid pre-reference note"
                );
                ensure!(
                    before["reasons"].as_array().is_some_and(|rs| rs
                        .iter()
                        .all(|r| ["trend", "a", "b", "space"].contains(&r.as_str().unwrap_or("")))),
                    "invalid pre-reference reasons"
                );
                ensure!(
                    reference["opened_at"]
                        .as_str()
                        .is_some_and(|s| chrono::DateTime::parse_from_rfc3339(s).is_ok()),
                    "invalid reference timestamp"
                );
            }
        }
        joined.push(json!({"label":label,"split":pair["split"],"category":pair["category"],"calibration_group":group}));
    }
    write_json(
        &out.join("preferences.json"),
        &json!({"manifest_hash":claimed,"display_version":manifest["display_version"],"preferences":joined}),
    )?;
    println!("Imported {} preferences; no model trained", joined.len());
    Ok(())
}

pub fn load_manifest(path: &Path) -> Result<Value> {
    let manifest_text = std::fs::read_to_string(path)?;
    let mut manifest: Value = serde_json::from_str(&manifest_text)?;
    let claimed = manifest["manifest_hash"]
        .as_str()
        .context("manifest hash missing")?
        .to_string();
    manifest
        .as_object_mut()
        .context("manifest object")?
        .remove("manifest_hash");
    ensure!(
        hash(
            manifest_text
                .replacen(
                    &format!("\"manifest_hash\": \"{claimed}\""),
                    &format!("\"manifest_hash\": \"{}\"", "0".repeat(64)),
                    1
                )
                .as_bytes()
        ) == claimed,
        "manifest changed"
    );
    manifest["manifest_hash"] = json!(claimed);
    Ok(manifest)
}
