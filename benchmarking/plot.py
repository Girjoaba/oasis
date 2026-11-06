#!/usr/bin/env python3
"""Draws the charts of a run folder from the summary.json of analyze.py, as SVG files in <run>/plots/.

    python3 plot.py <run folder>

Every value is the median over the repetitions; the whisker spans their min and max. Charts:
  throughput_<dataset>.svg            Q/h per mode (bars at one thread count, lines over a thread sweep)
  ablation_<dataset>_t<T>.svg         Q/h per stage set of the hardware filter, native as a reference line
  latency_<dataset>_t<T>.svg          seconds per query and mode (shaded: oasis filtered rows in hardware)
  ablation_latency_<dataset>_t<T>.svg speedup over oasis_nohw per query and stage set, as a labelled heatmap

Style of the slides (tpch-results-slides): DejaVu Sans, ink #0B0B0B, muted #8D8C8A, grid #E6E6E5. Colour
per mode, the same on every chart: oasis_nohw blue, oasis orange, native_rdma aqua, native_local yellow
(the first four slots of the dataviz reference palette). The stage sets are one series in the colour of
oasis. The heatmap is diverging: blue faster, red slower, gray within 5 % of oasis_nohw; every cell carries
its value. Every mark has a <title>, the tooltip of a browser. Standard library only.
"""

import argparse
import json
import math
from pathlib import Path

COLOR = {"oasis_nohw": "#2A78D6", "oasis": "#EB6834", "native_rdma": "#1BAF7A", "native_local": "#EDA100"}
LABEL = {"oasis_nohw": "oasis_nohw", "oasis": "oasis", "native_rdma": "native", "native_local": "native_local"}
MODES = ["oasis_nohw", "oasis", "native_rdma", "native_local"]
LADDER = ["oasis_nohw", "oasis_s1", "oasis_s2", "oasis_s3", "oasis_s4", "oasis"]
# The stage sets by what they add: constant comparisons, join bounds, bitmap lookup, dictionary ids, Bloom filters
LADDER_NAME = {"oasis_nohw": "none", "oasis_s1": "constants", "oasis_s2": "+ join bounds", "oasis_s3": "+ bitmaps",
               "oasis_s4": "+ dict ids", "oasis": "+ Bloom"}
INK, MUTED, SOFT, GRID, AXIS, SHADE, SURFACE = "#0B0B0B", "#8D8C8A", "#52514E", "#E6E6E5", "#A6A6A6", "#F0EFEC", "#FCFCFB"
FONT = 'font-family="DejaVu Sans, Verdana, sans-serif"'
# Diverging bins of the speedup: red arm, gray midpoint, blue arm (steps 250, 450, 600 of the blue ramp)
BINS = [(0.8, "#E34948", "#FCFCFB"), (0.95, "#E99C9A", INK), (1.05, "#F0EFEC", INK), (1.25, "#86B6EF", INK),
        (2.0, "#2A78D6", "#FCFCFB"), (math.inf, "#184F95", "#FCFCFB")]


def nice_axis(vmax: float, ticks=5):
    """Top of the axis and the tick step: 1, 2, 2.5 or 5 times a power of ten, about `ticks` steps."""
    raw = vmax / ticks if vmax > 0 else 1
    mag = 10 ** math.floor(math.log10(raw))
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    return step * math.ceil(vmax * 1.04 / step) or step, step


def fmt_tick(v: float) -> str:
    return f"{v:,.0f}" if v >= 1000 or v == int(v) else f"{v:g}"


def svg_open(W, H, title):
    return [f'<svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg" {FONT}>',
            f'<rect width="{W}" height="{H}" fill="{SURFACE}"/>',
            f'<text x="10" y="18" font-size="13" font-weight="700" fill="{INK}">{title}</text>']


