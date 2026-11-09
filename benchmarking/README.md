# Benchmarks on the U55C: TPC-H and ClickBench

Question: with the FilterStage, is DuckDB over Oasis faster than without it, how much does each stage of
the hardware filter add, and how do both compare with the DuckDB Parquet reader? All baselines are
measured in the same run, on the same bitstream and the same data, and every measurement is repeated.

## Nodes

| Node | Role |
|---|---|
| hacc-build-03 | build node: software, data, query checks (`bench.sh prepare`), any time before the booking |
| U55C, lower number (e.g. hacc-u55c-05) | compute node: DuckDB and the FPGA (`bench.sh run`) |
| U55C, the next number (e.g. hacc-u55c-06) | storage node: `oasis_rdma_server` with the Parquet files in RAM |

Every booking holds two U55C nodes. `bench.sh run` starts the servers on the storage node over ssh; if
ssh does not work, it prints the command to type there and waits until the servers answer. The storage
node finds its RoCE address (interface `data1`) itself and writes it to `$WORK/store/<host>.env`, so the
compute node needs no address.

## Commands

```bash
cd /pub/scratch/agirjoaba/oasis-pushdown/oasis

# hacc-build-03, before the booking (idempotent; the first run downloads and rewrites ClickBench, about 1 h)
benchmarking/bench.sh prepare --sf 30

# compute node, after the booking, in tmux: everything (asks for the sudo password at the start)
benchmarking/bench.sh run --bitstream filter-bloom-1dec-rdma --sf 30

benchmarking/bench.sh run --dry-run       # the cells in order and the estimated time
benchmarking/bench.sh release             # end of the booking
```

`bench.sh run` goes through these steps:
1. Node setup (`lib/node.sh`): driver, bitstream, hugepages. It skips a bitstream that this boot of the
   node already holds.
2. Storage servers on the storage node.
3. Smoke test: TPC-H SF 1 and the ClickBench sample, every mode and stage, against `native_rdma`. A
   failed query or a differing result stops the run.
4. The experiments.
5. `analyze.py` and `plot.py`.

A finished cell is skipped, so the same command continues a stopped run. A run on another day needs
`--run <folder>`. The options are in the header of `bench.sh` (`bench.sh --help`).

RoCE stall: a queue pair of the FPGA gives up after 12 timeouts of its 8 outstanding reads, without an
error to the host, and the query waits until the cell timeout (2026-11-05 and 2026-11-06, once 4 s after
programming). Only programming the board again clears it. While a cell runs, `bench.sh` watches the FPGA
network counters: `Retrans cnt` up by at least 96 and no RoCE packet for 60 s stop the cell. The run
then moves the cell to `stalls/`, programs the board again (`REPROGRAM=1 lib/node.sh program`) and runs
the cell once more, at most 3 times per run. This needs the sudo credentials of the start (the node
setup, or `sudo -v` right before `bench.sh run`), which the run keeps alive without asking again;
without them it stops and prints the command. By hand:

```bash
REPROGRAM=1 bash benchmarking/bench.sh node program <bitstream>   # then the same bench.sh run again
```

## Experiments

Modes: `oasis` (`read_oasis`, every stage of the hardware filter), `oasis_nohw` (`read_oasis`,
`oasis_hw_filter = false`, the filters on the CPU), `native_rdma` (`read_parquet` over `rdma://`:
DuckDB decodes, the same bytes over the bypass stream). The stage ablation adds `oasis_s<k>`
(`oasis_hw_filter_stage = k`): 1 constant comparisons, 2 join bounds, 3 bitmap lookup, 4 dictionary ids
of strings; stage 5 (Bloom filters) is `oasis`.

Mode variants: an oasis mode takes the suffix `_narrow` (`oasis_narrow_transfer = true`: BIGINT and
DECIMAL chunks whose statistics fit INT32 cross PCIe as INT32 and are widened on the CPU) and the
suffix `_pf<N>` (`oasis_prefetch_depth = N`), in this order: `oasis_nohw_narrow`, `oasis_narrow_pf4`.
`--modes` takes them directly; `--narrow` adds the `_narrow` variant of every oasis mode, of the
ablation and of the smoke test, so the narrow results are also checked against `native_rdma`.
`oasis_cast_pushdown` stays off in every mode.

| Experiment | Benchmarks | Cells (defaults) |
|---|---|---|
| throughput | TPC-H, ClickBench | 3 modes at `--threads` (8; `--sweep` = 2 4 8 16 32) |
| latency | TPC-H only | 3 modes at `--latency-threads` (4, 16) |
| ablation | throughput of both, TPC-H latency | `oasis_s1` to `oasis_s4` (ClickBench `s1 s3 s4`: no joins) at `--ablation-threads` (8) and the latency threads |
| hardware filter log (`--hw-log LIST`) | TPC-H only | one latency pass of the listed queries per oasis mode with the hardware filter, at the last latency thread count, repetition 0 |

