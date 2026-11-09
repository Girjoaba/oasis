#!/usr/bin/env python3
"""Summarises a run folder of bench.sh (or sim_check.sh): oasis against the baselines of the same run.

    python3 analyze.py <run folder>  [--reference native_rdma] [--out DIR]

Reads every cell folder <dataset>_<mode>_t<threads>_<tp|lat|log>_r<rep>/ (the output of bench) and writes
summary.md and summary.json into the run folder (or --out). A value over the repetitions is their median, with the
min and max beside it.

1. Correctness: the rows of every query (results/qNN.tsv) against the reference mode on the same dataset
   and repetition, numbers with a relative tolerance of 1e-6; errors of any query. For the queries in
   loose_<dataset>.json (gen_data.py queries: a LIMIT over ties) only the row count.
2. Throughput per dataset and thread count: queries per hour, CPU seconds per query, the decoder
   counters (handshake, starved, stalled and idle cycles apart), the bytes after the FilterStage and the
   time without a host buffer (oasis_stream_profile post_filter_bytes, no_buffer_ns, when present); the
   ratios oasis / oasis_nohw and oasis / native; narrow transfer against wide (modes <mode>_narrow).
3. Ablation per dataset: queries per hour of each stage of the hardware filter (oasis_nohw = no stage,
   oasis_s<k>, oasis = every stage), wide and narrow.
4. Latency per dataset and thread count (TPC-H): per query the seconds of each mode and the DuckDB profile
   (scan operator time, Filter Time, HW Filter Rows In and Out); then the seconds of each stage; the
   other "HW ..." metrics of the scans (lookup table regions), when present.
5. Hardware filter log (cells <dataset>_<mode>_t<T>_log_r0, bench --hw-log): the messages per query, a
   row group number as N, with their count.

Mode variants: the suffix _narrow (oasis_narrow_transfer) and _pf<N> (oasis_prefetch_depth) of a mode go
with that mode in the tables. The exit code is 1 when a query failed or a result differs, else 0.
Standard library only.
"""

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

CELL = re.compile(r"^(?P<ds>tpch_sf[0-9.]+|clickbench_sample|clickbench)_(?P<mode>[a-z0-9_]+?)"
                  r"_t(?P<threads>\d+)_(?P<kind>tp|lat|log)_r(?P<rep>\d+)$")
CYCLE_S = 4e-9  # 250 MHz
MAIN_MODES = ["oasis", "oasis_nohw", "native_rdma", "native_local"]
# The ladder of the ablation: no stage, the stages, every stage
LADDER = ["oasis_nohw"] + [f"oasis_s{k}" for k in range(1, 5)] + ["oasis"]
# Named by what each stage set adds: constant comparisons, join bounds, bitmap lookup, dictionary ids, Bloom filters
LADDER_NAME = {"oasis_nohw": "none (oasis_nohw)", "oasis_s1": "constants", "oasis_s2": "+ join bounds",
               "oasis_s3": "+ bitmaps", "oasis_s4": "+ dict ids", "oasis": "+ Bloom (oasis)"}
VARIANT = re.compile(r"_narrow|_pf\d+")
# Metrics of the scans in the DuckDB profile besides the ones of profile_facts (any extra info "HW ..." or
# "... Region ..."): the lookup table (HW Table Requests, Failures, Interval Fallbacks, CPU Fallbacks,
# Words, Peak Words), HW Bytes Out, HW Dictionary Pages Fetched and Cached; a "Peak" or "Max" one is a maximum
KNOWN_HW = {"HW Filter Rows In", "HW Filter Rows Out", "HW Filter Groups", "HW Filter Stage", "HW Dictionary Id Columns"}
HW_METRIC = re.compile(r"^HW |Region|Table Words|Table Peak")


def base_mode(mode: str) -> str:
    """The mode without its variant suffixes: oasis_nohw_narrow -> oasis_nohw"""
    return VARIANT.sub("", mode)


def narrow_of(mode: str) -> str:
    """The narrow variant of a mode, before its _pf<N>: oasis_pf4 -> oasis_narrow_pf4"""
    b = re.sub(r"_pf\d+$", "", mode)
    return b + "_narrow" + mode[len(b):]


def mode_order(modes) -> list:
    """The modes in the order of MAIN_MODES and LADDER, each variant after its base mode"""
    order = list(dict.fromkeys(MAIN_MODES + LADDER))
    rank = lambda m: (order.index(base_mode(m)) if base_mode(m) in order else len(order), base_mode(m),
                      "_narrow" in m, m)
    return sorted(modes, key=rank)


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                rows.append({"parse_error": str(e)})
    return rows


