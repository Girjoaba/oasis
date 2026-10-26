#!/usr/bin/env python3
"""Run one scenario of the DuckDB extension against the Coyote simulation and write result.json.

Usage: run.py <scenario.json> --sim-dir <hw build> [--duckdb <shell>] [--verilator-bin <coyote_vl>]
               [--vfpga-top <svh>] [--out <folder>] [--timeout <s>]

The scenario names the network model and its port timing, the memory image, the SQL file and
the checks (format: README.md next to this file). The runner sets the simulator variables,
starts the DuckDB shell on the SQL file, parses the results, applies the checks and writes
result.json next to the logs. Without --verilator-bin the run uses Vivado xsim.
"""

import argparse
import json
import os
import re
import resource
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

OASIS = Path(__file__).resolve().parents[1]
DEFAULT_DUCKDB = OASIS / "extension" / "build" / "release" / "duckdb"
DEFAULT_VFPGA_TOP = OASIS / "hardware" / "src" / "vfpga_top.svh"

# Network model `mock`: the port timing of the sim/hw mocks of Coyote. The variable of a
# parameter is COYOTE_SIM_<PORT>_<PARAM>, read in SIM_PARAMS of the Coyote submodule.
# This is the only place that names these variables; the scenario file never does.
PORTS = ("host", "card", "rdma")
PARAMS = ("latency_ns", "gbps", "mtu", "jitter_ns", "drop", "retransmit_ns")
SIM_PARAMS = OASIS / "parcore" / "libstf" / "coyote" / "sim" / "hw" / "sim_params.svh"

SIM_LOGS = {
    "xsim": "sim/*.sim/sim_1/behav/xsim/simulate.log",
    "verilator": "sim/coyote_vl.log",
}
FINISHED_RE = re.compile(r"^(\d+): \[DEBUG\] tb_user\.sv:\d+: Testbench finished!", re.M)
XSIM_FINISH_RE = re.compile(r"\$finish called at time : (\d+) ns")
LOG_TAIL_BYTES = 64 * 1024  # the finish lines are the last ones of a simulator log


class Scenario(NamedTuple):
    name: str
    network: dict
    checks: List[dict]
    sql: Path
    image: Optional[Path]
    files: List[Path]


def mock_env(network: dict) -> Dict[str, str]:
    """Environment variables for the `network` section of a scenario."""
    model = network.get("model", "mock")
    if model != "mock":
        raise ValueError(f"network model {model!r} is not implemented, only 'mock'")
    env = {}
    if "seed" in network:
        env["COYOTE_SIM_SEED"] = str(network["seed"])
    for port, params in network.get("ports", {}).items():
        if port not in PORTS:
            raise ValueError(f"unknown port {port!r}, expected one of {sorted(PORTS)}")
        for name, value in params.items():
            if name not in PARAMS:
                raise ValueError(f"unknown parameter {name!r} of port {port!r}, expected one of {sorted(PARAMS)}")
            env[f"COYOTE_SIM_{port.upper()}_{name.upper()}"] = str(value)
    # The mocks build the names as "COYOTE_SIM_" + port + "_<PARAM>"; a rename there must fail here.
    mocks = SIM_PARAMS.read_text()
    for var in env:
        if (var if var == "COYOTE_SIM_SEED" else "_" + var.split("_", 3)[3]) not in mocks:
            raise ValueError(f"{var} is not read by {SIM_PARAMS}: PARAMS of run.py drifted from the Coyote mocks")
    return env


def load_scenario(path: Path) -> Scenario:
    raw = json.loads(path.read_text())
    for key in ("name", "network", "sql"):
        if key not in raw:
            raise ValueError(f"{path}: missing field {key!r}")
    mock_env(raw["network"])  # rejects a bad network section before anything runs
    base = path.parent
    scenario = Scenario(
        name=raw["name"],
        network=raw["network"],
        checks=raw.get("checks", []),
        sql=(base / raw["sql"]).resolve(),
        image=(base / raw["image"]).resolve() if "image" in raw else None,
        files=[(base / f).resolve() for f in raw.get("files", [])],
    )
    for f in [scenario.sql, scenario.image, *scenario.files]:
        if f is not None and not f.is_file():
            raise FileNotFoundError(f"{path}: {f} does not exist (run prepare.py first?)")
    return scenario


