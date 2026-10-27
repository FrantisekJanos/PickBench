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


class Generator(unittest.TestCase):
    def test_cases_keep_the_guarantees(self):
        pools = bench.tictactoe_pools()
        for case in cases():
            b, you, answer, cls = case
            with self.subTest(case=case):
                self.assertIn(b, pools[cls][answer])
                self.assertEqual(you, bench.tictactoe_to_move(b))
                self.assertEqual(rules(b, you), answer)
                for form in bench.TICTACTOE_FORMS:
                    self.assertEqual(solve(bench.tictactoe_state(case, form)), CELLS[answer])

    def test_classes_and_sides_are_spread(self):
        c = cases()
        counts = Counter(x[3] for x in c)
        for cls in CLASSES:
            sub = [x for x in c if x[3] == cls]
            with self.subTest(cls=cls):
                self.assertTrue(0.22 <= counts[cls] / len(c) <= 0.28, counts)
                self.assertTrue(0.42 <= sum(x[1] == "X" for x in sub) / len(sub) <= 0.58)
        answers = Counter(x[2] for x in c)
        self.assertTrue(all(0.06 <= answers[i] / len(c) <= 0.2 for i in range(9)), answers)

    def test_cheap_shortcuts_stay_near_chance(self):
        c = cases()
        self.assertTrue(set().union(*bench.TICTACTOE_BALANCE.values()) <= set(bench.TICTACTOE_SHORTCUTS) <= set(CHEAP))    # zamítá se jen podle zkratek, které test zná
        for name, strategy in CHEAP.items():
            got = shares(strategy, c)
            with self.subTest(name=name, got=got):
                self.assertTrue(all(v <= 0.42 for v in got.values()))
        self.assertTrue(all(0.2 <= v <= 0.3 for v in shares(CHEAP["random"], c).values()))

    def test_partial_reasoning_is_what_the_classes_measure(self):
        c = cases()
        got = {name: shares(strategy, c) for name, strategy in PARTIAL.items()}
        for name in ("any_line", "block_first", "no_fork", "swap_sides", "always_X"):
            self.assertEqual((got[name]["win"], got[name]["block"]), (1, 1), name)         # kontrolní třídy: čtení linek
        self.assertTrue(0.45 <= got["any_line"]["win_over_block"] <= 0.55, got["any_line"])
        self.assertEqual((got["block_first"]["win_over_block"], got["swap_sides"]["win_over_block"]), (0, 0))
        self.assertEqual(got["block_first"]["fork"], 1)
        self.assertLess(got["no_fork"]["fork"], 0.35)
        self.assertTrue(0.6 <= got["open_lines"]["fork"] <= 0.9, got["open_lines"])      # vidlička je i částečné uvažování (README, nápověda)

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.tictactoe_cases(40), bench.tictactoe_cases(40, seed=1))
        self.assertNotEqual(bench.tictactoe_cases(40), bench.tictactoe_cases(40, seed=2))

    def test_bounded_search_and_broken_guarantee_raise(self):
        with mock.patch.object(bench, "TICTACTOE_TRIES", 0), self.assertRaises(RuntimeError):
            bench.tictactoe_cases(1)
        with self.assertRaises(RuntimeError):
            bench.tictactoe_case(random.Random(1), "draw")
        broken = {c: {i: (WIN[0],) for i in range(9)} for c in CLASSES}                   # pozice v koši cizího pole
        with mock.patch.object(bench, "tictactoe_pools", lambda: broken), self.assertRaises(RuntimeError):
            bench.tictactoe_case(random.Random(1), "win")


class State(unittest.TestCase):
    def test_spec_example_state(self):
        self.assertEqual(json.dumps(bench.tictactoe_state(BLOCK, "rows")), '{"you": "O", "board": ["X..", ".X.", "OXO"]}')
        self.assertEqual(json.dumps(bench.tictactoe_state(BLOCK, "cells")),
                         '{"you": "O", "cells": {"r1c1": "X", "r1c2": "", "r1c3": "", "r2c1": "", "r2c2": "X", "r2c3": "", '
                         '"r3c1": "O", "r3c2": "X", "r3c3": "O"}}')
        self.assertEqual(bench.tictactoe_state(BLOCK), bench.tictactoe_state(BLOCK, bench.TICTACTOE_FORM))
        self.assertIn(BLOCK[0], bench.tictactoe_pools()["block"][1])
        with self.assertRaises(ValueError):
            bench.tictactoe_state(BLOCK, "grid")

    def test_both_forms_carry_the_same_task(self):
        for x in cases()[:300]:
            a, b = bench.tictactoe_state(x, "rows"), bench.tictactoe_state(x, "cells")
            with self.subTest(case=x):
                self.assertEqual((list(a), list(b)), (["you", "board"], ["you", "cells"]))
                self.assertEqual(list(b["cells"]), list(CELLS))
                self.assertEqual((board_of(a), board_of(b), a["you"]), (x[0], x[0], b["you"]))


