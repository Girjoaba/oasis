#!/usr/bin/env bash
# Benchmarks of Oasis on the U55C: TPC-H and ClickBench, DuckDB over Oasis against DuckDB's own Parquet
# reader, every measurement repeated. See README.md.
#
# Usage: bench.sh prepare [--sf N] [--bitstream NAME] [--no-clickbench]
#            build node, before the booking: software, data, query checks (idempotent)
#        bench.sh run [options]
#            compute node of the booking: node setup, storage servers, smoke test, the experiments,
#            analysis and plots; a second call with the same options continues a stopped run
#        bench.sh store start|stop|status|detect|counters [dataset...]    storage node (lib/store.sh)
#        bench.sh node info|driver|program|hugepages|check|all [bitstream] compute node (lib/node.sh)
#        bench.sh release                 end of the booking, on the compute node
#        bench.sh analyze|plot <run>      any node
#        bench.sh simcheck                build node: the harness in the Verilator simulation
#
# Options of run (defaults in brackets):
#   --bitstream NAME        bitstream build in $BITSTREAMS [filter-bloom-1dec-rdma]
#   --sf N                  TPC-H scale factor [30]; ClickBench has one size (100M rows)
#   --reps N                repetitions, one process each; plots show the median [3]
#   --benchmarks LIST       tpch,clickbench [both]
#   --experiments LIST      throughput,latency,ablation [all three]; latency is TPC-H only
#   --threads LIST          threads of the throughput cells [8]
#   --sweep                 throughput at 2,4,8,16,32 threads
#   --latency-threads LIST  threads of the TPC-H latency cells [4,16]
#   --ablation-threads N    threads of the ablation throughput cells [8]
#   --stages LIST           ablation stages k of oasis_s<k> [tpch 1,2,3,4; clickbench 1,3,4]
#                           (1 constants, 2 join bounds, 3 bitmaps, 4 dictionary ids; the Bloom filters (5)
#                           are the mode oasis; ClickBench has no joins, so 2 = 1 and 5 = 4)
#   --modes LIST            the main modes [native_rdma,oasis_nohw,oasis]
#   --queries-tpch LIST     a subset of the queries, by number [all]
#   --queries-clickbench LIST
#   --sessions N            streams of a throughput run [4]
#   --store-host HOST       storage node [the U55C with the next number]
#   --run DIR               run folder [$WORK/runs/<date>_<host>_<bitstream>]
#   --no-setup              skip lib/node.sh (driver, program, hugepages)
#   --no-smoke              skip the smoke test (SF 1 and the ClickBench sample against native_rdma)
#   --mem-limit SIZE        DuckDB memory_limit [80 % of MemAvailable after the hugepages]
#   --timeout S             seconds per cell [3600]
#   --dry-run               print the cells in order with a time estimate, run nothing
set -uo pipefail
. "$(dirname "$0")/lib/env.sh"
L=$BENCH_DIR/lib
HOST=$(hostname -s)

usage() { sed -n '2,37p' "$0"; exit "${1:-2}"; }
list() { echo "${1//,/ }"; }

# --- prepare (build node) ----------------------------------------------------------------------------