def parse_results(text: str) -> List[List[dict]]:
    """The arrays that `duckdb -json` prints, one per statement with a result."""
    decoder = json.JSONDecoder(parse_float=str)
    results, pos = [], 0
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return results
        value, pos = decoder.raw_decode(text, pos)
        results.append(value if isinstance(value, list) else [value])


def single_row_values(results: List[List[dict]]) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    for rows in results:
        if len(rows) == 1 and isinstance(rows[0], dict):
            values.update(rows[0])
    return values


def _same(a: Any, b: Any) -> bool:
    """Numbers compare as decimals (DuckDB prints them, the scenario holds them as JSON numbers)."""
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (InvalidOperation, ValueError):
        return a == b


def evaluate_checks(checks: List[dict], values: Dict[str, Any]) -> List[dict]:
    out = []
    for check in checks:
        if "equal" in check:
            keys = check["equal"]
            got = [values.get(k) for k in keys]
            ok = all(k in values for k in keys) and all(_same(got[0], g) for g in got[1:])
            out.append({"equal": keys, "got": got, "ok": ok})
        elif "expect" in check:
            got = {k: values.get(k) for k in check["expect"]}
            ok = all(k in values and _same(values[k], v) for k, v in check["expect"].items())
            out.append({"expect": check["expect"], "got": got, "ok": ok})
        else:
            out.append({"unknown": check, "ok": False})
    return out


def simulated_ps(log_text: str) -> Optional[int]:
    m = FINISHED_RE.search(log_text)
    if m:
        return int(m.group(1))
    m = XSIM_FINISH_RE.search(log_text)
    if m:
        return int(m.group(1)) * 1000
    return None


def find_sim_log(sim_dir: Path, simulator: str) -> Optional[Path]:
    hits = sorted(sim_dir.glob(SIM_LOGS[simulator]))
    return hits[0] if hits else None


def read_tail(path: Path, size: int = LOG_TAIL_BYTES) -> str:
    with open(path, "rb") as f:
        f.seek(max(0, path.stat().st_size - size))
        return f.read().decode(errors="replace")


def build_env(scenario: Scenario, sim_dir: Path, verilator_bin: Optional[Path]) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("COYOTE_SIM_")}
    env.update(mock_env(scenario.network))
    env["COYOTE_SIM_DIR"] = str(sim_dir)
    env["COYOTE_SIM_VCD"] = "0"
    if scenario.image is not None:
        env["COYOTE_SIM_RDMA_IMAGE"] = str(scenario.image)
    if verilator_bin is not None:
        env["COYOTE_SIM_BIN"] = str(verilator_bin)
    return env


def place_vfpga_top(source: Path, sim_dir: Path, verilator_bin: Optional[Path]) -> Optional[str]:
    """Puts the top into the build folder (unchanged mtime when the content is the same). Returns a warning or None.

    The same rule as _copy_if_changed in sim/unit_test/process_runner.py of the Coyote submodule;
    a copy, because this script runs without the Python test framework of a hardware build on sys.path.
    """
    target = sim_dir / "sim" / "vfpga_top.svh"
    content = source.read_text()
    if not target.is_file() or target.read_text() != content:
        target.write_text(content)
    if verilator_bin is not None and verilator_bin.stat().st_mtime < target.stat().st_mtime:
        return f"{verilator_bin} is older than {target}: rebuild with sim/vl/build.sh"
    return None


