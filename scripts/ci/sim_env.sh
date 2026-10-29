# Paths of the simulation build and the step() banner, shared by sim_build.sh, bundle.sh, run_scenario.sh and q6_verilator.sh.
# Source it, do not run it.
#
# Environment (all optional):
#   OPT      prefix of jemalloc (default $HOME/opt; kept when $OPT/lib/libjemalloc.so exists)
#   OPT_SIM  prefix of the simulation library, libstf, parcore and oasis (default $HOME/opt-sim)
#   BUILD    folder of the CMake build trees (default $RUNNER_TEMP/sim-build, or /tmp/sim-build)
#   WORK     the hardware build folder, COYOTE_SIM_DIR of the run (default $RUNNER_TEMP/sim-work).
#            coyote_vl opens the pipes of this folder by its absolute path: a copy of the binary
#            runs only with the folder at the same path.
#   VL_OUT   output of the Verilator build (default $RUNNER_TEMP/sim-vl)
#   JOBS     compile jobs (default nproc)

OASIS=$(realpath "$(dirname "${BASH_SOURCE[0]}")/../..")
CYT=$OASIS/parcore/libstf/coyote
TMP=${RUNNER_TEMP:-/tmp}
OPT=${OPT:-$HOME/opt}
OPT_SIM=${OPT_SIM:-$HOME/opt-sim}
BUILD=${BUILD:-$TMP/sim-build}
WORK=${WORK:-$TMP/sim-work}
VL_OUT=${VL_OUT:-$TMP/sim-vl}
JOBS=${JOBS:-$(nproc)}
DUCKDB=$OASIS/extension/build/release/duckdb
CORRECTNESS=$OASIS/scenarios/correctness/out

# The installed libraries carry no rpath and the exported targets of libstf, parcore and oasis do not
# name jemalloc or the simulation library, so the link of the shell and every run need both paths.
# CMake reads CMAKE_PREFIX_PATH from the environment, also in the plain cmake call of the extension makefile.
export CMAKE_PREFIX_PATH=$OPT_SIM:$OPT
export LD_LIBRARY_PATH=$OPT_SIM/lib:$OPT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}

step() { echo; echo "== $*"; }
