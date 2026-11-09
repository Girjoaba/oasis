#!/usr/bin/env bash
# Checks bench, analyze.py and plot.py in the Verilator simulation of this branch before a booking. TPC-H
# Q6 and Q14 at SF 0.01 (latency cells) and four ClickBench queries on 20000 rows of hits (throughput
# cells, one stream): q01 (SMALLINT not-equal), q12 (strings as dictionary ids), q19 (BIGINT equality),
# q36 (INTEGER, DATE and SMALLINT conjuncts and a string not-equal; q41 has the same conjuncts, but its
# OFFSET skips every row of the sample). Every main mode, its narrow variant (oasis_narrow_transfer) and
# every stage, 2 repetitions; then one TPC-H cell of oasis under DuckDB's log (bench --hw-log). One
# Verilator binary: run nothing else in the simulation meanwhile.
#
# Usage: sim_check.sh [modes]    (default all modes; output and summary.md in $WORK/sim_check/)
#        SUITES=clickbench sim_check.sh   only the cells of these suites (default tpch clickbench); the
#                                         cells of the others stay and enter the summary
# Needs: the simulation build $SIM (SIM_DUCKDB: its libduckdb with the extension), bench.sh prepare
# (ClickBench) on the build node.
set -uo pipefail
. "$(dirname "$0")/lib/env.sh"
BENCH_WORK=$WORK
SIM=${SIM:-$P/build/sim}
SIM_DUCKDB=${SIM_DUCKDB:-$P/build/s5ext/src}
. "$SIM/env.sh"
SIM_WORK=$WORK
WORK=$BENCH_WORK
OUT=$WORK/sim_check
BIN=$WORK/bin/bench_sim
MODES=${1:-native_rdma oasis_nohw oasis oasis_nohw_narrow oasis_narrow oasis_s1 oasis_s2 oasis_s3 oasis_s4}
SUITES=${SUITES:-tpch clickbench}
mkdir -p "$OUT" "$WORK/bin"

g++ -O2 -std=c++17 -o "$BIN" "$BENCH_DIR/bench.cpp" -I "$O/extension/duckdb/src/include" \
    -L "$SIM_DUCKDB" -lduckdb -lpthread -Wl,-rpath,"$SIM_DUCKDB" \
    -Wl,-rpath-link,"$OPT_SIM/lib:$HOME/opt/lib" || exit 1

# Data: TPC-H SF 0.01 and a hits sample with rows of CounterID 62 (the filters of q36 keep some), one image
D=$OUT/data
if [ ! -f "$D/image.bin" ]; then
    rm -rf "$D"; mkdir -p "$D"
    [ -f "$DATA/tpch/sf0.01/manifest.json" ] || python3 "$BENCH_DIR/gen_data.py" tpch --sf 0.01 || exit 1
    python3 - "$DATA/clickbench/full/hits.parquet" "$D/hits.parquet" <<'EOF' || exit 1
import duckdb, sys
duckdb.sql(f"""COPY (SELECT * FROM (SELECT * FROM read_parquet('{sys.argv[1]}') WHERE CounterID = 62 LIMIT 10000)
               UNION ALL SELECT * FROM (SELECT * FROM read_parquet('{sys.argv[1]}') WHERE CounterID <> 62 LIMIT 10000))
               TO '{sys.argv[2]}' (FORMAT parquet)""")
EOF
    python3 "$BENCH_DIR/gen_data.py" check "$D" | tail -n 3
    python3 "$O/rdma_server/pack_image.py" "$D/image.bin" "$DATA"/tpch/sf0.01/*.parquet "$D/hits.parquet" > "$D/image.log" || exit 1
    for s in tpch clickbench; do
        python3 "$BENCH_DIR/gen_data.py" queries "$s" "$([ $s = tpch ] && echo "$DATA/tpch/sf0.01" || echo "$D")" > "$D/queries_$s.log"
    done
fi
cp "$DATA/tpch/sf0.01/loose.json" "$OUT/loose_tpch_sf0.01.json"
cp "$D/loose.json" "$OUT/loose_clickbench_sample.json"

# The environment of scenarios/run.py for the default mock (50 ns, no rate limit, MTU 4096)
export COYOTE_SIM_DIR=$SIM_WORK COYOTE_SIM_BIN=$SIM/vl/coyote_vl COYOTE_SIM_VCD=0
export COYOTE_SIM_RDMA_IMAGE=$D/image.bin COYOTE_SIM_SEED=1
export COYOTE_SIM_RDMA_LATENCY_NS=50 COYOTE_SIM_RDMA_GBPS=0 COYOTE_SIM_RDMA_MTU=4096
export COYOTE_SIM_RDMA_JITTER_NS=0 COYOTE_SIM_RDMA_DROP=0

[ "$SUITES" = "tpch clickbench" ] && : > "$OUT/index.tsv"
for rep in 0 1; do
    for mode in $MODES; do
        for cell in "tpch tpch_sf0.01 lat 6,14" "clickbench clickbench_sample tp 1,12,19,36"; do
            set -- $cell
            case " $SUITES " in *" $1 "*) ;; *) continue ;; esac
            name=${2}_${mode}_t4_${3}_r$rep
            d=$OUT/$name
            rm -rf "$d"; mkdir -p "$d"
            kind=(--tp-reps 0 --lat-reps 1)
            [ "$3" = tp ] && kind=(--tp-reps 1 --lat-reps 0)
            start=$SECONDS
            (cd "$d" && timeout 3600 "$BIN" --mode "$mode" --server sim --threads 4 --sessions 1 "${kind[@]}" \
                --queries "$4" --seed $((42 + 100 * rep)) --suite "$QUERIES/$1" --out . > bench.log 2>&1)
            rc=$?
            printf '%s\t%s\t%s\n' "$name" "$rc" "$((SECONDS - start))" >> "$OUT/index.tsv"
            echo "$name rc=$rc $((SECONDS - start)) s $(tail -n 1 "$d/bench.log")"
        done
    done
done
# The hardware filter messages of oasis (analyze.py: section Hardware filter log)
if [[ " $SUITES " == *" tpch "* ]]; then
    name=tpch_sf0.01_oasis_t4_log_r0
    d=$OUT/$name
    rm -rf "$d"; mkdir -p "$d"
    start=$SECONDS
    (cd "$d" && timeout 3600 "$BIN" --mode oasis --server sim --threads 4 --sessions 1 --tp-reps 0 --lat-reps 1 \
        --hw-log --queries 6,14 --seed 42 --suite "$QUERIES/tpch" --out . > bench.log 2>&1)
    rc=$?
    printf '%s\t%s\t%s\n' "$name" "$rc" "$((SECONDS - start))" >> "$OUT/index.tsv"
    echo "$name rc=$rc $((SECONDS - start)) s $(tail -n 1 "$d/bench.log")"
fi
python3 "$BENCH_DIR/analyze.py" "$OUT" --reference native_rdma > /dev/null
echo "analyze rc=$?: $OUT/summary.md"
python3 "$BENCH_DIR/plot.py" "$OUT"
echo RUN_DONE