def run_shell(duckdb: Path, timeout: float, env: Dict[str, str], run_dir: Path, sql: Path) -> Tuple[int, float, bool]:
    """Return (rc, wall seconds, timed out)."""
    cmd = [str(duckdb), "-json", "-no-agent"]
    start = time.monotonic()
    timed_out = False
    with open(sql, "rb") as stdin, open(run_dir / "duckdb.out", "wb") as out, open(run_dir / "duckdb.err", "wb") as err:
        proc = subprocess.Popen(cmd, stdin=stdin, stdout=out, stderr=err, cwd=run_dir, env=env, start_new_session=True)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, 9)
            rc = proc.wait()
    return rc, time.monotonic() - start, timed_out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("scenario", type=Path)
    p.add_argument("--sim-dir", required=True, type=Path, help="hardware build folder (COYOTE_SIM_DIR)")
    p.add_argument("--duckdb", default=DEFAULT_DUCKDB,
                   help=f"DuckDB shell built with the extension and EN_SIMULATION (default {DEFAULT_DUCKDB})")
    p.add_argument("--verilator-bin", help="coyote_vl binary; without it the run uses Vivado xsim")
    p.add_argument("--vfpga-top", type=Path, default=DEFAULT_VFPGA_TOP,
                   help=f"vfpga_top.svh placed into <sim-dir>/sim before the run (default {DEFAULT_VFPGA_TOP})")
    p.add_argument("--out", type=Path, help="run folder (default runs/<name>_<time>)")
    p.add_argument("--timeout", type=float, default=7200, help="seconds before the shell is killed (default 7200)")
    args = p.parse_args(argv)

    scenario_file = args.scenario.resolve()
    scenario = load_scenario(scenario_file)
    sim_dir = args.sim_dir.resolve()
    duckdb = Path(args.duckdb).resolve()
    verilator_bin = Path(args.verilator_bin).resolve() if args.verilator_bin else None
    simulator = "verilator" if verilator_bin else "xsim"
    started = datetime.now()
    run_dir = args.out or Path("runs") / f"{scenario.name}_{started:%Y%m%d_%H%M%S}"
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    for f in scenario.files:
        link = run_dir / f.name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(f)

    warnings: List[str] = []
    warning = place_vfpga_top(args.vfpga_top, sim_dir, verilator_bin)
    if warning:
        warnings.append(warning)
    env = build_env(scenario, sim_dir, verilator_bin)
    rc, wall, timed_out = run_shell(duckdb, args.timeout, env, run_dir, scenario.sql)
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)

    sim_log = find_sim_log(sim_dir, simulator)
    simulated = None
    if sim_log is not None:
        shutil.copyfile(sim_log, run_dir / sim_log.name)
        simulated = simulated_ps(read_tail(sim_log))
    shutil.rmtree(run_dir / ".Xil", ignore_errors=True)

    text = (run_dir / "duckdb.out").read_text(errors="replace")
    try:
        results = parse_results(text)
    except json.JSONDecodeError as e:
        results = []
        warnings.append(f"duckdb.out is not JSON: {e}")
    values = single_row_values(results)
    checks = evaluate_checks(scenario.checks, values)
    passed = rc == 0 and not timed_out and all(c["ok"] for c in checks)

    result = {
        "scenario": scenario.name,
        "scenario_file": str(scenario_file),
        "network": scenario.network,
        "simulator": simulator,
        "sim_dir": str(sim_dir),
        "duckdb": str(duckdb),
        "host": socket.gethostname(),
        "started": started.isoformat(timespec="seconds"),
        "pass": passed,
        "rc": rc,
        "timed_out": timed_out,
        "checks": checks,
        "values": values,
        "results": results,
        "simulated_ns": simulated / 1000 if simulated is not None else None,
        "wall_s": round(wall, 1),
        "cpu_user_s": round(usage.ru_utime, 1),
        "cpu_sys_s": round(usage.ru_stime, 1),
        "max_rss_kb": usage.ru_maxrss,
        "env": {k: v for k, v in env.items() if k.startswith("COYOTE_SIM_")},
        "logs": sorted(p.name for p in run_dir.iterdir() if p.is_file() and p.name != "result.json"),
        "warnings": warnings,
    }
    (run_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    sim_us = f"{simulated / 1e6:.1f}" if simulated is not None else "?"
    print(f"{scenario.name} {simulator} pass={passed} rc={rc} values={json.dumps(values)} "
          f"simulated_us={sim_us} wall_s={wall:.0f} out={run_dir}")
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, FileNotFoundError) as e:
        print(f"run.py: {e}", file=sys.stderr)
        sys.exit(2)
