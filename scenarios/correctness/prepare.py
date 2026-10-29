#!/usr/bin/env python3
"""Write the data, the SQL and the scenario files of the correctness suite.

Usage: prepare.py [--sf 0.01] [--out out]

Every scenario reads the same data through read_oasis (the simulated vFPGA) and through
read_parquet (DuckDB's own reader) and counts the rows that differ in either direction
(EXCEPT ALL both ways, so duplicates count). Scenarios:

- tpch_q01 .. tpch_q22: the TPC-H queries of tpch_queries() at --sf over rdma://.
- edge: the edge cases of CASES over rdma://. Every case must give 0 differing rows.
- gap_<case>: one scenario per input of GAPS, which the decoder does not support yet. The case
  must give 0 differing rows or fail with an error, never wrong rows. One scenario each, so a
  case that hangs or crashes the shell does not hide the others.

All scenarios read over rdma://: the build enables RDMA, and read_oasis on a local file then
stops the process ("LocalSourceOperator cannot be used when RDMA is enabled").

The output folder holds data/*.parquet, image.bin (every Parquet file, packed by
rdma_server/pack_image.py for the RDMA mock), <scenario>.sql, <scenario>.json and
scenarios.json (the list of scenario names, the matrix of the CI workflow).
Needs the duckdb and pyarrow Python modules. The first run downloads the tpch extension.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

HERE = Path(__file__).resolve().parent
PACK = HERE.parents[1] / "rdma_server" / "pack_image.py"  # HERE = <oasis>/scenarios/correctness

TPCH_TABLES = ("lineitem", "orders", "customer", "part", "partsupp", "supplier", "nation", "region")
NETWORK = {"model": "mock", "seed": 1,
           "ports": {"rdma": {"latency_ns": 50, "gbps": 0, "mtu": 4096, "jitter_ns": 0, "drop": 0}}}

# Rows of e_many_rg: 10 full row groups of 2048 rows and a partial one.
RG = 2048
MANY_RG_ROWS = 10 * RG + 123

# DuckDB-written files: name -> (query, COPY options). DuckDB is the writer of the benchmark data.
DUCKDB_FILES: Dict[str, Tuple[str, str]] = {
    # Every logical type on a hardware physical type, across two vector boundaries.
    "e_types": ("""
        SELECT i::INTEGER AS c_id,
               (i * 7919 - 40000000)::INTEGER AS c_int,
               (i::BIGINT * 1000000007 - 5000000000000)::BIGINT AS c_bigint,
               (i * 0.25 - 1000)::FLOAT AS c_float,
               (i * 1.0625 - 5000)::DOUBLE AS c_double,
               DATE '1970-01-01' + (i * 37 - 100000)::INTEGER AS c_date,
               ((i * 13 - 50000) / 100)::DECIMAL(9, 2) AS c_dec9,
               ((i::BIGINT * 1234567 - 6000000000) / 100)::DECIMAL(18, 2) AS c_dec18,
               make_timestamp(i::BIGINT * 1000000007 - 2000000000000) AS c_ts,
               TIME '00:00:00' + to_seconds(i * 17 % 86400) AS c_time,
               ((i % 256) - 128)::TINYINT AS c_tinyint,
               ((i * 7 % 65536) - 32768)::SMALLINT AS c_smallint,
               (i::BIGINT * 429497)::UINTEGER AS c_uint,
               (i::UBIGINT * 1844674407370955) AS c_ubigint,
               's' || i AS c_str
        FROM range(2 * 2048 + 5) r(i)""", ""),
    # Limits of every type, the special floats and both signs of zero.
    "e_extremes": ("""
        SELECT c_int::INTEGER AS c_int, c_bigint::BIGINT AS c_bigint, c_float::FLOAT AS c_float,
               c_double::DOUBLE AS c_double, c_date::DATE AS c_date, c_dec18::DECIMAL(18, 2) AS c_dec18
        FROM (VALUES
            (-2147483648, -9223372036854775808, '-inf'::FLOAT, '-inf'::DOUBLE, DATE '0001-01-01', -9999999999999999.99),
            (2147483647, 9223372036854775807, 'inf'::FLOAT, 'inf'::DOUBLE, DATE '9999-12-31', 9999999999999999.99),
            (0, 0, 0.0::FLOAT, 0.0::DOUBLE, DATE '1970-01-01', 0.00),
            (-1, -1, -0.0::FLOAT, -0.0::DOUBLE, DATE '1969-12-31', -0.01),
            (1, 1, 'nan'::FLOAT, 'nan'::DOUBLE, 'infinity'::DATE, 0.01),
            (2, 2, 1.4e-45::FLOAT, 5e-324::DOUBLE, '-infinity'::DATE, 1.00),
            (3, 3, 3.4028235e38::FLOAT, 1.7976931348623157e308::DOUBLE, DATE '2000-02-29', -1.00),
            (4, 4, 1.17549435e-38::FLOAT, 2.2250738585072014e-308::DOUBLE, DATE '1900-03-01', 12345.67))
            t(c_int, c_bigint, c_float, c_double, c_date, c_dec18)""", ""),
    # Low cardinality: DuckDB writes PLAIN_DICTIONARY for every column.
    "e_lowcard": ("""
        SELECT (i % 5)::INTEGER AS c_int, (i % 3 - 1)::BIGINT * 1000000000000 AS c_bigint,
               (i % 4 * 0.5)::FLOAT AS c_float, (i % 6 * 1.25)::DOUBLE AS c_double,
               DATE '2020-01-01' + (i % 7)::INTEGER AS c_date, (i % 9 / 4)::DECIMAL(18, 2) AS c_dec18
        FROM range(3 * 2048) r(i)""", ""),
    # More columns than decoder streams.
    "e_wide": ("SELECT " + ", ".join(
        f"(i * {k + 1} % 1000003)::{('INTEGER', 'BIGINT', 'DOUBLE', 'FLOAT')[k % 4]} AS c{k}" for k in range(40))
        + " FROM range(2100) r(i)", ""),
    # CPU columns: empty, long, multi-byte and NULL strings next to a hardware column.
    "e_strings": ("""
        SELECT i::INTEGER AS c_id,
               CASE i % 5 WHEN 0 THEN '' WHEN 1 THEN repeat('x', 3000 + i % 100)
                          WHEN 2 THEN 'üñî' || i WHEN 3 THEN NULL ELSE 'v' || i END AS c_str
        FROM range(1000) r(i)""", ""),
    # Many row groups, with a sorted key for zone map pruning.
    "e_many_rg": (f"""
        SELECT i::INTEGER AS c_id, (i * 31 % 1000)::INTEGER AS c_val,
               (i * 0.75)::DOUBLE AS c_double, 'k' || (i % 100) AS c_str
        FROM range({MANY_RG_ROWS}) r(i)""", f", ROW_GROUP_SIZE {RG}"),
    "e_uncompressed": ("SELECT c_id, c_int, c_bigint, c_float, c_double, c_date, c_dec18 FROM e_types",
                       ", COMPRESSION uncompressed"),
    # Known gaps.
    "g_nulls": ("""
        SELECT i::INTEGER AS c_id,
               CASE WHEN i % 7 = 0 THEN NULL ELSE i END::INTEGER AS c_int,
               CASE WHEN i % 5 = 0 THEN NULL ELSE i * 3 END::BIGINT AS c_bigint,
               CASE WHEN i % 3 = 0 THEN NULL ELSE i * 0.5 END::DOUBLE AS c_double,
               NULL::INTEGER AS c_allnull
        FROM range(3000) r(i)""", ""),
    "g_bool": ("SELECT i::INTEGER AS c_id, i % 3 = 0 AS c_bool FROM range(3000) r(i)", ""),
    "g_v2": ("SELECT c_id, c_int, c_bigint, c_double, c_date FROM e_types", ", PARQUET_VERSION V2"),
    "g_zstd": ("SELECT c_id, c_int, c_bigint, c_double FROM e_types", ", COMPRESSION zstd"),
    "g_dec38": ("SELECT c_id, c_dec18::DECIMAL(38, 2) AS c_dec38 FROM e_types", ""),
    "g_empty": ("SELECT c_id, c_bigint FROM e_types LIMIT 0", ""),
}


def write_pyarrow_files(data: Path) -> None:
    """Files of a second writer: exact row group sizes, many pages per column chunk, its own dictionary."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    def table(n: int, start: int = 0, nullable: bool = True) -> pa.Table:
        ids = list(range(start, start + n))
        cols = {"c_i32": pa.array(ids, pa.int32()),
                "c_i64": pa.array([i * 1000003 - 7 for i in ids], pa.int64()),
                "c_f32": pa.array([i * 0.5 for i in ids], pa.float32()),
                "c_f64": pa.array([i * -1.25 for i in ids], pa.float64())}
        schema = pa.schema([pa.field(k, v.type, nullable=nullable) for k, v in cols.items()])
        return pa.table(cols, schema=schema)

    # REQUIRED columns (no definition levels), row groups of 1, 7, 2047, 2049, 4097 and 1 rows.
    sizes = [1, 7, 2047, 2049, 4097, 1]
    with pq.ParquetWriter(data / "e_pa_rg.parquet", table(1, nullable=False).schema,
                          use_dictionary=False, compression="snappy") as w:
        start = 0
        for n in sizes:
            w.write_table(table(n, start, nullable=False), row_group_size=n)
            start += n
    # One row group, pages of 4 KiB: many data pages per column chunk.
    pq.write_table(table(20000), data / "e_pa_pages.parquet", use_dictionary=False,
                   compression="snappy", data_page_size=4096)
    # Dictionary pages of the Arrow writer (format 1.0: PLAIN_DICTIONARY).
    lc = pa.table({"c_i32": pa.array([i % 11 for i in range(5000)], pa.int32()),
                   "c_i64": pa.array([i % 13 * 10**12 for i in range(5000)], pa.int64()),
                   "c_f64": pa.array([i % 17 * 0.5 for i in range(5000)], pa.float64())})
    pq.write_table(lc, data / "e_pa_dict.parquet", use_dictionary=True, version="1.0", compression="snappy")
    # Gap: data page header V2.
    pq.write_table(table(5000), data / "g_pa_v2page.parquet", use_dictionary=False,
                   compression="snappy", data_page_version="2.0")


