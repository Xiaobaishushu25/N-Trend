# N-Trend V2 Probabilistic Trading — Spec (Phase 1-10)

> Phase 1-5: pipeline (contracts, V2 DB, features, replay, dataset) — delivered @ f21d3c5.
> Phase 6-8: modeling (Logistic baseline, GAM challenger, Rust inference) — pure Rust, no Python.
> Phase 9-10: UI / Shadow — placeholder in this doc, no decision logic change.

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

## Phase 6 — Logistic Baseline (pure Rust, hand-rolled)

- Hand-rolled `StandardScaler` (mean/std from train only) + L2-regularized gradient descent (`lr=0.05, epochs=400, l2=1.0`); no `linfa` heavy dep, zero Python.
- Input: `DatasetRow` via `ReplayEngine` -> `DatasetBuilder` (whitelist + `blake3` hash); `--from` controls window, empty DB falls back to deterministic synth (200 rows) so CI never blocks.
- Time split: 80% Development / 20% Final Test (strict `train_last < test_first` by `trigger_bar_ts`); Walk-Forward 5 expanding folds with purge (`assert_purge`).
- Metrics: Brier / LogLoss / AUC (pairwise tie-aware) / Accuracy / Calibration 10 bins / Top20% lift, plus Constant Baseline (train win rate) for Brier/LogLoss.
- Output: `v2_model_registry` (`logistic-v1-<hash8>`, feature_whitelist, train_window, dataset_hash, coefficients/intercept + scaler JSON, metrics, git_commit), `InferenceBundle` JSON (feature_order/mean/std/coefficients), `target/v2_reports/logistic_report.md`.

## Phase 7 — GAM Challenger (low-DF)

- Per-feature `df=3-4` quantile knots + linear interpolation lookup (`SplineTable::eval`); categorical `direction/level/warning_kind` via linear terms (future).
- One-pass training: knots = quantiles of feature in train; value at knot = shrunk mean logit residual in bin (`val = (logit(bin_win_rate)-intercept) * n/(n+l2)`, centered to zero mean for identifiability).
- Ablation groups in spec §32 order: `Base(a_move_atr) -> Setup(+b_move_atr/a_speed/retracement) -> Warning(+warning_volume_ratio) -> Trigger(+overshoot/close_location/body_atr/wick/swing/chase) -> Volume(+trigger_volume_ratio) -> OI` — only OOS-gain groups kept.
- Champion rule: GAM promotes to champion only if `auc > logistic +0.02` AND `brier < logistic` stably across folds; else logistic stays champion. Report `target/v2_reports/gam_report.md` + per-spline knots/values for curve plotting.
- Files: `crates/n-core/src/v2/model/gam.rs`, `crates/n-core/src/v2/model/metrics.rs`, `crates/n-core/src/v2/model/walk_forward.rs`.

## Phase 8 — Rust Inference & Versioning

- Pure-Rust `predict_p` reused from `logistic.rs` / `gam.rs` (no Python); `InferenceBundle` exports/imports JSON (`feature_whitelist/scaler_means/scaler_stds/coefficients/splines`) for reproducibility.
- `v2_model_predictions` is append-only: `prediction_mode=live/replay/shadow`, `feature_hash=blake3(whitelist+values)`, `predicted_at` frozen; history never overwritten by new model.
- Explanation API: Logistic returns per-feature `value/scaled_value/coefficient/contribution`; GAM returns `f_i(x)` per spline + linear terms, surfaced as `Prediction.contributions` for UI.
- CLI: `cargo run -p n-core --bin v2-train -- --from 2020-01-01 --paranoid` writes `target/v2_reports/{logistic_bundle.json,gam_bundle.json,logistic_report.md,gam_report.md}` and inserts both models into `v2_model_registry`. `cargo run -p n-core --bin v2-dataset` remains dataset-only.

## Phase 9 — Frontend P(win) Display (Deferred, placeholder)

- `v2_model_predictions.p_win` rendered in signal card as "P(win) 62% (logit 0.52)" with feature_hash badge.
- Threshold slider (user-tunable) filters displayed signals; does not affect pipeline.
- Setup quality 0-5 shown as stars, explicitly decoupled from P(win).

## Phase 10 — Shadow Prediction (Deferred, placeholder)

- Every `TriggerConfirmed` event writes a shadow row to `v2_model_predictions` with `p_win = NULL` until model is trained.
- Nightly job replays last 90 days via `ReplayEngine` and backfills `p_win` for calibration plots (reliability diagram).
- Shadow predictions are never auto-traded; they are reported in `target/v2_reports/shadow.md`.

## Training Data Contract

- Source of truth: local `ntrend.db` (`klines`) via `ReplayEngine` aggregation (5m -> 15/60, next-open entry, rollover gap isolation, dedup `symbol+s0/s1/s2+direction`).
- Live vs Replay share single `features::*` implementation; enforced by `feature_parity` test.
- Leakage guard: `assert_no_leakage` checks `trigger_bar_ts < exit_ts` and `feature_ts <= trigger_bar_ts`; walk-forward purge `train_last < valid_first`; `--paranoid` runs both.
- Reproducibility: `dataset_hash = blake3(features+labels+config)` stored in `model_registry` and reports.

## Out of Scope (Still)

Dynamic position sizing, EV/fee modeling, multi-target optimization, RL/NN, auto-retraining. Stack is pure Rust; `linfa` intentionally not introduced to keep build light — Logistic is hand-rolled gradient descent, GAM is quantile-binned lookup.
