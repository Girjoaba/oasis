#!/usr/bin/env bash
# Build the Oasis software against the Coyote simulation library, the Oasis vFPGA with Verilator,
# and run the TPC-H Q6 scenario. No Vivado: sim/vl/configure.sh of the Coyote submodule configures
# hardware/ with a stub Vivado and writes the generated sources and the file lists from the checkout.
#
# Usage: scripts/ci/q6_verilator.sh
#   from the Oasis checkout, after scripts/ci/pin_submodules.sh. The exit code is the one of
#   scenarios/run.py: 0 when every check of the scenario passes.
#
# Needs in PATH: verilator (the pin of sim/vl/verilator.env of the Coyote submodule or newer),
# ghdl (5 or newer, Verilog output), cmake, ninja, g++, python3 with the duckdb and jinja2 modules, and ccache
# when the DuckDB build should be cached. Boost headers, autoconf and wget for jemalloc.
#
# Environment (all optional):
#   OPT      prefix of jemalloc (default $HOME/opt; kept when $OPT/lib/libjemalloc.so exists)
#   OPT_SIM  prefix of the simulation library, libstf, parcore and oasis (default $HOME/opt-sim)
#   BUILD    folder of the CMake build trees (default $RUNNER_TEMP/sim-build, or /tmp/sim-build)
#   WORK     the hardware build folder, COYOTE_SIM_DIR of the run (default $RUNNER_TEMP/sim-work)
#   VL_OUT   output of the Verilator build (default $RUNNER_TEMP/sim-vl)
#   OUT      run folder of the scenario (default runs/q6 in the checkout)
#   SCENARIO scenario file (default scenarios/tpch_q6/scenario.json)
#   JOBS     compile jobs (default nproc); TIMEOUT seconds for the DuckDB shell (default 1800)
set -euo pipefail

OASIS=$(realpath "$(dirname "$0")/../..")
CYT=$OASIS/parcore/libstf/coyote
TMP=${RUNNER_TEMP:-/tmp}
OPT=${OPT:-$HOME/opt}
OPT_SIM=${OPT_SIM:-$HOME/opt-sim}
BUILD=${BUILD:-$TMP/sim-build}
WORK=${WORK:-$TMP/sim-work}
VL_OUT=${VL_OUT:-$TMP/sim-vl}
OUT=${OUT:-$OASIS/runs/q6}
SCENARIO=${SCENARIO:-$OASIS/scenarios/tpch_q6/scenario.json}
JOBS=${JOBS:-$(nproc)}
TIMEOUT=${TIMEOUT:-1800}

step() { echo; echo "== $*"; }
# Configure, build and install one CMake tree into $OPT_SIM, the output in $BUILD/<name>.log
cmake_install() {
    local name=$1 src=$2
    shift 2
    cmake -S "$src" -B "$BUILD/$name" -DCMAKE_INSTALL_PREFIX="$OPT_SIM" "$@" > "$BUILD/$name.log"
    cmake --build "$BUILD/$name" -j "$JOBS" >> "$BUILD/$name.log"
    cmake --install "$BUILD/$name" >> "$BUILD/$name.log"
}
mkdir -p "$BUILD"

# With ccache in PATH, the Verilator objects (OBJCACHE in verilated.mk) and the CMake trees join the
# cache that the workflow keeps between runs, like the DuckDB build.
if command -v ccache > /dev/null; then
    export OBJCACHE=ccache CMAKE_CXX_COMPILER_LAUNCHER=ccache
fi

step "vhsnunzip to Verilog (GHDL)"
"$OASIS/hardware/sim_models/gen_vhsnunzip.sh"

step "Verilator build of the vFPGA"
"$CYT/sim/vl/configure.sh" "$OASIS/hardware" "$WORK"
VL_MODELS=$OASIS/hardware/sim_models JOBS=$JOBS "$CYT/sim/vl/build.sh" "$WORK" "$VL_OUT"

step "jemalloc in $OPT"
if [ ! -f "$OPT/lib/libjemalloc.so" ]; then
    INSTALL_PATH=$OPT DOWNLOAD_PATH=$BUILD/download "$OASIS/parcore/libstf/scripts/install_jemalloc.sh" > "$BUILD/jemalloc.log" 2>&1 \
        || { tail -20 "$BUILD/jemalloc.log"; exit 1; }
    [ -f "$OPT/lib/libjemalloc.so" ] || { echo "jemalloc did not install into $OPT, see $BUILD/jemalloc.log" >&2; exit 1; }
fi

# The installed libraries carry no rpath and the exported targets of libstf, parcore and oasis do not
# name jemalloc or the simulation library, so the link of the shell and every run need both paths.
# CMake reads CMAKE_PREFIX_PATH from the environment, also in the plain cmake call of the extension makefile.
export CMAKE_PREFIX_PATH=$OPT_SIM:$OPT
export LD_LIBRARY_PATH=$OPT_SIM/lib:$OPT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}

step "Coyote simulation library in $OPT_SIM"
cmake_install coyotesim "$CYT/sim/sw"

step "libstf, parcore and oasis in $OPT_SIM (EN_SIMULATION)"
cmake_install oasis-sw "$OASIS/software" -DCMAKE_BUILD_TYPE=Release -DEN_SIMULATION=ON
ls "$OPT_SIM"/lib/libcoyotesim.so "$OPT_SIM"/lib/liblibstf.so "$OPT_SIM"/lib/libparcore.so "$OPT_SIM"/lib/liboasis.so

step "DuckDB shell with the extension"
# ccache is taken from PATH by DuckDB's CMake.
( cd "$OASIS/extension" && GEN=ninja CORE_EXTENSIONS=tpch EXT_FLAGS="-DEN_SIMULATION=ON" make release )
DUCKDB=$OASIS/extension/build/release/duckdb
if ldd "$DUCKDB" | grep "not found"; then echo "$DUCKDB: unresolved libraries" >&2; exit 1; fi

step "lineitem.parquet and image.bin"
python3 "$OASIS/scenarios/tpch_q6/prepare.py"

step "TPC-H Q6 under Verilator"
"$OASIS/scenarios/run.py" "$SCENARIO" --sim-dir "$WORK" --duckdb "$DUCKDB" \
    --verilator-bin "$VL_OUT/coyote_vl" --out "$OUT" --timeout "$TIMEOUT"
