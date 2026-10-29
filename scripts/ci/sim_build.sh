#!/usr/bin/env bash
# Build the Oasis software against the Coyote simulation library, the Oasis vFPGA with Verilator,
# and the DuckDB shell with the extension. No Vivado: sim/vl/configure.sh of the Coyote submodule
# configures hardware/ with a stub Vivado and writes the generated sources and the file lists from the checkout.
#
# Usage: scripts/ci/sim_build.sh
#   from the Oasis checkout, after scripts/ci/pin_submodules.sh. The paths and their environment
#   variables are in sim_env.sh.
#
# Needs in PATH: verilator (the pin of sim/vl/verilator.env of the Coyote submodule or newer),
# ghdl (5 or newer, Verilog output), cmake, ninja, g++, python3 with the jinja2 module, and ccache
# when the DuckDB build should be cached. Boost headers, autoconf and wget for jemalloc.
set -euo pipefail

. "$(dirname "$0")/sim_env.sh"

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

step "Coyote simulation library in $OPT_SIM"
cmake_install coyotesim "$CYT/sim/sw"

step "libstf, parcore and oasis in $OPT_SIM (EN_SIMULATION)"
cmake_install oasis-sw "$OASIS/software" -DCMAKE_BUILD_TYPE=Release -DEN_SIMULATION=ON
ls "$OPT_SIM"/lib/libcoyotesim.so "$OPT_SIM"/lib/liblibstf.so "$OPT_SIM"/lib/libparcore.so "$OPT_SIM"/lib/liboasis.so

step "DuckDB shell with the extension"
# ccache is taken from PATH by DuckDB's CMake.
( cd "$OASIS/extension" && GEN=ninja CORE_EXTENSIONS=tpch EXT_FLAGS="-DEN_SIMULATION=ON" make release )
if ldd "$DUCKDB" | grep "not found"; then echo "$DUCKDB: unresolved libraries" >&2; exit 1; fi
