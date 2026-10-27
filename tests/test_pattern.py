"""Scénář Vzor soupeře (bench.py --pattern). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
import contextlib
import io
import itertools
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
MOVES = ("rock", "paper", "scissors")
BEATS = {"rock": "paper", "paper": "scissors", "scissors": "rock"}      # BEATS[x] = tah, který porazí x
BEATEN = {v: k for k, v in BEATS.items()}                              # BEATEN[x] = tah, který x porazí
FORM = "predict"                                      # výchozí podoba (Volby po pokusu V1)
KEYS = {"counter": "throw", "predict": "opponent_throw"}               # klíče otázek (Volby po pokusu V4)
SPEC = ("rock", "rock", "paper", "scissors", "rock", "paper", "scissors", "rock", "paper")     # příklad ze specu: perioda 3, další scissors
SPEC_CASE = (SPEC, "scissors", "p3", "ABC", 8, 1)
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
        CACHE[n] = bench.pattern_cases(n)
    return CACHE[n]


def periods_of(h):
    """Periody 1–4, se kterými souhlasí posledních 8 tahů (nezávisle na bench.py)."""
    return [q for q in range(1, 5) if all(h[-i] == h[-i - q] for i in range(1, 8 - q + 1))]


def next_move(h):
    """Nezávislý řešitel: další tah soupeře z nejmenší periody posledních 8 tahů."""
    return h[-periods_of(h)[0]]


def solver(form):
    """Kontrolní model ze stavu: správná odpověď v dané podobě."""
    return lambda state: BEATS[next_move(state["opponent_moves"])] if form == "counter" else next_move(state["opponent_moves"])


def most(seq):
    c = Counter(seq)
    return [x for x in MOVES if c[x] == max(c.values())]


def least_seen(h):
    c = Counter(h)
    return [x for x in MOVES if x in c and c[x] == min(c.values())]


def least_all(h):
    c = Counter(h)
    return [x for x in MOVES if c[x] == min(c[m] for m in MOVES)]


def follow_last(h):
    """Co soupeř zahrál po posledním dřívějším výskytu svého posledního tahu."""
    return next(([h[j + 1]] for j in range(len(h) - 2, -1, -1) if h[j] == h[-1]), list(MOVES))


def follow_major(h):
    f = [h[j + 1] for j in range(len(h) - 1) if h[j] == h[-1]]
    return most(f) if f else list(MOVES)


# Levné zkratky ze specu (rodina, funkce historie → kandidáti na další tah soupeře), stejné jako v prototypu.
BASE = {"last": ("cheap", lambda h: [h[-1]]), "first": ("cheap", lambda h: [h[0]]), "most": ("cheap", most),
        "least_seen": ("cheap", least_seen), "least_all": ("cheap", least_all), "recent3": ("cheap", lambda h: most(h[-3:])),
        "recent4": ("cheap", lambda h: most(h[-4:])), "missing2": ("cheap", lambda h: [x for x in MOVES if x not in h[-2:]]),
        "follow_last": ("transition", follow_last), "follow_major": ("transition", follow_major)}
BASE.update({f"lag{k}": ("lag", (lambda k: lambda h: [h[-k]])(k)) for k in range(2, 7)})
TRANSFORMS = {"id": lambda x: x, "beats": BEATS.get, "beaten": BEATEN.get}


def shortcut_hits(h, nxt, p):
    """{jméno: (rodina, očekávaná trefa)}; zkratka = základ × převod, takže pokrývá obě podoby. Odstup dělitelný periodou je řešení."""
    out = {}
    for name, (family, f) in BASE.items():
        if family == "lag" and int(name[3:]) % p == 0:
            continue
        cand = f(h)
        for t, g in TRANSFORMS.items():
            out[f"{t}:{name}"] = (family, sum(g(x) == nxt for x in cand) / len(cand))
    return out


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Basics(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(bench.PATTERN_MOVES, MOVES)
        self.assertEqual((bench.PATTERN_BEATS, bench.PATTERN_BEATEN), (BEATS, BEATEN))
        self.assertEqual(bench.PATTERN_CLASSES, ("p1", "p2", "p3", "p4"))
        self.assertEqual((bench.PATTERN_FORMS, bench.PATTERN_FORM), (("counter", "predict"), FORM))       # Volby po pokusu V1
        self.assertEqual((bench.PATTERN_RUN, bench.PATTERN_WARMUP, bench.PATTERN_CHECK), ((8, 10), (1, 3), 8))
        self.assertEqual(bench.PATTERN_WEIGHTS, {                                     # opsané z prototypu doslova (spec)
            "p1": {"A": (1, 1, 1)}, "p2": {"AB": (1, 1, 1)},
            "p3": {"AAB": (2, 2, 13), "ABB": (41, 2, 2), "ABC": (14, 2, 2), "ABA": (2, 12, 2)},
            "p4": {"AAAB": (1, 14, 1), "AABB": (1, 1, 1), "AABC": (1, 1, 1), "ABBB": (1, 1, 1), "ABBC": (1, 1, 1),
                   "ABCC": (10, 1, 25), "AABA": (1, 1, 1), "ABAA": (3, 1, 1), "ABAC": (1, 1, 1), "ABBA": (11, 1, 1),
                   "ABCA": (1, 1, 1), "ABCB": (5, 1, 7)}})

    def test_solve_by_hand(self):
        self.assertEqual(bench.pattern_solve(SPEC), (3, "scissors"))
        self.assertEqual(bench.pattern_solve(("paper",) * 9), (1, "paper"))
        self.assertEqual(bench.pattern_solve(("scissors", "rock") + ("paper", "rock") * 4), (2, "paper"))
        self.assertEqual(bench.pattern_solve(("rock", "paper") + ("scissors", "scissors", "rock", "paper") * 2 + ("scissors",)), (4, "scissors"))
        self.assertEqual(bench.pattern_solve(("rock", "rock") + ("paper",) * 8), (1, "paper"))          # rozehrávka smí mít tah dvakrát

    def test_solve_rejects_ambiguous_histories(self):
        for h in (("rock", "paper") * 3 + ("rock",),                                   # kratší než 8
                  ("rock", "paper", "scissors", "scissors", "paper", "rock", "rock", "paper", "scissors"),     # bez periody do 4
                  ("rock", "paper", "scissors", "paper", "rock") * 2):                  # perioda 5
            with self.subTest(h=h), self.assertRaises(RuntimeError):
                bench.pattern_solve(h)

    def test_every_agreeing_period_gives_the_same_move(self):      # jednoznačnost (Fine–Wilf): výčet všech 3^8 konců historie
        solved = 0
        for tail in itertools.product(MOVES, repeat=8):
            qs = periods_of(tail)
            if not qs:
                with self.assertRaises(RuntimeError):
                    bench.pattern_solve(tail)
                continue
            self.assertEqual(len({tail[-q] for q in qs}), 1, tail)
            self.assertEqual(bench.pattern_solve(tail), (qs[0], tail[-qs[0]]))
            solved += 1
        self.assertEqual(solved, 3 + 6 + 24 + 72)                                       # primitivní cykly délky 1–4 nad třemi tahy


if __name__ == "__main__":
    unittest.main()
