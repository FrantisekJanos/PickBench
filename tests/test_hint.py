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


if __name__ == "__main__":
    unittest.main()
