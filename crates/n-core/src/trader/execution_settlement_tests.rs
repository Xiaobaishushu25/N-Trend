use super::*;
use serde_json::json;

fn record(id: &str, opening: f64, closing: f64, gross: f64) -> TradeRecord {
    serde_json::from_value(json!({
        "id": id, "condition_order_id": "position", "symbol": "MA0", "instrument_id": "MA701",
        "direction": "up", "tier": "small", "volume": 1, "fill_price": opening,
        "exit_price": closing, "risk": 1.0, "initial_stop_price": 1.0, "target_price": 1.0,
        "open_ts": "2026-11-25 21:10:00", "exit_ts": "2026-11-26 09:05:00",
        "exit_reason": "target", "realized_pnl_pts": closing - opening, "r_multiple": 1.0,
        "max_mfe_r": 1.0, "max_mae_r": 0.0, "bars_held": 1, "realized_pnl_amount": gross
    }))
    .unwrap()
}

fn fill(id: &str, side: &str, price: &str, quantity: i32, fee_cents: i64) -> StatementFill {
    StatementFill {
        exchange: "CZCE".into(),
        instrument: "ma701".into(),
        side: side.into(),
        price: price.into(),
        quantity,
        fee_cents,
        trade_id: id.into(),
        offset: if side == "buy" { "open" } else { "close" }.into(),
    }
}

fn detail(price: &str, opening: &str) -> StatementClose {
    StatementClose {
        exchange: "CZCE".into(),
        instrument: "ma701".into(),
        side: "sell".into(),
        price: price.into(),
        quantity: 1,
        open_day: "20261126".into(),
        open_price: opening.into(),
    }
}

fn fixture(open_fee_a: i64, open_fee_b: i64) -> Input {
    let fills = vec![
        fill("909999991001000101", "buy", "2300", 1, open_fee_a),
        fill("909999991001000102", "buy", "2303", 1, open_fee_b),
        fill("909999991001000103", "sell", "2310", 1, 50),
        fill("909999991001000104", "sell", "2320", 1, 50),
    ];
    let statement = Statement {
        trading_day: "20261126".into(),
        currency: "CNY".into(),
        broker_id: "9999".into(),
        investor_id: "test".into(),
        environment: "simnow".into(),
        total_fee_cents: Some(fills.iter().map(|fill| fill.fee_cents).sum()),
        fills,
        closes: vec![detail("2310", "2303"), detail("2320", "2300")],
    };
    Input {
        account: Some("9999:test".into()),
        history: SharedState(Arc::new(vec![
            record(
                "20261126:CZCE:MA701:sell:000000000103",
                2300.0,
                2310.0,
                100.0,
            ),
            record(
                "20261126:CZCE:MA701:sell:000000000104",
                2303.0,
                2320.0,
                170.0,
            ),
        ])),
        seen: SharedState(Arc::new(HashSet::from([
            "20261126:CZCE:MA701:buy:000000000101".into(),
            "20261126:CZCE:MA701:buy:000000000102".into(),
        ]))),
        links: SharedState::default(),
        days: SharedState(Arc::new(HashMap::from([("20261126".into(), statement)]))),
    }
}

#[test]
fn settlement_identifies_czce_fills_with_invariant_fees_despite_different_openings() {
    let rows = reconcile(&fixture(100, 100));
    assert_eq!(rows.len(), 2);
    assert!(rows
        .values()
        .all(|row| row.status == "confirmed" && row.source == "simnow_settlement"));
    assert_eq!(
        rows.values()
            .filter_map(|row| row.commission_cents)
            .sum::<i64>(),
        300
    );
    assert_eq!(
        rows.values()
            .filter_map(|row| row.net_pnl_cents)
            .sum::<i64>(),
        26_700
    );
}

#[test]
fn settlement_does_not_confirm_ambiguous_amounts_or_another_accounts_fees() {
    let mut input = fixture(100, 150);
    assert!(reconcile(&input)
        .values()
        .all(|row| row.status == "unmatched"
            && row.net_pnl_cents.is_none()
            && row.close_fee_cents == Some(50)));
    input.account = Some("9999:other".into());
    assert!(reconcile(&input)
        .values()
        .all(|row| row.status == "pending" && row.close_fee_cents.is_none()));
}

#[test]
fn settlement_fees_preserve_zero_and_prorate_the_last_cent() {
    assert_eq!(fee_share(1, 3, 0, 1), 0);
    assert_eq!(fee_share(1, 3, 1, 1), 1);
    assert_eq!(fee_share(1, 3, 2, 1), 0);
    let mut input = fixture(0, 0);
    let statement = input.days.get_mut("20261126").unwrap();
    statement.total_fee_cents = Some(0);
    for fill in &mut statement.fills {
        fill.fee_cents = 0;
    }
    assert!(reconcile(&input)
        .values()
        .all(|row| row.status == "confirmed" && row.commission_cents == Some(0)));
}

