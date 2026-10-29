//! Companion interpretations, deliberately independent of legacy and new total scores.
//! These are authored structural reading rules, not a claim of per-chart visual review.
use super::{
    hash,
    research::{write_json, AuditRow},
};
use anyhow::{ensure, Context, Result};
use serde_json::{json, Value};
use std::{collections::HashMap, path::Path};

pub const VERSION: &str = "assistant-structure-notes-v1";
fn metric(row: &AuditRow, key: &str) -> Option<f64> {
    row.quality.features.get(key).copied().flatten()
}
fn number(row: &AuditRow, key: &str) -> f64 {
    metric(row, key).unwrap_or(0.)
}

fn reading(row: &AuditRow) -> Value {
    let f = |key: &str| number(row, key);
    let mut trend = Vec::new();
    let prior = f("ma20_slope_pre");
    let during = f("ma20_slope_a");
    trend.push(match (prior>0.1,during>0.1) {
        (true,true)=>"此前均线与信号同向，A段也继续同向发展，具备趋势延续的依据。",
        (false,true)=>"A段正在把均线带向信号方向；我会把它看成趋势建立或修复，和成熟趋势中的回踩分别对待。",
        (true,false)=>"此前均线虽与信号同向，A段期间却没有维持这种配合，不能只沿用先前的趋势标签。",
        (false,false)=>"原有均线背景与A段配合都不鲜明，我会谨慎看待它的顺势属性。",
    }.to_string());
    if f("warning_close_ma_atr") < -0.1 {
        trend.push(
            "预警收盘已在均线不利侧：此前推动有趋势优势，也不代表这次回撤没有造成破坏。".into(),
        );
    }
    let mut push=vec![format!("内部收盘路径效率约{:.0}%，内部最大回吐约占A段幅度{:.0}%；最长{}根、末端{}根没有有效新进展。",100.*f("a_core_efficiency"),100.*f("a_drawdown_ratio"),f("a_stall_bars") as usize,f("a_terminal_stall_bars") as usize)];
    if f("a_overlap_stagnation") > 0.35 {
        push.push(
            "内部有较明显的重叠争夺。我更关心这些重叠是否伴随新进展，而不只数同向K线。".into(),
        );
    } else {
        push.push("重叠争夺相对有限；即使是小K线，只要持续取得并守住进展，我也会认可。".into());
    }
    if f("a_full_efficiency") - f("a_core_efficiency") > 0.25 {
        push.push("完整A段与内部推进差异较大，端点K或跳跃推进的贡献可能集中。内部效率低不等于整段没有位移，需要对照图形判断。".into());
    }
    let mut pullback = vec![format!(
        "回撤约{:.0}%，平均速度约为A段的{:.2}倍。我会把回吐深度与过程中的对手力量分开看。",
        100. * f("retracement"),
        f("b_a_speed_ratio")
    )];
    if f("b_a_speed_ratio") > 1. {
        pullback.push(
            "回撤速度超过推动，反向力量偏强；即使最后出现反转预警，也不能把前面的破坏全部抵消。"
                .into(),
        );
    }
    if f("b_ma_crosses") >= 2. {
        pullback.push(format!(
            "回撤期间收盘反复穿线{}次，靠近均线更可能包含争夺，不能直接当成有效支撑或阻力。",
            f("b_ma_crosses") as usize
        ));
    } else if f("ma_proximity_atr") <= 0.2 {
        pullback.push(
            "回撤确实回到均线附近，我会继续检查穿越深度和预警收回的位置，而不只看距离。".into(),
        );
    }
    match metric(row, "b_decay") {
        Some(x) => pullback.push(format!(
            "排除预警K后，反向实体衰减约{:.0}%；这只是衰减证据之一。",
            100. * x
        )),
        None => pullback
            .push("排除预警K后，末端衰减样本不足；这里应保留未知，不能默认已经转弱。".into()),
    }
    let rr = f("rr_to_s1");
    let mut space = vec![format!("到S1约{rr:.2}R，我会将这项与形态本身分别评价。")];
    if rr < 0.5 {
        space.push(
            "从这个入场位置到S1不足半个初始风险，综合选择时我会明显降低空间这一项的评价。".into(),
        );
    } else if rr < 1. {
        space.push("到S1不足1R，形态即使顺眼，综合条件也需要单独权衡。".into());
    } else {
        space.push("到S1留有一定空间；仍需检查路径上已经存在的阻碍。".into());
    }
    json!({"trend":trend,"push":push,"pullback":pullback,"space":space})
}