# Edge cases: name -> query. {f} is the source of file f (read_oasis or read_parquet).
CASES: Dict[str, str] = {
    "types_all": "SELECT * FROM {e_types}",
    "types_count": "SELECT count(*) AS n FROM {e_types}",
    "types_subset": "SELECT c_bigint, c_date FROM {e_types}",
    "types_permuted": "SELECT c_str, c_double, c_id, c_ubigint, c_int FROM {e_types}",
    "types_repeated": "SELECT c_int AS a, c_int AS b, c_dec9 FROM {e_types}",
    "types_agg": """SELECT sum(c_bigint) AS s1, sum(c_dec18) AS s2, min(c_date) AS d1, max(c_ts) AS t1,
                    sum(c_tinyint) AS s3, sum(c_smallint) AS s4, sum(c_uint) AS s5, max(c_ubigint) AS u1
                    FROM {e_types}""",
    # VARCHAR casts tell -0.0 from 0.0, which compare equal.
    "extremes": "SELECT *, c_float::VARCHAR AS f_txt, c_double::VARCHAR AS d_txt FROM {e_extremes}",
    "lowcard": "SELECT * FROM {e_lowcard}",
    "wide": "SELECT * FROM {e_wide}",
    "wide_sparse": "SELECT c39, c0, c17 FROM {e_wide}",
    "strings": "SELECT * FROM {e_strings}",
    "strings_filter": "SELECT c_id, length(c_str) AS len FROM {e_strings} WHERE c_str LIKE 'ü%' AND c_id > 500",
    "uncompressed": "SELECT * FROM {e_uncompressed}",
    "rg_all": "SELECT * FROM {e_many_rg}",
    "rg_prune_all": "SELECT * FROM {e_many_rg} WHERE c_id > 1000000",
    "rg_prune_none": "SELECT * FROM {e_many_rg} WHERE c_id >= 0",
    "rg_edge": f"SELECT * FROM {{e_many_rg}} WHERE c_id BETWEEN {RG - 1} AND {RG}",
    "rg_first_row": f"SELECT * FROM {{e_many_rg}} WHERE c_id = {2 * RG}",
    "rg_last_row": f"SELECT * FROM {{e_many_rg}} WHERE c_id = {MANY_RG_ROWS - 1}",
    "rg_in": f"SELECT * FROM {{e_many_rg}} WHERE c_id IN (0, {RG - 1}, {RG}, {5 * RG}, {MANY_RG_ROWS - 1})",
    "rg_value": "SELECT c_id, c_val FROM {e_many_rg} WHERE c_val < 10",
    "rg_double": "SELECT c_id, c_double FROM {e_many_rg} WHERE c_double BETWEEN 100.5 AND 2000.25",
    "rg_cpu_and_hw": "SELECT c_id, c_str FROM {e_many_rg} WHERE c_str = 'k7' AND c_val > 500",
    "rg_or": "SELECT c_id FROM {e_many_rg} WHERE c_id < 10 OR c_val = 999",
    # The hash join pushes a dynamic filter on c_id into the probe-side scan.
    "join_dynamic": """SELECT a.c_id, a.c_val, b.c_int FROM {e_many_rg} a
                       JOIN (SELECT c_id, c_int FROM {e_types} WHERE c_id BETWEEN 3000 AND 3100) b ON a.c_id = b.c_id""",
    "pa_rg": "SELECT * FROM {e_pa_rg}",
    "pa_rg_filter": "SELECT * FROM {e_pa_rg} WHERE c_i32 BETWEEN 2050 AND 2060",
    "pa_pages": "SELECT * FROM {e_pa_pages}",
    "pa_pages_filter": "SELECT c_i32, c_f64 FROM {e_pa_pages} WHERE c_i32 BETWEEN 9990 AND 10010",
    "pa_dict": "SELECT * FROM {e_pa_dict}",
}