#[test]
fn settlement_uses_persisted_opening_allocations_for_new_closes() {
    let mut input = fixture(100, 150);
    let first = "20261126:CZCE:MA701:buy:000000000101";
    let second = "20261126:CZCE:MA701:buy:000000000102";
    let opening = |price| FillLink {
        position_id: "position".into(),
        price,
        volume: 1,
        broker_volume: 1,
        offset: "open".into(),
        time: "21:10:00".into(),
        opening_allocations: None,
    };
    input.links.insert(first.into(), opening(2300.0));
    input.links.insert(second.into(), opening(2303.0));
    for (record, id) in input.history.iter().zip([first, second]) {
        input.links.insert(
            record.id.clone(),
            FillLink {
                position_id: "position".into(),
                price: record.exit_price,
                volume: 1,
                broker_volume: 1,
                offset: "close_today".into(),
                time: "09:05:00".into(),
                opening_allocations: Some(vec![(id.to_owned(), 1)]),
            },
        );
    }
    let fees = reconcile(&input);
    assert_eq!(fees[&input.history[0].id].commission_cents, Some(150));
    assert_eq!(fees[&input.history[1].id].commission_cents, Some(200));
}

#[test]
fn settlement_retry_reply_decodes_without_statement_fields() {
    let result: SettlementReply = serde_json::from_value(json!({"status": "retry"})).unwrap();
    assert!(result.statement.is_none());
}

#[tokio::test]
async fn settlement_reply_is_idempotent_and_updates_history_and_totals_together() {
    let engine = ExecutionEngine::new(CtpTraderClient::for_port(1), ExecutionConfig::default());
    let input = fixture(100, 100);
    {
        let mut state = engine.state.lock().await;
        state.account = input.account;
        state.history = input.history;
        state.seen_trades = input.seen;
    }
    let mut body = serde_json::to_value(&input.days["20261126"]).unwrap();
    body["status"] = json!("verified");
    let mut cache = ExecutionSnapshotCache::default();
    let mut publisher = n_protocol::trader_stream::TraderPublisher::new("settlement-test".into());
    let prior = engine.snapshot_fields(&mut cache, false).await.unwrap();
    publisher.update_shared(prior, false);
    for _ in 0..2 {
        assert!(engine
            .apply_settlement_reply("20261126", serde_json::from_value(body.clone()).unwrap())
            .await
            .unwrap());
    }
    engine.refresh_settlement_projection().await.unwrap();
    assert!(engine.settlement_days_to_query().await.is_empty());
    let next = engine.snapshot_fields(&mut cache, false).await.unwrap();
    let (full, delta, _) = publisher.update_shared(next, false);
    let data = full.to_value();
    assert_eq!(
        data["history"][0]["fee_reconciliation"]["commission_cents"],
        150
    );
    assert_eq!(data["statistics"]["total_pnl"], 270.0);
    assert_eq!(
        data["statistics"]["fee_reconciliation"]["net_pnl_cents"],
        26_700
    );
    let changes = serde_json::to_value(delta.unwrap()).unwrap();
    assert!(changes["changes"]["history"].is_array());
    assert_eq!(
        changes["changes"]["statistics"]["fee_reconciliation"]["confirmed_count"],
        2
    );
}

#[test]
#[ignore = "requires a sanitized read-only CTP statement fixture at NTREND_SETTLEMENT_FIXTURE"]
fn settlement_acceptance_existing_ctp_accounts() {
    let path = std::env::var_os("NTREND_SETTLEMENT_FIXTURE").expect("fixture path");
    let raw = std::fs::read_to_string(path).unwrap();
    let cases: Vec<serde_json::Value> =
        serde_json::from_str(raw.trim_start_matches('\u{feff}')).unwrap();
    assert_eq!(cases.len(), 4);
    for case in cases {
        let mut history = Vec::new();
        for row in case["history"].as_array().unwrap() {
            let mut value = serde_json::to_value(record("", 0.0, 0.0, 0.0)).unwrap();
            value
                .as_object_mut()
                .unwrap()
                .extend(row.as_object().unwrap().clone());
            history.push(serde_json::from_value(value).unwrap());
        }
        let input = Input {
            account: case["account"].as_str().map(str::to_owned),
            history: SharedState(Arc::new(history)),
            seen: serde_json::from_value(case["seen"].clone()).unwrap(),
            days: serde_json::from_value(case["statements"].clone()).unwrap(),
            links: SharedState::default(),
        };
        let fees = reconcile(&input);
        let confirmed = fees
            .values()
            .filter(|row| row.status == "confirmed")
            .collect::<Vec<_>>();
        let expected = case["expected"].as_array().unwrap();
        let dates = input
            .history
            .iter()
            .filter_map(|row| TradeKey::parse(&row.id))
            .map(|key| key.day)
            .collect::<HashSet<_>>();
        let matches = input
            .seen
            .iter()
            .filter_map(|key| TradeKey::parse(key))
            .filter(|key| {
                dates.contains(&key.day)
                    && input
                        .days
                        .get(&key.day)
                        .is_some_and(|statement| key.identities(statement).len() == 1)
            })
            .count();
        let actual = [
            matches as i64,
            confirmed.len() as i64,
            confirmed
                .iter()
                .filter_map(|row| row.commission_cents)
                .sum::<i64>(),
            confirmed
                .iter()
                .filter_map(|row| row.net_pnl_cents)
                .sum::<i64>(),
        ];
        let wanted = expected
            .iter()
            .map(|x| x.as_i64().unwrap())
            .collect::<Vec<_>>();
        if actual.as_slice() != wanted {
            let mut statuses = HashMap::new();
            for row in fees.values() {
                *statuses
                    .entry((row.trading_day.clone(), row.status))
                    .or_insert(0) += 1;
            }
            eprintln!("instance {}: {statuses:?}", case["port"]);
        }
        assert_eq!(actual.as_slice(), wanted, "instance {}", case["port"]);
    }
}