def axes(o, L, TOP, pw, ph, ymax, step, ylabel):
    y = lambda v: TOP + ph * (1 - v / ymax)
    v = 0.0
    while v <= ymax + 1e-9:
        o.append(f'<line x1="{L}" x2="{L+pw}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="{AXIS if v == 0 else GRID}"/>')
        o.append(f'<text x="{L-8}" y="{y(v)+4:.1f}" font-size="11" fill="{MUTED}" text-anchor="end">{fmt_tick(v)}</text>')
        v += step
    o.append(f'<text x="14" y="{TOP+ph/2:.1f}" font-size="12" fill="{SOFT}" text-anchor="middle" '
             f'transform="rotate(-90 14 {TOP+ph/2:.1f})">{ylabel}</text>')
    return y


def wrap(name, width=10):
    """A label in one line, or in two at its last space when it is longer than width"""
    if len(name) <= width or " " not in name:
        return [name]
    i = name.rindex(" ")
    return [name[:i], name[i + 1:]]


def bar(o, x, bw, y, v, color, tip):
    """A bar with a rounded data end, square at the baseline"""
    h = max(0.0, y(0) - y(v))
    r = min(4.0, h, bw / 2)
    o.append(f'<path d="M{x:.1f},{y(0):.1f} V{y(v)+r:.1f} Q{x:.1f},{y(v):.1f} {x+r:.1f},{y(v):.1f} '
             f'H{x+bw-r:.1f} Q{x+bw:.1f},{y(v):.1f} {x+bw:.1f},{y(v)+r:.1f} V{y(0):.1f} Z" fill="{color}">'
             f'<title>{tip}</title></path>')


def whisker(o, cx, y, s):
    """min-max of the repetitions"""
    if s.get("n", 0) < 2 or s["min"] is None:
        return
    o.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{y(s["min"]):.1f}" y2="{y(s["max"]):.1f}" stroke="{SOFT}" stroke-width="1"/>')
    for v in (s["min"], s["max"]):
        o.append(f'<line x1="{cx-3:.1f}" x2="{cx+3:.1f}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="{SOFT}" stroke-width="1"/>')


def legend(o, x, y, modes):
    for m in modes:
        o.append(f'<rect x="{x}" y="{y-9}" width="10" height="10" rx="2" fill="{COLOR[m]}"/>')
        o.append(f'<text x="{x+14}" y="{y}" font-size="11.5" fill="{INK}">{LABEL[m]}</text>')
        x += 24 + 7 * len(LABEL[m])


def tip(name, s, unit, digits=0):
    return (f"{name}: median {s['median']:,.{digits}f} {unit}, min {s['min']:,.{digits}f}, max {s['max']:,.{digits}f}, "
            f"{s['n']} repetitions")


def throughput_bars(ds, t, by_m):
    modes = [m for m in MODES if m in by_m]
    W, H, L, R, TOP, B = 480, 340, 70, 16, 50, 34
    pw, ph = W - L - R, H - TOP - B
    ymax, step = nice_axis(max(by_m[m]["qph"]["max"] for m in modes))
    o = svg_open(W, H, f"Throughput, {ds}, {t} threads")
    y = axes(o, L, TOP, pw, ph, ymax, step, "queries per hour")
    gw = pw / len(modes)
    bw = min(56.0, gw - 24)
    for i, m in enumerate(modes):
        s = by_m[m]["qph"]
        cx = L + (i + 0.5) * gw
        bar(o, cx - bw / 2, bw, y, s["median"], COLOR[m], tip(LABEL[m], s, "Q/h"))
        whisker(o, cx, y, s)
        o.append(f'<text x="{cx:.1f}" y="{y(s["max"]) - 6:.1f}" font-size="11.5" fill="{INK}" text-anchor="middle"'
                 + (' font-weight="700"' if m == "oasis" else "") + f'>{s["median"]:,.0f}</text>')
        o.append(f'<text x="{cx:.1f}" y="{H-12}" font-size="11.5" fill="{INK}" text-anchor="middle">{LABEL[m]}</text>')
    o.append("</svg>")
    return "\n".join(o)