# Known gaps (extension/test/README.md, "Known gaps", and the codecs and encodings ParCore lacks).
GAPS: Dict[str, str] = {
    "nulls": "SELECT * FROM {g_nulls}",
    "nulls_count": "SELECT count(c_int) AS a, count(c_bigint) AS b, count(c_allnull) AS c, count(*) AS n FROM {g_nulls}",
    "bool": "SELECT * FROM {g_bool}",
    "v2": "SELECT * FROM {g_v2}",
    "zstd": "SELECT * FROM {g_zstd}",
    "dec38": "SELECT * FROM {g_dec38}",
    "empty": "SELECT * FROM {g_empty}",
    "pa_v2page": "SELECT * FROM {g_pa_v2page}",
}


def hw_source(name: str) -> str:
    return f"read_oasis('rdma://{name}.parquet')"


def sw_source(name: str) -> str:
    return f"read_parquet('{name}.parquet')"


def diff_sql(hw: str, sw: str) -> str:
    """Rows of hw missing from sw plus rows of sw missing from hw, duplicates counted."""
    return (f"(SELECT count(*) FROM (FROM {hw} EXCEPT ALL FROM {sw}))"
            f" + (SELECT count(*) FROM (FROM {sw} EXCEPT ALL FROM {hw}))")


HEADER = "SET oasis_rdma_server = 'sim';\n"
# The decoders saw input: the scan ran in the vFPGA, not only on the CPU. The bypass row has no in-side.
FOOTER = ("SELECT coalesce(sum(in_handshakes_cycles), 0) > 0 AS hw_used FROM oasis_stream_profile();\n"
          "SELECT 1 AS done;\n")