prepare() {
    local sf=30 bitstream=$BITSTREAM_DEFAULT clickbench=1
    while [ $# -gt 0 ]; do
        case "$1" in
            --sf) sf=$2; shift ;;
            --bitstream) bitstream=$2; shift ;;
            --no-clickbench) clickbench=0 ;;
            *) echo "prepare: unknown option $1" >&2; usage ;;
        esac
        shift
    done
    set -e
    bash "$L/build.sh" all "$bitstream"
    local s
    for s in 1 "$sf"; do
        echo; echo "== TPC-H SF $s ($(date +%H:%M:%S))"
        [ -f "$(dataset_dir "tpch_sf$s")/manifest.json" ] || python3 "$BENCH_DIR/gen_data.py" tpch --sf "$s"
        [ -f "$(dataset_dir "tpch_sf$s")/loose.json" ] ||
            python3 "$BENCH_DIR/gen_data.py" queries tpch "$(dataset_dir "tpch_sf$s")"
    done
    if [ "$clickbench" = 1 ]; then
        echo; echo "== ClickBench ($(date +%H:%M:%S))"
        local dl=$DATA/clickbench/download
        mkdir -p "$dl"
        if [ ! -f "$dl/hits.parquet.done" ]; then
            curl -fL -C - -o "$dl/hits.parquet" https://datasets.clickhouse.com/hits_compatible/hits.parquet
            touch "$dl/hits.parquet.done"
        fi
        [ -f "$(dataset_dir clickbench)/manifest.json" ] || python3 "$BENCH_DIR/gen_data.py" clickbench
        for s in clickbench_sample clickbench; do
            [ -f "$(dataset_dir $s)/loose.json" ] || python3 "$BENCH_DIR/gen_data.py" queries clickbench "$(dataset_dir $s)"
        done
    fi
    python3 -m py_compile "$BENCH_DIR/analyze.py" "$BENCH_DIR/plot.py"
    echo
    echo "PREPARE_DONE: on the compute node of the booking run $BENCH_DIR/bench.sh run --bitstream $bitstream --sf $sf"
}

# --- run (compute node) ------------------------------------------------------------------------------

BITSTREAM=$BITSTREAM_DEFAULT SF=30 REPS=3 BENCHMARKS="tpch clickbench"
EXPERIMENTS="throughput latency ablation" THREADS=8 LAT_THREADS="4 16" ABL_THREADS=8 STAGES=
MODES="native_rdma oasis_nohw oasis" Q_TPCH= Q_CLICKBENCH= SESSIONS=4 STORE= RUN= SETUP=1 SMOKE=1
MEM_LIMIT= TIMEOUT=3600 DRY=0 LOCAL_DIR=

