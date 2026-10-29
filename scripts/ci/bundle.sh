#!/usr/bin/env bash
# Pack what a scenario run needs into one archive, so a machine without the build runs scenarios.
#
# Usage: scripts/ci/bundle.sh <archive.tar.gz>
#   after sim_build.sh and scenarios/correctness/prepare.py, with the same sim_env.sh variables.
#   Unpack with `tar -xPzf <archive.tar.gz>`: the paths are absolute, because coyote_vl opens the
#   pipes of $WORK by its absolute path. The hosted runners have the same $HOME, $RUNNER_TEMP and
#   checkout path in every job.
#
# The archive: the libraries in $OPT_SIM and $OPT/lib, coyote_vl, $WORK, the DuckDB shell, the
# generated correctness scenarios, and sim_params.svh of the Coyote submodule (run.py checks the
# simulator variables against it).
set -euo pipefail

. "$(dirname "$0")/sim_env.sh"
[ $# -eq 1 ] || { echo "usage: $0 <archive.tar.gz>" >&2; exit 2; }

# -h stores the targets of symbolic links: coyote_vl links into obj/, the libraries link to their versions.
tar -czhPf "$1" "$OPT_SIM/lib" "$OPT/lib" "$VL_OUT/coyote_vl" "$WORK" "$DUCKDB" "$CORRECTNESS" \
    "$CYT/sim/hw/sim_params.svh"
ls -l "$1"