def tpch_sql(query: str) -> Tuple[str, List[str]]:
    """The SQL of one TPC-H scenario and the tables it reads."""
    query = query.strip().rstrip(";")
    tables = [t for t in TPCH_TABLES if re.search(rf"\b{t}\b", query)]
    lines = [HEADER, "CREATE SCHEMA hw;\nCREATE SCHEMA sw;\n"]
    for t in tables:
        lines.append(f"CREATE VIEW hw.{t} AS FROM {hw_source(t)};\n")
        lines.append(f"CREATE VIEW sw.{t} AS FROM {sw_source(t)};\n")
    lines.append(f"USE hw;\nCREATE TEMP TABLE r_hw AS {query};\n")
    lines.append(f"USE sw;\nCREATE TEMP TABLE r_sw AS {query};\n")
    lines.append(f"USE main;\nSELECT {diff_sql('r_hw', 'r_sw')} AS diff_rows,"
                 " (SELECT count(*) FROM r_sw) AS rows_native;\n")
    return "".join(lines) + FOOTER, tables


def files_of(query: str) -> List[str]:
    """The files that a case query names as {f}."""
    return sorted(set(re.findall(r"\{(\w+)\}", query)))


def cases_sql(cases: Dict[str, str], files: List[str]) -> str:
    """One CREATE TEMP TABLE per side and one diff_<case> row per case. A failed case leaves its diff out."""
    hw = {f: hw_source(f) for f in files}
    sw = {f: sw_source(f) for f in files}
    lines = [HEADER]
    for name, query in cases.items():
        lines.append(f"CREATE TEMP TABLE {name}_hw AS {query.format_map(hw)};\n")
        lines.append(f"CREATE TEMP TABLE {name}_sw AS {query.format_map(sw)};\n")
        lines.append(f"SELECT {diff_sql(name + '_hw', name + '_sw')} AS diff_{name};\n")
    return "".join(lines) + FOOTER


