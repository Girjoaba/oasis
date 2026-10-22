"""Tests of the scenario runner and of the tpch_q6 scenario files. No simulator needed."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run  # noqa: E402

JSON_OUT = """[{"count_star()":60175}]
[{"revenue":1193053.2253}]
[{"revenue_native":1193053.2253}]
[{"stream":0,"in_handshakes_cycles":7531,"in_throughput_gbps":2.9098285438299927},
{"stream":1,"in_handshakes_cycles":0,"in_throughput_gbps":0.0}]
"""

XSIM_LOG = """179172000: [DEBUG] generator.svh:278: Input file was closed!
179212000: [DEBUG] tb_user.sv:400: Testbench finished!
$finish called at time : 179212 ns : File "tb_user.sv" Line 405
"""

VL_LOG = """3020220000: [DEBUG] tb_user.sv:400: Testbench finished!
- tb_user.sv:405: Verilog $finish
- Verilator: $finish at 3ms; walltime 27.271 s; speed 110.747 us/s
"""


class ParseTest(unittest.TestCase):
    def test_results_and_values(self):
        results = run.parse_results(JSON_OUT)
        self.assertEqual(len(results), 4)
        self.assertEqual(len(results[3]), 2)
        values = run.single_row_values(results)
        self.assertEqual(values["count_star()"], 60175)
        self.assertEqual(values["revenue"], "1193053.2253")  # decimals stay exact strings
        self.assertNotIn("stream", values)

    def test_empty_output(self):
        self.assertEqual(run.parse_results("  \n"), [])
        self.assertEqual(run.single_row_values([]), {})

    def test_simulated_time(self):
        self.assertEqual(run.simulated_ps(XSIM_LOG), 179212000)
        self.assertEqual(run.simulated_ps(VL_LOG), 3020220000)
        self.assertEqual(run.simulated_ps("$finish called at time : 42 ns"), 42000)
        self.assertIsNone(run.simulated_ps("nothing"))


class CheckTest(unittest.TestCase):
    def test_checks(self):
        values = run.single_row_values(run.parse_results(JSON_OUT))
        checks = run.evaluate_checks(
            [{"expect": {"count_star()": 60175}}, {"equal": ["revenue", "revenue_native"]}], values)
        self.assertTrue(all(c["ok"] for c in checks))
        bad = run.evaluate_checks(
            [{"expect": {"count_star()": 1}}, {"equal": ["revenue", "missing"]}, {"what": 1}], values)
        self.assertEqual([c["ok"] for c in bad], [False, False, False])

    def test_numbers_compare_as_decimals(self):
        self.assertTrue(run._same("1193053.2253", 1193053.2253))
        self.assertTrue(run._same("10", 10))
        self.assertFalse(run._same("10", "11"))
        self.assertTrue(run._same("sim", "sim"))


class MockEnvTest(unittest.TestCase):
    def test_env(self):
        env = run.mock_env({"model": "mock", "seed": 7, "ports": {
            "rdma": {"latency_ns": 2000, "gbps": 100, "mtu": 4096, "jitter_ns": 1000, "drop": 0.02},
            "host": {"latency_ns": 1000}}})
        self.assertEqual(env["COYOTE_SIM_SEED"], "7")
        self.assertEqual(env["COYOTE_SIM_RDMA_LATENCY_NS"], "2000")
        self.assertEqual(env["COYOTE_SIM_RDMA_GBPS"], "100")
        self.assertEqual(env["COYOTE_SIM_RDMA_MTU"], "4096")
        self.assertEqual(env["COYOTE_SIM_RDMA_JITTER_NS"], "1000")
        self.assertEqual(env["COYOTE_SIM_RDMA_DROP"], "0.02")
        self.assertEqual(env["COYOTE_SIM_HOST_LATENCY_NS"], "1000")
        self.assertEqual(len(env), 7)

    def test_unknown_names(self):
        with self.assertRaises(ValueError):
            run.mock_env({"model": "rtl"})
        with self.assertRaises(ValueError):
            run.mock_env({"ports": {"wire": {}}})
        with self.assertRaises(ValueError):
            run.mock_env({"ports": {"rdma": {"bandwidth": 1}}})

    def test_names_match_sim_params(self):
        """The parameter names are the ones the Coyote mocks read (sim_params.svh of the submodule)."""
        sim_params = run.OASIS / "parcore" / "libstf" / "coyote" / "sim" / "hw" / "sim_params.svh"
        if not sim_params.is_file():
            self.skipTest(f"{sim_params} is missing: submodules not checked out")
        text = sim_params.read_text()
        self.assertIn("COYOTE_SIM_SEED", text)
        for name in run.PARAMS:
            self.assertIn(f"_{name.upper()}", text)


class ScenarioFilesTest(unittest.TestCase):
    def test_tpch_q6_files(self):
        folder = run.OASIS / "scenarios" / "tpch_q6"
        for name in ("scenario.json", "scenario_100g.json", "scenario_10g.json"):
            scenario = json.loads((folder / name).read_text())
            run.mock_env(scenario["network"])
            self.assertTrue((folder / scenario["sql"]).is_file())


if __name__ == "__main__":
    unittest.main()