def throughput_lines(ds, by_t):
    threads = sorted(by_t)
    modes = [m for m in MODES if any(m in by_t[t] for t in threads)]
    W, H, L, R, TOP, B = 640, 420, 70, 120, 40, 40
    pw, ph = W - L - R, H - TOP - B
    ymax, step = nice_axis(max(by_t[t][m]["qph"]["max"] for t in threads for m in modes if m in by_t[t]))
    o = svg_open(W, H, f"Throughput, {ds}")
    y = axes(o, L, TOP, pw, ph, ymax, step, "queries per hour")
    span = math.log2(threads[-1] / threads[0]) or 1
    x = lambda t: L + pw * math.log2(t / threads[0]) / span
    for t in threads:
        o.append(f'<text x="{x(t):.1f}" y="{TOP+ph+17}" font-size="11" fill="{MUTED}" text-anchor="middle">{t}</text>')
    o.append(f'<text x="{L+pw/2}" y="{H-4}" font-size="12" fill="{SOFT}" text-anchor="middle">DuckDB threads (log scale)</text>')
    ends = []
    # the reference modes first, so the mode of this work lies on top
    for m in sorted(modes, key=lambda m: m == "oasis"):
        pts = [(t, by_t[t][m]["qph"]) for t in threads if m in by_t[t]]
        line = " ".join("%.1f,%.1f" % (x(t), y(s["median"])) for t, s in pts)
        o.append(f'<polyline points="{line}" fill="none" stroke="{COLOR[m]}" stroke-width="2" stroke-linejoin="round"/>')
        for t, s in pts:
            whisker(o, x(t), y, s)
            o.append(f'<circle cx="{x(t):.1f}" cy="{y(s["median"]):.1f}" r="4" fill="{COLOR[m]}" stroke="{SURFACE}" '
                     f'stroke-width="2"><title>{tip(f"{LABEL[m]}, {t} threads", s, "Q/h")}</title></circle>')
        ends.append((y(pts[-1][1]["median"]), m))
    # direct labels at the line ends, pushed apart by at least 15 px
    placed = []
    for ly, m in sorted(ends):
        ly = max(ly, placed[-1] + 15) if placed else ly
        placed.append(ly)
        o.append(f'<line x1="{L+pw+6}" x2="{L+pw+18}" y1="{ly:.1f}" y2="{ly:.1f}" stroke="{COLOR[m]}" stroke-width="2"/>')
        o.append(f'<text x="{L+pw+22}" y="{ly+4:.1f}" font-size="11.5" fill="{INK}"'
                 + (' font-weight="700"' if m == "oasis" else "") + f'>{LABEL[m]}</text>')
    legend(o, L, 34, modes)
    o.append("</svg>")
    return "\n".join(o)


def ablation_bars(ds, t, by_m):
    ladder = [m for m in LADDER if m in by_m]
    native = by_m.get("native_rdma", {}).get("qph", {}).get("median")
    base = by_m.get("oasis_nohw", {}).get("qph", {}).get("median")
    W, H, L, R, TOP, B = 560, 370, 70, 70, 50, 60
    pw, ph = W - L - R, H - TOP - B
    ymax, step = nice_axis(max([by_m[m]["qph"]["max"] for m in ladder] + [native or 0]))
    o = svg_open(W, H, f"Stages of the hardware filter, {ds}, {t} threads")
    y = axes(o, L, TOP, pw, ph, ymax, step, "queries per hour")
    gw = pw / len(ladder)
    bw = min(48.0, gw - 16)
    for i, m in enumerate(ladder):
        s = by_m[m]["qph"]
        cx = L + (i + 0.5) * gw
        color = COLOR["oasis_nohw"] if m == "oasis_nohw" else COLOR["oasis"]
        bar(o, cx - bw / 2, bw, y, s["median"], color, tip(LADDER_NAME[m], s, "Q/h"))
        whisker(o, cx, y, s)
        label = f'{s["median"] / base:.2f}x' if base and m != "oasis_nohw" else f'{s["median"]:,.0f}'
        o.append(f'<text x="{cx:.1f}" y="{y(s["max"]) - 6:.1f}" font-size="11.5" fill="{INK}" text-anchor="middle">{label}</text>')
        for k, line in enumerate(wrap(LADDER_NAME[m])):
            o.append(f'<text x="{cx:.1f}" y="{TOP+ph+17+14*k}" font-size="11.5" fill="{INK}" text-anchor="middle">{line}</text>')
    o.append(f'<text x="{L+pw/2}" y="{H-8}" font-size="12" fill="{SOFT}" text-anchor="middle">'
             f'stages on (labels: Q/h of oasis_nohw, then the ratio to it)</text>')
    if native:
        o.append(f'<line x1="{L}" x2="{L+pw}" y1="{y(native):.1f}" y2="{y(native):.1f}" stroke="{COLOR["native_rdma"]}" '
                 f'stroke-width="2" stroke-dasharray="6 4"><title>native: median {native:,.0f} Q/h</title></line>')
        o.append(f'<text x="{L+pw+6}" y="{y(native)+4:.1f}" font-size="11.5" fill="{INK}">native</text>')
    o.append("</svg>")
    return "\n".join(o)