def write_scenario(out: Path, name: str, sql: str, files: List[str], checks: List[dict],
                   allow_errors: bool = False) -> str:
    (out / f"{name}.sql").write_text(sql)
    scenario = {"name": name, "network": NETWORK, "image": "image.bin",
                "files": [f"data/{f}.parquet" for f in files], "sql": f"{name}.sql",
                "allow_errors": allow_errors, "checks": checks}
    (out / f"{name}.json").write_text(json.dumps(scenario, indent=2) + "\n")
    return name


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--sf", type=float, default=0.01, help="TPC-H scale factor (default 0.01)")
    p.add_argument("--out", type=Path, default=HERE / "out", help="output folder (default out/ next to this file)")
    args = p.parse_args()
    import duckdb

    out = args.out.resolve()
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.sql("SET threads = 1")  # one writer thread: row groups in insertion order, the same bytes every run

    # TPC-H tables
    con.sql(f"INSTALL tpch; LOAD tpch; CALL dbgen(sf={args.sf})")
    for t in TPCH_TABLES:
        con.sql(f"COPY {t} TO '{data / t}.parquet' (FORMAT parquet)")

    # Edge and gap files
    for name, (query, options) in DUCKDB_FILES.items():
        con.sql(f"CREATE TABLE {name} AS {query}")
        con.sql(f"COPY {name} TO '{data / name}.parquet' (FORMAT parquet{options})")
    write_pyarrow_files(data)

    # Pack the RDMA image
    parquet = sorted(data.glob("*.parquet"))
    for f in parquet:
        print(f"{f.name}: {f.stat().st_size} B")
    subprocess.run([sys.executable, str(PACK), str(out / "image.bin"), *map(str, parquet)],
                   check=True, stdout=subprocess.DEVNULL)

    names = []
    done = {"expect": {"hw_used": True, "done": 1}}

    # One per TPC-H query
    for nr, query in con.sql("SELECT query_nr, query FROM tpch_queries() ORDER BY query_nr").fetchall():
        sql, tables = tpch_sql(query)
        names.append(write_scenario(out, f"tpch_q{nr:02d}", sql, tables, [{"expect": {"diff_rows": 0}}, done]))

    # All edge cases together
    efiles = files_of(" ".join(CASES.values()))
    names.append(write_scenario(out, "edge", cases_sql(CASES, efiles), efiles,
                                [{"expect": {f"diff_{c}": 0 for c in CASES}}, done]))

    # One per gap
    for case, query in GAPS.items():
        # done = 1 tells a clean failure of the case from a crashed shell.
        files = files_of(query)
        names.append(write_scenario(out, f"gap_{case}", cases_sql({case: query}, files), files,
                                    [{"zero_or_error": [f"diff_{case}"]}, {"expect": {"done": 1}}],
                                    allow_errors=True))

    # CI matrix list
    (out / "scenarios.json").write_text(json.dumps(names) + "\n")
    print(f"{len(names)} scenarios in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