parse_run() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --bitstream) BITSTREAM=$2; shift ;;
            --sf) SF=$2; shift ;;
            --reps) REPS=$2; shift ;;
            --benchmarks) BENCHMARKS=$(list "$2"); shift ;;
            --experiments) EXPERIMENTS=$(list "$2"); shift ;;
            --threads) THREADS=$(list "$2"); shift ;;
            --sweep) THREADS="2 4 8 16 32" ;;
            --latency-threads) LAT_THREADS=$(list "$2"); shift ;;
            --ablation-threads) ABL_THREADS=$2; shift ;;
            --stages) STAGES=$(list "$2"); shift ;;
            --modes) MODES=$(list "$2"); shift ;;
            --queries-tpch) Q_TPCH=$2; shift ;;
            --queries-clickbench) Q_CLICKBENCH=$2; shift ;;
            --sessions) SESSIONS=$2; shift ;;
            --store-host) STORE=$2; shift ;;
            --run) RUN=$2; shift ;;
            --no-setup) SETUP=0 ;;
            --no-smoke) SMOKE=0 ;;
            --mem-limit) MEM_LIMIT=$2; shift ;;
            --timeout) TIMEOUT=$2; shift ;;
            --local-dir) LOCAL_DIR=$2; MODES="$MODES native_local"; shift ;;
            --dry-run) DRY=1 ;;
            -h|--help) usage 0 ;;
            *) echo "run: unknown option $1" >&2; usage ;;
        esac
        shift
    done
    RUN=${RUN:-$WORK/runs/$(date +%Y%m%d)_${HOST}_$BITSTREAM}
    if [ -z "$STORE" ]; then
        # hacc-u55c-05 -> hacc-u55c-06
        local num=${HOST##*[!0-9]} prefix=${HOST%"${HOST##*[!0-9]}"}
        [ -n "$num" ] || { echo "cannot derive the storage node from $HOST: give --store-host" >&2; exit 2; }
        STORE=$(printf "%s%0${#num}d" "$prefix" $((10#$num + 1)))
    fi
}

has() { case " $1 " in *" $2 "*) return 0 ;; *) return 1 ;; esac; }

dataset_of() { [ "$1" = tpch ] && echo "tpch_sf$2" || echo "$1"; }
stages_of() { [ -n "$STAGES" ] && echo "$STAGES" || { [ "$1" = tpch ] && echo "1 2 3 4" || echo "1 3 4"; }; }

# The cells of the run in order, one per line: <folder> <bench> <dataset> <mode> <threads> <kind> <rep>.
# Repetition by repetition, so a stopped run holds whole repetitions and a drift spreads over the modes;
# the ablation after the main cells.
cells() {
    local r b t m k
    for r in $(seq 0 $((REPS - 1))); do
        for b in $BENCHMARKS; do
            if has "$EXPERIMENTS" throughput; then
                for t in $THREADS; do for m in $MODES; do echo "$RUN $b $(dataset_of "$b" "$SF") $m $t tp $r"; done; done
            fi
            if has "$EXPERIMENTS" latency && [ "$b" = tpch ]; then
                for t in $LAT_THREADS; do for m in $MODES; do echo "$RUN $b $(dataset_of "$b" "$SF") $m $t lat $r"; done; done
            fi
        done
    done
    has "$EXPERIMENTS" ablation || return 0
    for r in $(seq 0 $((REPS - 1))); do
        for b in $BENCHMARKS; do
            for k in $(stages_of "$b"); do echo "$RUN $b $(dataset_of "$b" "$SF") oasis_s$k $ABL_THREADS tp $r"; done
            if [ "$b" = tpch ]; then
                for t in $LAT_THREADS; do
                    for k in $(stages_of "$b"); do echo "$RUN $b $(dataset_of "$b" "$SF") oasis_s$k $t lat $r"; done
                done
            fi
        done
    done
}

smoke_cells() {
    local b m
    for b in $BENCHMARKS; do
        local ds=tpch_sf1 kind=lat
        [ "$b" = clickbench ] && ds=clickbench_sample kind=tp
        for m in $MODES $(for k in $(stages_of "$b"); do echo "oasis_s$k"; done); do
            echo "$RUN/smoke $b $ds $m 8 $kind 0"
        done
    done
}

cell_name() { echo "${3}_${4}_t${5}_${6}_r${7}"; }

# Estimate of one cell in seconds: the median of the same cell (any repetition) in earlier runs, else
# a guess from the runs of 2026-11-02 (TPC-H SF 30: a throughput run of 4 streams 61 s at 8 threads, a
# latency pass 20 s), scaled by the scale factor and the threads (with 1 decoder, little gain above 8)
estimate() {
    local name=$1 bench=$2 ds=$3 t=$5 kind=$6 key
    key=${name%_r*}
    local prev
    prev=$(cat "$WORK"/runs/*/index.tsv "$WORK"/runs/*/smoke/index.tsv 2> /dev/null |
        awk -F'\t' -v k="$key" '{n = $1; sub(/_r[0-9]+$/, "", n)} n == k && $8 == 0 {print $9}' | sort -n |
        awk '{a[NR] = $1} END {if (NR) print a[int((NR + 1) / 2)]}')
    if [ -n "$prev" ]; then echo "$prev"; return; fi
    local scale=1
    case "$ds" in
        tpch_sf*) scale=$(awk -v s="${ds#tpch_sf}" 'BEGIN {print s / 30}') ;;
        clickbench) scale=5 ;;
        clickbench_sample) scale=0.05 ;;
    esac
    awk -v s="$scale" -v t="$t" -v k="$kind" -v n="$SESSIONS" 'BEGIN {
        f = (t < 8) ? 8 / t : 0.85
        base = (k == "tp") ? 61 * n / 4 : 20
        print int(10 + base * s * f) }'
}

all_cells() { [ "$SMOKE" = 1 ] && smoke_cells; cells; }