def latency_bars(ds, t, by_m):
    modes = [m for m in MODES if m in by_m]
    queries = sorted({int(q) for m in modes for q in by_m[m]})
    get = lambda m, q: by_m[m].get(str(q), {}).get("seconds", {})
    hw = {q for q in queries if (by_m.get("oasis", {}).get(str(q), {}).get("hw_rows_in") or 0) > 0}
    W, H, L, R, TOP, B = max(640, 50 * len(queries) + 80), 330, 60, 10, 50, 30
    pw, ph = W - L - R, H - TOP - B
    gw = pw / len(queries)
    bw = min(17.0, (gw - 10 - 2 * (len(modes) - 1)) / len(modes))
    ymax, step = nice_axis(max((get(m, q).get("max") or 0) for m in modes for q in queries))
    o = svg_open(W, H, f"Latency per query, {ds}, {t} threads (shaded: rows filtered in hardware)")
    for i, q in enumerate(queries):
        if q in hw:
            o.append(f'<rect x="{L + i*gw + 1:.1f}" y="{TOP}" width="{gw - 2:.1f}" height="{ph}" fill="{SHADE}"/>')
    y = axes(o, L, TOP, pw, ph, ymax, step, "seconds")
    for i, q in enumerate(queries):
        cx = L + (i + 0.5) * gw
        x0 = cx - (len(modes) * bw + 2 * (len(modes) - 1)) / 2
        for j, m in enumerate(modes):
            s = get(m, q)
            if not s or s.get("median") is None:
                continue
            x = x0 + j * (bw + 2)
            bar(o, x, bw, y, s["median"], COLOR[m], tip(f"Q{q} {LABEL[m]}", s, "s", 3))
            whisker(o, x + bw / 2, y, s)
        o.append(f'<text x="{cx:.1f}" y="{H-10}" font-size="11.5" fill="{INK}" text-anchor="middle"'
                 + (' font-weight="700"' if q in hw else "") + f'>Q{q}</text>')
    legend(o, L, 36, modes)
    o.append("</svg>")
    return "\n".join(o)


