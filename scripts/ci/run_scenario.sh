#!/usr/bin/env bash
# Run one scenario against the Verilator build.
#
# Usage: scripts/ci/run_scenario.sh <name | scenario.json> [run.py options]
#   <name> is an entry of scenarios/correctness/out/scenarios.json (prepare.py writes it); any other
#   scenario is given by its file. The exit code is the one of scenarios/run.py.
#   Needs the build of sim_build.sh, or a bundle of bundle.sh unpacked at the same paths.
#
# Environment (all optional): the variables of sim_env.sh, and
#   OUT      run folder (default runs/<name> in the checkout, <name> without .json for a file)
#   TIMEOUT  seconds for the DuckDB shell (default 1800)
set -euo pipefail

. "$(dirname "$0")/sim_env.sh"
[ $# -ge 1 ] || { echo "usage: $0 <name | scenario.json> [run.py options]" >&2; exit 2; }
if [ -f "$1" ]; then scenario=$1; else scenario=$CORRECTNESS/$1.json; fi
shift

"$OASIS/scenarios/run.py" "$scenario" --sim-dir "$WORK" --duckdb "$DUCKDB" \
    --verilator-bin "$VL_OUT/coyote_vl" --out "${OUT:-$OASIS/runs/$(basename "$scenario" .json)}" \
    --timeout "${TIMEOUT:-1800}" "$@"