dry_run() {
    local total=0 n=0 name e dir bench ds m t kind rep
    printf '%-6s %-44s %s\n' "est s" "cell" "folder"
    while read -r dir bench ds m t kind rep; do
        name=$(cell_name "$dir" "$bench" "$ds" "$m" "$t" "$kind" "$rep")
        e=$(estimate "$name" "$bench" "$ds" "$m" "$t" "$kind")
        total=$((total + e)) n=$((n + 1))
        printf '%-6s %-44s %s\n' "$e" "$name" "${dir#"$RUN"}"
    done < <(all_cells)
    echo "$n cells, about $((total / 60)) min (estimate; a ClickBench cell without an earlier run is a guess)"
}

NSTATS=/sys/kernel/coyote_sysfs_0/cyt_attr_nstats
HP=/sys/kernel/mm/hugepages/hugepages-1048576kB
NCPU=$(nproc)
FAILS=0
STORE_SSH=0
# RoCE stall: a queue pair of the FPGA gave up after 12 timeouts of its 8 outstanding reads (Retrans + 96)
# and no response came for STALL_S seconds. Only lib/node.sh program with REPROGRAM=1 clears it.
RC_STALL=97
STALL_S=60
MAX_RECOVER=3
RECOVERS=0

# -n: the cell loops read their list on stdin
ssh_store() { ssh -n -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "$STORE" "$@"; }

datasets() {
    local b
    for b in $BENCHMARKS; do
        dataset_of "$b" "$SF"
        if [ "$SMOKE" = 1 ]; then [ "$b" = tpch ] && echo tpch_sf1 || echo clickbench_sample; fi
    done
}

port_open() { timeout 3 bash -c "< /dev/tcp/$1/$2" 2> /dev/null; }

# The storage servers on the storage node: over ssh, else by hand on that node; then wait until every
# port answers
storage() {
    local ds all_up
    local need
    need=$(datasets | tr '\n' ' ')
    echo; echo "== storage on $STORE: $need($(date +%H:%M:%S))"
    if ssh_store "bash $BENCH_DIR/bench.sh store start $need"; then
        STORE_SSH=1
    else
        echo "ssh to $STORE failed. On $STORE, run:"
        echo "    bash $BENCH_DIR/bench.sh store start $need"
        echo "waiting for the servers (15 min) ..."
    fi
    local envf
    envf=$(store_env "${STORE%%.*}")
    for _ in $(seq 1 180); do
        if [ -f "$envf" ]; then
            . "$envf"
            all_up=1
            for ds in $need; do port_open "$STORE_IP" "$(dataset_port "$ds")" || all_up=0; done
            [ "$all_up" = 1 ] && break
        fi
        sleep 5
    done
    [ "${all_up:-0}" = 1 ] || { echo "the storage servers do not answer (see $envf and $WORK/store/)" >&2; exit 1; }
    echo "storage $STORE_IP ($STORE_HOST): $(for ds in $need; do echo -n "$ds:$(dataset_port "$ds") "; done)"
}

store_counters() {
    [ "$STORE_SSH" = 1 ] || return 0
    timeout 10 ssh_store "bash $BENCH_DIR/bench.sh store counters" 2> /dev/null | grep -E '^[a-z_]+ [0-9]+$' || true
}

