#!/usr/bin/env python3
"""Writes the Parquet files of the benchmarks and checks them against the decoder limits.

    python3 gen_data.py tpch --sf 30                       # $DATA/tpch/sf30/*.parquet
    python3 gen_data.py clickbench --source hits.parquet   # $DATA/clickbench/{full,sample}/hits.parquet
    python3 gen_data.py check <folder>                     # the check alone
    python3 gen_data.py queries <suite> <folder>           # every query on the CPU, twice (see below)

The writer is the duckdb Python module with the defaults of COPY (FORMAT parquet), the same as
scenarios/correctness/prepare.py (Snappy, 122880 rows per row group). Run it on hacc-build-03 (dbgen at
SF 30 needs about 40 GB of memory). Every folder gets a manifest.json.

ClickBench: the hits.parquet of ClickBench (hits_compatible) stores the times as seconds, the date as
days and the strings as BINARY. The rewrite gives them the types of the DuckDB schema of ClickBench
(TIMESTAMP, DATE, VARCHAR), as its view in duckdb-parquet/create.sql does at every scan. The sample is
1 % of the row groups (about 1M rows), for the smoke test.

The check fails when a fixed-width column has NULLs, an encoding other than PLAIN or RLE_DICTIONARY, or a
compression other than SNAPPY or UNCOMPRESSED (extension/README.md), or when a row group has more rows than
the selection bitmap of the FilterStage (FILTER_SEL_ROWS).

The query check runs every query of queries/<suite> with read_parquet at 8 and at 32 threads and writes
loose.json into the folder: the queries whose rows differ between the two runs (a LIMIT over ties, a LIMIT
without ORDER BY). analyze.py compares only the row count of those.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
TPCH_TABLES = ["lineitem", "orders", "customer", "part", "partsupp", "supplier", "nation", "region"]
FIXED_TYPES = {"INT32", "INT64", "FLOAT", "DOUBLE"}
ENCODINGS_OK = {"PLAIN", "RLE_DICTIONARY", "PLAIN_DICTIONARY", "RLE", "BIT_PACKED"}
COMPRESSION_OK = {"SNAPPY", "UNCOMPRESSED"}
FILTER_SEL_ROWS = 2 ** 17  # hardware/src/hdl/common.sv
# Seconds since 1970 in hits_compatible
CLICKBENCH_TIMES = ["EventTime", "ClientEventTime", "LocalEventTime"]


def data_dir() -> Path:
    work = os.environ.get("WORK")
    if not work:
        raise SystemExit("WORK is not set: run it through bench.sh, or source lib/env.sh")
    return Path(work) / "data"


def gen_tpch(con, sf: float, out: Path) -> None:
    con.sql("INSTALL tpch; LOAD tpch;")
    t0 = time.time()
    con.sql(f"CALL dbgen(sf = {sf})")
    print(f"dbgen sf={sf}: {time.time() - t0:.0f} s", flush=True)
    for t in TPCH_TABLES:
        t0 = time.time()
        con.sql(f"COPY {t} TO '{out / t}.parquet' (FORMAT parquet)")
        print(f"  {t}.parquet: {time.time() - t0:.0f} s", flush=True)


def clickbench_select(con, source: Path) -> str:
    """SELECT of the source with the ClickBench types of DuckDB"""
    cols = con.sql(f"DESCRIBE SELECT * FROM read_parquet('{source}', binary_as_string = true)").fetchall()
    exprs = []
    for name, typ, *_ in cols:
        if name in CLICKBENCH_TIMES and "INT" in typ:
            exprs.append(f"make_timestamp({name}::BIGINT * 1000000) AS {name}")
        elif name == "EventDate" and "INT" in typ:
            exprs.append(f"(DATE '1970-01-01' + {name}::INTEGER) AS {name}")
        else:
            exprs.append(name)
    return f"SELECT {', '.join(exprs)} FROM read_parquet('{source}', binary_as_string = true)"


def gen_clickbench(con, source: Path, out: Path) -> None:
    full, sample = out / "full", out / "sample"
    full.mkdir(parents=True, exist_ok=True)
    sample.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    con.sql(f"COPY ({clickbench_select(con, source)}) TO '{full}/hits.parquet' (FORMAT parquet)")
    print(f"full/hits.parquet: {time.time() - t0:.0f} s", flush=True)
    t0 = time.time()
    con.sql(f"COPY (SELECT * FROM read_parquet('{full}/hits.parquet') USING SAMPLE 1 PERCENT (system, 42)) "
            f"TO '{sample}/hits.parquet' (FORMAT parquet)")
    print(f"sample/hits.parquet: {time.time() - t0:.0f} s", flush=True)


def check(con, out: Path) -> dict:
    files = {}
    problems = []
    for f in sorted(out.glob("*.parquet")):
        t = f.stem
        rows = con.sql(f"""
            SELECT row_group_id, row_group_num_rows, path_in_schema, type, encodings, compression,
                   stats_null_count
            FROM parquet_metadata('{f}')""").fetchall()
        groups = {}
        for rg, nrows, col, typ, enc, comp, nulls in rows:
            groups[rg] = nrows
            if comp not in COMPRESSION_OK:
                problems.append(f"{t}.{col}: compression {comp}")
            if typ in FIXED_TYPES:
                bad = set(re.findall(r"[A-Z_]+", str(enc))) - ENCODINGS_OK
                if bad:
                    problems.append(f"{t}.{col} row group {rg}: encodings {enc}")
                if nulls not in (None, 0):
                    problems.append(f"{t}.{col} row group {rg}: {nulls} NULLs")
        sizes = sorted(set(groups.values()))
        if sizes and sizes[-1] > FILTER_SEL_ROWS:
            problems.append(f"{t}: row group of {sizes[-1]} rows > FILTER_SEL_ROWS {FILTER_SEL_ROWS}")
        files[t] = {"bytes": f.stat().st_size, "row_groups": len(groups),
                    "rows": sum(groups.values()), "row_group_rows_max": sizes[-1] if sizes else 0}
    return {"files": files, "problems": sorted(set(problems))}


def write_manifest(con, out: Path, extra: dict) -> int:
    result = check(con, out)
    manifest = {**extra, "duckdb_writer": duckdb.__version__, "written": time.strftime("%Y-%m-%d %H:%M:%S"),
                **result}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    total = sum(f["bytes"] for f in result["files"].values())
    groups = max((f["row_groups"] for f in result["files"].values()), default=0)
    print(f"{out}: {total / 2**30:.2f} GiB, {len(result['files'])} files, at most {groups} row groups per file")
    for p in result["problems"]:
        print(f"PROBLEM {p}")
    return 1 if result["problems"] else 0


def run_queries(folder: Path, suite: Path, threads: int) -> dict:
    con = duckdb.connect()
    con.sql(f"SET threads = {threads}")
    for t in (suite / "tables.txt").read_text().split():
        con.sql(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{folder / t}.parquet')")
    out = {}
    for q in sorted(suite.glob("q*.sql")):
        t0 = time.time()
        try:
            rows = con.sql(q.read_text()).fetchall()
            out[q.stem] = {"rows": sorted(map(repr, rows)), "seconds": time.time() - t0}
        except duckdb.Error as e:
            out[q.stem] = {"error": str(e).splitlines()[0]}
    return out


def check_queries(suite: Path, folder: Path) -> int:
    a, b = run_queries(folder, suite, 8), run_queries(folder, suite, 32)
    errors = {q: r["error"] for q, r in a.items() if "error" in r}
    loose = sorted(q for q in a if q not in errors and a[q]["rows"] != b[q]["rows"])
    for q, r in a.items():
        print(f"{q}: " + (f"ERROR {r['error']}" if "error" in r else
                          f"{len(r['rows'])} rows, {r['seconds']:.2f} s" + (" (loose)" if q in loose else "")))
    (folder / "loose.json").write_text(json.dumps({"suite": suite.name, "loose": loose, "errors": errors},
                                                  indent=2) + "\n")
    print(f"{len(a)} queries, {len(errors)} errors, loose: {' '.join(loose) or 'none'}")
    return 1 if errors else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tpch")
    p.add_argument("--sf", type=float, default=30)
    p.add_argument("--check-only", action="store_true")
    p = sub.add_parser("clickbench")
    p.add_argument("--source", type=Path, help="hits.parquet of ClickBench (default: download/ of the data folder)")
    p.add_argument("--check-only", action="store_true")
    p = sub.add_parser("check")
    p.add_argument("folder", type=Path)
    p = sub.add_parser("queries")
    p.add_argument("suite", help="folder name in queries/: tpch or clickbench")
    p.add_argument("folder", type=Path)
    args = ap.parse_args()

    con = duckdb.connect()
    if args.cmd == "tpch":
        out = data_dir() / "tpch" / f"sf{args.sf:g}"
        out.mkdir(parents=True, exist_ok=True)
        if not args.check_only:
            gen_tpch(con, args.sf, out)
        return write_manifest(con, out, {"benchmark": "tpch", "scale_factor": args.sf})
    if args.cmd == "clickbench":
        out = data_dir() / "clickbench"
        source = args.source or out / "download" / "hits.parquet"
        if not args.check_only:
            gen_clickbench(con, source, out)
        rc = write_manifest(con, out / "full", {"benchmark": "clickbench", "source": str(source)})
        return rc | write_manifest(con, out / "sample", {"benchmark": "clickbench", "source": str(source),
                                                         "sample": "1 PERCENT (system, 42)"})
    if args.cmd == "check":
        old = args.folder / "manifest.json"
        keep = json.loads(old.read_text()) if old.exists() else {}
        return write_manifest(con, args.folder, {k: v for k, v in keep.items()
                                                 if k in ("benchmark", "scale_factor", "source", "sample")})
    return check_queries(HERE / "queries" / args.suite, args.folder)


if __name__ == "__main__":
    sys.exit(main())
