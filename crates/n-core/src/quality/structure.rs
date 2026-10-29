//! Versioned, modular research evidence. No trading score, fitted threshold or automatic grade.
use super::{qualification, research::AuditRow};
use anyhow::{ensure, Context, Result};
use serde_json::{json, Value};

pub const VERSION: &str = "qualification-evidence-v2";
pub const DISPLAY: &str = "qualification-modules-v2";
pub const MODULES: [&str; 6] = ["trend_ma", "a_process", "b_process", "levels", "warning", "space"];

fn num(v: &Value) -> Option<f64> { v.as_f64().filter(|x| x.is_finite()) }
fn div(a: f64, b: f64) -> Option<f64> { (b > 1e-12).then(|| a / b) }
fn fmt(v: Option<f64>) -> String { v.map(|x| format!("{x:.2}")).unwrap_or_else(|| "数据不足".into()) }
fn pct(v: Option<f64>) -> String { v.map(|x| format!("{:.1}%", x * 100.)).unwrap_or_else(|| "数据不足".into()) }
fn module(title: &str, metrics: Value, facts: Vec<String>, reading: &str, limitations: &str) -> Value {
    json!({"title":title,"metrics":metrics,"facts":facts,"reading":reading,"limitations":limitations,"scoring_effect":"none"})
}
fn directional_path(p: &Value) -> Value {
    let signed = num(&p["close_net_atr"]).zip(num(&p["close_travel_atr"]))
        .and_then(|(n,t)| div(n,t)).map(|x| x.clamp(-1.,1.));
    json!({"bars":p["bars"],"signed_efficiency":signed,
        "forward_efficiency":signed.map(|x|x.max(0.)),"straightness":p["close_efficiency"],
        "net_atr":p["close_net_atr"],"travel_atr":p["close_travel_atr"],
        "mean_overlap":p["mean_overlap"],"mean_wick_fraction":p["mean_wick_fraction"]})
}
fn candle(b: &Value, d: f64) -> Value {
    let (o,h,l,c)=(b[0].as_f64().unwrap(),b[1].as_f64().unwrap(),b[2].as_f64().unwrap(),b[3].as_f64().unwrap());
    let range=h-l;
    let opposing=if d>0. {h-o.max(c)}else{o.min(c)-l};
    json!({"range_atr":range,"signed_body_atr":d*(c-o),"body_fraction":div((c-o).abs(),range),
        "opposing_wick_fraction":div(opposing,range),
        "close_location":div(if d>0. {c-l}else{h-c},range)})
}

