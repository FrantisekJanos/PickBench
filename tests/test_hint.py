"""Scénář Rušivá nápověda (bench.py --hint). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
import contextlib
import io
import json
import os
import runpy
import sys
import time
import unittest
import urllib.request
from collections import Counter
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import bench
import i18n
from test_bench import fake_post, largest_gate
from test_golden import Clock, fake_urlopen

LANG_ENV = "PICKBENCH_LANG"
KEYS3 = ["gate_1", "gate_2", "gate_3"]
ROW_A, ROW_B = (10, 30, 20), (100, 98, 5)          # správně gate_2 (jasné), gate_1 (těsné)
CASES = [(ROW_A, 1, "clear", "none", None, 0), (ROW_A, 1, "clear", "congruent", 1, 0), (ROW_A, 1, "clear", "incongruent", 2, 0),
         (ROW_B, 0, "tight", "incongruent", 1, 1), (ROW_B, 0, "tight", "none", None, 1), (ROW_B, 0, "tight", "congruent", 0, 1)]
CACHE = {}


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def cases(n=900, lanes=3):
    if (n, lanes) not in CACHE:
        CACHE[n, lanes] = bench.hint_cases(n, lanes)
    return CACHE[n, lanes]


def follower(state):
    """Model, který jde za nápovědou; bez ní volí největší číslo."""
    return state.get(bench.HINT_FIELD) or largest_gate(state)


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Generator(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(bench.HINT_CONDS, ("none", "congruent", "incongruent"))
        self.assertEqual(bench.HINT_FIELD, "most_popular")

    def test_every_row_comes_three_times(self):
        for lanes in (2, 3, 4, 8):
            c = cases(900, lanes)
            with self.subTest(lanes=lanes):
                self.assertEqual(len(c), 2700)
                self.assertEqual(Counter(x[3] for x in c), {"none": 900, "congruent": 900, "incongruent": 900})
                rows = {}
                for values, answer, gap, cond, hint, row in c:
                    rows.setdefault(row, []).append((values, answer, gap, cond, hint))
                    self.assertIsInstance(values, tuple)
                    self.assertEqual(len(values), lanes)
                    self.assertEqual(values.count(max(values)), 1)
                    self.assertEqual(values.index(max(values)), answer)
                    if cond == "none":
                        self.assertIsNone(hint)
                    elif cond == "congruent":
                        self.assertEqual(hint, answer)
                    else:
                        self.assertTrue(0 <= hint < lanes and hint != answer)
                self.assertEqual(sorted(rows), list(range(900)))
                for trio in rows.values():
                    self.assertEqual(sorted(x[3] for x in trio), sorted(bench.HINT_CONDS))
                    self.assertEqual(len({x[:3] for x in trio}), 1)                 # stejná čísla, správná brána i třída
                numbers = [(list(x[0]), x[1], x[2]) for x in c if x[3] == "none"]
                self.assertEqual(sorted(numbers, key=str), sorted(bench.number_cases(900, lanes), key=str))    # řady jako --numbers

    def test_answers_gaps_and_wrong_hints_are_spread(self):
        c = cases(900, 3)
        none = [x for x in c if x[3] == "none"]
        answers, gaps = Counter(x[1] for x in none), Counter(x[2] for x in none)
        for gate in range(3):
            self.assertAlmostEqual(answers[gate] / 900, 1 / 3, delta=0.07)
        for gap in bench.GAPS:
            self.assertTrue(0.25 <= gaps[gap] / 900 <= 0.42, gaps)
        inc = [x for x in c if x[3] == "incongruent"]
        second = sum(x[0][x[4]] == sorted(x[0])[-2] for x in inc) / len(inc)
        self.assertTrue(0.4 <= second <= 0.6, second)

    def test_control_strategies(self):
        c = cases(900, 3)
        share = lambda pick, cond=None: (lambda sub: sum(pick(x) == x[1] for x in sub) / len(sub))(
            [x for x in c if cond is None or x[3] == cond])
        follow = lambda x: 0 if x[4] is None else x[4]
        anti = lambda x: 1 if x[4] == 0 else 0                             # nejnižší brána mimo nápovědu
        self.assertEqual((share(follow, "congruent"), share(follow, "incongruent")), (1, 0))
        self.assertTrue(0.4 <= share(anti, "incongruent") <= 0.6)
        self.assertLess(share(anti), 0.34)
        self.assertLess(share(lambda x: 0), 0.4)

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.hint_cases(30), bench.hint_cases(30, 3, seed=1))
        self.assertNotEqual(bench.hint_cases(30), bench.hint_cases(30, 3, seed=2))
        self.assertNotEqual(bench.hint_cases(30), bench.hint_cases(30, 4))

    def test_row_without_a_unique_largest_number_raises(self):
        with mock.patch.object(bench, "number_cases", lambda n, lanes, seed: [([50, 50, 1], 0, "tight")]), self.assertRaises(RuntimeError):
            bench.hint_cases(1)


class StateAndQuestion(unittest.TestCase):
    def test_state_without_a_hint_is_the_numbers_state(self):
        for values in ((10, 30, 20), (1, 2), tuple(range(1, 9))):
            with self.subTest(values=values):
                self.assertEqual(json.dumps(bench.hint_state(values, None)), json.dumps(dict(zip(bench.gate_keys(len(values)), values))))

    def test_hint_is_the_last_key(self):
        self.assertEqual(json.dumps(bench.hint_state((4120, 3987, 1203), 1)),
                         '{"gate_1": 4120, "gate_2": 3987, "gate_3": 1203, "most_popular": "gate_2"}')

    def test_question_is_the_numbers_question(self):
        for lanes in range(2, 9):
            with self.subTest(lanes=lanes):
                q = bench.hint_question(lanes)
                self.assertEqual(json.dumps(q), json.dumps(bench.number_question(lanes)))
                self.assertEqual(bench.variant(q, 2)["largest"]["instructions"], bench.ALT["largest"])
                self.assertEqual(list(bench.variant(q, 1)["largest"]["criteria"]), bench.gate_keys(lanes)[::-1])


class Summary(unittest.TestCase):
    def test_perfect_model(self):
        s = bench.hint_summary(CASES, [KEYS3[c[1]] for c in CASES], KEYS3)
        self.assertEqual(s["by_cond"], {"none": [2, 2], "congruent": [2, 2], "incongruent": [2, 2]})
        self.assertEqual(s["by_gap"], {"tight": [1, 1], "medium": [0, 0], "clear": [1, 1]})
        self.assertEqual((s["help"], s["hurt"], s["pull"], s["flipped"], s["pairs"]), (0, 0, 0, 0, 2))
        self.assertEqual(s["errors"], {"hint": 0, "other": 0, "invalid": 0})

    def test_hint_follower(self):                                         # nápověda, jinak gate_1
        s = bench.hint_summary(CASES, ["gate_1", "gate_2", "gate_3", "gate_2", "gate_1", "gate_1"], KEYS3)
        self.assertEqual(s["by_cond"], {"none": [1, 2], "congruent": [2, 2], "incongruent": [0, 2]})
        self.assertEqual((s["help"], s["hurt"], s["pull"], s["flipped"], s["pairs"]), (0.5, 0.5, 1, 1, 2))
        self.assertEqual(s["errors"], {"hint": 2, "other": 0, "invalid": 0})

    def test_hint_taken_for_a_decoy(self):                                # největší číslo mimo bránu nápovědy
        s = bench.hint_summary(CASES, ["gate_2", "gate_3", "gate_2", "gate_1", "gate_1", "gate_2"], KEYS3)
        self.assertEqual((s["help"], s["hurt"], s["pull"], s["flipped"]), (-1, 0, 0, 0))
        self.assertEqual(s["errors"], {"hint": 0, "other": 0, "invalid": 0})

    def test_invalid_answers(self):
        for bad in (None, "gate_9", "lane_1", 5, ["gate_1"]):
            with self.subTest(bad=bad):
                choices = [bad if c[3] == "incongruent" else KEYS3[c[1]] for c in CASES]
                s = bench.hint_summary(CASES, choices, KEYS3)
                self.assertEqual(s["errors"], {"hint": 0, "other": 0, "invalid": 2})
                self.assertEqual((s["hurt"], s["pull"], s["flipped"]), (1, 0, 0))
                self.assertEqual(s["by_gap"], {"tight": [0, 1], "medium": [0, 0], "clear": [0, 1]})


class RunHint(unittest.TestCase):
    def run_with(self, answer, *args, **kw):
        with mock.patch.object(bench, "post", fake_post(answer)):
            return bench.run_hint(*args, **kw)

    def test_perfect_model(self):
        r = self.run_with(largest_gate, "m", cases(40), 3)
        self.assertEqual(r["acc"], 1)
        self.assertEqual(r["by_cond"], {"none": [40, 40], "congruent": [40, 40], "incongruent": [40, 40]})
        self.assertEqual((r["help"], r["hurt"], r["pull"], r["flipped"], r["pairs"]), (0, 0, 0, 0, 40))
        self.assertEqual(sum(n for _, n in r["by_gap"].values()), 40)

    def test_hint_follower(self):
        r = self.run_with(follower, "m", cases(40), 3)
        self.assertEqual(r["by_cond"], {"none": [40, 40], "congruent": [40, 40], "incongruent": [0, 40]})
        self.assertEqual((r["help"], r["hurt"], r["pull"], r["flipped"]), (0, 1, 1, 40))
        self.assertEqual(r["errors"], {"hint": 40, "other": 0, "invalid": 0})
        self.assertAlmostEqual(r["acc"], 2 / 3)

    def test_requests_carry_the_state_and_the_question(self):
        c, sent = bench.hint_cases(5, 6), []

        def post(model, payload):
            sent.append(payload)
            return {"answers": {next(iter(payload["questions"])): {"choice": "gate_1", "confidence": 0.5}}}, 10.0
        with mock.patch.object(bench, "post", post):
            bench.run_hint("m", c, 6)
        self.assertEqual(len(sent), 16)                                   # zahřátí a 3 × 5 úloh
        self.assertEqual([p["state"] for p in sent], [bench.hint_state(c[0][0], None)] + [bench.hint_state(x[0], x[4]) for x in c])
        self.assertTrue(all(p["questions"] == bench.number_question(6) for p in sent))
        self.assertEqual(sum(bench.HINT_FIELD in p["state"] for p in sent), 10)

    def test_mirror(self):
        r = self.run_with(largest_gate, "m", cases(20), 3, mirror=True)
        self.assertEqual((r["stab"], r["variant_acc"]), (1, [1, 1, 1]))
        self.assertEqual(r["by_cond"]["incongruent"], [20, 20])


class Lines(unittest.TestCase):
    def test_note_by_condition(self):
        r = {"by_cond": {"none": [10, 12], "congruent": [12, 12], "incongruent": [3, 12]}}
        for lang, want in (("cs", "(bez nápovědy 10/12, shodná 12/12, konfliktní 3/12)"),
                           ("en", "(no hint 10/12, congruent 12/12, incongruent 3/12)")):
            with i18n.process(lang):
                self.assertEqual(bench.hint_note(r), want)

    def test_lines_in_both_languages(self):
        r = {"help": 0.12, "hurt": -0.034, "pull": 0.0, "errors": {"hint": 7, "other": 2, "invalid": 1}, "flipped": 5, "pairs": 100,
             "by_gap": {"tight": [3, 30], "medium": [20, 35], "clear": [33, 35]}}
        self.assertEqual(printed("cs", bench.print_hint, r), [
            " " * 20 + "nápověda: pomoc +12 pb, škoda -3 pb, tah +0 pb; konfliktní: chyby na nápovědu 7, jiné 2, neplatné 1; převráceno 5/100",
            " " * 20 + "konfliktní podle rozdílu: těsné 3/30, střední 20/35, jasné 33/35"])
        self.assertEqual(printed("en", bench.print_hint, r), [
            " " * 20 + "hint: help +12 pp, harm -3 pp, pull +0 pp; incongruent: errors on the hint 7, other 2, invalid 1; flipped 5/100",
            " " * 20 + "incongruent by gap: narrow 3/30, medium 20/35, clear 33/35"])

    def test_points_keep_the_sign(self):
        self.assertEqual([bench.pp(x) for x in (0.5, -1, 0.0, -0.004, 0.004, 0.125, float("nan"), None)],
                         ["+50", "-100", "+0", "+0", "+0", "+12", "—", "—"])


class Cli(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)                            # jazyk z prostředí vývojáře tu nesmí rozhodovat
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(LANG_ENV, None)
        self.sent = []

    def cli(self, *argv):
        """Pustí bench.py jako příkaz proti podstrčené Ollamě. Vrací (výstup, čím skončil: None nebo argument sys.exit)."""
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
        a = bench.parse_args(["40", "--hint", "m1"])
        self.assertEqual((a.n, a.hint, a.lanes, a.models), (40, True, 3, ["m1"]))
        self.assertEqual(bench.parse_args(["--hint", "--lanes", "2", "m1"]).lanes, 2)

    def test_first_line_and_rows(self):
        cs_tail = " čísel, každá třikrát (bez nápovědy, nápověda na správnou a na špatnou bránu), "
        en_tail = " of numbers, each asked three times (no hint, hint on the right gate, hint on a wrong gate), "
        for argv, cs, en in ((["1", "--hint", "m1"], "1 řada" + cs_tail + "3 brány", "1 row" + en_tail + "3 gates"),
                             (["7", "--hint", "--lanes", "5", "m1"], "7 řad" + cs_tail + "5 bran", "7 rows" + en_tail + "5 gates"),
                             (["2", "--lanes", "2", "--hint", "m1"], "2 řady" + cs_tail + "2 brány", "2 rows" + en_tail + "2 gates")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[0].splitlines()[0], cs + "; sada je pro všechny modely stejná")
                self.assertEqual(self.cli(*argv, "--lang", "en")[0].splitlines()[0], en + ", the same for all models")
        self.assertTrue(self.cli("--hint", "m1")[0].startswith("100 řad čísel"))
        self.sent.clear()
        en = self.cli("6", "--hint", "m1", "--lang", "en")[0].splitlines()
        self.assertEqual(len(self.sent), 1 + 18)                         # zahřátí a 3 × 6 dotazů
        self.assertEqual(len(en), 6)
        self.assertRegex(en[3], r"^m1 .*   \(no hint \d+/6, congruent \d+/6, incongruent \d+/6\)$")
        self.assertRegex(en[4], r"^ {20}hint: help [+-]\d+ pp, harm [+-]\d+ pp, pull [+-]\d+ pp; incongruent: errors on the hint \d+, "
                                r"other \d+, invalid \d+; flipped \d+/6$")
        self.assertRegex(en[5], r"^ {20}incongruent by gap: narrow \d+/\d+, medium \d+/\d+, clear \d+/\d+$")

    def test_option_errors_in_both_languages(self):
        for argv, cs, en in ((["--hint", "--inject", "m1"], "Volba --inject se s --hint nekombinuje.",
                              "The --inject option cannot be combined with --hint."),
                             (["--hint", "--mirror", "--inject", "m1"], "Volba --inject se s --hint nekombinuje.",
                              "The --inject option cannot be combined with --hint."),
                             (["--hint", "--weighted", "m1"], "Volba --weighted jde jen s --queue.", "The --weighted option works only with --queue.")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", cs))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", en))
        for argv in (["--hint", "--numbers", "m1"], ["--hint", "--bet", "m1"], ["--hint", "--cruise", "m1"], ["--hint", "--timetable", "m1"],
                     ["--hint", "--lanes", "1", "m1"],
                     ["--hint", "--lanes", "9", "m1"], ["--hint"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", bench.__doc__))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", bench.USAGE_EN))
        self.assertEqual(self.sent, [])

    def test_mirror_works_with_hint(self):
        out, code = self.cli("4", "--hint", "--mirror", "--deadline-ms", "9", "m1")
        self.assertIsNone(code)
        lines = out.splitlines()
        self.assertIn("stabilita", lines[2])
        self.assertTrue(lines[4].strip().startswith("přesnost variant: "), lines[4])
        self.assertTrue(lines[5].strip().startswith("nápověda: pomoc "), lines[5])
        self.assertTrue(lines[6].strip().startswith("konfliktní podle rozdílu: "), lines[6])
        self.assertEqual(len(self.sent), 1 + 3 * 12)                     # zahřátí a 12 úloh ve třech variantách

    def test_usage_names_the_options(self):
        for text in (bench.__doc__, bench.USAGE_EN):
            self.assertIn("python bench.py [N] --hint [--lanes K] [--mirror] [--deadline-ms L] model1 model2 ...", text)
            self.assertIn("python bench.py 100 --hint tev1", text)
            self.assertIn("\n--hint:     ", text)


if __name__ == "__main__":
    unittest.main()
