#!/usr/bin/env bash
# Build the simulation stack (sim_build.sh) and run the TPC-H Q6 scenario (run_scenario.sh).
#
# Usage: scripts/ci/q6_verilator.sh
#   from the Oasis checkout, after scripts/ci/pin_submodules.sh. The exit code is the one of
#   scenarios/run.py: 0 when every check of the scenario passes.
#
# Needs: what sim_build.sh needs, and the duckdb Python module for prepare.py.
# Environment (all optional): the variables of sim_env.sh, and
#   OUT      run folder of the scenario (default runs/q6 in the checkout)
#   SCENARIO scenario file (default scenarios/tpch_q6/scenario.json)
#   TIMEOUT  seconds for the DuckDB shell (default 1800)
set -euo pipefail

. "$(dirname "$0")/sim_env.sh"
SCENARIO=${SCENARIO:-$OASIS/scenarios/tpch_q6/scenario.json}

"$OASIS/scripts/ci/sim_build.sh"

step "lineitem.parquet and image.bin"
python3 "$OASIS/scenarios/tpch_q6/prepare.py"

step "TPC-H Q6 under Verilator"
OUT=${OUT:-$OASIS/runs/q6} "$OASIS/scripts/ci/run_scenario.sh" "$SCENARIO"
