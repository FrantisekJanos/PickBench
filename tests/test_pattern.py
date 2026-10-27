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


class Generator(unittest.TestCase):
    def test_cases_keep_the_guarantees(self):
        for case in cases():
            h, nxt, cls, shape, run, warm = case
            p = int(cls[1])
            with self.subTest(case=case):
                self.assertIsInstance(h, tuple)
                self.assertTrue(set(h) <= set(MOVES))
                self.assertEqual(len(h), run + warm)
                self.assertTrue(8 <= run <= 10 and 1 <= warm <= 3)
                self.assertIn(shape, bench.PATTERN_WEIGHTS[cls])
                letters = dict(zip(shape, h[-p:]))                                           # posledních p tahů je tvar
                self.assertEqual("".join(sorted(letters)), "ABC"[:len(set(shape))])
                self.assertEqual(len(set(letters.values())), len(letters))
                self.assertEqual([letters[ch] for ch in shape], list(h[-p:]))
                self.assertEqual(letters["A"], nxt)
                self.assertEqual(periods_of(h)[0], p)
                self.assertEqual({h[-q] for q in periods_of(h)}, {nxt})                       # každá souhlasící perioda dá stejný tah
                body = h[-run:]
                self.assertTrue(all(body[i] == body[i - p] for i in range(p, run)))
                self.assertNotEqual(h[-run - 1], h[-run - 1 + p])                             # rozehrávka cyklus neprodlužuje
                self.assertEqual(min(q for q in range(1, 5) if all(body[i] == body[i - q] for i in range(q, run))), p)

    def test_classes_and_answers_are_spread(self):
        c = cases()
        classes = Counter(x[2] for x in c)
        for cls in bench.PATTERN_CLASSES:
            self.assertTrue(0.22 <= classes[cls] / len(c) <= 0.28, classes)
            answers = Counter(x[1] for x in c if x[2] == cls)
            for move in MOVES:
                self.assertTrue(0.28 <= answers[move] / classes[cls] <= 0.39, (cls, answers))
        self.assertEqual({len(x[0]) for x in c}, set(range(9, 14)))

    def test_shortcuts_stay_near_chance(self):
        for cls, top, cheap_top in (("p2", 0.56, 0.56), ("p3", 0.48, 0.48), ("p4", 0.48, 0.48)):
            sub = [x for x in cases() if x[2] == cls]
            hits = {}
            for h, nxt, *_ in sub:
                for name, (family, v) in shortcut_hits(h, nxt, int(cls[1])).items():
                    hits[name] = (family, hits.get(name, (family, 0))[1] + v / len(sub))
            with self.subTest(cls=cls):
                for name, (family, v) in hits.items():
                    if cls == "p2" and family == "transition":
                        continue                                                    # přechod je v p2 přesné řešení (spec)
                    self.assertLessEqual(v, top if family != "cheap" else cheap_top, name)
        p2 = [x for x in cases() if x[2] == "p2"]
        self.assertEqual(sum(x[0][-1] == x[1] for x in p2), 0)                       # opakuj poslední v p2: nikdy

    def test_exact_shortcut_rates(self):                     # výčet rozdělení generátoru (tvar × délka × rozehrávka × permutace), bez šumu
        for cls, top in (("p3", 0.43), ("p4", 0.41)):
            p, acc, total = int(cls[1]), {}, 0.0
            for shape, ws in bench.PATTERN_WEIGHTS[cls].items():
                for run, w in zip(range(8, 11), ws):
                    for warm in range(1, 4):
                        for perm in itertools.permutations(MOVES):
                            cycle = [dict(zip("ABC", perm))[ch] for ch in shape]
                            body = [cycle[(i - run) % p] for i in range(run)]
                            lasts = [m for m in MOVES if m != cycle[(-1 - run) % p]]
                            opens = [list(a) + [b] for a in itertools.product(MOVES, repeat=warm - 1) for b in lasts]
                            for opening in opens:
                                weight = w / 3 / 6 / len(opens)
                                for name, (_, v) in shortcut_hits(opening + body, cycle[0], p).items():
                                    acc[name] = acc.get(name, 0) + weight * v
                                total += weight
            with self.subTest(cls=cls):
                self.assertLessEqual(max(v / total for v in acc.values()), top)

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.pattern_cases(40), bench.pattern_cases(40, seed=1))
        self.assertNotEqual(bench.pattern_cases(40), bench.pattern_cases(40, seed=2))

    def test_unknown_class_and_broken_guarantee_raise(self):
        with self.assertRaises(RuntimeError):
            bench.pattern_case(random.Random(1), "p5")
        with mock.patch.dict(bench.PATTERN_WEIGHTS, {"p2": {"AA": (1, 1, 1)}}), self.assertRaises(RuntimeError):     # tvar s periodou 1 ve třídě p2
            bench.pattern_case(random.Random(1), "p2")


if __name__ == "__main__":
    unittest.main()