INS_ROWS = ("You play tic-tac-toe on a 3x3 board and it is your move; you is your mark, X or O. board lists the rows from the "
            "top; in each row the first character is column 1, '.' marks an empty cell. Three of one mark in a row, a column "
            "or a diagonal win. Pick an empty cell by these rules, in this order: 1. If a move wins at once, take it. "
            "2. Otherwise, if the opponent could win on their next move, take that cell to block it. 3. Otherwise, take the "
            "cell after which you have two different ways to win on your next move.")
INS_CELLS = ("You play tic-tac-toe on a 3x3 board and it is your move; you is your mark, X or O. cells gives the mark in "
             "each cell, rNcM is row N from the top and column M from the left, and an empty string is an empty cell. "
             "Three of one mark in a row, a column or a diagonal win. Pick an empty cell by these rules, in this order: "
             "1. If a move wins at once, take it. 2. Otherwise, if the opponent could win on their next move, take that "
             "cell to block it. 3. Otherwise, take the cell after which you have two different ways to win on your next move.")
ALT_ROWS = ("It is your turn in a game of tic-tac-toe and your mark is the one in you. board shows the three rows from top "
            "to bottom, each written left to right, with '.' for a free cell. A player who gets three marks in a line, "
            "across, down or diagonally, wins. Choose a free cell for your mark: a cell that completes a line of your own "
            "comes first; failing that, the cell where the opponent would complete a line; failing that, the cell that "
            "leaves you two separate lines that each need just one more mark.")
ALT_CELLS = ("It is your turn in a game of tic-tac-toe and your mark is the one in you. cells names every cell as rNcM, row N "
             "counted from the top and column M from the left, and gives its mark; an empty string means the cell is free. "
             "A player who gets three marks in a line, across, down or diagonally, wins. Choose a free cell for your mark: "
             "a cell that completes a line of your own comes first; failing that, the cell where the opponent would "
             "complete a line; failing that, the cell that leaves you two separate lines that each need just one more mark.")


class Question(unittest.TestCase):
    def test_question_and_its_alternative_wording(self):
        for form, ins, alt in (("rows", INS_ROWS, ALT_ROWS), ("cells", INS_CELLS, ALT_CELLS)):
            key = KEYS[form]
            q = bench.tictactoe_question(form)
            with self.subTest(form=form):
                self.assertEqual(list(q), [key])
                self.assertEqual((q[key]["type"], q[key]["instructions"]), ("choice", ins))
                self.assertEqual(json.dumps(q[key]["criteria"]), json.dumps({k: f"Row {k[1]}, column {k[3]}" for k in CELLS}))
                self.assertEqual(bench.ALT[key], alt)
                self.assertEqual(bench.variant(q, 2)[key]["instructions"], alt)
                self.assertEqual(list(bench.variant(q, 1)[key]["criteria"]), list(CELLS)[::-1])
        self.assertEqual(bench.tictactoe_question(), bench.tictactoe_question(bench.TICTACTOE_FORM))

    def test_prompt_stays_short(self):                   # odhad: 4 znaky JSON na token; cíl pro hru 600
        for form in bench.TICTACTOE_FORMS:
            q = json.dumps(bench.tictactoe_question(form))
            longest = max(len(json.dumps(bench.with_note(bench.tictactoe_state(x, form), "r3c3"))) for x in cases()[:300])
            self.assertLess((longest + len(q)) / 4, 600, form)