/// All additional evidence uses the same warning-truncated payload as v1. Legacy MA and
/// stall/drawdown features are copied from the verified Rust audit, never reimplemented.
pub fn diagnose(case: &Value, warning: usize, row: Option<&AuditRow>, tick_atr: Option<f64>) -> Result<Value> {
    let old=qualification::diagnose(case,warning)?;
    let raw=case["bars"].as_array().context("bars")?;
    let bars=&raw[..=warning];
    let (s0,s1,s2)=(old["s0_index"].as_u64().unwrap() as usize,old["s1_index"].as_u64().unwrap() as usize,old["s2_index"].as_u64().unwrap() as usize);
    let d=if case["direction"]=="up" {1.}else{-1.};
    let feature=|key:&str| row.and_then(|r| r.quality.features.get(key).copied().flatten());
    let full=directional_path(&old["a"]["full_path"]);
    let interior=directional_path(&old["a"]["interior_path"]);
    let a=&old["a"];
    let concentration=&a["directional_body"];
    let records=&a["closing_records"];
    let mut am=json!({"bars":a["bars"],"interior_bars":a["interior_bars"],"amplitude_atr":a["amplitude_atr"],
        "full":full,"interior":interior,"directional_body":concentration,"closing_records":records,
        "legacy_process_window":a["legacy_process_window"],"tick_atr":tick_atr});
    for key in ["a_core_efficiency","a_full_efficiency","a_drawdown_ratio","a_stall_bars","a_terminal_stall_bars","a_overlap_stagnation","a_gap_count","a_gap_sum"] {
        am[key]=json!(feature(key));
    }
    let mut modules=serde_json::Map::new();
    let mut tm=json!({"persisted_trend_state":row.and_then(|r|r.old_dims["trend_state"].as_str())});
    for key in ["ma20_slope_pre","ma20_slope_a","ma20_slope_warning","ma_residence","a_ma_crosses","b_ma_crosses","warning_close_ma_atr","ma_departure_atr","ma_proximity_atr","ma_damage_atr","ma_retest"] {
        tm[key]=json!(feature(key));
    }
    let pre=feature("ma20_slope_pre"); let during=feature("ma20_slope_a");
    let trend_read=match (pre,during) {
        (Some(p),Some(a)) if p>0. && a>0. => "MA20在S0前及A段都沿信号方向发展；这是背景证据，仍需核对A推进、回撤破坏与前高前低。",
        (Some(p),Some(a)) if p<=0. && a>0. => "MA20从此前逆向或平缓转为A段同向，包含趋势建立或修复；与成熟趋势延续分开观察。",
        (Some(_),Some(_)) => "MA20背景与当前推动有取舍，不能沿用一个趋势标签代替过程；预警在均线哪侧及反复穿线需要同时保留。",
        _ => "缺少可信趋势指标，保留未知。",
    };
    modules.insert("trend_ma".into(),module("趋势与MA20",tm,vec![
        format!("S0前 / A段 / 预警MA20顺向斜率：{} / {} / {}。",fmt(pre),fmt(during),fmt(feature("ma20_slope_warning"))),
        format!("A段有利侧驻留{}；A / B穿线{} / {}次。",pct(feature("ma_residence")),fmt(feature("a_ma_crosses")),fmt(feature("b_ma_crosses"))),
        format!("预警收盘距MA20（顺向为正）{} ATR；离开 / 回到均线距离{} / {} ATR，平均不利穿越{} ATR。",fmt(feature("warning_close_ma_atr")),fmt(feature("ma_departure_atr")),fmt(feature("ma_proximity_atr")),fmt(feature("ma_damage_atr"))),
        format!("原落盘趋势状态：{}。",row.and_then(|r|r.old_dims["trend_state"].as_str()).unwrap_or("未知")),
    ],trend_read,"保留v1 Rust指标：斜率及距离按对应时点ATR归一；落盘趋势状态只作原记录，不宣称已重建。60分钟及手工区域未纳入。"));
    modules.insert("a_process".into(),module("A段持续推进",am,vec![
        format!("完整{}根 / 内部{}根，端点幅度{} ATR。",a["bars"],a["interior_bars"],fmt(num(&a["amplitude_atr"]))),
        format!("顺向收盘效率：完整{} / 内部{}；内部带方向效率{}，净位移{} ATR。",pct(num(&full["forward_efficiency"])),pct(num(&interior["forward_efficiency"])),pct(num(&interior["signed_efficiency"])),fmt(num(&interior["net_atr"]))),
        format!("顺向实体最大单根{}、前两根{}、等效{}根；收盘新纪录最大一步{}、等效{}步。",pct(num(&concentration["largest_share"])),pct(num(&concentration["largest_two_share"])),fmt(num(&concentration["effective_bars"])),pct(num(&records["largest_share"])),fmt(num(&records["effective_bars"]))),
        format!("旧过程窗口：{}；最大回吐占A幅度{}，最长 / 末端停滞{} / {}根，重叠停滞{}。",if a["legacy_process_window"]=="interior" {"剔除端点"}else{"短A回退完整段"},pct(feature("a_drawdown_ratio")),fmt(feature("a_stall_bars")),fmt(feature("a_terminal_stall_bars")),pct(feature("a_overlap_stagnation"))),
        format!("原口径大跳空{}处；内部平均重叠{}、影线{}。",fmt(feature("a_gap_count")),pct(num(&interior["mean_overlap"])),pct(num(&interior["mean_wick_fraction"]))),
    ],"效率说明来回路程，贡献分布说明推进由几根完成；两项并列观察。完整与内部的差距帮助定位端点影响，不据单一指标自动判合格。",
    "内部少于4根时证据偏少；两根仅有一步，100%不能当充分推进。零路程效率未知；逆行带方向效率为负，顺向效率为0。实体与收盘纪录分母不同。"));

    let end=s2.min(warning-1);
    let bs=&bars[s1+1..=end];
    let start_close=bars[s1][3].as_f64().unwrap();
    let peak_price=case["points"][1][1].as_f64().context("S1 price")?;
    let amplitude=a["amplitude_atr"].as_f64().unwrap();
    let mut prev=start_close;let mut record=0_f64;let mut run=0;let mut longest=0;
    let mut bodies=0;let mut advances=0;let mut record_steps=0;let mut travel=0.;let mut favorable=0.;let mut adverse=0.;
    let mut max_range=0_f64;let mut max_close=0_f64;let mut steps=Vec::new();let mut sizes=Vec::new();
    for b in bs {
        let (o,h,l,c)=(b[0].as_f64().unwrap(),b[1].as_f64().unwrap(),b[2].as_f64().unwrap(),b[3].as_f64().unwrap());
        let body=-d*(c-o);if body>1e-9 {bodies+=1; sizes.push(body);}
        let step=-d*(c-prev);travel+=step.abs();favorable+=step.max(0.);adverse+=(-step).max(0.);
        if step>1e-9 {advances+=1;run+=1;longest=longest.max(run);}else{run=0;}
        let retreat=-d*(c-start_close);let gain=(retreat-record).max(0.);if gain>1e-9 {record_steps+=1;}record=record.max(retreat);steps.push(gain);
        max_close=max_close.max(d*(peak_price-c));max_range=max_range.max(if d>0. {peak_price-l}else{h-peak_price});prev=c;
    }
    let last_retreat=(!bs.is_empty()).then_some(-d*(prev-start_close));
    let bm=json!({"bars_without_warning":bs.len(),"reverse_body_bars":bodies,"reverse_close_steps":advances,
        "reverse_record_steps":record_steps,"longest_reverse_close_run":longest,
        "mean_reverse_body_atr":if sizes.is_empty(){None}else{Some(sizes.iter().sum::<f64>()/sizes.len() as f64)},
        "max_retreat_from_s1_extreme_atr":(!bs.is_empty()).then_some(max_range),"max_close_retreat_from_s1_extreme_atr":(!bs.is_empty()).then_some(max_close),
        "retracement_of_a_before_warning":if bs.is_empty(){None}else{div(max_range,amplitude)},
        "net_retreat_from_s1_close_atr":last_retreat,"close_travel_from_s1_atr":travel,
        "reverse_close_travel_atr":favorable,"return_close_travel_atr":adverse,
        "signed_efficiency_from_s1":last_retreat.and_then(|n|div(n,travel)),
        "range_exit_bars":old["b"]["range_exit_bars"],"close_exit_bars":old["b"]["close_exit_bars"],
        "outside_s1_before_warning_atr":old["b"]["outside_s1_before_warning_atr"],
        "mean_overlap":old["b"]["path_without_warning"]["mean_overlap"],"mean_wick_fraction":old["b"]["path_without_warning"]["mean_wick_fraction"],
        "second_half_range_ratio":old["b"]["path_without_warning"]["second_half_range_ratio"],
        "b_a_speed_ratio":feature("b_a_speed_ratio"),"b_decay":feature("b_decay"),"warning_lag_after_s2":warning-s2});
    let br=if bs.is_empty(){"预警前没有B段样本；预警K不能充当此前已回撤的证据。"}
        else if bs.len()==1 {"预警前仅1根B；记录其反向尝试，但持续过程尚缺样本，不据此自动淘汰或认可。"}
        else if advances>=2 || bodies>=2 {"预警前已有多次反向K线或收盘推进尝试；即使未出S1整根范围，也与单根回撤不同。还要结合累计位移、反复和持续时间，不能仅靠根数判合格。"}
        else {"虽有多根B，实际反向推进尝试仍有限；需要区分原地重叠与有位移的回撤过程。"};
    modules.insert("b_process".into(),module("B段回撤过程",bm,vec![
        format!("排除预警共{}根：反向实体{}根、反向收盘步数{}、新回撤纪录{}步、最长连续反向收盘{}步。",bs.len(),bodies,advances,record_steps,longest),
        format!("从S1极值回撤：区间{} / 收盘{} ATR，占A幅度{}；从S1收盘净回撤{} ATR。",fmt((!bs.is_empty()).then_some(max_range)),fmt((!bs.is_empty()).then_some(max_close)),pct(if bs.is_empty(){None}else{div(max_range,amplitude)}),fmt(last_retreat)),
        format!("反向收盘路程{} ATR，折返{} ATR，带方向回撤效率{}；首步从S1收盘计入。",fmt(Some(favorable)),fmt(Some(adverse)),pct(last_retreat.and_then(|n|div(n,travel)))),
        format!("辅助范围证据：超出S1整根区间{} ATR，区间 / 收盘超出{} / {}根。",fmt(num(&old["b"]["outside_s1_before_warning_atr"])),old["b"]["range_exit_bars"],old["b"]["close_exit_bars"]),
        format!("原口径B/A速度比{}，排除预警后的实体衰减{}；前后半区间比{}（不足4根未知）。",fmt(feature("b_a_speed_ratio")),pct(feature("b_decay")),fmt(num(&old["b"]["path_without_warning"]["second_half_range_ratio"]))),
    ],br,"B段截至S2并排除预警；不奖励无限延长，也不把两根、出S1范围或衰减当作自动资格门槛。反向实体、相邻收盘进展与创回撤纪录分别计数。"));

    let bg=&bars[s0.saturating_sub(40)..s0];let offset=s0.saturating_sub(40);
    let hi=bg.iter().filter_map(|b|num(&b[1])).reduce(f64::max);
    let lo=bg.iter().filter_map(|b|num(&b[2])).reduce(f64::min);
    let mut pivots=Vec::new();
    // Two candles on both sides must be completed BEFORE S0. No future confirmation.
    for i in 2..bg.len().saturating_sub(2) {
        let h=bg[i][1].as_f64().unwrap();let l=bg[i][2].as_f64().unwrap();
        let neighbors=[i-2,i-1,i+1,i+2];
        if neighbors.iter().all(|&j|h>bg[j][1].as_f64().unwrap()) {pivots.push(json!({"kind":"high","index":offset+i,"confirmed_index":offset+i+2,"price_atr":h}));}
        if neighbors.iter().all(|&j|l<bg[j][2].as_f64().unwrap()) {pivots.push(json!({"kind":"low","index":offset+i,"confirmed_index":offset+i+2,"price_atr":l}));}
    }
    let entry=num(&case["entry"]).context("entry")?;let stop=num(&case["stop"]).context("stop")?;let risk=d*(entry-stop);ensure!(risk>0.,"nonpositive risk");
    let mut obstacles:Vec<f64>=pivots.iter().filter(|p|p["kind"]==if d>0.{"high"}else{"low"}).filter_map(|p|num(&p["price_atr"])).collect();
    if let Some(p)=if d>0.{hi}else{lo} {obstacles.push(p);}
    let nearest=obstacles.into_iter().map(|p|d*(p-entry)).filter(|x|*x>1e-9).min_by(f64::total_cmp);
    let lm=json!({"lookback_bars":bg.len(),"asof_index":s0.checked_sub(1),"prior_high_atr":hi,"prior_low_atr":lo,
        "high_distance_from_entry_atr":hi.map(|h|d*(h-entry)),"low_distance_from_entry_atr":lo.map(|l|d*(l-entry)),
        "nearest_forward_obstacle_atr":nearest,"nearest_forward_obstacle_r":nearest.map(|x|x/risk),"confirmed_pivots":pivots});
    modules.insert("levels".into(),module("前高前低与位置",lm,vec![
        format!("S0之前{}根已完成K线：前高{} / 前低{}（相对S0的ATR坐标）。",bg.len(),fmt(hi),fmt(lo)),
        format!("入场前方最近的窗口极值或已确认拐点距离{} ATR / {}R。",fmt(nearest),fmt(nearest.map(|x|x/risk))),
        format!("局部拐点{}个，左右各2根，右侧确认最晚在S0之前完成。",pivots.len()),
    ],"保留前高前低位置，区分到S1空间和路径上的已知结构。前方没有此窗口内的候选位置只表示未识别，不代表没有阻力或支撑。",
    "统一使用S0前最多40根窗口，与旧页范围一致；这是可复现的关键位代理，不是完整历史结构、60分钟或手工灰紫区域。严格极值不识别平台高低点。"));
    let endpoint=candle(&bars[s1],d);let warn=candle(&bars[warning],d);
    modules.insert("warning".into(),module("端点与预警",json!({"s1":endpoint,"warning":warn}),vec![
        format!("S1逆向影线占本根{}；顺向实体{} ATR。",pct(num(&endpoint["opposing_wick_fraction"])),fmt(num(&endpoint["signed_body_atr"]))),
        format!("预警振幅{} ATR，顺向实体{} ATR，顺向收盘位置{}、逆向影线{}。",fmt(num(&warn["range_atr"])),fmt(num(&warn["signed_body_atr"])),pct(num(&warn["close_location"])),pct(num(&warn["opposing_wick_fraction"]))),
    ],"记录端点拒绝与预警反应；长影线先作为待验证观测，不新增扣分，也不让强预警直接补偿全部结构问题。",
    "是否扣分留待主要结构调整后历史消融；人工标签不是盈利真值。"));
    modules.insert("space".into(),module("交易空间",json!({"rr_to_s1":case["rr"],"risk_atr":risk,"risk_ticks":tick_atr.and_then(|t|div(risk,t))}),vec![
        format!("入场到S1 {}R，初始风险{} ATR / {} tick。",fmt(num(&case["rr"])),fmt(Some(risk)),fmt(tick_atr.and_then(|t|div(risk,t)))),
    ],"空间与形态分别评价；到S1的结构空间不是回放目标或盈利概率。",
    "tick由校验过的快照品种数据补齐；若缺失则未知。综合机会可能来自箱体等其他理由，不能自动变成N形态分。"));
    Ok(json!({"version":VERSION,"warning_index":warning,"s0_index":s0,"s1_index":s1,"s2_index":s2,
        "module_order":MODULES,"modules":modules,"scoring_effect":"diagnostic_only"}))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn sample()->Value {json!({"direction":"up","bars":[[0,1,0,1,null],[1,2,1,2,null],[2,3,2,3,null],[3,5,2,4,null],[4,4,3,3.5,null],[3.5,3.6,2.8,3,null],[3,4,2.9,3.8,null]],"points":[[0,0],[3,5],[6,2.9]],"entry":4.,"stop":2.,"rr":0.5})}
    #[test]
    fn reverse_efficiency_and_sparse_windows_do_not_claim_forward_progress() {
        let p=json!({"bars":3,"close_net_atr":-2.,"close_travel_atr":2.,"close_efficiency":1.});
        let d=directional_path(&p);assert_eq!(d["signed_efficiency"],-1.);assert_eq!(d["forward_efficiency"],0.);
        assert!(directional_path(&json!({"close_net_atr":0.,"close_travel_atr":0.}))["forward_efficiency"].is_null());
    }
    #[test]
    fn inside_s1_multi_bar_pullback_is_distinct_and_warning_does_not_create_it() {
        let c=sample();let x=diagnose(&c,6,None,None).unwrap();let b=&x["modules"]["b_process"]["metrics"];
        assert_eq!(b["outside_s1_before_warning_atr"],0.);assert_eq!(b["reverse_body_bars"],2);assert_eq!(b["reverse_close_steps"],2);assert_eq!(b["longest_reverse_close_run"],2);
        let mut changed=c;changed["bars"][6]=json!([3,90,-80,4,null]);changed["bars"].as_array_mut().unwrap().push(json!([9,100,-100,0,null]));
        assert_eq!(b,&diagnose(&changed,6,None,None).unwrap()["modules"]["b_process"]["metrics"]);
    }
    #[test]
    fn confirmed_levels_are_causal_and_modules_are_direction_symmetric() {
        let mut c=sample();let prefix=vec![json!([0,2,-1,1,null]),json!([0,3,-2,1,null]),json!([0,6,-3,1,null]),json!([0,4,-2,1,null]),json!([0,2,-1,1,null])];
        c["bars"].as_array_mut().unwrap().splice(0..0,prefix);for p in c["points"].as_array_mut().unwrap(){p[0]=json!(p[0].as_u64().unwrap()+5);}
        let x=diagnose(&c,11,None,Some(0.1)).unwrap();let levels=&x["modules"]["levels"]["metrics"];assert_eq!(levels["prior_high_atr"],6.);assert_eq!(levels["confirmed_pivots"][0]["confirmed_index"],4);
        let mut mirror=c.clone();mirror["direction"]=json!("down");for b in mirror["bars"].as_array_mut().unwrap(){*b=json!([-b[0].as_f64().unwrap(),-b[2].as_f64().unwrap(),-b[1].as_f64().unwrap(),-b[3].as_f64().unwrap(),null]);}for p in mirror["points"].as_array_mut().unwrap(){p[1]=json!(-p[1].as_f64().unwrap());}for k in ["entry","stop"]{mirror[k]=json!(-mirror[k].as_f64().unwrap());}
        let y=diagnose(&mirror,11,None,Some(0.1)).unwrap();for k in ["a_process","b_process","warning","space"]{assert_eq!(x["modules"][k]["metrics"],y["modules"][k]["metrics"]);}
        assert_eq!(levels["nearest_forward_obstacle_atr"],y["modules"]["levels"]["metrics"]["nearest_forward_obstacle_atr"]);
        let mut future=c;future["bars"][10]=json!([0,100,-100,0,null]);assert_eq!(levels,&diagnose(&future,11,None,Some(0.1)).unwrap()["modules"]["levels"]["metrics"]);
    }
}
