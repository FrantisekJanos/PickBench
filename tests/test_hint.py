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


if __name__ == "__main__":
    unittest.main()
