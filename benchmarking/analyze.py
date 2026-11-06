#!/usr/bin/env python3
"""Summarises a run folder of bench.sh (or sim_check.sh): oasis against the baselines of the same run.

    python3 analyze.py <run folder>  [--reference native_rdma]

Reads every cell folder <dataset>_<mode>_t<threads>_<tp|lat>_r<rep>/ (the output of bench) and writes
summary.md and summary.json into the run folder. A value over the repetitions is their median, with the
min and max beside it.

1. Correctness: the rows of every query (results/qNN.tsv) against the reference mode on the same dataset
   and repetition, numbers with a relative tolerance of 1e-6; errors of any query. For the queries in
   loose_<dataset>.json (gen_data.py queries: a LIMIT over ties) only the row count.
2. Throughput per dataset and thread count: queries per hour, CPU seconds per query, the decoder
   counters; the ratios oasis / oasis_nohw and oasis / native.
3. Ablation per dataset: queries per hour of each stage of the hardware filter (oasis_nohw = no stage,
   oasis_s<k>, oasis = every stage).
4. Latency per dataset and thread count (TPC-H): per query the seconds of each mode and the DuckDB profile
   (scan operator time, Filter Time, HW Filter Rows In and Out); then the seconds of each stage.

The exit code is 1 when a query failed or a result differs, else 0. Standard library only.
"""

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

CELL = re.compile(r"^(?P<ds>tpch_sf[0-9.]+|clickbench_sample|clickbench)_(?P<mode>[a-z0-9_]+?)"
                  r"_t(?P<threads>\d+)_(?P<kind>tp|lat)_r(?P<rep>\d+)$")
CYCLE_S = 4e-9  # 250 MHz
MAIN_MODES = ["oasis", "oasis_nohw", "native_rdma", "native_local"]
# The ladder of the ablation: no stage, the stages, every stage
LADDER = ["oasis_nohw"] + [f"oasis_s{k}" for k in range(1, 5)] + ["oasis"]
# Named by what each stage set adds: constant comparisons, join bounds, bitmap lookup, dictionary ids, Bloom filters
LADDER_NAME = {"oasis_nohw": "none (oasis_nohw)", "oasis_s1": "constants", "oasis_s2": "+ join bounds",
               "oasis_s3": "+ bitmaps", "oasis_s4": "+ dict ids", "oasis": "+ Bloom (oasis)"}


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

