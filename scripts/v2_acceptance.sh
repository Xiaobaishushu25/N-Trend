#!/usr/bin/env bash
set -e
echo "=== V2 Acceptance ==="
echo "[1/3] migrate dry-run"
cargo run -p n-core --bin migrate -- --dry-run
echo "[2/3] v2-dataset --paranoid"
cargo run -p n-core --bin v2-dataset -- --symbol all --from 2020-01-01 --paranoid
echo "[3/3] cargo test v2::"
cargo test -p n-core --lib v2:: -- --nocapture
echo "=== Acceptance complete — see target/v2_reports/acceptance.md ==="
cat target/v2_reports/acceptance.md || true
echo "--- dataset artifacts ---"
ls -lh target/v2_reports/ || true