# Paths and defaults of the benchmarks (source it; bench.sh and the scripts in lib/ do). The layout of
# the folder that holds this checkout, P:
#   $P/oasis       the checkout ($O)
#   $P/build/hw    the hardware software build: Coyote, libstf, parcore, oasis, oasis_rdma_server
#   $P/bench       data, binaries, storage logs, node facts and runs (WORK), outside git
# Every path can be overridden from the environment.
BENCH_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export O=${O:-$(cd "$BENCH_DIR/.." && pwd)}
P=$(cd "$O/.." && pwd)
export WORK=${BENCH_WORK:-$P/bench}
export CYT=$O/parcore/libstf/coyote

# Hardware (not simulation) software build: $HW/build.sh
export HW=${BENCH_HW:-$P/build/hw}
export OPT_HW=$HW/opt                                   # Coyote, libstf, parcore, oasis
export DUCKDB_BUILD=${BENCH_DUCKDB_BUILD:-$O/extension/build/hw-release}   # libduckdb with the extension
export RDMA_SERVER=$HW/cmake/rdma_server/oasis_rdma_server
export BENCH=$WORK/bin/bench                            # bench.cpp, by lib/build.sh

# Bitstream builds: $BITSTREAMS/<name>/bitstreams/cyt_top.bit
export BITSTREAMS=${BENCH_BITSTREAMS:-$(cd "$P/.." && pwd)/bitstreams/oasis-pushdown}
export BITSTREAM_DEFAULT=filter-bloom-1dec-rdma

export DATA=$WORK/data
export QUERIES=$BENCH_DIR/queries

# The installed libraries carry no rpath (see scripts/ci/sim_env.sh): jemalloc is in $HOME/opt
export CMAKE_PREFIX_PATH=$OPT_HW:$HOME/opt
export LD_LIBRARY_PATH=$DUCKDB_BUILD/src:$OPT_HW/lib:$HOME/opt/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}

# Datasets: tpch_sf<N>, clickbench (the 100M rows of hits), clickbench_sample (its first 1M rows)
dataset_dir() {
    case "$1" in
        tpch_sf*) echo "$DATA/tpch/sf${1#tpch_sf}" ;;
        clickbench) echo "$DATA/clickbench/full" ;;
        clickbench_sample) echo "$DATA/clickbench/sample" ;;
        *) echo "unknown dataset $1" >&2; return 1 ;;
    esac
}

# One server per dataset: the server keys the files by basename, so the scale factors need their own
dataset_port() {
    case "$1" in
        tpch_sf30) echo 18488 ;;
        tpch_sf1) echo 18489 ;;
        clickbench) echo 18480 ;;
        clickbench_sample) echo 18481 ;;
        tpch_sf*) local sf=${1#tpch_sf}; echo $((18490 + ${sf%%.*})) ;;
        *) echo "unknown dataset $1" >&2; return 1 ;;
    esac
}

# The storage node writes its address here (lib/store.sh), on the shared filesystem
store_env() { echo "$WORK/store/$1.env"; }
