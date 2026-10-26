"""Scénář Dva kroky dopředu (bench.py --lookahead). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
import contextlib
import io
import json
import os
import random
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
from test_bench import fake_post, obedient, recording
from test_golden import Clock, fake_urlopen

LANG_ENV = "PICKBENCH_LANG"
EXAMPLE = (".#..", "..#.", "###.", "....")           # příklad z rešerše: auto v pruhu 2 (index 1); správně right, hladový left
MOVES = ("left", "stay", "right")
CACHE = {}


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def cases(n=900, lanes=4):
    """Úlohy ze semínka 1; velké sady se losují jednou za běh modulu."""
    if (n, lanes) not in CACHE:
        CACHE[n, lanes] = bench.lookahead_cases(n, lanes)
    return CACHE[n, lanes]


def rows_of(state, lanes):
    """Řady ze stavu v obou podobách jako řetězce '.'/'#'."""
    if "rows_ahead" in state:
        return tuple(state["rows_ahead"])
    return tuple("".join("#" if i + 1 in row else "." for i in range(lanes)) for row in state["blocked_lanes_ahead"])


def solver(lanes):
    """Model, který v obou podobách stavu odpoví správně: spočítá hodnoty tahů."""
    def answer(state):
        values = bench.lookahead_values(rows_of(state, lanes), state["car_lane"] - 1)
        return max(MOVES, key=values.get)
    return answer


def greedy_model(lanes):
    return lambda state: bench.lookahead_greedy(rows_of(state, lanes), state["car_lane"] - 1)


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Search(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(bench.LOOKAHEAD_MOVES, MOVES)
        self.assertEqual(bench.LOOKAHEAD_SHIFT, {"left": -1, "stay": 0, "right": 1})
        self.assertEqual((bench.LOOKAHEAD_ROWS, bench.LOOKAHEAD_DENSITY, bench.LOOKAHEAD_TRIES), (4, 0.4, 100000))
        self.assertEqual(bench.LOOKAHEAD_CLASSES, ("d1", "d2", "d3+"))
        self.assertEqual(bench.LOOKAHEAD_FORMS, ("rows", "blocked"))
        self.assertIn(bench.LOOKAHEAD_FORM, bench.LOOKAHEAD_FORMS)

    def test_research_example(self):
        self.assertEqual(bench.lookahead_survive(EXAMPLE, 1), 4)
        self.assertEqual(bench.lookahead_values(EXAMPLE, 1), {"left": 2, "stay": 0, "right": 4})
        self.assertEqual(list(bench.lookahead_values(EXAMPLE, 1)), list(MOVES))
        self.assertEqual(bench.lookahead_values(EXAMPLE, 1, 1), {"left": 1, "stay": 0, "right": 1})
        self.assertEqual(bench.lookahead_values(EXAMPLE, 1, 2), {"left": 2, "stay": 0, "right": 2})
        self.assertEqual(bench.lookahead_greedy(EXAMPLE, 1), "left")       # pruh 1 má volné dvě řady, pruh 3 jen jednu
        self.assertEqual(bench.lookahead_depth(EXAMPLE, 1), ("right", 3))
        self.assertEqual(bench.lookahead_class(3), "d3+")

    def test_hand_grids(self):
        self.assertEqual(bench.lookahead_survive(("###", "..."), 1), 0)
        self.assertEqual(bench.lookahead_survive(("#..", "..#"), 0), 2)   # z pruhu 1 jen do pruhu 2, pak kamkoli kromě 3
        d1 = ("#.#", "...")                                                 # jediný tah do volného pole
        self.assertEqual(bench.lookahead_values(d1, 1), {"left": 0, "stay": 2, "right": 0})
        self.assertEqual(bench.lookahead_depth(d1, 1), ("stay", 1))
        d2 = ("..#", "##.")                                                 # stay i right projedou první řadu, druhou jen right
        self.assertEqual(bench.lookahead_values(d2, 0), {"left": 0, "stay": 1, "right": 2})
        self.assertEqual(bench.lookahead_depth(d2, 0), ("right", 2))
        self.assertEqual(bench.lookahead_greedy(d2, 0), "stay")            # obě dráhy rovně mají délku 1: shoda → stay
        self.assertEqual(bench.lookahead_depth(("...",), 1), (None, None))  # tři stejně dobré tahy
        self.assertEqual([bench.lookahead_class(d) for d in (1, 2, 3, 4)], ["d1", "d2", "d3+", "d3+"])

    def test_edges_of_the_road(self):
        rows = ("...", "...")
        self.assertEqual(bench.lookahead_values(rows, 0), {"left": 0, "stay": 2, "right": 2})      # vlevo z pruhu 1 = mimo silnici
        self.assertEqual(bench.lookahead_values(rows, 2), {"left": 2, "stay": 2, "right": 0})
        self.assertEqual(bench.lookahead_greedy(rows, 0), "stay")
        self.assertEqual(bench.lookahead_greedy((".#.", "..."), 1), "left")                    # stay má 0, left a right shodně 2
        self.assertEqual(bench.lookahead_greedy(("..#", "..#"), 2), "left")                    # right je mimo silnici

    def test_density_and_center(self):
        rows = ("....", "##..", "##..", "....")                         # pruhy 3 a 4 volnější
        self.assertEqual(bench.lookahead_density(rows, 1), "right")     # stay 2+2+4, left 2+2, right 2+4+4
        self.assertEqual(bench.lookahead_density(rows, 0), "right")     # left mimo silnici, stay 2+2, right 2+2+4
        self.assertEqual(bench.lookahead_density(("...", "...", "...", "..."), 1), "stay")    # shoda → stay
        self.assertEqual(bench.lookahead_density((".#.", "...", "#..", "..."), 1), "right")   # stay do '#' se nepočítá
        self.assertEqual(bench.lookahead_density((".#.", "...", "...", "..."), 1), "left")    # left a right shodně: left
        self.assertEqual(bench.lookahead_center(("....",), 0), "right")     # střed silnice je 1,5
        self.assertEqual(bench.lookahead_center(("....",), 1), "stay")      # pruhy 1 a 2 (index) stejně blízko: shoda → stay
        self.assertEqual(bench.lookahead_center((".#...",), 1), "right")    # stay do '#' se nepočítá, pruh 3 je střed
        self.assertEqual(bench.lookahead_center(("#.#..",), 1), "stay")     # jediný tah do volného pole
        self.assertEqual(bench.lookahead_center((".#.",), 1), "left")       # left a right stejně daleko: left


def shortcut_rates(c):
    """Podíl úloh, kde kontrolní strategie trefí správný tah, podle třídy: {strategie: {třída: podíl}}."""
    picks = {"greedy": bench.lookahead_greedy, "density": bench.lookahead_density, "center": bench.lookahead_center}
    out = {}
    for name, pick in picks.items():
        out[name] = {}
        for cls in bench.LOOKAHEAD_CLASSES:
            sub = [x for x in c if bench.lookahead_class(x[3]) == cls]
            out[name][cls] = sum(pick(rows, lane) == answer for rows, lane, answer, _, _ in sub) / len(sub)
    return out


class Generator(unittest.TestCase):
    def test_cases_keep_the_guarantees(self):
        for lanes, n in ((3, 300), (4, 900), (8, 300)):
            for rows, lane, answer, depth, greedy in cases(n, lanes):
                with self.subTest(lanes=lanes, rows=rows, lane=lane):
                    self.assertIsInstance(rows, tuple)
                    self.assertEqual(len(rows), bench.LOOKAHEAD_ROWS)
                    self.assertTrue(all(len(r) == lanes and set(r) <= {".", "#"} for r in rows))
                    self.assertTrue(0 <= lane < lanes)
                    values = bench.lookahead_values(rows, lane)
                    self.assertEqual(values[answer], bench.LOOKAHEAD_ROWS)                      # nejlepší tah projede všechny řady
                    self.assertEqual([m for m in MOVES if values[m] == values[answer]], [answer])   # a je jediný
                    self.assertEqual(bench.lookahead_depth(rows, lane), (answer, depth))
                    self.assertEqual(bench.lookahead_greedy(rows, lane), greedy)

    def test_classes_and_answers_are_spread(self):
        c = cases()
        classes = Counter(bench.lookahead_class(d) for _, _, _, d, _ in c)
        answers = Counter(a for _, _, a, _, _ in c)
        for cls in bench.LOOKAHEAD_CLASSES:
            self.assertTrue(0.25 <= classes[cls] / len(c) <= 0.42, (cls, classes))
        for move in MOVES:
            self.assertGreaterEqual(answers[move] / len(c), 0.20, (move, answers))

    def test_control_strategies(self):
        c = cases()

        def acc(pick, cls=None):
            sub = [x for x in c if cls is None or bench.lookahead_class(x[3]) == cls]
            return sum(pick(rows, lane) == answer for rows, lane, answer, _, _ in sub) / len(sub)

        def lowest_free(rows, lane):                                            # tah do volného pole s nejnižším číslem pruhu
            return next((m for m in MOVES if 0 <= lane + bench.LOOKAHEAD_SHIFT[m] < len(rows[0])
                         and rows[0][lane + bench.LOOKAHEAD_SHIFT[m]] == "."), "stay")
        self.assertEqual(acc(bench.lookahead_greedy, "d1"), 1)
        rates = shortcut_rates(c)
        for cls in ("d2", "d3+"):
            for name in ("greedy", "density", "center"):
                with self.subTest(cls=cls, strategy=name):
                    self.assertLessEqual(rates[name][cls], 0.45, rates)
            with self.subTest(cls=cls, strategy="lowest_free"):
                self.assertLessEqual(acc(lowest_free, cls), 0.6)                # v d1 má každá strategie bez nárazu 100 % (Rozpory 1)
        self.assertLess(acc(lambda rows, lane: "stay"), 0.5)

    def test_control_strategies_on_five_lanes(self):
        rates = shortcut_rates(cases(300, 5))                                   # při K ≥ 5 se vyvažuje i center
        for cls in ("d2", "d3+"):
            for name in ("greedy", "density", "center"):
                with self.subTest(cls=cls, strategy=name):
                    self.assertLessEqual(rates[name][cls], 0.45, rates)

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.lookahead_cases(40, 4), bench.lookahead_cases(40, 4))
        self.assertEqual(bench.lookahead_cases(40), bench.lookahead_cases(40, 4, seed=1))
        self.assertNotEqual(bench.lookahead_cases(40, 4), bench.lookahead_cases(40, 4, seed=2))

    def test_bounded_search_and_broken_guarantee_raise(self):
        with mock.patch.object(bench, "LOOKAHEAD_TRIES", 0):
            with self.assertRaises(RuntimeError):
                bench.lookahead_case(random.Random(1), 4, "d1")
        for lanes, cls in ((2, "d1"), (9, "d1"), (4, "d4")):
            with self.subTest(lanes=lanes, cls=cls), self.assertRaises(RuntimeError):
                bench.lookahead_case(random.Random(1), lanes, cls)
        tie = lambda rows, lane, depth=None: {"left": 4, "stay": 4, "right": 0}   # dva nejlepší tahy: konstrukce nesmí projít
        with mock.patch.object(bench, "lookahead_depth", lambda rows, lane: ("left", 1)), \
                mock.patch.object(bench, "lookahead_values", tie), self.assertRaises(RuntimeError):
            bench.lookahead_case(random.Random(1), 4, "d1")


if __name__ == "__main__":
    unittest.main()