preflight() {
    [ -e /dev/coyote_fpga_0_v0 ] || { echo "no /dev/coyote_fpga_0_v0: run without --no-setup" >&2; exit 1; }
    [ -x "$BENCH" ] || { echo "no $BENCH: run bench.sh prepare on the build node" >&2; exit 1; }
    [ "$(cat $HP/free_hugepages)" -ge 1 ] || { echo "no free 1 GiB hugepage: bench.sh node hugepages" >&2; exit 1; }
    mkdir -p "$RUN"
    if [ ! -f "$RUN/memory_limit" ]; then
        local avail_kb
        avail_kb=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
        echo "${MEM_LIMIT:-$((avail_kb * 8 / 10 / 1048576))GB}" > "$RUN/memory_limit"
    fi
    local bdir=$BITSTREAMS/$BITSTREAM
    {
        echo "started $(date '+%Y-%m-%d %H:%M:%S') on $HOST, $NCPU CPUs"
        echo "command: bench.sh run $ARGS"
        echo "storage $STORE_IP ($STORE_HOST), memory_limit $(cat "$RUN/memory_limit")"
        echo "hugepages 1 GiB: nr $(cat $HP/nr_hugepages) free $(cat $HP/free_hugepages)"
        echo "pinning: $(command -v numactl > /dev/null && echo numactl --physcpubind || echo taskset -c), CPUs 0..n-1"
        echo "software build ($WORK/build/state.txt):"
        sed -n '1,3p;/bitstream source/p;/WARNING/p' "$WORK/build/state.txt" 2> /dev/null | sed 's/^/  /'
    } >> "$RUN/run.txt"
    {
        echo "$bdir/bitstreams/cyt_top.bit"
        md5sum "$bdir/bitstreams/cyt_top.bit" | cut -d' ' -f1
        grep -E "^Status|^WNS|^Failing" "$bdir/analysis.txt"
        sed -n '2,3p' "$bdir/source/state.txt"
    } > "$RUN/bitstream.txt"
    local st_tpch st_cb
    st_tpch=$(stages_of tpch) st_cb=$(stages_of clickbench)
    python3 - "$RUN/run.json" <<EOF
import json, sys
ints = lambda s: [int(x) for x in s.split()]
json.dump({"bitstream": "$BITSTREAM", "sf": "$SF", "reps": $REPS, "benchmarks": "$BENCHMARKS".split(),
           "experiments": "$EXPERIMENTS".split(), "threads": ints("$THREADS"),
           "latency_threads": ints("$LAT_THREADS"), "ablation_threads": $ABL_THREADS, "modes": "$MODES".split(),
           "stages": {"tpch": ints("$st_tpch"), "clickbench": ints("$st_cb")}, "sessions": $SESSIONS,
           "host": "$HOST", "store": "$STORE"}, open(sys.argv[1], "w"), indent=1)
EOF
    local ds
    for ds in $(datasets); do cp "$(dataset_dir "$ds")/loose.json" "$RUN/loose_$ds.json" 2> /dev/null || true; done
    mkdir -p "$RUN/smoke"
    cp "$RUN"/run.json "$RUN"/loose_*.json "$RUN/smoke/" 2> /dev/null || true
    cp -r "$WORK"/nodes/"$HOST"_2* "$RUN/" 2> /dev/null || true
    tail -n 8 "$RUN/run.txt"
}

# Keeps the sudo credentials of the start (node setup, or sudo -v before the run) alive while the run
# goes, for roce_recover. Never asks: a run that starts unattended (after another one) must not block.
# Without them a RoCE stall stops the run
sudo_keepalive() {
    sudo -n true 2> /dev/null ||
        { echo "WARNING: no sudo credentials (sudo -v before the run): a RoCE stall stops the run" >&2; return 0; }
    (while kill -0 $$ 2> /dev/null; do sudo -n -v 2> /dev/null; sleep 60; done) < /dev/null > /dev/null 2>&1 &
}

# nstat <counter>: one counter of $NSTATS, 0 without the file
nstat() {
    local v
    v=$(sed -n "s/^$1[^:]*: *//p" "$NSTATS" 2> /dev/null | head -n 1)
    echo "${v:-0}"
}