- **Throughput cell:** warm-up, then one run of 4 streams (`--sessions`), each the queries in its own
  random order. Q/h = queries x 3600 / seconds. With `--tp-profile` every stream also keeps the DuckDB
  profile of each query (the scan metrics under concurrent streams, such as the lookup table regions).
- **Latency cell:** warm-up, then one pass over the queries on one connection, with the DuckDB profile
  of each query.
- **Log cell** (`<dataset>_<mode>_t<T>_log_r0`): a latency pass of the `--hw-log` queries, each under
  DuckDB's log at DEBUG. The messages of the hardware filter (the conjuncts the hardware takes, the
  FilterSpec per row group, the row groups that stay on the CPU and why) go to `hwlog/`. `analyze.py`
  summarises them per query as `runs/s5_correctness/logsum.py` does. The logging slows the queries, so
  their seconds stay out of the latency tables.
- `--prefetch N` and `--tp-profile` change every cell without changing the cell names: give such a run
  its own `--run` folder.
- **Repetitions:** every cell runs `--reps` times (3), as separate processes, in the order rep 0 of
  every cell, then rep 1, then rep 2, with the ablation after the main cells. A stopped run then still
  holds whole repetitions, and a drift spreads over the modes. Repetition r uses the seed 42 + 100 r for
  the stream orders, the same for every mode.
- **Results:** analysis and plots show the median and the min and max over the repetitions.

Time with the defaults: about 3 h. The TPC-H cells take 1 to 2 min each, based on the runs of
2026-11-02. ClickBench is not measured yet: `--dry-run` guesses about 5 min per throughput cell, and
after a run it uses the measured times. A variant cell without an earlier run takes the estimate of
its base mode.

## 4-decoder evaluation

TPC-H SF 30 on a 4-decoder bitstream (`filter-bloom-4dec-rdma-try<N>`, the try with the best timing).
Three runs on the compute node, each in its own folder; the smoke test of the first one checks every
mode and stage, wide and narrow, against `native_rdma` at SF 1.

```bash
cd /pub/scratch/agirjoaba/oasis-pushdown/oasis
BS=filter-bloom-4dec-rdma-try1        # the chosen try
R=/pub/scratch/agirjoaba/oasis-pushdown/bench/runs/$(date +%Y%m%d)_$(hostname -s)_$BS

# 1. 8 sessions x 32 threads: oasis, oasis_nohw, native_rdma, oasis and oasis_nohw narrow, the stage
#    ablation s1..s4 (s5 = oasis, none = oasis_nohw) at 8 sessions; latency at 4 and 16 threads;
#    the hardware filter log of Q4, Q5, Q8, Q17, Q18, Q21
bash benchmarking/bench.sh run --bitstream $BS --sf 30 --benchmarks tpch --threads 32 --sessions 8 \
    --ablation-threads 32 --latency-threads 4,16 \
    --modes native_rdma,oasis_nohw,oasis,oasis_nohw_narrow,oasis_narrow --hw-log 4,5,8,17,18,21 --run $R

# 2. latency at 1 and 2 threads, prefetch depth 2 (default), 3 and 4
bash benchmarking/bench.sh run --bitstream $BS --sf 30 --benchmarks tpch --experiments latency \
    --latency-threads 1,2 --modes oasis_nohw,oasis,oasis_pf3,oasis_pf4 --no-setup --no-smoke --run ${R}_lowt

# 3. the scan metrics (lookup table regions) under 8 sessions: throughput with the DuckDB profiles
bash benchmarking/bench.sh run --bitstream $BS --sf 30 --benchmarks tpch --experiments throughput \
    --threads 32 --sessions 8 --modes oasis,oasis_narrow --tp-profile --reps 1 --no-setup --no-smoke --run ${R}_tpprof
```

What to read in `summary.md`: post-filter GB/s near 12.3 means PCIe binds (the decoder output stalled
share rises with it); CPU s/query times the rate near the 32 cores means the CPU binds; the narrow table
gives the gain and the widening cost in CPU s/query; no buffer separates host buffer starvation from PCIe
backpressure; the idle share of each decoder, oasis against oasis_nohw, tells a dispatcher that holds
back flows from too few groups in flight; the latency seconds of `oasis_pf3` and `oasis_pf4` against
`oasis` at 1 and 2 threads show the effect of the prefetch depth.

## Data