fn comparison(left: &AuditRow, right: &AuditRow) -> Vec<String> {
    let mut result = Vec::new();
    let mut l = 0;
    let mut r = 0;
    for (key, epsilon, higher_better) in [
        ("a_core_efficiency", 0.1, true),
        ("a_drawdown_ratio", 0.05, false),
        ("a_stall_fraction", 0.1, false),
        ("a_overlap_stagnation", 0.15, false),
    ] {
        let delta = (number(left, key) - number(right, key)) * if higher_better { 1. } else { -1. };
        if delta > epsilon {
            l += 1;
        } else if delta < -epsilon {
            r += 1;
        }
    }
    result.push(if l>=2 && l>r {"单看A段的持续推进与进展保护，我更偏左侧；这种偏好不自动代表综合条件也选左。"}
        else if r>=2 && r>l {"单看A段的持续推进与进展保护，我更偏右侧；这种偏好不自动代表综合条件也选右。"}
        else {"两侧A段存在取舍，我暂不只凭幅度或平均速度给出明确胜者，尤其需要核对端点贡献与内部推进的差别。"}.into());
    let delta = number(left, "ma20_slope_a") - number(right, "ma20_slope_a");
    if delta.abs() > 0.3 {
        result.push(format!(
            "A段期间的均线配合更支持{}侧，但均线这一项不能替代对K线推进过程的评价。",
            if delta > 0. { "左" } else { "右" }
        ));
    }
    let lr = number(left, "rr_to_s1");
    let rr = number(right, "rr_to_s1");
    if (lr - rr).abs() > 0.25 {
        result.push(format!("仅比较到S1的空间，我更偏{}侧（左{lr:.2}R，右{rr:.2}R）；这可能让综合选择与形态选择不同。",if lr>rr{"左"}else{"右"}));
    }
    result
}

pub fn build(manifest: &Value, rows: &[AuditRow]) -> Result<Value> {
    let by_case: HashMap<_, _> = rows
        .iter()
        .filter(|r| r.quality.status == "ok")
        .map(|r| {
            (
                hash(format!("{}|{}", r.quality.event_key, r.quality.input_hash).as_bytes()),
                r,
            )
        })
        .collect();
    let mut pairs = serde_json::Map::new();
    for pair in manifest["pairs"]
        .as_array()
        .context("blind pairs missing")?
    {
        let left = by_case
            .get(
                pair["left"]["case_id"]
                    .as_str()
                    .context("left case missing")?,
            )
            .context("audit missing left case")?;
        let right = by_case
            .get(
                pair["right"]["case_id"]
                    .as_str()
                    .context("right case missing")?,
            )
            .context("audit missing right case")?;
        ensure!(
            left.quality.input_hash == pair["left"]["input_hash"]
                && right.quality.input_hash == pair["right"]["input_hash"],
            "audit case hash mismatch"
        );
        let item = json!({"pair_id":pair["pair_id"],"left_case_id":pair["left"]["case_id"],"right_case_id":pair["right"]["case_id"],"left_input_hash":pair["left"]["input_hash"],"right_input_hash":pair["right"]["input_hash"],"review_status":"structure_draft","left":reading(left),"right":reading(right),"comparison":comparison(left,right),"uncertainties":["这份草稿依据预警前原始结构指标整理，没有逐图视觉复核，也没有使用旧总分或新总分来代替判断。","成交量、更大周期背景及未能可靠重建的关键区域未进入本稿；你可以用补充说明指出遗漏和不同取舍。"]});
        pairs.insert(pair["pair_id"].as_str().context("pair id")?.into(), item);
    }
    let mut notes = json!({"version":VERSION,"base_manifest_hash":manifest["manifest_hash"],"method":"assistant-authored structural interpretation; per-chart visual review is explicitly marked","pairs":pairs});
    seal(&mut notes)?;
    Ok(notes)
}
fn seal(notes: &mut Value) -> Result<()> {
    notes
        .as_object_mut()
        .context("notes object")?
        .remove("notes_hash");
    notes["notes_hash"] = json!(hash(&serde_json::to_vec(notes)?));
    Ok(())
}
pub fn load(path: &Path, manifest: &Value) -> Result<Value> {
    let mut notes: Value = serde_json::from_slice(&std::fs::read(path)?)?;
    let claimed = notes
        .as_object_mut()
        .context("notes object")?
        .remove("notes_hash")
        .context("notes hash missing")?;
    ensure!(
        claimed == hash(&serde_json::to_vec(&notes)?),
        "assistant notes changed"
    );
    ensure!(
        notes["base_manifest_hash"] == manifest["manifest_hash"],
        "assistant notes belong to another task"
    );
    notes["notes_hash"] = claimed;
    Ok(notes)
}
pub fn write_page(out: &Path, manifest: &Value, notes: &Value) -> Result<()> {
    write_json(&out.join("assistant-notes.json"), notes)?;
    let safe = |v: &Value| -> Result<String> {
        Ok(serde_json::to_string(v)?
            .replace('<', "\\u003c")
            .replace('>', "\\u003e")
            .replace('&', "\\u0026"))
    };
    let html = include_str!("blind-review.html")
        .replace("__BLIND_PAYLOAD__", &safe(manifest)?)
        .replace("__ASSISTANT_PAYLOAD__", &safe(notes)?);
    std::fs::write(out.join("blind-review.html"), html)?;
    Ok(())
}