class RunTictactoe(unittest.TestCase):
    def run_with(self, answer, *args, **kw):
        with mock.patch.object(bench, "post", fake_post(answer)):
            return bench.run_tictactoe(*args, **kw)

    def test_errors_by_hand(self):
        for case, choice, kind in ((WIN, "r1c2", None), (WIN, "r2c2", "occupied"), (WIN, "r1c1", "missed_win"),
                                   (BLOCK, "r1c2", None), (BLOCK, "r1c1", "occupied"), (BLOCK, "r1c3", "missed_block"),
                                   (WOB, "r1c1", "block_not_win"), (WOB, "r1c3", "missed_win"),
                                   (FORK, "r2c3", "wrong_side"), (FORK, "r1c2", "single_threat"), (FORK, "r1c1", "other"),
                                   (FORK, "r3c1", "occupied"), (FORK, None, "invalid"), (FORK, "R1C1", "invalid"), (FORK, 4, "invalid")):
            with self.subTest(case=case, choice=choice):
                self.assertEqual(bench.tictactoe_error(case, choice), kind)
        c = [WIN, WIN, BLOCK, BLOCK, WOB, WOB, FORK, FORK, FORK]
        choices = ["r1c2", "r1c1", "r1c3", "r1c3", "r1c1", "r1c2", "r2c3", "r1c2", None]
        s = bench.tictactoe_summary(c, choices)
        self.assertEqual(json.dumps(s["errors"]), json.dumps({"block_not_win": 1, "missed_win": 1, "missed_block": 2, "wrong_side": 1,
                                                              "single_threat": 1, "occupied": 0, "other": 0, "invalid": 1}))
        self.assertEqual(s["by_class"], {"win": [1, 2], "block": [0, 2], "win_over_block": [1, 2], "fork": [0, 3]})
        self.assertEqual(s["by_side"], {"X": [2, 7], "O": [0, 2]})
        self.assertEqual(s["by_free"], {"few": [0, 0], "many": [2, 9]})
        self.assertEqual(bench.tictactoe_errors(c, choices), s["errors"])

    def test_free_cells_split_at_three(self):
        late = (".OXXOX.XO", "O", 0, "win")                                              # dvě volná pole
        s = bench.tictactoe_summary([late, WIN], ["r1c1", "r1c2"])
        self.assertEqual(s["by_free"], {"few": [1, 1], "many": [1, 1]})

    def test_perfect_model(self):
        c = cases(120)
        for form in bench.TICTACTOE_FORMS:
            with self.subTest(form=form):
                r = self.run_with(solve, "m", c, form=form)
                self.assertEqual(r["acc"], 1)
                self.assertEqual((list(r["by_class"]), list(r["by_side"]), list(r["by_free"])), (list(CLASSES), ["X", "O"], ["few", "many"]))
                self.assertEqual(sum(n for _, n in r["by_class"].values()), 120)
                self.assertTrue(all(ok == n for part in ("by_class", "by_side", "by_free") for ok, n in r[part].values()))
                self.assertEqual(set(r["errors"].values()), {0})

    def test_control_models_fall_into_their_error_class(self):
        c = cases(300)
        n = Counter(x[3] for x in c)

        def block_first(state):                         # obrácené pořadí pravidel 1 a 2
            b, m = board_of(state), state["you"]
            return CELLS[(wins(b, other(m)) or wins(b, m) or forks(b, m))[0]]

        def skip_block(state):                          # pravidla, jen místo bloku jiné volné pole
            b, m = board_of(state), state["you"]
            if wins(b, other(m)) and not wins(b, m):
                return CELLS[next(i for i in free(b) if i not in wins(b, other(m)))]
            return solve(state)
        taken = lambda state: CELLS[next(i for i in range(9) if board_of(state)[i] != ".")]
        for model, key, wrong in ((block_first, "block_not_win", n["win_over_block"]), (skip_block, "missed_block", n["block"]),
                                  (taken, "occupied", 300)):
            with self.subTest(model=model):
                r = self.run_with(model, "m", c)
                self.assertGreater(wrong, 0)
                self.assertEqual(r["errors"], dict.fromkeys(bench.TICTACTOE_ERRORS, 0) | {key: wrong})
                self.assertEqual(r["acc"], (300 - wrong) / 300)

    def test_invalid_answers(self):
        c = cases(30)
        for answer in (None, "R1C1", "center", ["r1c1"], 3):
            with self.subTest(answer=answer):
                r = self.run_with(lambda state: answer, "m", c)
                self.assertEqual((r["acc"], r["errors"]["invalid"]), (0, 30))

    def test_requests_carry_the_state_and_the_question(self):
        c = bench.tictactoe_cases(5)
        for form in bench.TICTACTOE_FORMS:
            sent = []

            def post(model, payload):
                sent.append(payload)
                return {"answers": {next(iter(payload["questions"])): {"choice": "r2c2", "confidence": 0.5}}}, 10.0
            with mock.patch.object(bench, "post", post):
                bench.run_tictactoe("m", c, form=form)
            with self.subTest(form=form):
                self.assertEqual(len(sent), 6)                                  # zahřátí a pět úloh
                self.assertEqual([p["state"] for p in sent], [bench.tictactoe_state(c[0], form)] + [bench.tictactoe_state(x, form) for x in c])
                self.assertTrue(all(p["questions"] == bench.tictactoe_question(form) for p in sent))

    def test_mirror_and_inject(self):
        c = cases(40)
        r = self.run_with(solve, "m", c, mirror=True)
        self.assertEqual((r["stab"], r["acc"]), (1, 1))
        r = self.run_with(obedient(solve), "m", c, inject=True)
        self.assertEqual((r["inject"]["n"], r["inject"]["fooled"], r["inject"]["clean_acc"]), (10, 1, 1))
        self.assertEqual(r["acc"], 0.75)
        self.assertEqual((r["errors"]["occupied"], r["errors"]["invalid"]), (0, 0))     # pokyn míří jen na volná pole

    def test_inject_targets_only_free_wrong_cells(self):
        c = cases(400)
        notes = []

        def post(model, payload):
            notes.append((payload["state"], payload["state"].get("note")))
            return {"answers": {next(iter(payload["questions"])): {"choice": "r2c2", "confidence": 0.5}}}, 10.0
        with mock.patch.object(bench, "post", post):
            bench.run_tictactoe("m", c, inject=True)
        targets = [(state, note.rsplit(" ", 1)[1].rstrip(".")) for state, note in notes[1:] if note]
        self.assertEqual(len(targets), 100)
        for state, key in targets:
            self.assertEqual(board_of(state)[CELLS.index(key)], ".")
            self.assertNotEqual(key, solve(state))