def ablation_heatmap(ds, t, by_m):
    ladder = [m for m in LADDER if m in by_m and m != "oasis_nohw"]
    base = by_m["oasis_nohw"]
    queries = sorted({int(q) for q in base})
    cw, ch, L, TOP = 64, 22, 56, 70
    keys, lo = [], 0.0
    for lim, c, k in BINS:
        keys.append((c, f"&lt;{lim:g}" if lo == 0 else (f"&gt;={lo:g}" if lim == math.inf else f"{lo:g}-{lim:g}")))
        lo = lim
    key_w = sum(24 + 6.5 * len(t.replace("&lt;", "<").replace("&gt;", ">")) for _, t in keys)
    W, H = max(L + cw * len(ladder) + 20, L + key_w + 10, 420), TOP + ch * len(queries) + 50
    o = svg_open(W, H, f"Speedup over oasis_nohw, {ds}, {t} threads")
    o.append(f'<text x="{L}" y="36" font-size="11.5" fill="{SOFT}">stages on; median seconds of oasis_nohw / of the stages</text>')
    for j, m in enumerate(ladder):
        lines = wrap(LADDER_NAME[m])
        for k, line in enumerate(lines):
            o.append(f'<text x="{L + (j + 0.5) * cw:.1f}" y="{TOP - 6 - 14 * (len(lines) - 1 - k)}" font-size="11.5" '
                     f'fill="{INK}" text-anchor="middle">{line}</text>')
    for i, q in enumerate(queries):
        yy = TOP + i * ch
        o.append(f'<text x="{L - 8}" y="{yy + ch/2 + 4:.1f}" font-size="11.5" fill="{INK}" text-anchor="end">Q{q}</text>')
        b = base.get(str(q), {}).get("seconds", {}).get("median")
        for j, m in enumerate(ladder):
            s = by_m[m].get(str(q), {}).get("seconds", {}).get("median")
            x = L + j * cw
            if not b or not s:
                o.append(f'<rect x="{x+1}" y="{yy+1}" width="{cw-2}" height="{ch-2}" fill="{SURFACE}" stroke="{GRID}"/>')
                continue
            sp = b / s
            fill, ink = next((c, k) for lim, c, k in BINS if sp < lim)
            o.append(f'<rect x="{x+1}" y="{yy+1}" width="{cw-2}" height="{ch-2}" rx="2" fill="{fill}">'
                     f'<title>Q{q}, {LADDER_NAME[m]}: {s:.3f} s, oasis_nohw {b:.3f} s, {sp:.2f}x</title></rect>')
            o.append(f'<text x="{x + cw/2:.1f}" y="{yy + ch/2 + 4:.1f}" font-size="11" fill="{ink}" '
                     f'text-anchor="middle">{sp:.2f}</text>')
    # key of the bins
    x, yk = L, TOP + ch * len(queries) + 24
    for c, text in keys:
        o.append(f'<rect x="{x}" y="{yk-10}" width="12" height="12" rx="2" fill="{c}" stroke="{GRID}"/>')
        o.append(f'<text x="{x+16}" y="{yk}" font-size="10.5" fill="{SOFT}">{text}</text>')
        x += 24 + 6.5 * len(text.replace("&lt;", "<").replace("&gt;", ">"))
    o.append("</svg>")
    return "\n".join(o)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run", type=Path)
    a = ap.parse_args()
    data = json.loads((a.run / "summary.json").read_text())
    out = a.run / "plots"
    out.mkdir(exist_ok=True)
    charts = {}
    for ds, by_t in data.get("throughput", {}).items():
        main_t = {int(t): {m: r for m, r in by_m.items() if m in MODES and r.get("qph", {}).get("n")}
                  for t, by_m in by_t.items()}
        main_t = {t: by_m for t, by_m in main_t.items() if by_m}
        if len(main_t) == 1:
            t, by_m = next(iter(main_t.items()))
            charts[f"throughput_{ds}.svg"] = throughput_bars(ds, t, by_m)
        elif main_t:
            charts[f"throughput_{ds}.svg"] = throughput_lines(ds, main_t)
        for t, by_m in by_t.items():
            if any(m.startswith("oasis_s") for m in by_m):
                charts[f"ablation_{ds}_t{t}.svg"] = ablation_bars(ds, t, by_m)
    for ds, by_t in data.get("latency", {}).items():
        for t, by_m in by_t.items():
            if any(m in MODES for m in by_m):
                charts[f"latency_{ds}_t{t}.svg"] = latency_bars(ds, t, {m: r for m, r in by_m.items() if m in MODES})
            if "oasis_nohw" in by_m and any(m.startswith("oasis_s") for m in by_m):
                charts[f"ablation_latency_{ds}_t{t}.svg"] = ablation_heatmap(ds, t, by_m)
    for name, svg in charts.items():
        (out / name).write_text(svg + "\n")
        print(f"{out / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