def load_cells(run: Path) -> list:
    cells = []
    for d in sorted(p for p in run.iterdir() if p.is_dir()):
        m = CELL.match(d.name)
        if not m:
            continue
        cells.append({"dir": d, "name": d.name, "ds": m["ds"], "mode": m["mode"], "threads": int(m["threads"]),
                      "kind": m["kind"], "rep": int(m["rep"]),
                      "throughput": read_jsonl(d / "throughput.jsonl"), "latency": read_jsonl(d / "latency.jsonl")})
    return cells


def stats(values: list) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"median": None, "min": None, "max": None, "n": 0}
    return {"median": statistics.median(values), "min": min(values), "max": max(values), "n": len(values)}


# --- decoder counters --------------------------------------------------------------------------------

SOFT_COUNTERS = ("post_filter_bytes", "no_buffer_ns")


def soft_delta(s: dict, b: dict, key: str):
    """A counter of the host software over a window: after - before when it only grows, else the after
    value (a counter that restarts at each read); None without the column (runs before 2026-11-06)"""
    if s.get(key) is None:
        return None
    if b and b.get(key) is not None and s[key] >= b[key]:
        return s[key] - b[key]
    return s[key]


def stream_sums(profile, before=None) -> dict:
    """Sums of the decoder slots (in-side counters present) and of the bypass slot. A slot whose cycle
    counters read the same before and after the run saw no data beat in it and still holds an earlier
    window (native_rdma). The output cycles of a decoder split into handshake (a beat out), starved (the
    decoder has no beat), stalled (the FilterStage and the host do not take it) and idle (no column chunk).
    bypass_bytes is None when the bitstream has no bypass slot, post_filter_bytes and no_buffer_ns when the
    software has no such column."""
    out = {"dec_in_bytes": 0, "dec_out_bytes": 0, "dec_busy": 0, "dec_total": 0, "dec_handshake": 0,
           "dec_starved": 0, "dec_stalled": 0, "dec_idle": 0, "idle_each": [], "bypass_bytes": None, "decoders": 0,
           "post_filter_bytes": None, "no_buffer_ns": None, "bypass_post_filter_bytes": None}
    if not isinstance(profile, list):
        return out
    cycles = lambda s: tuple(v for k, v in sorted(s.items()) if k.endswith("_cycles") or k.endswith("_handshakes"))
    held = {b.get("stream"): b for b in (before if isinstance(before, list) else []) if isinstance(b, dict)}

    def add(key, v):
        if v is not None:
            out[key] = (out[key] or 0) + v

    for s in profile:
        if not isinstance(s, dict):
            continue
        b = held.get(s.get("stream"))
        if s.get("in_handshakes_cycles") is not None:
            if b and cycles(b) == cycles(s):
                continue
            out["decoders"] += 1
            out["dec_in_bytes"] += 64 * s["in_handshakes_cycles"]
            out["dec_out_bytes"] += 64 * s["out_handshakes_cycles"]
            busy = s["out_handshakes_cycles"] + s["out_starved_cycles"] + s["out_stalled_cycles"]
            out["dec_busy"] += busy
            out["dec_total"] += busy + s["out_idle_cycles"]
            out["idle_each"].append(s["out_idle_cycles"] / (busy + s["out_idle_cycles"]) if busy + s["out_idle_cycles"] else None)
            for k, col in (("handshake", "out_handshakes_cycles"), ("starved", "out_starved_cycles"),
                           ("stalled", "out_stalled_cycles"), ("idle", "out_idle_cycles")):
                out[f"dec_{k}"] += s[col]
            add("post_filter_bytes", soft_delta(s, b, "post_filter_bytes"))
            add("no_buffer_ns", soft_delta(s, b, "no_buffer_ns"))
        else:
            held_bypass = b and cycles(b) == cycles(s)
            add("bypass_bytes", 0 if held_bypass else 64 * (s.get("out_handshakes_cycles") or 0))
            add("bypass_post_filter_bytes", soft_delta(s, b, "post_filter_bytes"))
    return out


# --- DuckDB profile ----------------------------------------------------------------------------------

def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def number(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r"\s*([-+0-9.eE]+)", str(v))
    return float(m.group(1)) if m else 0.0


