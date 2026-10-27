"""Scénář Tah na výhru, piškvorky 3×3 (bench.py --tictactoe). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
import contextlib
import functools
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
from test_bench import fake_post, obedient
from test_golden import Clock, fake_urlopen

LANG_ENV = "PICKBENCH_LANG"
CELLS = ("r1c1", "r1c2", "r1c3", "r2c1", "r2c2", "r2c3", "r3c1", "r3c2", "r3c3")
KEYS = {"rows": "cell", "cells": "cell_cells"}        # klíč otázky podle podoby stavu
LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))
CLASSES = ("win", "block", "win_over_block", "fork")
WIN = ("....X.OXO", "X", 1, "win")                    # X dokončí sloupec 2
BLOCK = ("X...X.OXO", "O", 1, "block")                # příklad ze specu: O musí zablokovat sloupec 2
WOB = ("...OX.OX.", "X", 1, "win_over_block")         # X vyhraje na r1c2, O by vyhrálo na r1c1
FORK = ("...O..XXO", "X", 4, "fork")                  # X na středu: hrozby r1c2 a r1c3
CACHE = {}


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def cases(n=3000):
    """Úlohy ze semínka 1; velká sada se losuje jednou za běh modulu."""
    if n not in CACHE:
        CACHE[n] = bench.tictactoe_cases(n)
    return CACHE[n]


# ---------- nezávislá pravidla (test nepoužívá funkce bench.py, které ověřuje) ----------
def other(m):
    return "O" if m == "X" else "X"


def free(b):
    return [i for i in range(9) if b[i] == "."]


def wins(b, m):
    """Volná pole, kterými m hned vyhraje."""
    return sorted({i for line in LINES for i in line if b[i] == "." and sum(b[j] == m for j in line) == 2})


def put(b, i, m):
    return b[:i] + m + b[i + 1:]


def forks(b, m):
    return [i for i in free(b) if len(wins(put(b, i, m), m)) >= 2]


def won(b):
    return any(b[x] != "." and b[x] == b[y] == b[z] for x, y, z in LINES)


@functools.lru_cache(maxsize=None)
def negamax(b, m):
    """Hodnota pozice pro m na tahu: 1 výhra, 0 remíza, −1 prohra."""
    best = 0 if not free(b) else -1
    for i in free(b):
        nb = put(b, i, m)
        best = max(best, 1 if won(nb) else -negamax(nb, other(m)))
    return best


def rules(b, m):
    """Tah podle pravidel v zadání, nebo None, když první platné pravidlo nedává právě jedno pole."""
    own, opp = wins(b, m), wins(b, other(m))
    if own:
        return own[0] if len(own) == 1 and len(opp) <= 1 and own != opp else None
    if opp:
        return opp[0] if len(opp) == 1 else None
    f = forks(b, m)
    return f[0] if len(f) == 1 else None


def board_of(state):
    """Deska ze stavu v obou podobách jako řetězec devíti znaků ('.' volné pole)."""
    if "board" in state:
        return "".join(state["board"])
    return "".join(state["cells"][k] or "." for k in CELLS)


def solve(state):
    """Nezávislý řešitel nad stavem: klíč pole podle pravidel."""
    return CELLS[rules(board_of(state), state["you"])]


def expected_hit(answer, picks):
    """Očekávaná trefa strategie, která ukazuje na množinu polí (remíza = náhodná volba mezi nimi)."""
    return (answer in picks) / len(picks)


def neighbors(i):
    return [j for j in range(9) if j != i and abs(j // 3 - i // 3) <= 1 and abs(j % 3 - i % 3) <= 1]


def top(b, score):
    best = max(score(i) for i in free(b))
    return [i for i in free(b) if score(i) == best]


def count(b, i, marks):
    return sum(b[j] in marks for j in neighbors(i))


CHEAP = {                                             # levné zkratky: nečtou linky, jen polohu a sousedy
    "random": lambda b, m: free(b),
    "center_corner": lambda b, m: next(g for g in ([i for i in grp if b[i] == "."] for grp in ((4,), (0, 2, 6, 8), (1, 3, 5, 7))) if g),
    "corner": lambda b, m: [i for i in (0, 2, 6, 8) if b[i] == "."] or free(b),
    "edge": lambda b, m: [i for i in (1, 3, 5, 7) if b[i] == "."] or free(b),
    "first_free": lambda b, m: free(b)[:1],
    "last_free": lambda b, m: free(b)[-1:],
    "neighbors": lambda b, m: top(b, lambda i: count(b, i, "XO")),
    "fewest_neighbors": lambda b, m: top(b, lambda i: -count(b, i, "XO")),
    "own_neighbors": lambda b, m: top(b, lambda i: count(b, i, m)),
    "opp_neighbors": lambda b, m: top(b, lambda i: count(b, i, other(m))),
    "fewest_opp": lambda b, m: top(b, lambda i: -count(b, i, other(m))),
}


def by_rules_for(b, m):
    """Pravidla bez kontroly jednoznačnosti (první pole), za značku m."""
    return (wins(b, m) or wins(b, other(m)) or forks(b, m) or [None])[0]


PARTIAL = {                                           # částečné uvažování: to, co třídy rozlišují
    "any_line": lambda b, m: sorted(set(wins(b, m)) | set(wins(b, other(m)))) or free(b),
    "block_first": lambda b, m: wins(b, other(m)) or wins(b, m) or forks(b, m) or free(b),
    "no_fork": lambda b, m: wins(b, m) or wins(b, other(m)) or free(b),
    "swap_sides": lambda b, m: [x] if (x := by_rules_for(b, other(m))) is not None else free(b),
    "always_X": lambda b, m: [x] if (x := by_rules_for(b, "X")) is not None else free(b),
    "open_lines": lambda b, m: top(b, lambda i: sum(i in line and all(b[j] != other(m) for j in line) for line in LINES)),
}


def shares(strategy, cs):
    """Očekávaná trefa strategie po třídách: {třída: podíl}."""
    out = {}
    for cls in CLASSES:
        sub = [c for c in cs if c[3] == cls]
        out[cls] = sum(expected_hit(c[2], strategy(c[0], c[1])) for c in sub) / len(sub)
    return out


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Basics(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(bench.TICTACTOE_CELLS, CELLS)
        self.assertEqual(bench.TICTACTOE_LINES, LINES)
        self.assertEqual(bench.TICTACTOE_CLASSES, CLASSES)
        self.assertEqual((bench.TICTACTOE_EMPTY, bench.TICTACTOE_FORMS, bench.TICTACTOE_TRIES), (".", ("rows", "cells"), 1000))
        self.assertEqual(bench.TICTACTOE_FORM, "cells")                                   # Volby po pokusu V1
        self.assertEqual(bench.TICTACTOE_FEW, 3)
        self.assertEqual(bench.TICTACTOE_SIDE_X, {"win": 0.28, "block": 0.48, "win_over_block": 0.54, "fork": 0.47})
        self.assertEqual(bench.TICTACTOE_CELL_WEIGHT["block"], {"corner": 1, "edge": 2.6, "center": 5.5})
        self.assertEqual(bench.TICTACTOE_BALANCE["fork"], {"fewest_opp": 0.2, "edge": 0.2, "own_neighbors": 0.6})
        self.assertEqual(set(bench.TICTACTOE_BALANCE), set(CLASSES))

    def test_rules_by_hand(self):
        self.assertEqual((bench.tictactoe_winner("XXX.OO..."), bench.tictactoe_winner("X.OXO.O.X"), bench.tictactoe_winner("XO.......")),
                         ("X", "O", None))
        self.assertEqual((bench.tictactoe_to_move("." * 9), bench.tictactoe_to_move("X........"), bench.tictactoe_to_move("XO.......")),
                         ("X", "O", "X"))
        self.assertEqual(bench.tictactoe_wins(WIN[0], "X"), [1])
        self.assertEqual(bench.tictactoe_wins(WOB[0], "O"), [0])
        self.assertEqual(bench.tictactoe_wins("XX.XO.O..", "X"), [2])                    # 6 má O: sloupec 1 je zavřený
        self.assertEqual(bench.tictactoe_threats(FORK[0], 4, "X"), 2)
        self.assertEqual(bench.tictactoe_forks(FORK[0], "X"), [4])
        self.assertEqual(bench.tictactoe_forks(FORK[0], "O"), forks(FORK[0], "O"))

    def test_solve_by_hand(self):
        for case in (WIN, BLOCK, WOB, FORK):
            with self.subTest(case=case):
                self.assertEqual(bench.tictactoe_solve(case[0]), (case[3], case[2]))
        self.assertEqual(bench.tictactoe_solve("XX.OO...."), ("win_over_block", 2))
        for b in ("..O.XXOOX",      # X na tahu má dvě výhry (r2c1, r1c1)
                  "....XOOXX",      # O na tahu, X hrozí dvakrát (r1c1, r1c2): prohraná pozice
                  "....OXO.X",      # výhra X i blok O na stejném poli r1c3: nerozliší pořadí pravidel
                  ".....XXOO",      # X na tahu má dvě vidličky
                  "......XOX",      # nikdo nehrozí a vidlička není
                  "." * 9):
            with self.subTest(b=b):
                self.assertIsNone(bench.tictactoe_solve(b))
                self.assertIsNone(rules(b, bench.tictactoe_to_move(b)))

    def test_minimax_by_hand(self):
        self.assertEqual(bench.tictactoe_value("." * 9), 0)                            # prázdná deska: remíza
        self.assertEqual(bench.tictactoe_value(WIN[0]), 1)
        self.assertEqual(bench.tictactoe_value("....XOOXX"), -1)                       # O na tahu proti dvěma hrozbám
        self.assertEqual(bench.tictactoe_move_value(FORK[0], 4), 1)
        self.assertEqual(bench.tictactoe_move_value(BLOCK[0], 2), -1)                  # bez bloku O hned prohraje
        for b in ("X........", "XO.......", "......XOX", BLOCK[0], WOB[0], FORK[0]):
            with self.subTest(b=b):
                self.assertEqual(bench.tictactoe_value(b), negamax(b, bench.tictactoe_to_move(b)))


class Pools(unittest.TestCase):
    def test_positions_and_pool_sizes(self):
        positions = bench.tictactoe_positions()
        self.assertEqual(len(positions), 4298)
        self.assertEqual(positions, sorted(set(positions)))
        pools = bench.tictactoe_pools()
        self.assertEqual({c: sum(map(len, pools[c].values())) for c in CLASSES}, {"win": 444, "block": 936, "win_over_block": 616, "fork": 124})
        self.assertEqual({c: sum(bench.tictactoe_to_move(b) == "X" for p in pools[c].values() for b in p) for c in CLASSES},
                         {"win": 356, "block": 356, "win_over_block": 348, "fork": 96})
        self.assertEqual(sorted(pools["fork"]), list(range(9)))                         # každé pole je někde odpovědí

    def test_every_pool_position_keeps_the_guarantees(self):
        for cls, pool in bench.tictactoe_pools().items():
            for cell, boards in pool.items():
                for b in boards:
                    m = bench.tictactoe_to_move(b)
                    with self.subTest(cls=cls, b=b):
                        self.assertFalse(won(b))
                        self.assertGreaterEqual(b.count("."), 2)
                        self.assertIn(b.count("X") - b.count("O"), (0, 1))
                        self.assertEqual(rules(b, m), cell)
                        own, opp = wins(b, m), wins(b, other(m))
                        self.assertEqual(cls, "win" if own and not opp else "win_over_block" if own else "block" if opp else "fork")
                        if cls == "block":                                               # každý jiný tah prohrává hned
                            self.assertTrue(all(wins(put(b, i, m), other(m)) for i in free(b) if i != cell))
                        if cls == "fork":                                                # jediný tah, který vyhrává silou
                            self.assertEqual([i for i in free(b) if -negamax(put(b, i, m), other(m)) == 1], [cell])


if __name__ == "__main__":
    unittest.main()