class Lines(unittest.TestCase):
    def test_note_by_class(self):
        r = {"by_class": {"win": [5, 10], "block": [9, 11], "win_over_block": [3, 12], "fork": [1, 9]}}
        for lang, want in (("cs", "(výhra 5/10, blok 9/11, výhra před blokem 3/12, vidlička 1/9)"),
                           ("en", "(win 5/10, block 9/11, win before block 3/12, fork 1/9)")):
            with i18n.process(lang):
                self.assertEqual(bench.tictactoe_note(r), want)

    def test_lines_in_both_languages(self):
        r = {"by_side": {"X": [9, 10], "O": [6, 10]}, "by_free": {"few": [2, 7], "many": [8, 13]},
             "errors": {"block_not_win": 4, "missed_win": 3, "missed_block": 5, "wrong_side": 1, "single_threat": 2, "occupied": 6,
                        "other": 7, "invalid": 8}}
        pad = " " * 20
        self.assertEqual(printed("cs", bench.print_tictactoe, r),
                         [pad + "hraje X 9/10, hraje O 6/10; volná pole 2–3 2/7, 4–6 8/13",
                          pad + "chyby: blok místo výhry 4, přehlédnutá výhra 3, přehlédnutý blok 5, vidlička soupeře 1, "
                                "jen jedna hrozba 2, obsazené pole 6, jiné 7, neplatné 8"])
        self.assertEqual(printed("en", bench.print_tictactoe, r),
                         [pad + "plays X 9/10, plays O 6/10; free cells 2–3 2/7, 4–6 8/13",
                          pad + "errors: block instead of win 4, win missed 3, block missed 5, opponent's fork 1, "
                                "single threat only 2, occupied cell 6, other 7, invalid 8"])

    def test_lines_with_empty_classes(self):
        r = {"by_class": {"win": [1, 1], "block": [0, 0], "win_over_block": [0, 0], "fork": [0, 0]},
             "by_side": {"X": [1, 1], "O": [0, 0]}, "by_free": {"few": [0, 0], "many": [1, 1]},
             "errors": dict.fromkeys(bench.TICTACTOE_ERRORS, 0)}
        with i18n.process("en"):
            self.assertEqual(bench.tictactoe_note(r), "(win 1/1, block 0/0, win before block 0/0, fork 0/0)")
        self.assertEqual(len(printed("en", bench.print_tictactoe, r)), 2)


if __name__ == "__main__":
    unittest.main()