def profile_facts(path: Path) -> dict:
    facts = {"scan_s": 0.0, "operators_s": 0.0, "filter_time_s": 0.0, "hw_rows_in": 0, "hw_rows_out": 0,
             "hw_groups": 0, "scans": [], "cpu_time_s": None, "hw_metrics": {}}
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(doc, dict) and isinstance(doc.get("query"), dict) and "cpu_time" in doc["query"]:
        facts["cpu_time_s"] = number(doc["query"]["cpu_time"])
    for n in walk(doc):
        # DuckDB 2.0: "type" and "timing" of every operator under "operator"; 1.x: operator_type, operator_timing
        timing = n.get("timing", n.get("operator_timing"))
        name = str(n.get("type", n.get("operator_type", n.get("operator_name", ""))))
        if timing is None or not name or not isinstance(n.get("extra_info", {}), dict):
            continue
        t = number(timing)
        facts["operators_s"] += t
        extra = n.get("extra_info") or {}
        function = str(extra.get("Function", ""))
        is_scan = any(k in (name + " " + function).upper()
                      for k in ("TABLE_SCAN", "READ_OASIS", "READ_PARQUET", "SEQ_SCAN"))
        if is_scan:
            facts["scan_s"] += t
            facts["scans"].append(extra.get("Function", extra.get("Table", name)))
        if isinstance(extra, dict):
            facts["filter_time_s"] += number(extra.get("Filter Time", 0))
            facts["hw_rows_in"] += int(number(extra.get("HW Filter Rows In", 0)))
            facts["hw_rows_out"] += int(number(extra.get("HW Filter Rows Out", 0)))
            facts["hw_groups"] += int(number(extra.get("HW Filter Groups", 0)))
            for k, v in extra.items():
                if k in KNOWN_HW or not HW_METRIC.search(k) or not re.match(r"\s*[-+]?[0-9.]", str(v)):
                    continue
                prev = facts["hw_metrics"].get(k, 0)
                facts["hw_metrics"][k] = max(prev, number(v)) if re.search("Peak|Max", k) else prev + number(v)
    return facts


def merge_metrics(dicts) -> dict:
    """Sum of the "HW ..." metrics of several profiles (a Peak or Max one: the maximum)"""
    out = {}
    for d in dicts:
        for k, v in d.items():
            out[k] = max(out.get(k, 0), v) if re.search("Peak|Max", k) else out.get(k, 0) + v
    return out


# --- results -----------------------------------------------------------------------------------------

def read_tsv(path: Path):
    lines = path.read_text().split("\n")
    header = lines[0].split("\t") if lines else []
    rows = [line.split("\t") for line in lines[1:] if line != ""]
    return header, rows


def as_number(s: str):
    try:
        return float(s)
    except ValueError:
        return None


def rows_equal(a: list, b: list) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if len(x) != len(y):
            return False
        for u, v in zip(x, y):
            if u == v:
                continue
            fu, fv = as_number(u), as_number(v)
            if fu is None or fv is None or abs(fu - fv) > 1e-6 * max(1.0, abs(fu), abs(fv)):
                return False
    return True


def sort_key(row: list):
    return [("%.9g" % f) if (f := as_number(c)) is not None else c for c in row]


def compare_results(cell: dict, ref: dict, loose: set) -> list:
    """Queries whose rows differ from the reference cell; the order of rows is ignored after a direct miss.
    Of a loose query only the row count. A log cell ran a subset: only its queries."""
    issues = []
    rdir, cdir = ref["dir"] / "results", cell["dir"] / "results"
    for rf in sorted(rdir.glob("q*.tsv")):
        cf = cdir / rf.name
        q = rf.stem
        if not cf.exists():
            if cell["kind"] != "log":
                issues.append(f"{q}: no result")
            continue
        rh, rr = read_tsv(rf)
        ch, cr = read_tsv(cf)
        if len(rr) != len(cr):
            issues.append(f"{q}: {len(cr)} rows, reference {len(rr)}")
        elif q in loose:
            continue
        elif not rows_equal(cr, rr) and not rows_equal(sorted(cr, key=sort_key), sorted(rr, key=sort_key)):
            first = next(((i, x, y) for i, (x, y) in enumerate(zip(cr, rr)) if not rows_equal([x], [y])), None)
            issues.append(f"{q}: values differ" + (f", row {first[0]}: {first[1]} vs {first[2]}" if first else ""))
    return issues


# --- report ------------------------------------------------------------------------------------------

