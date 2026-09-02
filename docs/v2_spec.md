# N-Trend V2 Probabilistic Trading — Spec (Phase 9-10 Placeholder)

> This document reserves the UI and Shadow prediction interfaces for Phase 6+; Phase 1-5 is data pipeline only (no training weights).

## Decision Architecture

`SetupDetected -> WaitingTrigger -> TriggerTouched -> TriggerConfirmed -> ModelDecided -> Open/Skipped -> Closed`

- Setup snapshot is frozen at warning bar close (a/b legs, retracement, trend60, warning raw). Never rewritten by later bars.
- Trigger features are frozen at Trigger K close (overshoot_r, close_location, body_atr, wick_atr, volume_ratio, etc.).
- Direction normalization: short samples are mirrored to long perspective before dataset hashing (close_overshoot_r sign flip, close_location = 1 - loc).
- Missing policy: OI missing -> NULL + mask bit; ATR missing -> discard event (mask bit 4).

## Version Contracts

- `FEATURE_SCHEMA_VERSION = v2.1`
- `PATTERN_LOGIC_VERSION = v2-strict-1`
- `EXECUTION_VERSION = v2-exec-1`
- `LABEL_CONTRACT_VERSION = v2-label-1`
All four versions are written into every `v2_trade_events` row for reproducibility.

## Phase 9 — Frontend P(win) Display (Deferred)

- `v2_model_predictions.p_win` rendered in signal card as "P(win) 62% (logit 0.52)" with feature_hash badge.
- Threshold slider (user-tunable) filters displayed signals; does not affect pipeline.
- Setup quality 0-5 shown as stars, explicitly decoupled from P(win).

## Phase 10 — Shadow Prediction (Deferred)

- Every `TriggerConfirmed` event writes a shadow row to `v2_model_predictions` with `p_win = NULL` until model is trained.
- Nightly job replays last 90 days via `ReplayEngine` and backfills `p_win` for calibration plots (reliability diagram).
- Shadow predictions are never auto-traded; they are reported in `target/v2_reports/shadow.md`.

## Out of Scope (Still)

Dynamic position sizing, EV/fee modeling, multi-target optimization, RL/NN, auto-retraining.