# run_bench <folder> <command...>: the command in the folder under the timeout, while the FPGA network
# counters are watched; a RoCE stall stops it with rc $RC_STALL instead of waiting for the timeout
run_bench() {
    local d=$1
    shift
    (cd "$d" && exec timeout -k 30 "$TIMEOUT" "$@") < /dev/null > "$d/bench.log" 2>&1 &
    local pid=$! r0 rx0 rx since=$SECONDS
    r0=$(nstat Retrans) rx0=$(nstat "ROCE RX")
    while kill -0 "$pid" 2> /dev/null; do
        sleep 2
        rx=$(nstat "ROCE RX")
        [ "$rx" != "$rx0" ] && rx0=$rx since=$SECONDS
        if [ $(($(nstat Retrans) - r0)) -ge 96 ] && [ $((SECONDS - since)) -ge "$STALL_S" ]; then
            echo "RoCE stall: Retrans +$(($(nstat Retrans) - r0)), no RoCE packet for $((SECONDS - since)) s" |
                tee -a "$d/bench.log" >&2
            kill -TERM "$pid" 2> /dev/null
            wait "$pid"
            return $RC_STALL
        fi
    done
    wait "$pid"
}

# roce_recover <folder> <name>: after a RoCE stall, keeps the cell in stalls/ and programs the board again,
# so that the cell can run once more; stops the run without sudo or after $MAX_RECOVER stalls
roce_recover() {
    local d=$1 name=$2 keep
    keep=$RUN/stalls/${name}_$(date +%H%M%S)
    mkdir -p "$RUN/stalls"
    mv "$d" "$keep"
    echo "$(date '+%Y-%m-%d %H:%M:%S') $name: RoCE stall, kept in $keep" >> "$RUN/stalls/stalls.txt"
    RECOVERS=$((RECOVERS + 1))
    if [ "$RECOVERS" -gt "$MAX_RECOVER" ] || ! sudo -n true 2> /dev/null; then
        echo "$name: RoCE stall $RECOVERS, stopping (no sudo, or more than $MAX_RECOVER stalls). Then run:" >&2
        echo "    REPROGRAM=1 bash $L/node.sh program $BITSTREAM" >&2
        echo "and the same bench.sh run again" >&2
        exit 1
    fi
    echo "$name: RoCE stall $RECOVERS, programming the board again ($(date +%H:%M:%S))"
    # < /dev/null: the cell loops read their list on stdin
    REPROGRAM=1 bash "$L/node.sh" program "$BITSTREAM" < /dev/null >> "$RUN/stalls/program.log" 2>&1 &&
        [ -e /dev/coyote_fpga_0_v0 ] ||
        { echo "programming failed, see $RUN/stalls/program.log" >&2; exit 1; }
}

