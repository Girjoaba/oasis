# Scenarios

One command runs the DuckDB extension against the simulated vFPGA with a given network model and writes `result.json`. `run.py` needs Python 3.10.

```bash
$ scenarios/run.py scenarios/tpch_q6/scenario.json --sim-dir path/to/build-sim \
    [--duckdb path/to/duckdb] [--verilator-bin path/to/coyote_vl] [--out runs/q6]
```

`--sim-dir` is the hardware simulation build (`COYOTE_SIM_DIR`). `--duckdb` is the shell built against the simulation library (`EN_SIMULATION`). Its libraries must be on `LD_LIBRARY_PATH`. Without `--verilator-bin` the run uses Vivado xsim. `--vfpga-top FILE` places that top into `<sim-dir>/sim/vfpga_top.svh` before the run. `--timeout S` kills the shell after `S` seconds (default 7200).

The exit code is 0 when every check passes, 1 when one fails, 2 on a bad scenario.

## Scenario file

```json
{
  "name": "tpch_q6",
  "network": {"model": "mock", "seed": 1,
              "ports": {"rdma": {"latency_ns": 50, "gbps": 0, "mtu": 4096, "jitter_ns": 0, "drop": 0}}},
  "image": "image.bin",
  "files": ["lineitem.parquet"],
  "sql": "q6.sql",
  "checks": [{"expect": {"count_star()": 60175}}, {"equal": ["revenue", "revenue_native"]}]
}
```

Paths are relative to the scenario file. `network.model` is `mock`: the memory mocks of the Coyote simulation, with the timing of the ports `host`, `card` and `rdma` (`latency_ns`, `gbps`, `mtu`, `jitter_ns`, `drop`, `retransmit_ns`). `image` is the memory image of the RDMA mock (`COYOTE_SIM_RDMA_IMAGE`), `files` are linked into the run folder so the SQL can name them, `sql` goes to the DuckDB shell. A check is `expect` (column equals value) or `equal` (columns equal each other) over the columns of every one-row result.

## Result

The run folder (`--out`, default `runs/<name>_<time>`) holds `result.json`, `duckdb.out` (the JSON results), `duckdb.err`, the simulator log (`simulate.log` or `coyote_vl.log`) and `vivado.log` when xsim ran. `result.json` fields:

- `pass`, `rc`, `timed_out`: the outcome; `checks` with the values seen.
- `values`: every column of every one-row result (the decoder counters of stream 0 land here); `results`: all rows of every result.
- `simulated_ns`: from the `Testbench finished` line of the simulator log; `wall_s`, `cpu_user_s`, `cpu_sys_s`, `max_rss_kb`: the shell and the simulator together.
- `scenario`, `scenario_file`, `network`, `simulator`, `sim_dir`, `duckdb`, `host`, `started`, `env` (the `COYOTE_SIM_*` variables), `logs` (the files of the run folder), `warnings`.

## TPC-H Q6

`tpch_q6/` runs Q6 through `read_oasis('rdma://lineitem.parquet')` against `read_parquet` on the same file, plus the decoder counters of `oasis_stream_profile()`:

- `scenario.json`: the default mock (50 ns, unlimited rate, MTU 4096).
- `scenario_100g.json`, `scenario_10g.json`: 2 us latency, 1 us jitter, a rate limit of 100 or 10 Gbit/s.
- `q6.sql`: the statements for the DuckDB shell.
- `prepare.py`: writes `lineitem.parquet` at scale factor 0.01 with the `duckdb` module and packs `image.bin` with `rdma_server/pack_image.py`. Both files are not committed.

The checks of every scenario: 60 175 rows, and the revenue through Oasis equals the native one.
