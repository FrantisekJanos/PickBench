import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import gpu

LINE = "71, 38.60, 1282, P4, 5200, 93\n"


class Parse(unittest.TestCase):
    def test_full_line(self):
        self.assertEqual(gpu.parse(LINE), {"available": True, "temp_c": 71, "power_w": 38.6, "clock_mhz": 1282,
                                           "pstate": "P4", "mem_mb": 5200, "util_pct": 93})

    def test_not_available_values_become_none(self):
        g = gpu.parse("55, [N/A], [N/A], [N/A], 1024, [Not Supported]\n")
        self.assertEqual(g, {"available": True, "temp_c": 55, "power_w": None, "clock_mhz": None,
                             "pstate": None, "mem_mb": 1024, "util_pct": None})

    def test_bracketed_text_value_becomes_none(self):
        for raw in ("[Not Supported]", "[Unknown Error]"):
            with self.subTest(raw=raw):
                self.assertIsNone(gpu.parse(f"55, 10.0, 300, {raw}, 1024, 5")["pstate"])
        self.assertEqual(gpu.parse("55, 10.0, 300, P4, 1024, 5")["pstate"], "P4")

    def test_first_gpu_wins(self):
        self.assertEqual(gpu.parse("40, 10.00, 300, P8, 100, 0\n" + LINE)["temp_c"], 40)

    def test_unreadable_output_is_unavailable(self):
        for text in ("", "\n\n", "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.\n",
                     "71, 38.60, 1282\n"):
            with self.subTest(text=text):
                self.assertEqual(gpu.parse(text), {"available": False})


class Read(unittest.TestCase):
    def test_uses_the_query_and_two_second_limit(self):
        seen = []

        def run(cmd, timeout):
            seen.append((cmd, timeout))
            return LINE
        self.assertEqual(gpu.read(run)["power_w"], 38.6)
        cmd, timeout = seen[0]
        self.assertEqual(cmd, ["nvidia-smi",
                               "--query-gpu=temperature.gpu,power.draw,clocks.gr,pstate,memory.used,utilization.gpu",
                               "--format=csv,noheader,nounits"])
        self.assertEqual(timeout, 2)

    def test_missing_program_error_and_timeout_are_unavailable(self):
        def missing(cmd, timeout):
            raise FileNotFoundError("nvidia-smi")

        def failed(cmd, timeout):
            raise subprocess.CalledProcessError(9, cmd)

        def slow(cmd, timeout):
            raise subprocess.TimeoutExpired(cmd, timeout)
        for run in (missing, failed, slow):
            with self.subTest(run=run.__name__):
                self.assertEqual(gpu.read(run), {"available": False})

    def test_result_is_a_fresh_dict(self):
        a = gpu.read(lambda cmd, timeout: "")
        a["available"] = True
        self.assertEqual(gpu.read(lambda cmd, timeout: ""), {"available": False})


class Energy(unittest.TestCase):
    def test_constant_power(self):
        self.assertAlmostEqual(gpu.energy_j([(t, 40.0) for t in range(11)]), 400.0)      # 10 s × 40 W

    def test_changing_power_uses_the_average_of_neighbours(self):
        self.assertAlmostEqual(gpu.energy_j([(0, 10), (1, 30), (2, 30), (3, 30), (4, 50)]), 20 + 30 + 30 + 40)

    def test_too_few_samples_is_nan(self):
        e = gpu.energy_j([(t, 40.0) for t in range(4)])
        self.assertTrue(e != e)
        self.assertTrue(gpu.energy_j([]) != gpu.energy_j([]))

    def test_samples_without_power_are_skipped(self):
        samples = [(0, None)] + [(t, 30.0) for t in range(1, 7)]
        self.assertAlmostEqual(gpu.energy_j(samples), 150.0)

    def test_long_gap_is_nan(self):
        e = gpu.energy_j([(0, 40), (1, 40), (2, 40), (3, 40), (20, 40)])
        self.assertTrue(e != e)

    def test_run_span_scales_to_the_whole_run(self):
        samples = [(t, 40.0) for t in range(1, 10)]                    # vzorky v 1.–9. s, běh trval 0–10 s
        self.assertAlmostEqual(gpu.energy_j(samples, 0, 10), 400.0)    # průměrný příkon × celý běh
        self.assertAlmostEqual(gpu.energy_j(samples), 320.0)           # bez zadaného běhu jen mezi vzorky

    def test_samples_not_covering_the_run_are_nan(self):
        samples = [(t, 40.0) for t in range(11)]
        for start, end in ((0, 30), (-20, 10)):                        # vzorky chybí na konci / na začátku běhu
            with self.subTest(start=start, end=end):
                e = gpu.energy_j(samples, start, end)
                self.assertTrue(e != e)


if __name__ == "__main__":
    unittest.main()
