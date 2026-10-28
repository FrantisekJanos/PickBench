"""bench.py --defer: Brierovo skóre a práh jistoty pod řádkem modelu. Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
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

LANG_ENV = "PICKBENCH_LANG"


def setUpModule():
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def replying(replies):
    """Náhrada bench.post: odpovědi (volba, jistota) po řadě, i pro zahřívací dotaz; jistota None = pole chybí."""
    it = iter(replies)

    def post(model, payload):
        key = next(iter(payload["questions"]))
        choice, conf = next(it)
        a = {"choice": choice} if conf is None else {"choice": choice, "confidence": conf}
        return {"answers": {key: a}, "usage": {"input_tokens": 50}}, 10.0
    return post


def numbers_run(confs):
    """Číselné duely, 4 úlohy: správně, špatně, správně, správně; jistoty podle confs."""
    cases = bench.number_cases(4, 3)
    keys = bench.gate_keys(3)
    picks = [keys[top] if ok else keys[(top + 1) % 3] for (_, top, _), ok in zip(cases, (True, False, True, True))]
    with mock.patch.object(bench, "post", replying([("gate_1", 0.5)] + list(zip(picks, confs)))):
        return bench.run_numbers("m", cases, 3)


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Summary(unittest.TestCase):
    def test_measure_keeps_the_pairs_the_brier_score_and_the_tasks(self):
        r = numbers_run([0.9, 0.8, 0.6, None])
        self.assertEqual((r["cal"], r["tasks"]), ([(0.9, True), (0.8, False), (0.6, True)], 4))
        self.assertAlmostEqual(r["brier"], (0.01 + 0.64 + 0.16) / 3)

    def test_noul_pairs_use_the_confidence_of_the_decision(self):
        cases = bench.barrier_cases(4)
        with mock.patch.object(bench, "post", replying([("x", None)] * 5)):     # noul chybí: neplatné odpovědi, žádná jistota
            r = bench.run_barrier("m", cases)
        self.assertEqual((r["cal"], r["tasks"], r["brier"]), ([], 4, None))

    def test_public_keeps_the_pairs(self):
        ds = bench.load_public(ROOT / "tests" / "public-mini.json")
        cases = bench.public_cases(ds)
        post = lambda model, payload: ({"answers": {"label": {"choice": "zzz", "confidence": 0.7}}}, 10.0)
        with mock.patch.object(bench, "post", post):
            r = bench.run_public("m", ds, cases)
        self.assertEqual((len(r["cal"]), r["tasks"], r["cal"][0]), (len(cases), len(cases), (0.7, False)))


class Line(unittest.TestCase):
    def test_line_in_both_languages(self):
        r = numbers_run([0.9, 0.8, 0.6, None])
        cs, en = printed("cs", bench.print_row, r, "", 0.8), printed("en", bench.print_row, r, "", 0.8)
        self.assertEqual(cs[-1], " " * 20 + "práh jistoty 80%: pokrytí 50% (2/4), přesnost nad prahem 50%, předat člověku nebo pravidlům 2; Brier 0.270")
        self.assertEqual(en[-1], " " * 20 + "confidence threshold 80%: coverage 50% (2/4), accuracy above the threshold 50%, "
                                            "hand over to a person or the rules 2; Brier 0.270")
        self.assertEqual(len(printed("cs", bench.print_row, r, "")), len(cs) - 1)        # bez prahu řádek není

    def test_task_without_confidence_is_handed_over(self):
        r = numbers_run([None, None, None, None])
        self.assertEqual(printed("cs", bench.print_row, r, "", 0.5)[-1],
                         " " * 20 + "práh jistoty 50%: pokrytí 0% (0/4), přesnost nad prahem —, předat člověku nebo pravidlům 4; Brier —")


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
        self.assertEqual(bench.parse_args(["--defer", "0.8", "m1"]).defer, 0.8)
        self.assertIsNone(bench.parse_args(["m1"]).defer)

    def test_bad_threshold_in_both_languages(self):
        for value in ("0", "1.5", "-1", "nan"):
            with self.subTest(value=value):
                self.assertEqual(self.cli("--defer", value, "m1"), ("", "--defer chce práh jistoty větší než 0 a nejvýš 1, třeba 0.8."))
                self.assertEqual(self.cli("--defer", value, "m1", "--lang", "en"),
                                 ("", "--defer needs a confidence threshold above 0 and at most 1, such as 0.8."))
        self.assertEqual(self.sent, [])

    def test_line_under_every_model_in_every_scenario(self):
        for argv in (["6", "--defer", "0.8", "m1", "m2"], ["6", "--numbers", "--defer", "0.8", "m1", "m2"],
                     ["6", "--barrier", "--defer", "0.7", "m1", "m2"], ["--public", str(ROOT / "tests" / "public-mini.json"), "--defer", "1", "m1", "m2"]):
            with self.subTest(argv=argv):
                out, code = self.cli(*argv)
                self.assertIsNone(code)
                self.assertEqual(sum(line.strip().startswith("práh jistoty ") for line in out.splitlines()), 2)
        out, _ = self.cli("6", "m1")
        self.assertNotIn("práh jistoty", out)

    def test_line_under_every_model_in_the_later_scenarios(self):
        """Každý scénář tiskne řádek modelu přes print_row, takže řádek prahu má každý, i škála tempomatu (measure_score)."""
        for scenario in (["--signs"], ["--memory"], ["--lookahead"], ["--rule"], ["--queue"], ["--bet"], ["--cruise"], ["--cruise", "--as-score"],
                         ["--timetable"], ["--hint"], ["--diff"], ["--pattern"], ["--tictactoe"], ["--bandit"],
                         ["--emails", str(ROOT / "datasets" / "cz-firemni-emaily.json")]):
            with self.subTest(scenario=scenario):
                out, code = self.cli("6", *scenario, "--defer", "0.8", "m1", "m2")
                self.assertIsNone(code)
                self.assertEqual(sum(line.strip().startswith("práh jistoty ") for line in out.splitlines()), 2)

    def test_usage_names_the_option(self):
        for text in (bench.__doc__, bench.USAGE_EN):
            self.assertIn("\n--defer T:  ", text)


if __name__ == "__main__":
    unittest.main()