| Dataset | Folder | Size | Used by |
|---|---|---|---|
| `tpch_sf<N>` | `$WORK/data/tpch/sf<N>` | SF 30: 9.84 GiB, 1465 lineitem row groups | TPC-H |
| `tpch_sf1` | `$WORK/data/tpch/sf1` | | smoke |
| `clickbench` | `$WORK/data/clickbench/full` | 100M rows of `hits` | ClickBench |
| `clickbench_sample` | `$WORK/data/clickbench/sample` | 1 % of the row groups | smoke |

`gen_data.py` writes the files with the DuckDB writer defaults (Snappy, 122880 rows per row group, below
`FILTER_SEL_ROWS`) and checks them against the decoder limits (`extension/README.md`). ClickBench comes
from `hits_compatible/hits.parquet`, rewritten with the types of DuckDB's ClickBench schema: `EventTime`,
`ClientEventTime` and `LocalEventTime` as TIMESTAMP, `EventDate` as DATE, the strings as VARCHAR.
`gen_data.py queries` runs every query on the CPU at 8 and at 32 threads. The queries whose rows differ
between the two (a LIMIT over ties) go to `loose.json`, and `analyze.py` compares only their row count.

TPC-DS is not included: its columns hold NULLs, which the decoder does not read.

## Output

`$WORK/runs/<date>_<host>_<bitstream>/`:
- **Run facts:** `run.txt`, `run.json` (the options), `bitstream.txt`, `memory_limit`, `index.tsv`
  (one line per cell: name, benchmark, dataset, mode, threads, kind, rep, rc, seconds), `loose_*.json`
  and the node facts of `lib/node.sh info`.
- **Cell folders** `<dataset>_<mode>_t<threads>_<tp|lat>_r<rep>/`:
  - `bench.log`, `meta.json`, `settings.tsv`, `warmup.jsonl`
  - `throughput.jsonl` (seconds, Q/h, CPU, the decoder counters before and after, start and end of
    every query of every stream)
  - `latency.jsonl` (per query: seconds, CPU, rows, decoder counters after and, since 2026-11-06,
    before the query). The counters are the rows of `oasis_stream_profile()`; its software columns
    `post_filter_bytes` (bytes the FPGA wrote to host memory after the FilterStage) and `no_buffer_ns`
    (time with a decoded flow in flight but no host buffer) enter the analysis when present.
  - `profile/qNN_r0.json`; with `--tp-profile` also `profile/tpR_sS_pPP_qNN.json` (repetition, stream,
    position, query)
  - `hwlog/qNN_r0.txt` of a log cell: the hardware filter messages of DuckDB's log, one per line
  - `results/qNN.tsv` (from the latency pass, else from stream 0 of the throughput run)
  - `nstats_before.txt`, `nstats_after.txt` (FPGA network counters), `store_before.txt`,
    `store_after.txt` (NIC counters of the storage node, when ssh works)
- **`smoke/`:** the smoke cells, with their own `summary.md`.
- **`stalls/`:** a cell stopped by a RoCE stall, as `<cell>_<HHMMSS>/`; `stalls.txt` (one line per
  stall) and `program.log` of the programming that followed.
- **Analysis:** `summary.md` and `summary.json` (`analyze.py`), `plots/*.svg` (`plot.py`): throughput
  per mode, Q/h per stage set, latency per query, and the speedup per query and stage set; a narrow mode
  in the colour of its mode, striped. `summary.md` holds the decoder output cycles apart (handshake,
  starved, stalled, idle, and idle per decoder), the post-filter GB and GB/s, the narrow against the
  wide modes, the "HW ..." scan metrics of the profiles (lookup table regions) and the hardware filter
  log. `analyze.py <run> --out DIR` and `plot.py <run> --out DIR` write to another folder.

## Files

| File | Where | Does |
|---|---|---|
| `bench.sh` | all | the entry point |
| `lib/env.sh` | all | paths (`WORK`, `HW`, `BITSTREAMS`) and the port of each dataset; each one can be overridden |
| `lib/build.sh` | build node | state against the bitstream source, `$HW/build.sh` when the sources changed, `bench` |
| `lib/node.sh` | compute node | node facts, driver, program, hugepages, release |
| `lib/store.sh` | storage node | the servers, their RoCE address, NIC counters |
| `bench.cpp` | compute node | the measurement |
| `queries/<suite>/` | | `tables.txt` and `qNN.sql`: TPC-H of `tpch_queries()` (DuckDB 1.5.6), ClickBench `duckdb/queries.sql` (q00 to q42) |
| `gen_data.py` | build node | the Parquet files, the decoder limit check, the query check |
| `analyze.py` | any | `summary.md`, `summary.json` |
| `plot.py` | any | `plots/*.svg` |
| `sim_check.sh` | build node | the harness in the Verilator simulation (`bench.sh simcheck`) |
