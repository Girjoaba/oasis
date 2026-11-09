#!/usr/bin/env python3
"""Write the data, the SQL and the scenario files of the correctness suite.

Usage: prepare.py [--sf 0.01] [--out out]

Every scenario reads the same data through read_oasis (the simulated vFPGA) and through
read_parquet (DuckDB's own reader) and counts the rows that differ in either direction
(EXCEPT ALL both ways, so duplicates count). Scenarios:

- tpch_q01 .. tpch_q22: the TPC-H queries of tpch_queries() at --sf over rdma://.
- edge: the edge cases of CASES over rdma://. Every case must give 0 differing rows.
- edge_stages: the cases of STAGE_CASES at every oasis_hw_filter_stage, each with 0 differing rows,
  and a check of the log that no stage uses a feature of a later stage.
- edge_table: the cases of TABLE_CASES with oasis_filter_table_early_release off and on, each with 0
  differing rows, and checks of the lookup table messages of the log.
- edge_dict_cache: the cases of DICT_CACHE_CASES with oasis_dict_cache off, off with
  oasis_prefetch_depth = 0 and on, each with 0 differing rows, and checks of the dictionary page
  messages of the log.
- edge_lossy: the cases of LOSSY_CASES at stages 3 and 5, each with 0 differing rows, filtered in
  hardware, with the CPU check of a join filter that the hardware passes a superset of (folded
  bitmap, Bloom filter, bitmap without room in the table) and without it for an exact bitmap.
- edge_prof: the cases of PROF_CASES, with the bytes that oasis_stream_profile() counts for the
  decoders between two reads (post_filter_bytes): 4 per value of an INTEGER column without a
  filter, under a tenth of that with a selective hardware filter.
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
# Rows of e_bloom_big: a build side of 60 000 keys gives a Bloom filter larger than half the lookup
# table, and the build side has to stay below the probe side.
BLOOM_BIG_ROWS = 64 * RG

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
    # Hardware filter: the types the FilterStage compares next to ones it only passes along, many
    # row groups. c_gap has only 0 and 199 in the odd row groups, so a range inside fails every row
    # of the group without the zone map pruning it.
    "e_filter": (f"""
        SELECT i::INTEGER AS c_id, (i * 31 % 1000)::INTEGER AS c_val,
               (CASE WHEN (i // {RG}) % 2 = 1 THEN (i % 2) * 199 ELSE i % 200 END)::INTEGER AS c_gap,
               (i::BIGINT * 1000003 - 5000000000)::BIGINT AS c_big,
               DATE '2000-01-01' + (i % 3650)::INTEGER AS c_date,
               ((i * 13 % 100000) / 100)::DECIMAL(9, 2) AS c_dec9,
               ((i::BIGINT * 7919 % 10000000) / 100)::DECIMAL(18, 2) AS c_dec18,
               (i * 0.75)::DOUBLE AS c_double, (i % 300)::SMALLINT AS c_small
        FROM range({MANY_RG_ROWS}) r(i)""", f", ROW_GROUP_SIZE {RG}"),
    # One row group with more rows than the selection bitmap of the FilterStage holds (2**17).
    "e_big_rg": ("SELECT i::INTEGER AS c_id, (i * 31 % 1000)::INTEGER AS c_val FROM range(200000) r(i)",
                 ", ROW_GROUP_SIZE 200000"),
    # Hardware lookup: join keys for the prefix range bitmaps of DuckDB, INT32 and INT64 with small
    # spans, and c_kwide in blocks of 64 keys 4096 apart, whose span the lookup table holds only
    # folded and whose chunks narrow to INT32.
    "e_prf": (f"""
        SELECT i::INTEGER AS c_id, (i * 3)::INTEGER AS c_k32,
               (1000000000000 + i::BIGINT * 5)::BIGINT AS c_k64,
               ((i // 64)::BIGINT * 4096 + i % 64)::BIGINT AS c_kwide,
               (i * 31 % 1000)::INTEGER AS c_val, (i * 0.5)::DOUBLE AS c_double
        FROM range({MANY_RG_ROWS}) r(i)""", f", ROW_GROUP_SIZE {RG}"),
    # Hardware Bloom filter: join keys whose span exceeds the 2^26 bits of DuckDB's prefix range
    # filters, so a join with more keys than an IN list pushes a Bloom filter. c_k32 and c_kn have the
    # same values of both signs as INTEGER and BIGINT (c_kn narrows to INT32 with
    # oasis_narrow_transfer), c_k64 lies above 2^40.
    "e_bloom": (f"""
        SELECT i::INTEGER AS c_id, (i::BIGINT * 104729 % 2147483647 - 1073741824)::INTEGER AS c_k32,
               (1000000000000 + i::BIGINT * 1000003)::BIGINT AS c_k64,
               (i::BIGINT * 104729 % 2147483647 - 1073741824)::BIGINT AS c_kn,
               (i * 31 % 1000)::INTEGER AS c_val
        FROM range({MANY_RG_ROWS}) r(i)""", f", ROW_GROUP_SIZE {RG}"),
    "e_bloom_big": (f"""
        SELECT i::INTEGER AS c_id, (1000000000000 + i::BIGINT * 1000003)::BIGINT AS c_k64,
               (i * 31 % 1000)::INTEGER AS c_val
        FROM range({BLOOM_BIG_ROWS}) r(i)""", f", ROW_GROUP_SIZE {RG}"),
    # Join keys that a join reads as INTEGER, as DuckDB reads compressed join keys: BIGINT c_kneg and
    # c_ksmall with the same values as INTEGER c_k32 and c_ks32, c_kbig. Distinct positive values in
    # [1, 10^9 + 7] (c_ksmall in [1, 250 000], a span the lookup table holds unfolded), except the
    # odd rows of row group 4, negative in c_kneg and c_ksmall, and of row group 7, above INT32_MAX in c_kbig.
    "e_tc": (f"""
        SELECT i::INTEGER AS c_id, k::BIGINT AS c_kneg, k::INTEGER AS c_k32,
               (CASE WHEN i // {RG} = 7 AND i % 2 = 1 THEN abs(k) + 3000000000 ELSE abs(k) END)::BIGINT AS c_kbig,
               s::BIGINT AS c_ksmall, s::INTEGER AS c_ks32, (i * 31 % 1000)::INTEGER AS c_val
        FROM (SELECT i, sg * (i::BIGINT * 104729 % 1000000007 + 1) AS k, sg * (i * 37 % 250000 + 1) AS s
              FROM (SELECT i, CASE WHEN i // {RG} = 4 AND i % 2 = 1 THEN -1 ELSE 1 END AS sg
                    FROM range({MANY_RG_ROWS}) r(i)))""", f", ROW_GROUP_SIZE {RG}"),
    # Lossy hardware forms: the prefix range bitmap of c_k23 over its span of 473 847 keys takes 7 404
    # words unfolded, more than a third of the lookup table, so three such bitmaps in one query do not
    # all fit. c_f is a filter-only column for a required conjunct.
    "e_lx": (f"""
        SELECT i::INTEGER AS c_id, (i * 23)::INTEGER AS c_k23,
               (i * 31 % 1000)::INTEGER AS c_val, (i % 10)::INTEGER AS c_f
        FROM range({MANY_RG_ROWS}) r(i)""", f", ROW_GROUP_SIZE {RG}"),
    # Hardware strings as their dictionary ids: low cardinality strings whose dictionary order changes
    # per row group (the first row of a group fixes it), empty and multi-byte strings, about 3 000
    # values (dictionaries of many words), c_gap with 'b' only in the odd row groups inside the [min,
    # max] of every group, so only the dictionary rules it out, and c_plain with NULLs (a CPU column).
    "e_dict": (f"""
        SELECT i::INTEGER AS c_id, (i * 31 % 1000)::INTEGER AS c_val,
               ['MAIL', 'SHIP', 'AIR', 'RAIL', 'TRUCK', 'FOB', 'REG AIR'][1 + (i + i // {RG}) % 7] AS c_mode,
               ['', 'üñî', 'naïve', 'x', 'Ω≈ç'][1 + (i * 3 + i // {RG}) % 5] AS c_small,
               'v' || lpad((i * 7919 % 3000)::VARCHAR, 4, '0') AS c_many,
               CASE WHEN (i // {RG}) % 2 = 0 THEN ['a', 'c'][1 + i % 2] ELSE ['a', 'b', 'c'][1 + i % 3] END AS c_gap,
               CASE WHEN i % 1000 = 0 THEN NULL ELSE 'u' || i END AS c_plain
        FROM range({MANY_RG_ROWS}) r(i)""", f", ROW_GROUP_SIZE {RG}, DICTIONARY_SIZE_LIMIT 4096"),
    # Dictionary cache: two files whose c_mode differs by a shift of every letter, which keeps the lengths,
    # the order and the repeats of the strings, so that the dictionary pages lie at the same offsets with
    # the same sizes but hold other strings (main checks the layout). The dictionary order changes per row
    # group.
    "e_dc_a": (f"""
        SELECT i::INTEGER AS c_id, ['MAIL', 'SHIP', 'AIR', 'RAIL'][1 + (i + i // {RG}) % 4] AS c_mode
        FROM range(4 * {RG}) r(i)""", f", ROW_GROUP_SIZE {RG}"),
    "e_dc_b": (f"""
        SELECT i::INTEGER AS c_id, ['NBJM', 'TIJQ', 'BJS', 'SBJM'][1 + (i + i // {RG}) % 4] AS c_mode
        FROM range(4 * {RG}) r(i)""", f", ROW_GROUP_SIZE {RG}"),
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


def same_dictionary_pages(data: Path, a: str, b: str, column: int) -> bool:
    """Whether the dictionary pages of the column lie at the same offsets with the same sizes in both files."""
    import pyarrow.parquet as pq

    def pages(name: str) -> List[Tuple[int, int]]:
        meta = pq.ParquetFile(data / f"{name}.parquet").metadata
        return [(meta.row_group(g).column(column).dictionary_page_offset,
                 meta.row_group(g).column(column).data_page_offset) for g in range(meta.num_row_groups)]

    return pages(a) == pages(b)


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
    # Hardware filter (oasis_hw_filter): the FilterStage drops the failing rows of the row group.
    "hwf_all_fail_group": "SELECT c_id, c_gap FROM {e_filter} WHERE c_gap BETWEEN 50 AND 150",
    "hwf_filter_only": "SELECT c_id FROM {e_filter} WHERE c_val < 10",
    "hwf_count": "SELECT count(*) AS n FROM {e_filter} WHERE c_val < 10",
    "hwf_count_all_fail": "SELECT count(*) AS n FROM {e_filter} WHERE c_gap BETWEEN 50 AND 150",
    "hwf_two_on_one": "SELECT c_id, c_val FROM {e_filter} WHERE c_val >= 100 AND c_val < 110",
    "hwf_neq": "SELECT c_id FROM {e_filter} WHERE c_val <> 500",
    "hwf_neq_and": "SELECT c_id FROM {e_filter} WHERE c_val <> 500 AND c_val > 100",
    "hwf_one_sided": "SELECT c_id, c_big FROM {e_filter} WHERE c_big > 0",
    "hwf_one_sided_lo": "SELECT c_id, c_big FROM {e_filter} WHERE c_big <= -4000000000",
    "hwf_date": "SELECT c_id, c_date FROM {e_filter} WHERE c_date >= DATE '2005-01-01' AND c_date < DATE '2005-02-01'",
    "hwf_decimal": "SELECT c_id, c_dec18 FROM {e_filter} WHERE c_dec9 BETWEEN 10.00 AND 20.50 AND c_dec18 < 5000",
    "hwf_mixed": "SELECT c_id, c_big, c_val FROM {e_filter} WHERE c_val < 500 AND c_big > 1000000000",
    "hwf_eq_first": f"SELECT c_id FROM {{e_filter}} WHERE c_id = {RG}",
    "hwf_eq_last": f"SELECT c_id FROM {{e_filter}} WHERE c_id = {MANY_RG_ROWS - 1}",
    "hwf_double_proj": "SELECT c_id, c_double FROM {e_filter} WHERE c_val < 10",
    "hwf_small_proj": "SELECT c_id, c_small FROM {e_filter} WHERE c_val < 10",
    "hwf_big_rg": "SELECT c_id FROM {e_big_rg} WHERE c_val < 5",
    # Bounds from a hash join: the build side filters a column that is not a key, so the optimizer
    # cannot derive the keys. One build key: an equality filter on the probe side.
    "hwf_join_one_key_i32": """SELECT a.c_id, a.c_val FROM {e_filter} a
                               JOIN (SELECT c_id FROM {e_filter} WHERE c_big = 2777023331) b ON a.c_id = b.c_id""",
    "hwf_join_one_key_i64": """SELECT a.c_id, a.c_big FROM {e_filter} a
                               JOIN (SELECT c_big FROM {e_filter} WHERE c_id = 7777) b ON a.c_big = b.c_big""",
    # Two keys (no prefix range or bloom filter): optional min/max on both, rows 4000 to 4200.
    "hwf_join_minmax": """SELECT a.c_id, a.c_big FROM {e_filter} a
                          JOIN (SELECT c_id, c_big FROM {e_filter} WHERE c_double BETWEEN 3000 AND 3150) b
                          ON a.c_id = b.c_id AND a.c_big = b.c_big""",
    # Keys from 100 up: the bounds keep 95 % of the first row group, which stays on the CPU.
    "hwf_join_minmax_wide": """SELECT a.c_id, a.c_big FROM {e_filter} a
                               JOIN (SELECT c_id, c_big FROM {e_filter} WHERE c_double >= 75) b
                               ON a.c_id = b.c_id AND a.c_big = b.c_big""",
    # A constant on a filter-only column, the optional bounds ride along.
    "hwf_join_minmax_const": """SELECT a.c_id, a.c_big FROM {e_filter} a
                                JOIN (SELECT c_id, c_big FROM {e_filter} WHERE c_double BETWEEN 3000 AND 3150) b
                                ON a.c_id = b.c_id AND a.c_big = b.c_big WHERE a.c_val < 500""",
    # A constant on a projected column: it is compared in place of the optional bounds.
    "hwf_join_required_wins": """SELECT a.c_id, a.c_val, a.c_big FROM {e_filter} a
                                 JOIN (SELECT c_id, c_big FROM {e_filter} WHERE c_double BETWEEN 3000 AND 3150) b
                                 ON a.c_id = b.c_id AND a.c_big = b.c_big WHERE a.c_val < 500""",
    # The bounds and a constant on the same key intersect.
    "hwf_join_and_const": """SELECT a.c_id, a.c_big FROM {e_filter} a
                             JOIN (SELECT c_id, c_big FROM {e_filter} WHERE c_double BETWEEN 3000 AND 3150) b
                             ON a.c_id = b.c_id AND a.c_big = b.c_big WHERE a.c_id >= 4100""",
    # A range join pushes a selectivity-optional bound (c_id <= 4200).
    "hwf_join_ineq": """SELECT a.c_id, count(*) AS n FROM {e_filter} a
                        JOIN (SELECT c_id FROM {e_filter} WHERE c_double BETWEEN 3000 AND 3150) b ON a.c_id < b.c_id
                        GROUP BY a.c_id""",
    # Prefix range bitmap of a hash join (one key, many build keys): the hardware looks the probe keys up.
    "hwf_prf_i32": """SELECT a.c_id, a.c_val FROM {e_prf} a
                      JOIN (SELECT c_k32 FROM {e_prf} WHERE c_val < 150 AND c_id < 6000) b ON a.c_k32 = b.c_k32""",
    "hwf_prf_i64": """SELECT a.c_id, a.c_val FROM {e_prf} a
                      JOIN (SELECT c_k64 FROM {e_prf} WHERE c_val < 150 AND c_id < 6000) b ON a.c_k64 = b.c_k64""",
    # The span is larger than a region: the bitmap folds 4 keys into a bit, the join drops the extra rows.
    "hwf_prf_fold": """SELECT a.c_id, a.c_kwide FROM {e_prf} a
                       JOIN (SELECT c_kwide FROM {e_prf} WHERE c_id % 5 = 0) b ON a.c_kwide = b.c_kwide""",
    # The keys fill the buckets of the folded bitmap, which then passes only the keys, fewer values than
    # the Bloom filter of the keys: the folded bitmap is looked up.
    "hwf_prf_cluster": """SELECT a.c_id, a.c_kwide FROM {e_prf} a
                          JOIN (SELECT c_kwide FROM {e_prf} WHERE c_id % 64 < 32) b ON a.c_kwide = b.c_kwide""",
    # Two bitmaps on two emitted keys: one is looked up, the other stays on the CPU.
    "hwf_prf_two": """SELECT a.c_id FROM {e_prf} a
                      JOIN (SELECT c_k32 FROM {e_prf} WHERE c_val < 300 AND c_id < 8000) b ON a.c_k32 = b.c_k32
                      JOIN (SELECT c_k64 FROM {e_prf} WHERE c_val >= 200 AND c_id < 8000) c ON a.c_k64 = c.c_k64""",
    # A constant on the key intersects with the [min, max] of the bitmap.
    "hwf_prf_and_const": """SELECT a.c_id, a.c_k32 FROM {e_prf} a
                            JOIN (SELECT c_k32 FROM {e_prf} WHERE c_val < 150 AND c_id < 6000) b ON a.c_k32 = b.c_k32
                            WHERE a.c_k32 >= 9000""",
    # A constant on a filter-only column, compared before the lookup of the emitted key.
    "hwf_prf_const_other": """SELECT a.c_id, a.c_k32 FROM {e_prf} a
                              JOIN (SELECT c_k32 FROM {e_prf} WHERE c_val < 150 AND c_id < 6000) b ON a.c_k32 = b.c_k32
                              WHERE a.c_val < 500""",
    # Every key of [min, max] is a build key: the interval alone.
    "hwf_prf_dense": """SELECT a.c_id, a.c_val FROM {e_prf} a
                        JOIN (SELECT c_id FROM {e_prf} WHERE c_double BETWEEN 1000 AND 1500) b ON a.c_id = b.c_id""",
    # IN lists of constants: DuckDB keeps the IN above the scan and pushes it as an optional filter; the
    # hardware looks the values up in a bitmap over their span.
    "hwf_in_i32": "SELECT c_id, c_val FROM {e_prf} WHERE c_val IN (3, 77, 150, 151, 500, 999)",
    "hwf_in_i64": """SELECT c_id, c_k64 FROM {e_prf}
                     WHERE c_k64 IN (1000000000000, 1000000000005, 1000000050000, 1000000100010, 1000000100011)""",
    "hwf_in_and_range": "SELECT c_id, c_val FROM {e_prf} WHERE c_val IN (3, 77, 150, 151, 500, 999) AND c_val > 100",
    # The span of the values does not fit a region: the bitmap folds, the IN above the scan drops the rest.
    "hwf_in_wide": "SELECT c_id FROM {e_prf} WHERE c_k64 IN (1000000000000, 1000000100000, 1001000000000, 1000000000005)",
    # Strings as their dictionary ids: the conjuncts are evaluated over the dictionary of each row
    # group, the ids that pass go to the hardware as an interval, the interval negated or a bitmap.
    # Bloom filters of the joins (stage 5): about 2 000 build keys, so DuckDB pushes the Bloom filter
    # with the min/max bounds; 60 000 keys in e_bloom_big, whose filter folds to half the table.
    "hwf_bf_i32": """SELECT a.c_id, a.c_val FROM {e_bloom} a
                     JOIN (SELECT c_k32 FROM {e_bloom} WHERE c_val < 100) b ON a.c_k32 = b.c_k32""",
    "hwf_bf_i64": """SELECT a.c_id, a.c_k64 FROM {e_bloom} a
                     JOIN (SELECT c_k64 FROM {e_bloom} WHERE c_val < 100) b ON a.c_k64 = b.c_k64""",
    "hwf_bf_kn": """SELECT a.c_id, a.c_val FROM {e_bloom} a
                    JOIN (SELECT c_kn FROM {e_bloom} WHERE c_val < 100) b ON a.c_kn = b.c_kn""",
    "hwf_bf_and_const": """SELECT a.c_id, a.c_k32 FROM {e_bloom} a
                           JOIN (SELECT c_k32 FROM {e_bloom} WHERE c_val < 100) b ON a.c_k32 = b.c_k32
                           WHERE a.c_k32 > 0""",
    "hwf_bf_const_other": """SELECT a.c_id, a.c_k32 FROM {e_bloom} a
                             JOIN (SELECT c_k32 FROM {e_bloom} WHERE c_val < 100) b ON a.c_k32 = b.c_k32
                             WHERE a.c_val < 500""",
    "hwf_bf_two": """SELECT a.c_id FROM {e_bloom} a
                     JOIN (SELECT c_k32 FROM {e_bloom} WHERE c_val < 200) b ON a.c_k32 = b.c_k32
                     JOIN (SELECT c_k64 FROM {e_bloom} WHERE c_val >= 100 AND c_val < 300) c ON a.c_k64 = c.c_k64""",
    "hwf_bf_fold": """SELECT a.c_id, a.c_val FROM {e_bloom_big} a
                      JOIN (SELECT c_k64 FROM {e_bloom_big} WHERE c_val < 460) b ON a.c_k64 = b.c_k64""",
    # A BIGINT probe key cast to the INTEGER build key: DuckDB pushes its filters on
    # TRY_CAST(column AS INTEGER), as for compressed join keys. The hardware takes them only in the row
    # groups whose values the cast keeps: the Bloom filter not in row group 4 (negative keys, which the
    # hardware would hash as 64 bit values) nor in row group 7 of c_kbig (above INT32_MAX, TRY_CAST
    # gives NULL); the prefix range bitmap of c_ksmall also in row group 4.
    "hwf_tc_bf": """SELECT a.c_id, a.c_kneg FROM {e_tc} a
                    JOIN (SELECT c_k32 FROM {e_tc} WHERE c_val < 100) b ON a.c_kneg::INTEGER = b.c_k32""",
    "hwf_tc_bf_big": """SELECT a.c_id, a.c_kbig FROM {e_tc} a
                        JOIN (SELECT c_k32 FROM {e_tc} WHERE c_val < 100) b ON TRY_CAST(a.c_kbig AS INTEGER) = b.c_k32""",
    "hwf_tc_prf": """SELECT a.c_id, a.c_ksmall FROM {e_tc} a
                     JOIN (SELECT c_ks32 FROM {e_tc} WHERE c_val < 100) b ON a.c_ksmall::INTEGER = b.c_ks32""",
    # Lossy hardware forms (edge_lossy): the hardware passes a superset of the join filter, so the CPU
    # checks the rows of the emitted key again. A folded prefix range bitmap (stages 3 and 4; the Bloom
    # filter of its keys at stage 5), a Bloom filter of a join folded to half the table, and three
    # unfolded bitmaps of which one folds or finds no room in the table and goes as its interval alone
    # (a required conjunct on c_f takes the group). An unfolded bitmap is exact and keeps no CPU check.
    # The interval alone of a dense prefix range filter keeps 76 % of row group 1, more than DuckDB's
    # threshold of 0.5 for a prefix range filter: that group stays on the CPU, row group 0 does not.
    "hwf_lx_fold": """SELECT a.c_id, a.c_kwide FROM {e_prf} a
                      JOIN (SELECT c_kwide FROM {e_prf} WHERE c_id % 7 = 0) b ON a.c_kwide = b.c_kwide""",
    "hwf_lx_bloom": """SELECT a.c_id, a.c_k64 FROM {e_bloom_big} a
                       JOIN (SELECT c_k64 FROM {e_bloom_big} WHERE c_val < 470) b ON a.c_k64 = b.c_k64""",
    "hwf_lx_noregion": """SELECT 1 AS q, a.c_id, a.c_k23 FROM {e_lx} a
                          JOIN (SELECT c_k23 FROM {e_lx} WHERE c_val < 150) b ON a.c_k23 = b.c_k23 WHERE a.c_f < 9
                          UNION ALL SELECT 2, a.c_id, a.c_k23 FROM {e_lx} a
                          JOIN (SELECT c_k23 FROM {e_lx} WHERE c_val >= 850) b ON a.c_k23 = b.c_k23 WHERE a.c_f < 9
                          UNION ALL SELECT 3, a.c_id, a.c_k23 FROM {e_lx} a
                          JOIN (SELECT c_k23 FROM {e_lx} WHERE c_val >= 400 AND c_val < 550) b ON a.c_k23 = b.c_k23
                          WHERE a.c_f < 9""",
    "hwf_lx_exact": """SELECT a.c_id, a.c_k23 FROM {e_lx} a
                       JOIN (SELECT c_k23 FROM {e_lx} WHERE c_val < 150) b ON a.c_k23 = b.c_k23""",
    "hwf_lx_gate": """SELECT a.c_id, a.c_val FROM {e_prf} a
                      JOIN (SELECT c_id FROM {e_prf} WHERE c_double BETWEEN 1000 AND 1800) b ON a.c_id = b.c_id""",
    # The lookup table fills (edge_table): the Bloom filter of each join on e_bloom_big folds to half the
    # table, and a statement holds its regions until it ends unless oasis_filter_table_early_release
    # returns them when their scan is done. Three such joins in one statement, then two next to a prefix
    # range bitmap, an IN list and the bitmap of a dictionary id column, which then fold more, go as their
    # interval alone or stay on the CPU.
    "hwf_tb_bloom3": """SELECT 1 AS q, a.c_id FROM {e_bloom_big} a
                        JOIN (SELECT c_k64 FROM {e_bloom_big} WHERE c_val < 460) b ON a.c_k64 = b.c_k64
                        UNION ALL SELECT 2, a.c_id FROM {e_bloom_big} a
                        JOIN (SELECT c_k64 FROM {e_bloom_big} WHERE c_val >= 500 AND c_val < 960) b ON a.c_k64 = b.c_k64
                        UNION ALL SELECT 3, a.c_id FROM {e_bloom_big} a
                        JOIN (SELECT c_k64 FROM {e_bloom_big} WHERE c_val >= 40 AND c_val < 500) b ON a.c_k64 = b.c_k64""",
    "hwf_tb_mixed": """SELECT 1 AS q, a.c_id FROM {e_bloom_big} a
                       JOIN (SELECT c_k64 FROM {e_bloom_big} WHERE c_val < 460) b ON a.c_k64 = b.c_k64
                       UNION ALL SELECT 2, a.c_id FROM {e_bloom_big} a
                       JOIN (SELECT c_k64 FROM {e_bloom_big} WHERE c_val >= 500 AND c_val < 960) b ON a.c_k64 = b.c_k64
                       UNION ALL SELECT 3, a.c_id FROM {e_prf} a
                       JOIN (SELECT c_k32 FROM {e_prf} WHERE c_val < 150 AND c_id < 6000) b ON a.c_k32 = b.c_k32
                       UNION ALL SELECT 4, c_id FROM {e_prf} WHERE c_val IN (3, 77, 150, 151, 500, 999)
                       UNION ALL SELECT 5, c_id FROM {e_dict} WHERE c_many LIKE '%3'""",
    "hws_eq": "SELECT c_id, c_mode FROM {e_dict} WHERE c_mode = 'SHIP'",
    "hws_ne": "SELECT c_id, c_mode FROM {e_dict} WHERE c_mode <> 'SHIP'",
    "hws_range": "SELECT c_id, c_mode FROM {e_dict} WHERE c_mode < 'RAIL'",
    "hws_in": "SELECT c_id, c_mode FROM {e_dict} WHERE c_mode IN ('MAIL', 'SHIP', 'FOB')",
    "hws_like": "SELECT c_id, c_many FROM {e_dict} WHERE c_many LIKE 'v00%'",
    "hws_not_like": "SELECT c_id, c_mode FROM {e_dict} WHERE c_mode NOT LIKE '%AIR'",
    "hws_bitmap": "SELECT c_id, c_many FROM {e_dict} WHERE c_many LIKE '%3'",
    "hws_unicode": "SELECT c_id, c_small FROM {e_dict} WHERE c_small IN ('', 'üñî')",
    # No entry passes in the even row groups, every entry passes, no filter.
    "hws_skip": "SELECT c_id, c_gap FROM {e_dict} WHERE c_gap = 'b'",
    "hws_all": "SELECT c_id, c_mode FROM {e_dict} WHERE c_mode <> 'NONE'",
    "hws_proj": "SELECT c_id, c_mode, c_small, c_many, c_gap FROM {e_dict}",
    "hws_count": "SELECT count(*) AS n FROM {e_dict} WHERE c_many LIKE '%7%'",
    # A dictionary id column next to a constant on an INTEGER column, as filter, as projection, both.
    "hws_with_int": "SELECT c_id, c_mode FROM {e_dict} WHERE c_val < 300",
    "hws_and_int": "SELECT c_id FROM {e_dict} WHERE c_mode IN ('AIR', 'TRUCK') AND c_val < 500",
    "hws_emit_both": "SELECT c_id, c_val, c_mode FROM {e_dict} WHERE c_mode IN ('AIR', 'TRUCK') AND c_val < 500",
    # Two dictionary id columns, and two conjuncts on one.
    "hws_two": "SELECT c_id, c_small FROM {e_dict} WHERE c_mode IN ('AIR', 'RAIL') AND c_small = 'naïve'",
    "hws_same_col": "SELECT c_id, c_mode FROM {e_dict} WHERE c_mode >= 'B' AND c_mode <> 'SHIP'",
    # The keys of a join on a string column.
    "hws_join": """SELECT a.c_id, a.c_mode FROM {e_dict} a
                   JOIN (SELECT DISTINCT c_mode FROM {e_dict} WHERE c_id < 2) b ON a.c_mode = b.c_mode""",
    # Downstream of the scan: the dictionary vectors in a GROUP BY.
    "hws_group": "SELECT c_mode, count(*) AS n, sum(c_val) AS s FROM {e_dict} WHERE c_val > 100 GROUP BY c_mode",
    # A CPU string column in the scan: the hardware filter is off, the CPU looks the dictionary ids up.
    "hws_plain_next": "SELECT c_id, c_mode, c_plain FROM {e_dict} WHERE c_mode = 'AIR'",
    # The dictionary pages and their cache (oasis_dict_cache): two scans of one dictionary id column in a
    # query, with a conjunct (its pages come before the row group is submitted) and without (with the row
    # group); then two files whose pages lie at the same offsets with the same sizes, with and without a
    # conjunct, whose strings must not mix in the cache.
    "hws_dc_self": """SELECT a.c_id, a.c_mode, b.c_mode AS b_mode FROM {e_dict} a
                      JOIN {e_dict} b ON a.c_id = b.c_id + 1 WHERE b.c_mode = 'AIR'""",
    "hws_dc_union": """SELECT 1 AS q, c_id, c_mode FROM {e_dict} WHERE c_mode IN ('RAIL', 'FOB')
                       UNION ALL SELECT 2, c_id, c_mode FROM {e_dict} WHERE c_val < 100""",
    "hws_dc_files_filter": """SELECT 'a' AS f, c_id, c_mode FROM {e_dc_a} WHERE c_mode >= 'S'
                              UNION ALL SELECT 'b', c_id, c_mode FROM {e_dc_b} WHERE c_mode >= 'S'""",
    "hws_dc_files": """SELECT 'a' AS f, c_id, c_mode FROM {e_dc_a}
                       UNION ALL SELECT 'b', c_id, c_mode FROM {e_dc_b}""",
}

# The cases of edge_stages, run at every oasis_hw_filter_stage: one or two per stage of the hardware
# filter and every hws_* case but the hws_dc_* ones of edge_dict_cache.
STAGE_CASES = ["hwf_mixed", "hwf_eq_first", "hwf_join_one_key_i32", "hwf_join_minmax", "hwf_join_ineq",
               "hwf_prf_i32", "hwf_prf_fold", "hwf_in_i32", "hwf_in_wide", "hwf_bf_i32",
               "hwf_bf_i64"] + [c for c in CASES if c.startswith("hws_") and not c.startswith("hws_dc_")]
STAGES = range(6)

# The hardware filter messages of the log (duckdb_logs at DEBUG) and the first stage that may log them: no
# stage uses a feature of a later one. A join bound comes from stage 2 on, a bitmap from stage 3, a
# dictionary id column from stage 4 and a Bloom filter from stage 5; the last stage must log both.
DICT_IDS_MESSAGE = "message LIKE '%as its dictionary ids%'"
BLOOM_MESSAGE = "message LIKE 'Hardware filter on column%bloom%'"
STAGE_LOG_CHECKS = {
    "no_hw": ("message LIKE 'Row group %: column%'", 1),
    "no_join": ("message LIKE 'Hardware filter on column%: join%'", 2),
    "no_bitmap": ("message LIKE 'Hardware filter on column%bitmap%'", 3),
    "no_dict_ids": (DICT_IDS_MESSAGE, 4),
    "no_bloom": (BLOOM_MESSAGE, 5),
}

# The cases of edge_lossy: the stages each runs at, and whether the CPU checks the rows of the join filter
# again after the hardware (the hardware passes a superset) and keeps a row group whose estimated share of
# passing rows is above DuckDB's threshold of the filter on the CPU. Every case also has to be filtered in
# hardware. The table holds its regions until the statement ends (oasis_filter_table_early_release off),
# so the three bitmaps of hwf_lx_noregion do not all fit.
# A case without the gate check may log the gate message too.
LOSSY_CASES = {
    "hwf_lx_fold": ([3, 5], True, False),
    "hwf_lx_bloom": ([5], True, False),
    "hwf_lx_noregion": ([3, 5], True, False),
    "hwf_lx_exact": ([3, 5], False, False),
    "hwf_lx_gate": ([3, 5], False, True),
}
LOSSY_HW_MESSAGE = "message LIKE 'Row group %: column%Filter(%'"
LOSSY_RECHECK_MESSAGE = "message LIKE 'Row group %: column%the hardware passes a superset'"
LOSSY_GATE_MESSAGE = "message LIKE 'Row group 1 on the CPU filter: the join bounds keep%'"

# The cases of edge_table, run with oasis_filter_table_early_release off and on, and the lookup table
# messages of the log: a region taken, the counters of a scan when it is done, the regions of a scan
# returned early. A region of half the table is the folded Bloom filter of e_bloom_big.
TABLE_CASES = [c for c in CASES if c.startswith("hwf_tb_")]
TABLE_REGION_MESSAGE = "message LIKE 'Lookup table: region of%'"
TABLE_HALF_MESSAGE = "message LIKE 'Lookup table: region of 8192 words%'"
TABLE_SUMMARY_MESSAGE = "message LIKE 'Lookup table of the scan:%'"
TABLE_RELEASE_MESSAGE = "message LIKE 'Lookup table: released%'"

# The cases of edge_dict_cache, run with oasis_dict_cache off, off with oasis_prefetch_depth = 0 (the worker
# waits for the dictionary pages of its head group itself) and on, and the dictionary messages of the log:
# the pages a conjunct needs come before the row group is submitted, the others with the row group, and
# only with the cache on do later scans take dictionaries from the cache.
DICT_CACHE_CASES = [c for c in CASES if c.startswith("hws_dc_")]
DICT_BEFORE_MESSAGE = "message LIKE 'Row group %dictionary page(s) fetched before the row group is submitted.'"
DICT_WITH_MESSAGE = "message LIKE 'Row group %dictionary page(s) fetched with the row group.'"
DICT_CACHED_MESSAGE = "message LIKE 'Row group %: the dictionary of column%is in the cache'"

# The cases of edge_prof: the INTEGER column c_id without a filter, then with a filter that 1 % of the
# rows pass. The post_filter_bytes of oasis_stream_profile() count from the start of the shell, so the
# scenario reads the profile before and after each case.
PROF_CASES = {
    "hwf_prof_full": "SELECT c_id FROM {e_filter}",
    "hwf_prof_filtered": "SELECT c_id FROM {e_filter} WHERE c_val < 10",
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
# The decoders wrote to host memory (hw_bytes, the software count of the scheduler).
FOOTER = ("SELECT coalesce(sum(in_handshakes_cycles), 0) > 0 AS hw_used,"
          " coalesce(sum(post_filter_bytes) FILTER (WHERE in_handshakes_cycles IS NOT NULL), 0) > 0 AS hw_bytes"
          " FROM oasis_stream_profile();\n"
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


def stages_sql(cases: Dict[str, str], files: List[str]) -> Tuple[str, List[str]]:
    """The cases at every stage of the hardware filter, with the log checks of each stage, and the names
    of the result columns that must be true."""
    hw = {f: hw_source(f) for f in files}
    sw = {f: sw_source(f) for f in files}
    lines = [HEADER, "SET enable_logging = true;\nSET logging_level = 'DEBUG';\nSET logging_storage = 'memory';\n"]
    for name, query in cases.items():
        lines.append(f"CREATE TEMP TABLE {name}_sw AS {query.format_map(sw)};\n")
    true_keys = []
    for stage in STAGES:
        lines.append(f"SET oasis_hw_filter_stage = {stage};\nCALL truncate_duckdb_logs();\n")
        for name, query in cases.items():
            lines.append(f"CREATE TEMP TABLE {name}_hw{stage} AS {query.format_map(hw)};\n")
            lines.append(f"SELECT {diff_sql(f'{name}_hw{stage}', name + '_sw')} = 0 AS same_{name}_s{stage};\n")
            true_keys.append(f"same_{name}_s{stage}")
        for key, (condition, first_stage) in STAGE_LOG_CHECKS.items():
            if stage < first_stage:
                lines.append(f"SELECT count(*) = 0 AS {key}_s{stage} FROM duckdb_logs WHERE {condition};\n")
                true_keys.append(f"{key}_s{stage}")
        if stage == max(STAGES):
            lines.append(f"SELECT count(*) > 0 AS dict_ids_s{stage} FROM duckdb_logs WHERE {DICT_IDS_MESSAGE};\n")
            lines.append(f"SELECT count(*) > 0 AS bloom_s{stage} FROM duckdb_logs WHERE {BLOOM_MESSAGE};\n")
            true_keys += [f"dict_ids_s{stage}", f"bloom_s{stage}"]
    return "".join(lines) + FOOTER, true_keys


def table_sql(cases: Dict[str, str], files: List[str]) -> Tuple[str, List[str]]:
    """The cases with the early release of the lookup table off and on, each with its log checks, and the
    names of the result columns that must be true. Off, a statement holds at most two regions of half the
    table and returns none early; on, the scans return theirs."""
    hw = {f: hw_source(f) for f in files}
    sw = {f: sw_source(f) for f in files}
    lines = [HEADER, "SET enable_logging = true;\nSET logging_level = 'DEBUG';\nSET logging_storage = 'memory';\n"]
    for name, query in cases.items():
        lines.append(f"CREATE TEMP TABLE {name}_sw AS {query.format_map(sw)};\n")
    true_keys = []
    for release, tag in ((False, "off"), (True, "on")):
        lines.append(f"SET oasis_filter_table_early_release = {str(release).lower()};\n")
        for name, query in cases.items():
            key = f"{name}_{tag}"
            lines.append("CALL truncate_duckdb_logs();\n")
            lines.append(f"CREATE TEMP TABLE {name}_hw_{tag} AS {query.format_map(hw)};\n")
            lines.append(f"SELECT {diff_sql(f'{name}_hw_{tag}', name + '_sw')} = 0 AS same_{key};\n")
            lines.append(f"SELECT count(*) > 0 AS regions_{key} FROM duckdb_logs WHERE {TABLE_REGION_MESSAGE};\n")
            lines.append(f"SELECT count(*) > 0 AS summary_{key} FROM duckdb_logs WHERE {TABLE_SUMMARY_MESSAGE};\n")
            true_keys += [f"same_{key}", f"regions_{key}", f"summary_{key}"]
            if release:
                lines.append(f"SELECT count(*) > 0 AS released_{key} FROM duckdb_logs WHERE {TABLE_RELEASE_MESSAGE};\n")
                true_keys.append(f"released_{key}")
            else:
                lines.append(f"SELECT count(*) = 0 AS kept_{key} FROM duckdb_logs WHERE {TABLE_RELEASE_MESSAGE};\n")
                lines.append(f"SELECT count(*) <= 2 AS full_{key} FROM duckdb_logs WHERE {TABLE_HALF_MESSAGE};\n")
                true_keys += [f"kept_{key}", f"full_{key}"]
    return "".join(lines) + FOOTER, true_keys


def dict_cache_sql(cases: Dict[str, str], files: List[str]) -> Tuple[str, List[str]]:
    """The cases with the dictionary cache off, off with a synchronous scan and on, each round with its log
    checks, and the names of the result columns that must be true. Off, every page is fetched: before the
    row group is submitted for a conjunct, else with it. On, the scans after the first of a file take the
    dictionaries from the cache (which pages they fetch first depends on the order of the scans)."""
    hw = {f: hw_source(f) for f in files}
    sw = {f: sw_source(f) for f in files}
    lines = [HEADER, "SET enable_logging = true;\nSET logging_level = 'DEBUG';\nSET logging_storage = 'memory';\n"]
    for name, query in cases.items():
        lines.append(f"CREATE TEMP TABLE {name}_sw AS {query.format_map(sw)};\n")
    true_keys = []
    for tag, cache, synchronous in (("off", False, False), ("sync", False, True), ("on", True, False)):
        lines.append(f"SET oasis_dict_cache = {str(cache).lower()};\n")
        lines.append("SET oasis_prefetch_depth = 0;\n" if synchronous else "RESET oasis_prefetch_depth;\n")
        lines.append("CALL truncate_duckdb_logs();\n")
        for name, query in cases.items():
            key = f"{name}_{tag}"
            lines.append(f"CREATE TEMP TABLE {name}_hw_{tag} AS {query.format_map(hw)};\n")
            lines.append(f"SELECT {diff_sql(f'{name}_hw_{tag}', name + '_sw')} = 0 AS same_{key};\n")
            true_keys.append(f"same_{key}")
        if cache:
            lines.append(f"SELECT count(*) > 0 AS cached_{tag} FROM duckdb_logs WHERE {DICT_CACHED_MESSAGE};\n")
            true_keys.append(f"cached_{tag}")
        else:
            lines.append(f"SELECT count(*) > 0 AS before_{tag} FROM duckdb_logs WHERE {DICT_BEFORE_MESSAGE};\n")
            lines.append(f"SELECT count(*) > 0 AS with_{tag} FROM duckdb_logs WHERE {DICT_WITH_MESSAGE};\n")
            lines.append(f"SELECT count(*) = 0 AS uncached_{tag} FROM duckdb_logs WHERE {DICT_CACHED_MESSAGE};\n")
            true_keys += [f"before_{tag}", f"with_{tag}", f"uncached_{tag}"]
    return "".join(lines) + FOOTER, true_keys


def lossy_sql(cases: Dict[str, str], files: List[str]) -> Tuple[str, List[str]]:
    """The cases of LOSSY_CASES at their stages, each with its log checks, and the names of the result
    columns that must be true."""
    hw = {f: hw_source(f) for f in files}
    sw = {f: sw_source(f) for f in files}
    lines = [HEADER, "SET enable_logging = true;\nSET logging_level = 'DEBUG';\nSET logging_storage = 'memory';\n"]
    for name, query in cases.items():
        lines.append(f"CREATE TEMP TABLE {name}_sw AS {query.format_map(sw)};\n")
    true_keys = []
    for name, query in cases.items():
        stages, recheck, gate = LOSSY_CASES[name]
        for stage in stages:
            key = f"{name}_s{stage}"
            lines.append(f"SET oasis_hw_filter_stage = {stage};\nCALL truncate_duckdb_logs();\n")
            lines.append(f"CREATE TEMP TABLE {name}_hw{stage} AS {query.format_map(hw)};\n")
            lines.append(f"SELECT {diff_sql(f'{name}_hw{stage}', name + '_sw')} = 0 AS same_{key};\n")
            lines.append(f"SELECT count(*) > 0 AS hw_{key} FROM duckdb_logs WHERE {LOSSY_HW_MESSAGE};\n")
            lines.append(f"SELECT count(*) {'>' if recheck else '='} 0 AS recheck_{key} FROM duckdb_logs"
                         f" WHERE {LOSSY_RECHECK_MESSAGE};\n")
            true_keys += [f"same_{key}", f"hw_{key}", f"recheck_{key}"]
            if gate:
                lines.append(f"SELECT count(*) > 0 AS gate_{key} FROM duckdb_logs WHERE {LOSSY_GATE_MESSAGE};\n")
                true_keys.append(f"gate_{key}")
    lines.append(f"SET oasis_hw_filter_stage = {max(STAGES)};\n")
    return "".join(lines) + FOOTER, true_keys


def prof_sql(files: List[str]) -> str:
    """The cases of PROF_CASES, each between two reads of oasis_stream_profile(), with the differing rows
    and the bytes the decoders wrote of each case (<case>_bytes), and the checks of edge_prof."""
    hw = {f: hw_source(f) for f in files}
    sw = {f: sw_source(f) for f in files}
    profile = ("SELECT stream, post_filter_bytes, no_buffer_ns, in_handshakes_cycles IS NOT NULL AS decoder"
               " FROM oasis_stream_profile()")
    lines = [HEADER, f"CREATE TEMP TABLE hwf_prof_p0 AS {profile};\n"]
    for k, (name, query) in enumerate(PROF_CASES.items()):
        lines.append(f"CREATE TEMP TABLE {name}_hw AS {query.format_map(hw)};\n")
        lines.append(f"CREATE TEMP TABLE hwf_prof_p{k + 1} AS {profile};\n")
    bytes_of = {}
    for k, (name, query) in enumerate(PROF_CASES.items()):
        bytes_of[name] = (f"(SELECT sum(b.post_filter_bytes - a.post_filter_bytes) FROM hwf_prof_p{k} a"
                          f" JOIN hwf_prof_p{k + 1} b USING (stream) WHERE a.decoder)")
        lines.append(f"CREATE TEMP TABLE {name}_sw AS {query.format_map(sw)};\n")
        lines.append(f"SELECT {diff_sql(name + '_hw', name + '_sw')} AS diff_{name}, {bytes_of[name]} AS {name}_bytes;\n")
    # 4 bytes per INTEGER value without a filter, under a tenth of that with the filter, and counters that
    # never go back.
    lines.append("SELECT 4 * count(*) AS hwf_prof_full_expect FROM hwf_prof_full_hw;\n")
    lines.append(f"SELECT 10 * {bytes_of['hwf_prof_filtered']} < {bytes_of['hwf_prof_full']} AS hwf_prof_filtered_less;\n")
    lines.append("SELECT bool_and(b.post_filter_bytes >= a.post_filter_bytes AND b.no_buffer_ns >= a.no_buffer_ns)"
                 f" AS hwf_prof_monotonic FROM hwf_prof_p0 a JOIN hwf_prof_p{len(PROF_CASES)} b USING (stream);\n")
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
    # Else hws_dc_files only checks the strings of two files, not the file in the keys of the cache
    if not same_dictionary_pages(data, "e_dc_a", "e_dc_b", 1):
        print("warning: the dictionary pages of e_dc_a and e_dc_b lie at different offsets", file=sys.stderr)

    # Pack the RDMA image
    parquet = sorted(data.glob("*.parquet"))
    for f in parquet:
        print(f"{f.name}: {f.stat().st_size} B")
    subprocess.run([sys.executable, str(PACK), str(out / "image.bin"), *map(str, parquet)],
                   check=True, stdout=subprocess.DEVNULL)

    names = []
    done = {"expect": {"hw_used": True, "hw_bytes": True, "done": 1}}

    # One per TPC-H query
    for nr, query in con.sql("SELECT query_nr, query FROM tpch_queries() ORDER BY query_nr").fetchall():
        sql, tables = tpch_sql(query)
        names.append(write_scenario(out, f"tpch_q{nr:02d}", sql, tables, [{"expect": {"diff_rows": 0}}, done]))

    # All edge cases together
    efiles = files_of(" ".join(CASES.values()))
    names.append(write_scenario(out, "edge", cases_sql(CASES, efiles), efiles,
                                [{"expect": {f"diff_{c}": 0 for c in CASES}}, done]))

    # The ablation of the stages of the hardware filter, by the setting alone
    stage_cases = {c: CASES[c] for c in STAGE_CASES}
    sfiles = files_of(" ".join(stage_cases.values()))
    sql, true_keys = stages_sql(stage_cases, sfiles)
    names.append(write_scenario(out, "edge_stages", sql, sfiles, [{"expect": {k: True for k in true_keys}}, done]))

    # The lookup table filled, with the early release of its regions off and on
    table_cases = {c: CASES[c] for c in TABLE_CASES}
    tfiles = files_of(" ".join(table_cases.values()))
    sql, true_keys = table_sql(table_cases, tfiles)
    names.append(write_scenario(out, "edge_table", sql, tfiles, [{"expect": {k: True for k in true_keys}}, done]))

    # The dictionary pages of the dictionary id columns, with their cache off, off synchronously and on
    dict_cache_cases = {c: CASES[c] for c in DICT_CACHE_CASES}
    dfiles = files_of(" ".join(dict_cache_cases.values()))
    sql, true_keys = dict_cache_sql(dict_cache_cases, dfiles)
    names.append(write_scenario(out, "edge_dict_cache", sql, dfiles,
                                [{"expect": {k: True for k in true_keys}}, done]))

    # The lossy hardware forms of the join filters, with the CPU check after the hardware
    lossy_cases = {c: CASES[c] for c in LOSSY_CASES}
    lfiles = files_of(" ".join(lossy_cases.values()))
    sql, true_keys = lossy_sql(lossy_cases, lfiles)
    names.append(write_scenario(out, "edge_lossy", sql, lfiles, [{"expect": {k: True for k in true_keys}}, done]))

    # The decoder bytes of the software counters of oasis_stream_profile()
    pfiles = files_of(" ".join(PROF_CASES.values()))
    names.append(write_scenario(out, "edge_prof", prof_sql(pfiles), pfiles,
                                [{"equal": ["hwf_prof_full_bytes", "hwf_prof_full_expect"]},
                                 {"expect": {"diff_hwf_prof_full": 0, "diff_hwf_prof_filtered": 0,
                                             "hwf_prof_filtered_less": True, "hwf_prof_monotonic": True}},
                                 done]))

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
