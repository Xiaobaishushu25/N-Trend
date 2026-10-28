include!("../src/bin/swing_structure_diagnostic.rs");
#[test]
fn missing_quote_and_lunch_calendar_are_distinct(){
    assert!(missing("RB0",time("2026-01-05 09:01:00"),time("2026-01-05 09:03:00"),&[]));
    assert!(!missing("RB0",time("2026-01-05 11:30:00"),time("2026-01-05 13:31:00"),&[]));
    assert!(missing("RB0",time("2026-01-05 11:30:00"),time("2026-01-05 13:32:00"),&[]));
    assert!(missing("PB0",time("2026-01-05 23:59:00"),time("2026-01-06 00:01:00"),&[]));
}
#[test]
fn day_only_archive_is_not_expected_to_have_night_quotes(){
    assert!(!missing("SF0",time("2026-01-05 15:00:00"),time("2026-01-06 09:01:00"),&[]));
    assert!(missing("RB0",time("2026-01-05 15:00:00"),time("2026-01-06 09:01:00"),&[]));
}
fn stopped()->Value{json!({"slip":1.,"result":{"evaluation":{"exit_ts":"2026-01-05 09:02:00","fill":100.,"risk":10.,"final_price":89.}}})}
fn bar(t:&str,o:f64,h:f64,l:f64,c:f64)->Kline{Kline{datetime:t.into(),open:o,high:h,low:l,close:c,volume:1.,hold:1.}}
#[test]
fn same_minute_recovery_and_wider_stop_touch_are_unknown_order(){
    let e=ReplayEvent{symbol:"RB0".into(),direction:"up".into(),tick_size:1.,..Default::default()};
    let bars=[bar("2026-01-05 09:03:00",89.,101.,89.,100.),bar("2026-01-05 09:04:00",100.,111.,79.,100.)];
    let v=recovery(&stopped(),&e,&bars,4,&[]);assert_eq!(v["entry_vs_two_r"],"recovery_first");assert_eq!(v["one_r_vs_two_r"],"same_minute_ambiguous");assert_eq!(v["complete"],false);
}
#[test]
fn missing_recovery_minutes_are_not_counted_as_success(){
    let e=ReplayEvent{symbol:"RB0".into(),direction:"up".into(),tick_size:1.,..Default::default()};
    let v=recovery(&stopped(),&e,&[bar("2026-01-05 09:04:00",100.,111.,79.,100.)],4,&[]);
    assert_eq!(v["quality"],"missing_data");assert!(v["entry_recovery"].is_null());
}
#[test]
fn gap_already_beyond_two_r_cannot_be_rescued_by_widening(){
    let e=ReplayEvent{symbol:"RB0".into(),direction:"up".into(),tick_size:1.,..Default::default()};let mut row=stopped();row["result"]["evaluation"]["final_price"]=json!(77.);
    let v=recovery(&row,&e,&[bar("2026-01-05 09:03:00",80.,111.,79.,100.)],4,&[]);assert_eq!(v["entry_vs_two_r"],"two_r_adverse_first");
}
