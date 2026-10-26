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


if __name__ == "__main__":
    unittest.main()