# cell <folder> <bench> <dataset> <mode> <threads> <kind> <rep>
cell() {
    local dir=$1 bench=$2 ds=$3 mode=$4 t=$5 kind=$6 rep=$7
    local name d
    name=$(cell_name "$@")
    d=$dir/$name
    if grep -q '^rc=0' "$d/done" 2> /dev/null; then echo "$name: done before, skipped"; return 0; fi
    if [ "$t" -gt "$NCPU" ]; then echo "$name: $t threads > $NCPU CPUs, skipped"; return 0; fi
    rm -rf "$d"
    mkdir -p "$d"
    [ "$(cat $HP/free_hugepages)" -eq "$(cat $HP/nr_hugepages)" ] ||
        echo "WARNING: $(cat $HP/free_hugepages) of $(cat $HP/nr_hugepages) hugepages free; another process holds some" >&2
    local pin=(taskset -c "0-$((t - 1))")
    command -v numactl > /dev/null && pin=(numactl --physcpubind="0-$((t - 1))")
    local args=(--mode "$mode" --threads "$t" --sessions "$SESSIONS" --suite "$QUERIES/$bench" --seed $((42 + 100 * rep))
                --memory-limit "$(cat "$RUN/memory_limit")" --out .)
    [ "$kind" = tp ] && args+=(--tp-reps 1 --lat-reps 0) || args+=(--tp-reps 0 --lat-reps 1)
    [ "$bench" = tpch ] && [ -n "$Q_TPCH" ] && args+=(--queries "$Q_TPCH")
    [ "$bench" = clickbench ] && [ -n "$Q_CLICKBENCH" ] && args+=(--queries "$Q_CLICKBENCH")
    [ "$mode" = native_local ] && args+=(--local-dir "$LOCAL_DIR/$ds") ||
        args+=(--server "$STORE_IP" --port "$(dataset_port "$ds")")
    cat "$NSTATS" > "$d/nstats_before.txt" 2>&1
    store_counters > "$d/store_before.txt"
    local start=$SECONDS
    echo "$name: $(date +%H:%M:%S) start"
    run_bench "$d" "${pin[@]}" "$BENCH" "${args[@]}"
    local rc=$?
    cat "$NSTATS" > "$d/nstats_after.txt" 2>&1
    store_counters > "$d/store_after.txt"
    if [ "$rc" -eq "$RC_STALL" ]; then
        roce_recover "$d" "$name"
        cell "$@"
        return
    fi
    echo "rc=$rc seconds=$((SECONDS - start))" > "$d/done"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "$bench" "$ds" "$mode" "$t" "$kind" "$rep" "$rc" \
        "$((SECONDS - start))" >> "$dir/index.tsv"
    echo "$name: rc=$rc $((SECONDS - start)) s; $(grep -E '^throughput|^FAILED' "$d/bench.log" | tail -n 2 | tr '\n' ' ')"
    if [ "$rc" -ne 0 ]; then
        FAILS=$((FAILS + 1))
        tail -n 5 "$d/bench.log"
        # Two failed cells in a row: the board may hang (bench.sh run again reprograms with REPROGRAM=1)
        [ "$FAILS" -ge 2 ] && { echo "two failed cells in a row, stopping; see $d/bench.log" >&2; exit 1; }
    else
        FAILS=0
    fi
}

summarize() {
    python3 "$BENCH_DIR/analyze.py" "$1" > /dev/null
    local rc=$?
    echo "summary: $1/summary.md (analyze rc=$rc)"
    grep -E '^- `' "$1/summary.md" | grep -v -E ': (pass|not compared)' || echo "correctness: no failures"
    return $rc
}

run() {
    ARGS="$*"
    parse_run "$@"
    if [ "$DRY" = 1 ]; then
        echo "run folder $RUN, storage $STORE"
        dry_run
        return
    fi
    echo "run folder $RUN, bitstream $BITSTREAM, storage node $STORE"
    dry_run | tail -n 1
    if [ "$SETUP" = 1 ]; then
        bash "$L/node.sh" all "$BITSTREAM" || exit 1
    fi
    sudo_keepalive
    storage
    preflight
    if [ "$SMOKE" = 1 ]; then
        echo; echo "== smoke ($(date +%H:%M:%S))"
        while read -r line; do cell $line; done < <(smoke_cells)
        summarize "$RUN/smoke" || { echo "smoke: a query failed or a result differs, stopping (see $RUN/smoke/summary.md)" >&2; exit 1; }
    fi
    echo; echo "== experiments ($(date +%H:%M:%S))"
    while read -r line; do cell $line; done < <(cells)
    summarize "$RUN"
    python3 "$BENCH_DIR/plot.py" "$RUN" && echo "plots: $RUN/plots/"
    echo "RUN_DONE $(date '+%Y-%m-%d %H:%M:%S')"
}

case "${1:-}" in
    prepare) shift; prepare "$@" ;;
    run) shift; run "$@" ;;
    store) shift; bash "$L/store.sh" "$@" ;;
    node) shift; bash "$L/node.sh" "$@" ;;
    release) bash "$L/node.sh" release ;;
    analyze) python3 "$BENCH_DIR/analyze.py" "${2:?run folder}" ;;
    plot) python3 "$BENCH_DIR/plot.py" "${2:?run folder}" ;;
    simcheck) shift; bash "$BENCH_DIR/sim_check.sh" "$@" ;;
    -h|--help) usage 0 ;;
    *) usage ;;
esac