def stream_sums(profile, before=None) -> dict:
    """Sums of the decoder slots (in-side counters present) and of the bypass slot. A slot that reads the
    same before and after the run saw no data beat in it and still holds an earlier window (native_rdma)."""
    out = {"dec_in_bytes": 0, "dec_out_bytes": 0, "dec_busy": 0, "dec_total": 0, "bypass_bytes": 0, "decoders": 0}
    if not isinstance(profile, list):
        return out
    held = before if isinstance(before, list) else []
    for s in profile:
        if s in held:
            continue
        if s.get("in_handshakes_cycles") is not None:
            out["decoders"] += 1
            out["dec_in_bytes"] += 64 * s["in_handshakes_cycles"]
            out["dec_out_bytes"] += 64 * s["out_handshakes_cycles"]
            busy = s["out_handshakes_cycles"] + s["out_starved_cycles"] + s["out_stalled_cycles"]
            out["dec_busy"] += busy
            out["dec_total"] += busy + s["out_idle_cycles"]
        else:
            out["bypass_bytes"] += 64 * (s.get("out_handshakes_cycles") or 0)
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
             "hw_groups": 0, "scans": [], "cpu_time_s": None}
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
    return facts


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
    Of a loose query only the row count."""
    issues = []
    rdir, cdir = ref["dir"] / "results", cell["dir"] / "results"
    for rf in sorted(rdir.glob("q*.tsv")):
        cf = cdir / rf.name
        q = rf.stem
        if not cf.exists():
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
    util_busy = s["dec_busy"] / s["dec_total"] if s["dec_total"] else None
    util_wall = s["dec_busy"] * CYCLE_S / max(1, s["decoders"]) / mid["seconds"] if s["decoders"] else None
    # Per query: the median over every execution (streams and repetitions) of its seconds
    per_q = {}
    for t in runs:
        for r in t.get("runs", []):
            if r.get("ok"):
                per_q.setdefault(r["q"], []).append(r["end"] - r["start"])
    return {"qph": qph, "cpu_s_per_query": cpu, "errors": sum(t["errors"] for t in runs),
            "util_busy": util_busy, "util_wall": util_wall, **s,
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
        out[q] = {"seconds": stats(sec), "cpu_s": stats(cpu),
                  "scan_s": stats([f["scan_s"] for f in facts]),
                  "filter_time_s": stats([f["filter_time_s"] for f in facts]),
                  "hw_rows_in": facts[0]["hw_rows_in"] if facts else None,
                  "hw_rows_out": facts[0]["hw_rows_out"] if facts else None}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run", type=Path)
    ap.add_argument("--reference", default="native_rdma", help="mode the results are compared against")
    args = ap.parse_args()
    cells = load_cells(args.run)
    if not cells:
        print(f"no cell folders <dataset>_<mode>_t<N>_<tp|lat>_r<rep> in {args.run}", file=sys.stderr)
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
               "(out is before the FilterStage) and the utilisation (non-idle / profiled cycles of the decoder "
               "output), of the median repetition.", "",
               "| threads | mode | reps | Q/h | CPU s/query | errors | dec in GB | dec out GB | bypass GB | util |",
               "|---|---|---|---|---|---|---|---|---|---|"]
        for t, by_m in sorted(by_t.items()):
            order = list(dict.fromkeys(MAIN_MODES + LADDER))
            for m in [m for m in order if m in by_m] + sorted(set(by_m) - set(order)):
                r = by_m[m]
                md.append(f"| {t} | {m} | {r['qph']['n']} | {spread(r['qph'])} | {fmt(r['cpu_s_per_query']['median'], 2)} | "
                          f"{r['errors']} | {fmt(r['dec_in_bytes'] / 1e9, 2)} | {fmt(r['dec_out_bytes'] / 1e9, 2)} | "
                          f"{fmt(r['bypass_bytes'] / 1e9, 2)} | {fmt(r['util_busy'], 0, True)} |")
        md += ["", "oasis = every stage of the hardware filter; oasis_nohw = the same bitstream with "
               "`oasis_hw_filter = false`; native = `read_parquet` over `rdma://`.", "",
               "| threads | oasis | oasis_nohw | native | oasis / oasis_nohw | oasis / native | CPU s/q oasis / oasis_nohw |",
               "|---|---|---|---|---|---|---|"]
        for t, by_m in sorted(by_t.items()):
            g = lambda m: by_m.get(m, {}).get("qph", {}).get("median")
            cpu = lambda m: by_m.get(m, {}).get("cpu_s_per_query", {}).get("median")
            native = g("native_rdma") or g("native_local")
            md.append(f"| {t} | {fmt(g('oasis'))} | {fmt(g('oasis_nohw'))} | {fmt(native)} | "
                      f"{fmt(ratio(g('oasis'), g('oasis_nohw')), 2)} | {fmt(ratio(g('oasis'), native), 2)} | "
                      f"{fmt(ratio(cpu('oasis'), cpu('oasis_nohw')), 2)} |")
        md.append("")

        # 3. Ablation: the thread counts that have a stage
        for t, by_m in sorted(by_t.items()):
            if not any(m.startswith("oasis_s") for m in by_m):
                continue
            base = by_m.get("oasis_nohw", {}).get("qph", {}).get("median")
            native = by_m.get("native_rdma", {}).get("qph", {}).get("median")
            md += [f"### Ablation, {ds}, {t} threads", "",
                   "Stages of the hardware filter (`oasis_hw_filter_stage`): 1 constant comparisons, 2 join bounds, "
                   "3 bitmap lookup, 4 dictionary ids of strings, 5 Bloom filters. Each row adds one stage to the "
                   "rows above it.", "",
                   "| stages | Q/h | / oasis_nohw | / native | CPU s/query | dec out GB |", "|---|---|---|---|---|---|"]
            for m in [m for m in LADDER if m in by_m]:
                r = by_m[m]
                md.append(f"| {LADDER_NAME[m]} | {spread(r['qph'])} | {fmt(ratio(r['qph']['median'], base), 2)} | "
                          f"{fmt(ratio(r['qph']['median'], native), 2)} | {fmt(r['cpu_s_per_query']['median'], 2)} | "
                          f"{fmt(r['dec_out_bytes'] / 1e9, 2)} |")
            md.append("")

    # 4. Latency
    lat = {}
    for (ds, kind, t, mode), reps in sorted(groups.items()):
        if kind == "lat" and any(c["latency"] for c in reps):
            lat.setdefault(ds, {}).setdefault(t, {})[mode] = latency_rows(reps)
    summary["latency"] = lat
    for ds, by_t in lat.items():
        for t, by_m in sorted(by_t.items()):
            modes = [m for m in MAIN_MODES if m in by_m]
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
            md += ["", "Sum of the medians: " + ", ".join(f"{m} {tot[m]:.2f} s" for m in modes), ""]

            ladder = [m for m in LADDER if m in by_m]
            if any(m.startswith("oasis_s") for m in ladder):
                md += [f"### Latency ablation, {ds}, {t} threads", "",
                       "Median seconds of each query per stage set; the last column is oasis_nohw / oasis.", "",
                       "| q | " + " | ".join(LADDER_NAME[m] for m in ladder) + " | speedup |",
                       "|" + "---|" * (len(ladder) + 2)]
                for q in qs:
                    sec = [by_m[m].get(q, {}).get("seconds", {}).get("median") for m in ladder]
                    sp = ratio(by_m.get("oasis_nohw", {}).get(q, {}).get("seconds", {}).get("median"),
                               by_m.get("oasis", {}).get(q, {}).get("seconds", {}).get("median"))
                    md.append(f"| Q{q} | " + " | ".join(fmt(s, 3) for s in sec) + f" | {fmt(sp, 2)} |")
                md.append("")

    # NIC counters of the FPGA (bench.sh: nstats_before.txt and nstats_after.txt of each cell)
    md += ["## FPGA network counters", "", "Lines of `cyt_attr_nstats` that changed over a cell.", ""]
    for c in cells:
        b, a = c["dir"] / "nstats_before.txt", c["dir"] / "nstats_after.txt"
        if b.exists() and a.exists():
            changed = [f"{x.strip()} -> {y.strip()}" for x, y in zip(b.read_text().splitlines(), a.read_text().splitlines())
                       if x != y]
            md.append(f"- `{c['name']}`: " + ("; ".join(changed[:12]) if changed else "no change"))
    md.append("")

    (args.run / "summary.md").write_text("\n".join(md) + "\n")
    (args.run / "summary.json").write_text(json.dumps(summary, indent=1, default=str) + "\n")
    print("\n".join(md))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
