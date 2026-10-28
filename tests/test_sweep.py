"""bench.py --sweep-lanes: průchod 2 až 8 pruhů (bran). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
import contextlib
import io
import os
import runpy
import sys
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import bench
import i18n
from test_golden import Clock, fake_urlopen
from test_bench import recording

LANG_ENV = "PICKBENCH_LANG"

R2 = {"acc": 0.5, "p50": 11.4, "p95": 16.0, "avg": 12.0, "ece": 0.12}
R8 = {"acc": 0.17, "p50": 12.0, "p95": 17.2, "avg": 12.5, "ece": 0.4}


def setUpModule():
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Sweep(unittest.TestCase):
    def test_every_number_of_lanes_gets_the_tasks_of_bench(self):
        post, seen = recording(lambda state: "lane_1")
        with mock.patch.object(bench, "post", post):
            rows = bench.run_sweep("m", 5)
        self.assertEqual([k for k, _ in rows], [2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(len(seen), 7 * 6)                             # zahřívací dotaz a 5 úloh na každý počet pruhů
        for i, k in enumerate(bench.SWEEP_LANES):
            keys = bench.lane_keys(k)
            want = [{"car_is_in": keys[lane], **{key: bench.describe(d) for key, d in zip(keys, dists)}} for lane, dists in bench.make_cases(5, k)]
            self.assertEqual(seen[i * 6 + 1:(i + 1) * 6], want, k)

    def test_numbers_sweep_uses_gates(self):
        post, seen = recording(lambda state: "gate_1")
        with mock.patch.object(bench, "post", post):
            rows = bench.run_sweep("m", 4, numbers=True)
        self.assertEqual(len(rows), 7)
        self.assertEqual(seen[1], dict(zip(bench.gate_keys(2), bench.number_cases(4, 2)[0][0])))
        self.assertEqual(seen[-1], dict(zip(bench.gate_keys(8), bench.number_cases(4, 8)[-1][0])))

    def test_rows_and_trend(self):
        lines = printed("cs", bench.print_sweep, "m1", [(2, R2), (8, R8)])
        self.assertEqual(lines[0].split(), ["m1", "2", "50%", "11ms", "16ms", "83.3", "12%", "#" * 10])
        self.assertEqual(lines[1].split(), ["m1", "8", "17%", "12ms", "17ms", "80.0", "40%", "###"])
        self.assertEqual(lines[2], " " * 20 + "od 2 do 8: správně 50% → 17%, medián 11 → 12 ms")
        en = printed("en", bench.print_sweep, "m1", [(2, R2), (8, R8)])
        self.assertEqual(en[:2], lines[:2])                             # řádky modelu jsou v obou jazycích stejné
        self.assertEqual(en[2], " " * 20 + "from 2 to 8: correct 50% → 17%, median 11 → 12 ms")
        zero = printed("cs", bench.print_sweep, "m1", [(2, {**R2, "acc": 0.0}), (8, R8)])
        self.assertEqual(zero[0], zero[0].rstrip())                     # bez pruhu žádné mezery na konci

    def test_header_has_the_same_width_in_both_languages(self):
        for numbers in (False, True):
            cs = printed("cs", lambda: print(bench.sweep_header(numbers)))[0]
            en = printed("en", lambda: print(bench.sweep_header(numbers)))[0]
            self.assertEqual((len(cs), len(cs.split())), (len(en), len(en.split())))
            self.assertTrue(cs.startswith("model "))
            self.assertIn("bran" if numbers else "pruhů", cs.split())
            self.assertIn("gates" if numbers else "lanes", en.split())


class Cli(unittest.TestCase):
    def setUp(self):
        self.sent = []

    def cli(self, *argv):
        def urlopen(req, timeout=None):
            self.sent.append(req.data)
            return fake_urlopen(req, timeout)
        out, code = io.StringIO(), None
        with mock.patch.object(sys, "argv", ["bench.py", *argv]), mock.patch.object(urllib.request, "urlopen", urlopen), \
                mock.patch.object(time, "perf_counter", Clock()), contextlib.redirect_stdout(out):
            try:
                runpy.run_path(str(ROOT / "bench.py"), run_name="__main__")
            except SystemExit as e:
                code = e.code
        return out.getvalue(), code

    def test_parse(self):
        a = bench.parse_args(["40", "--sweep-lanes", "m1"])
        self.assertEqual((a.n, a.sweep_lanes, a.lanes, a.lanes_given), (40, True, 3, False))
        self.assertFalse(bench.parse_args(["m1"]).sweep_lanes)

    def test_first_line_and_rows(self):
        out, code = self.cli("3", "--sweep-lanes", "m1")
        lines = out.splitlines()
        self.assertIsNone(code)
        self.assertEqual(lines[0], "3 situace pro každý počet pruhů od 2 do 8; sada je pro všechny modely stejná")
        self.assertEqual((lines[1], len(lines)), ("", 3 + 7 + 1))
        en = self.cli("1", "--sweep-lanes", "--numbers", "m1", "--lang", "en")[0].splitlines()
        self.assertEqual(en[0], "1 row of numbers for every number of gates from 2 to 8, the same for all models")
        self.assertTrue(self.cli("--sweep-lanes", "m1")[0].startswith("100 situací pro každý počet pruhů"))     # výchozí počet úloh

    def test_errors_in_both_languages(self):
        scenario = ("Volba --sweep-lanes jde jen s překážkami (bez volby scénáře) nebo s --numbers.",
                    "The --sweep-lanes option works only with obstacles (no scenario option) or with --numbers.")
        combo = ("Volba --sweep-lanes se nekombinuje s --lanes, --mirror, --inject, --deadline-ms ani --defer.",
                 "The --sweep-lanes option cannot be combined with --lanes, --mirror, --inject, --deadline-ms or --defer.")
        for argv, (cs, en) in ((["--sweep-lanes", "--signs", "m1"], scenario), (["--sweep-lanes", "--barrier", "m1"], scenario),
                               (["--sweep-lanes", "--lanes", "4", "m1"], combo), (["--sweep-lanes", "--mirror", "m1"], combo),
                               (["--sweep-lanes", "--inject", "m1"], combo), (["--sweep-lanes", "--deadline-ms", "9", "m1"], combo),
                               (["--sweep-lanes", "--defer", "0.8", "m1"], combo)):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", cs))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", en))
        self.assertEqual(self.sent, [])

    def test_later_scenarios_are_refused_too(self):
        """Scénáře přidané po plánu (a všechny ostatní) stojí v n-tici scenarios, takže je --sweep-lanes odmítne stejnou větou."""
        cs = "Volba --sweep-lanes jde jen s překážkami (bez volby scénáře) nebo s --numbers."
        for scenario in (["--emails", str(ROOT / "datasets" / "cz-firemni-emaily.json")], ["--public", str(ROOT / "tests" / "public-mini.json")],
                         ["--memory"], ["--lookahead"], ["--rule"], ["--queue"], ["--bet"], ["--cruise"], ["--timetable"], ["--hint"],
                         ["--diff"], ["--pattern"], ["--tictactoe"], ["--bandit"]):
            with self.subTest(scenario=scenario):
                self.assertEqual(self.cli("--sweep-lanes", *scenario, "m1"), ("", cs))
        self.assertEqual(self.sent, [])

    def test_usage_names_the_option(self):
        for text in (bench.__doc__, bench.USAGE_EN):
            self.assertIn("python bench.py [N] --sweep-lanes [--numbers] model1 model2 ...", text)
            self.assertIn("python bench.py 50 --sweep-lanes tev1", text)
            self.assertIn("\n--sweep-lanes:  ", text)


if __name__ == "__main__":
    unittest.main()