pub fn annotate(args: &[String], out: &Path) -> Result<()> {
    let arg = |k: &str| {
        args.windows(2)
            .find(|p| p[0] == k)
            .map(|p| p[1].clone())
            .with_context(|| format!("{k} required"))
    };
    let source = std::path::PathBuf::from(arg("--manifest")?);
    let manifest = super::blind::load_manifest(&source)?;
    let rows: Vec<AuditRow> = serde_json::from_slice(&std::fs::read(arg("--audit")?)?)?;
    let mut notes = build(&manifest, &rows)?;
    if let Some(path) = args
        .windows(2)
        .find(|p| p[0] == "--review-notes")
        .map(|p| &p[1])
    {
        let overrides: Value = serde_json::from_slice(&std::fs::read(path)?)?;
        ensure!(
            overrides["base_manifest_hash"] == manifest["manifest_hash"],
            "reviewed notes belong to another task"
        );
        for (id, review) in overrides["pairs"]
            .as_object()
            .context("reviewed pairs object missing")?
        {
            let base = notes["pairs"]
                .get_mut(id)
                .context("reviewed pair not in task")?;
            for key in [
                "left_case_id",
                "right_case_id",
                "left_input_hash",
                "right_input_hash",
            ] {
                ensure!(base[key] == review[key], "reviewed case identity mismatch");
            }
            ensure!(
                review["review_status"] == "reviewed_from_chart"
                    || review["review_status"] == "reviewed_from_structure",
                "review status required"
            );
            let text = review["comparison"]
                .as_array()
                .context("reviewed comparison required")?;
            ensure!(
                !text.is_empty()
                    && text
                        .iter()
                        .all(|v| v.as_str().is_some_and(|s| s.chars().count() <= 4000)),
                "invalid reviewed prose"
            );
            base["comparison"] = review["comparison"].clone();
            base["review_status"] = review["review_status"].clone();
            base["uncertainties"] = json!(["单侧分项保留指标草稿；比较结论已按标注来源复核，只使用预警前资料。", "成交量、更大周期背景和未能可靠重建的关键区域仍有信息缺口，可以在补充说明中质疑我的取舍。"]);
        }
        seal(&mut notes)?;
    }
    // Preserve the original task bytes/hash and keep existing judgments importable.
    std::fs::copy(&source, out.join("blind-manifest.json"))?;
    write_page(out, &manifest, &notes)?;
    println!(
        "Annotated blind review: {}",
        out.join("blind-review.html").display()
    );
    Ok(())
}