def fmt(v, digits=0, pct=False):
    if v is None:
        return "-"
    if pct:
        return f"{100 * v:.{digits}f} %"
    return f"{v:,.{digits}f}"


def ratio(a, b):
    return a / b if a and b else None


def gb(v):
    return v / 1e9 if v is not None else None


def spread(s: dict, digits=0) -> str:
    return f"{fmt(s['median'], digits)} ({fmt(s['min'], digits)}-{fmt(s['max'], digits)})" if s["n"] else "-"


def throughput_row(reps: list) -> dict:
    """The throughput cells of one (dataset, mode, threads) over the repetitions"""
    runs = [t for c in reps for t in c["throughput"] if "qph" in t]
    if not runs:
        return {}
    qph = stats([t["qph"] for t in runs])
    mid = sorted(runs, key=lambda t: t["qph"])[len(runs) // 2]
    s = stream_sums(mid.get("profile"), mid.get("profile_before"))
    cpu = stats([(t["cpu_user_s"] + t["cpu_sys_s"]) / t["queries"] for t in runs])
    # util_busy counts stalled cycles as busy (kept for the readers of summary.json); frac_* split them
    util_busy = s["dec_busy"] / s["dec_total"] if s["dec_total"] else None
    util_wall = s["dec_busy"] * CYCLE_S / max(1, s["decoders"]) / mid["seconds"] if s["decoders"] else None
    frac = {f"frac_{k}": s[f"dec_{k}"] / s["dec_total"] if s["dec_total"] else None
            for k in ("handshake", "starved", "stalled", "idle")}
    # Bytes the FPGA wrote to host memory after the FilterStage, per second of the run, and the share of the
    # time a decoder stream had a flow in flight but no host buffer (mean over the decoders)
    post = stats([stream_sums(t.get("profile"), t.get("profile_before"))["post_filter_bytes"] for t in runs])
    post_gbps = stats([v / t["seconds"] / 1e9 for t in runs
                       if (v := stream_sums(t.get("profile"), t.get("profile_before"))["post_filter_bytes"]) is not None])
    no_buffer = (s["no_buffer_ns"] * 1e-9 / s["decoders"] / mid["seconds"]
                 if s["no_buffer_ns"] is not None and s["decoders"] else None)
    # The "HW ..." metrics of the scans, when the cells profiled the throughput run (bench --tp-profile)
    tp_metrics = [merge_metrics(f["hw_metrics"] for p in sorted((c["dir"] / "profile").glob("tp*_q*.json"))
                                if (f := profile_facts(p))) for c in reps]
    tp_metrics = [m for m in tp_metrics if m]
    # Per query: the median over every execution (streams and repetitions) of its seconds
    per_q = {}
    for t in runs:
        for r in t.get("runs", []):
            if r.get("ok"):
                per_q.setdefault(r["q"], []).append(r["end"] - r["start"])
    return {"qph": qph, "cpu_s_per_query": cpu, "errors": sum(t["errors"] for t in runs),
            "util_busy": util_busy, "util_wall": util_wall, **frac, **s, "post_filter": post,
            "post_filter_gbps": post_gbps, "no_buffer_frac": no_buffer,
            "hw_metrics": {k: stats([m.get(k) for m in tp_metrics]) for k in sorted({k for m in tp_metrics for k in m})},
            "query_seconds": {q: stats(v) for q, v in sorted(per_q.items())}}


def latency_rows(reps: list) -> dict:
    """Per query, the latency cells of one (dataset, mode, threads) over the repetitions"""
    out = {}
    qs = sorted({r["q"] for c in reps for r in c["latency"] if "q" in r})
    for q in qs:
        sec = [r["seconds"] for c in reps for r in c["latency"] if r.get("q") == q and r.get("ok")]
        cpu = [r["cpu_user_s"] + r["cpu_sys_s"] for c in reps for r in c["latency"] if r.get("q") == q and r.get("ok")]
        facts = [profile_facts(p) for c in reps for p in sorted((c["dir"] / "profile").glob(f"q{q:02d}_r*.json"))]
        facts = [f for f in facts if f]
        # Bytes after the FilterStage: latency.jsonl holds the counters before the query since 2026-11-06
        post = [stream_sums(r.get("profile"), r.get("profile_before"))["post_filter_bytes"]
                for c in reps for r in c["latency"] if r.get("q") == q and r.get("ok") and "profile_before" in r]
        out[q] = {"seconds": stats(sec), "cpu_s": stats(cpu),
                  "scan_s": stats([f["scan_s"] for f in facts]),
                  "filter_time_s": stats([f["filter_time_s"] for f in facts]),
                  "hw_rows_in": facts[0]["hw_rows_in"] if facts else None,
                  "hw_rows_out": facts[0]["hw_rows_out"] if facts else None,
                  "post_filter_bytes": stats(post),
                  "hw_metrics": facts[0].get("hw_metrics", {}) if facts else {}}
    return out


def log_summary(cell: dict) -> dict:
    """The hardware filter messages of a log cell per query (hwlog/qNN_rR.txt), as in
    runs/s5_correctness/logsum.py: a row group number as N, the messages in their first order, with counts"""
    out = {}
    for p in sorted((cell["dir"] / "hwlog").glob("q*_r*.txt")):
        seen = {}
        for line in p.read_text().splitlines():
            m = re.sub(r"([Rr]ow group) \d+", r"\1 N", line.strip())
            if m:
                seen[m] = seen.get(m, 0) + 1
        out[int(p.stem[1:].split("_")[0])] = seen
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run", type=Path)
    ap.add_argument("--reference", default="native_rdma", help="mode the results are compared against")
    ap.add_argument("--out", type=Path, help="folder of summary.md and summary.json [the run folder]")
    args = ap.parse_args()
    cells = load_cells(args.run)
    if not cells:
        print(f"no cell folders <dataset>_<mode>_t<N>_<tp|lat|log>_r<rep> in {args.run}", file=sys.stderr)
        return 2
    md = [f"# Oasis benchmarks: {args.run.name}", ""]
    summary = {"run": str(args.run), "correctness": {}, "throughput": {}, "latency": {}}
    failed = False

    # Run facts that bench.sh records
    for f in ["run.txt", "bitstream.txt"]:
        if (args.run / f).exists():
            md += [f"## {f}", "", "```", (args.run / f).read_text().strip(), "```", ""]

    groups = {}
    for c in cells:
        groups.setdefault((c["ds"], c["kind"], c["threads"], c["mode"]), []).append(c)

    # 1. Correctness
    md += ["## Correctness", "",
           f"Rows of every query against `{args.reference}` (same dataset and repetition), numbers within 1e-6; "
           "of the loose queries (a LIMIT over ties) only the row count.", ""]
    loose = {}
    for p in args.run.glob("loose_*.json"):
        try:
            loose[p.stem[len("loose_"):]] = set(json.loads(p.read_text()).get("loose", []))
        except (OSError, json.JSONDecodeError):
            pass
    for c in cells:
        errs = [f"q{r['q']:02d} (latency): {r.get('error', '')[:200]}" for r in c["latency"] if not r.get("ok", True)]
        for t in c["throughput"]:
            errs += [f"q{r['q']:02d} (throughput stream {r['stream']}): {r.get('error', '')[:200]}"
                     for r in t.get("runs", []) if not r.get("ok", True)]
        has_results = lambda r: any((r["dir"] / "results").glob("q*.tsv"))
        refs = [r for r in cells if r["mode"] == args.reference and r["ds"] == c["ds"] and r is not c and has_results(r)]
        ref = next((r for r in refs if r["rep"] == c["rep"] and r["kind"] == c["kind"]), refs[0] if refs else None)
        compared = ref is not None and has_results(c)
        diffs = compare_results(c, ref, loose.get(c["ds"], set())) if compared else []
        status = "FAIL" if errs or diffs else ("pass" if compared else "not compared")
        failed |= bool(errs or diffs)
        summary["correctness"][c["name"]] = {"status": status, "errors": errs, "diffs": diffs}
        md.append(f"- `{c['name']}`: {status}" + (f", {len(errs)} failed queries" if errs else "") +
                  (f", {len(diffs)} differing results" if diffs else ""))
        for e in (errs + diffs)[:10]:
            md.append(f"  - {e}")
    md.append("")

    # 2. Throughput
    tp = {}
    for (ds, kind, t, mode), reps in sorted(groups.items()):
        if kind == "tp" and (row := throughput_row(reps)):
            tp.setdefault(ds, {}).setdefault(t, {})[mode] = row
    summary["throughput"] = tp
    for ds, by_t in tp.items():
        md += [f"## Throughput, {ds}", "",
               "Queries per hour: median (min-max) over the repetitions, one process each. CPU s/query: user + sys "
               "of the process over the run, divided by the queries. Decoder: bytes into and out of the decoders "
               "(out is before the FilterStage), of the median repetition. post-filter: the bytes the FPGA wrote "
               "to host memory after the FilterStage, in GB and GB/s of the run (`post_filter_bytes`; '-' before "
               "the column existed). bypass: the bypass slot ('-': the bitstream has no bypass profiler).", "",
               "| threads | mode | reps | Q/h | CPU s/query | errors | dec in GB | dec out GB | post-filter GB | "
               "post-filter GB/s | bypass GB |",
               "|---|---|---|---|---|---|---|---|---|---|---|"]
        for t, by_m in sorted(by_t.items()):
            for m in mode_order(by_m):
                r = by_m[m]
                dec = lambda k: fmt(r[k] / 1e9, 2) if r["decoders"] else "-"
                md.append(f"| {t} | {m} | {r['qph']['n']} | {spread(r['qph'])} | {fmt(r['cpu_s_per_query']['median'], 2)} | "
                          f"{r['errors']} | {dec('dec_in_bytes')} | {dec('dec_out_bytes')} | "
                          f"{fmt(gb(r['post_filter']['median']), 2)} | {fmt(r['post_filter_gbps']['median'], 2)} | "
                          f"{fmt(gb(r['bypass_bytes']), 2)} |")
        md += ["", "Decoder output cycles of the median repetition, summed over the decoders: handshake (a beat "
               "leaves), starved (the decoder has no beat ready), stalled (the FilterStage, the StreamWriter or "
               "PCIe does not take it), idle (no column chunk). util = the old busy share, handshake + starved + "
               "stalled. idle per decoder: the idle share of each decoder slot. no buffer: the share of the run "
               "during which a decoder stream had a flow in flight but no host buffer (`no_buffer_ns`, mean over "
               "the decoders).", "",
               "| threads | mode | handshake | starved | stalled | idle | idle per decoder | util | no buffer |",
               "|---|---|---|---|---|---|---|---|---|"]
        for t, by_m in sorted(by_t.items()):
            for m in mode_order(by_m):
                r = by_m[m]
                if not r["decoders"]:
                    continue
                md.append(f"| {t} | {m} | " + " | ".join(fmt(r[f"frac_{k}"], 1, True)
                                                        for k in ("handshake", "starved", "stalled", "idle")) +
                          f" | {' / '.join(fmt(v, 1, True) for v in r['idle_each'])}"
                          f" | {fmt(r['util_busy'], 0, True)} | {fmt(r['no_buffer_frac'], 1, True)} |")
        md += ["", "oasis = every stage of the hardware filter; oasis_nohw = the same bitstream with "
               "`oasis_hw_filter = false`; native = `read_parquet` over `rdma://`; narrow = the same with "
               "`oasis_narrow_transfer = true` (native has no narrow variant).", "",
               "| threads | oasis | oasis_nohw | native | oasis / oasis_nohw | oasis / native | CPU s/q oasis / oasis_nohw |",
               "|---|---|---|---|---|---|---|"]
        for t, by_m in sorted(by_t.items()):
            g = lambda m: by_m.get(m, {}).get("qph", {}).get("median")
            cpu = lambda m: by_m.get(m, {}).get("cpu_s_per_query", {}).get("median")
            native = g("native_rdma") or g("native_local")
            for sfx in ["", "_narrow"]:
                o, n = "oasis" + sfx, "oasis_nohw" + sfx
                if sfx and o not in by_m and n not in by_m:
                    continue
                md.append(f"| {t}{' narrow' if sfx else ''} | {fmt(g(o))} | {fmt(g(n))} | {fmt(native)} | "
                          f"{fmt(ratio(g(o), g(n)), 2)} | {fmt(ratio(g(o), native), 2)} | "
                          f"{fmt(ratio(cpu(o), cpu(n)), 2)} |")
        md.append("")

        # Narrow transfer against wide: every mode that has both
        pairs = [(t, m) for t, by_m in sorted(by_t.items()) for m in mode_order(by_m)
                 if "_narrow" not in m and narrow_of(m) in by_m]
        if pairs:
            md += [f"### Narrow transfer, {ds}", "",
                   "`oasis_narrow_transfer = true`: BIGINT and DECIMAL chunks that fit INT32 cross as INT32 and are "
                   "widened on the CPU. The CPU s/query difference holds the cost of the widening.", "",
                   "| threads | mode | Q/h wide | Q/h narrow | narrow / wide | CPU s/q wide | CPU s/q narrow | "
                   "post-filter GB wide | narrow | stalled wide | narrow |",
                   "|---|---|---|---|---|---|---|---|---|---|---|"]
            for t, m in pairs:
                w, n = by_t[t][m], by_t[t][narrow_of(m)]
                md.append(f"| {t} | {m} | {spread(w['qph'])} | {spread(n['qph'])} | "
                          f"{fmt(ratio(n['qph']['median'], w['qph']['median']), 2)} | "
                          f"{fmt(w['cpu_s_per_query']['median'], 2)} | {fmt(n['cpu_s_per_query']['median'], 2)} | "
                          f"{fmt(gb(w['post_filter']['median']), 2)} | {fmt(gb(n['post_filter']['median']), 2)} | "
                          f"{fmt(w['frac_stalled'], 1, True)} | {fmt(n['frac_stalled'], 1, True)} |")
            md.append("")

        # Metrics of the scans in profiled throughput runs (bench --tp-profile): the lookup table regions
        rows = [(t, m, k, s) for t, by_m in sorted(by_t.items()) for m in mode_order(by_m)
                for k, s in by_m[m].get("hw_metrics", {}).items()]
        if rows:
            md += [f"### Scan metrics in the throughput runs, {ds}", "",
                   "The \"HW ...\" metrics of every scan of every query in the DuckDB profile, summed over the run "
                   "(a Peak or Max metric: the maximum); median (min-max) over the repetitions.", "",
                   "| threads | mode | metric | value |", "|---|---|---|---|"]
            md += [f"| {t} | {m} | {k} | {spread(s)} |" for t, m, k, s in rows]
            md.append("")

        # 3. Ablation: the thread counts that have a stage, wide and narrow
        for t, by_m in sorted(by_t.items()):
            for sfx in ["", "_narrow"]:
                if not any(f"oasis_s{k}{sfx}" in by_m for k in range(1, 5)):
                    continue
                base = by_m.get("oasis_nohw" + sfx, {}).get("qph", {}).get("median")
                native = by_m.get("native_rdma", {}).get("qph", {}).get("median")
                md += [f"### Ablation, {ds}, {t} threads{', narrow transfer' if sfx else ''}", "",
                       "Stages of the hardware filter (`oasis_hw_filter_stage`): 1 constant comparisons, 2 join bounds, "
                       "3 bitmap lookup, 4 dictionary ids of strings, 5 Bloom filters. Each row adds one stage to the "
                       "rows above it.", "",
                       f"| stages | Q/h | / oasis_nohw{sfx} | / native | CPU s/query | dec out GB | post-filter GB | "
                       "stalled |", "|---|---|---|---|---|---|---|---|"]
                for m in [m for m in LADDER if m + sfx in by_m]:
                    r = by_m[m + sfx]
                    md.append(f"| {LADDER_NAME[m]} | {spread(r['qph'])} | {fmt(ratio(r['qph']['median'], base), 2)} | "
                              f"{fmt(ratio(r['qph']['median'], native), 2)} | {fmt(r['cpu_s_per_query']['median'], 2)} | "
                              f"{fmt(r['dec_out_bytes'] / 1e9, 2)} | {fmt(gb(r['post_filter']['median']), 2)} | "
                              f"{fmt(r['frac_stalled'], 1, True)} |")
                md.append("")

    # 4. Latency
    lat = {}
    for (ds, kind, t, mode), reps in sorted(groups.items()):
        if kind == "lat" and any(c["latency"] for c in reps):
            lat.setdefault(ds, {}).setdefault(t, {})[mode] = latency_rows(reps)
    summary["latency"] = lat
    for ds, by_t in lat.items():
        for t, by_m in sorted(by_t.items()):
            modes = mode_order(m for m in by_m if base_mode(m) in MAIN_MODES)
            qs = sorted({q for m in by_m for q in by_m[m]})
            md += [f"## Latency, {ds}, {t} threads", "",
                   "Median seconds over the repetitions. scan = CPU seconds of the scan operators in the DuckDB profile "
                   "(summed over threads), filter = their `Filter Time`, HW rows = `HW Filter Rows In -> Out` of oasis.", ""]
            head = ["q"] + [f"{m} s" for m in modes] + [f"{m} scan / filter" for m in modes] + ["HW rows (oasis)"]
            md += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
            for q in qs:
                rec = {m: by_m[m].get(q) for m in modes}
                o = rec.get("oasis") or {}
                hw = f"{o['hw_rows_in']:,} -> {o['hw_rows_out']:,}" if o.get("hw_rows_in") else "-"
                row = ([f"Q{q}"] + [fmt(r["seconds"]["median"], 3) if r else "-" for r in rec.values()] +
                       [f"{fmt(r['scan_s']['median'], 3)} / {fmt(r['filter_time_s']['median'], 3)}" if r else "-"
                        for r in rec.values()] + [hw])
                md.append("| " + " | ".join(row) + " |")
            tot = {m: sum(v["seconds"]["median"] or 0 for v in by_m[m].values()) for m in modes}
            md += ["", "Sum of the medians: " + ", ".join(f"{m} {tot[m]:.2f} s" for m in modes)]
            post = {m: [v["post_filter_bytes"]["median"] for v in by_m[m].values()] for m in modes}
            post = {m: sum(v) for m, v in post.items() if v and None not in v}
            if post:
                md += ["", "Post-filter GB over the pass (sum of the medians): " +
                       ", ".join(f"{m} {v / 1e9:.2f}" for m, v in post.items())]
            md.append("")

            for sfx in ["", "_narrow"]:
                ladder = [m for m in LADDER if m + sfx in by_m]
                if not any(m.startswith("oasis_s") for m in ladder):
                    continue
                md += [f"### Latency ablation, {ds}, {t} threads{', narrow transfer' if sfx else ''}", "",
                       f"Median seconds of each query per stage set; the last column is oasis_nohw{sfx} / oasis{sfx}.", "",
                       "| q | " + " | ".join(LADDER_NAME[m] for m in ladder) + " | speedup |",
                       "|" + "---|" * (len(ladder) + 2)]
                for q in qs:
                    sec = [by_m[m + sfx].get(q, {}).get("seconds", {}).get("median") for m in ladder]
                    sp = ratio(by_m.get("oasis_nohw" + sfx, {}).get(q, {}).get("seconds", {}).get("median"),
                               by_m.get("oasis" + sfx, {}).get(q, {}).get("seconds", {}).get("median"))
                    md.append(f"| Q{q} | " + " | ".join(fmt(s, 3) for s in sec) + f" | {fmt(sp, 2)} |")
                md.append("")

            # The other "HW ..." metrics of the scans (lookup table regions), of the first repetition
            rows = [(q, m, by_m[m][q]["hw_metrics"]) for q in qs for m in mode_order(by_m)
                    if any(by_m[m].get(q, {}).get("hw_metrics", {}).values())]
            keys = sorted({k for _, _, h in rows for k in h})
            if rows:
                md += [f"### Scan metrics, {ds}, {t} threads", "",
                       "The \"HW ...\" metrics of the scans in the DuckDB profile of the first repetition, summed over "
                       "the scans of the query (a Peak or Max metric: the maximum); the rows with a value.", "",
                       "| q | mode | " + " | ".join(keys) + " |", "|" + "---|" * (len(keys) + 2)]
                md += [f"| Q{q} | {m} | " + " | ".join(fmt(h.get(k)) for k in keys) + " |" for q, m, h in rows]
                md.append("")

    # 5. Hardware filter log
    logs = {c["name"]: log_summary(c) for c in cells if c["kind"] == "log"}
    summary["hw_log"] = logs
    if logs:
        md += ["## Hardware filter log", "",
               "Cells under DuckDB's log at DEBUG (bench --hw-log): the messages of the hardware filter per query, "
               "a row group number as N, in their first order, with their count. The timings of these cells are "
               "not in the tables above.", ""]
        for name, by_q in logs.items():
            for q, seen in sorted(by_q.items()):
                md += [f"### `{name}` Q{q}", "", "```"]
                md += [f"x{n} {m[:230]}" for m, n in seen.items()] or ["(no message)"]
                md += ["```", ""]

    # NIC counters of the FPGA (bench.sh: nstats_before.txt and nstats_after.txt of each cell)
    md += ["## FPGA network counters", "", "Lines of `cyt_attr_nstats` that changed over a cell.", ""]
    for c in cells:
        b, a = c["dir"] / "nstats_before.txt", c["dir"] / "nstats_after.txt"
        if b.exists() and a.exists():
            changed = [f"{x.strip()} -> {y.strip()}" for x, y in zip(b.read_text().splitlines(), a.read_text().splitlines())
                       if x != y]
            md.append(f"- `{c['name']}`: " + ("; ".join(changed[:12]) if changed else "no change"))
    md.append("")

    out = args.out or args.run
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text("\n".join(md) + "\n")
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str) + "\n")
    print("\n".join(md))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
