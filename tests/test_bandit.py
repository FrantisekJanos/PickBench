"""Scénář Mýtné brány (bench.py --bandit). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
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
from fractions import Fraction
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import bench
import i18n
from test_bench import fake_post, obedient
from test_golden import Clock, fake_urlopen

LANG_ENV = "PICKBENCH_LANG"
FORM = "totals"                                       # výchozí podoba stavu (Volby po pokusu)
EXPLORE = ((5, 9, 45), (4, 4, 90), (0, 6, 35), (1, 2, 70))       # příklad ze specu: správně gate_4 (jen 2 průjezdy)
EXPLOIT = ((6, 10, 70), (3, 4, 60), (5, 9, 50), (1, 3, 40))      # podíly 0,6 / 0,75 / 0,56 / 0,33: správně gate_2
TIE = ((2, 4, 50), (4, 8, 55), (1, 5, 20), (3, 9, 30))           # 2/4 a 4/8 ve shodě: správně gate_1 (méně průjezdů)
CACHE = {}


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def cases(n=900, k=4):
    """Úlohy ze semínka 1; velká sada se losuje jednou za běh modulu (900 úloh trvá asi 5 s na K)."""
    for (m, kk), got in CACHE.items():
        if kk == k and m >= n:
            return got[:n]                            # prvních n úloh velké sady = bandit_cases(n): stejné semínko, stejné pořadí
    CACHE[n, k] = bench.bandit_cases(n, k)
    return CACHE[n, k]


def by_hand(gates, answer, cls):
    """Ruční úloha s deníkem v pořadí bran (nejdřív výhry, pak prohry)."""
    log = tuple((i, True) for i, (w, _, _) in enumerate(gates) for _ in range(w)) + \
        tuple((i, False) for i, (w, n, _) in enumerate(gates) for _ in range(n - w))
    return gates, answer, cls, log


def totals_of(state):
    """Souhrn [(výhry, průjezdy)] ze stavu obou podob; pole note (trojský kůň) se přeskočí."""
    if "log" in state:
        drives = Counter(e["gate"] for e in state["log"])
        wins = Counter(e["gate"] for e in state["log"] if e["result"] == "win")
        return [(wins[g], drives[g]) for g in state["gates"]]
    keys = sorted((k for k in state if k.startswith("gate_")), key=lambda k: int(k[5:]))
    return [(state[k]["wins"], state[k]["drives"]) for k in keys]


def solve(state):
    """Nezávislý řešitel podle zadání: brána pod 3 průjezdy, jinak nejvyšší podíl (křížové násobení), při shodě méně průjezdů."""
    t = totals_of(state)
    fresh = [i for i, (_, n) in enumerate(t) if n < 3]
    if fresh:
        return f"gate_{fresh[0] + 1}"
    best = [i for i, (w, n) in enumerate(t) if all(w * m >= v * n for v, m in t)]
    return f"gate_{min(best, key=lambda i: t[i][1]) + 1}"


def greedy_tried(state):
    """Kontrolní model: pravidlo 1 vynechá, bere nejvyšší podíl mezi vyzkoušenými, při shodě nižší číslo brány."""
    t = totals_of(state)
    tried = [i for i, (_, n) in enumerate(t) if n >= 3]
    return f"gate_{max(tried, key=lambda i: (Fraction(*t[i]), -i)) + 1}"


def fewest_drives(state):
    """Kontrolní model: vždy průzkum, brána s nejméně průjezdy, při shodě nižší číslo."""
    t = totals_of(state)
    return f"gate_{min(range(len(t)), key=lambda i: (t[i][1], i)) + 1}"


def hit(picks, answer):
    """Očekávaná trefa zkratky, která ukazuje na množinu bran (remíza = náhodná volba mezi nimi)."""
    return (answer in picks) / len(picks)


def share(sub, name):
    return sum(hit(bench.BANDIT_SHORTCUTS[name]([(w, n) for w, n, _ in g]), a) for g, a, _, _ in sub) / len(sub)


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()



class Rule(unittest.TestCase):
    def test_constants(self):
        self.assertEqual((bench.BANDIT_GATES, bench.BANDIT_EXPLORE, bench.BANDIT_SPLIT, bench.BANDIT_TOTAL), ((4, 5), 3, ((3, 6), (9, 16)), 10))
        self.assertEqual((bench.BANDIT_MARGIN, bench.BANDIT_CHANCES), (Fraction(1, 10), tuple(range(10, 95, 5))))
        self.assertEqual((bench.BANDIT_CLASSES, bench.BANDIT_FORMS, bench.BANDIT_TRIES), (("explore", "exploit", "tie"), ("totals", "history"), 20000))
        self.assertEqual(bench.BANDIT_FRESH, ((0, 0), (0, 1), (0, 2), (1, 2), (1, 1), (2, 2)))
        self.assertEqual(bench.BANDIT_FORM, FORM)                                        # Volby po pokusu
        for k in bench.BANDIT_GATES:
            for cls in bench.BANDIT_CLASSES:
                with self.subTest(k=k, cls=cls):
                    patterns = bench.BANDIT_PATTERNS[k][cls]
                    self.assertTrue(all(len(p) == len(bench.BANDIT_WATCH[cls]) and set(p) <= set("HTM") for p in patterns))
                    self.assertTrue(all(isinstance(w, int) and 0 < w <= 30 for w in patterns.values()))

    def test_rule_matches_the_independent_check(self):              # spec: všechny souhrny pro K = 3 a 0–4 průjezdy (3 375)
        small = [(w, n) for n in range(5) for w in range(n + 1)]
        combos = list(itertools.product(small, repeat=3))
        self.assertEqual(len(combos), 3375)
        rng = random.Random(4)
        combos += [[(w, n) for n in (rng.randint(0, 16) for _ in range(k)) for w in (rng.randint(0, n),)] for k in (4, 5) for _ in range(3000)]
        for totals in combos:
            got = bench.bandit_answer(list(totals))
            self.assertEqual(got[0] if got else None, bench.bandit_check(list(totals)), totals)

    def test_rule_by_hand(self):
        self.assertEqual(bench.bandit_answer([(w, n) for w, n, _ in EXPLORE]), (3, "explore"))
        self.assertEqual(bench.bandit_answer([(w, n) for w, n, _ in EXPLOIT]), (1, "exploit"))
        self.assertEqual(bench.bandit_answer([(w, n) for w, n, _ in TIE]), (0, "tie"))
        self.assertEqual(bench.bandit_answer([(0, 0), (5, 9), (4, 8), (1, 3)]), (0, "explore"))       # 0/0 je brána k průzkumu
        self.assertEqual(bench.bandit_answer([(7, 12), (5, 8), (3, 9), (2, 4)]), (1, "exploit"))      # 5/8 > 7/12, ne podle výher
        self.assertEqual(bench.bandit_answer([(2, 4), (3, 6), (4, 8), (1, 5)]), (0, "tie"))   # shoda tří: rozhodnou průjezdy (generátor ji vyřadí odstupem)
        for totals in ([(1, 2), (0, 1), (5, 9), (3, 4)],                  # dvě brány pod 3 průjezdy
                       [(2, 4), (2, 4), (1, 5), (3, 9)]):                 # shoda dvou se stejnými průjezdy
            with self.subTest(totals=totals):
                self.assertIsNone(bench.bandit_answer(totals))
                self.assertIsNone(bench.bandit_check(totals))

    def test_shortcuts_by_hand(self):
        t = [(w, n) for w, n, _ in EXPLORE]
        s = bench.BANDIT_SHORTCUTS
        self.assertEqual((s["most_wins"](t), s["most_tries"](t), s["fewest_tries"](t), s["fewest_losses"](t), s["lowest_rate"](t)),
                         ({0}, {0}, {3}, {1}, {2}))
        self.assertEqual((s["greedy"](t), s["greedy_tried"](t), s["lowest_rate_tried"](t), s["tie_more"](t), s["exclusion"](t)), ({1}, {1}, {2}, {1}, {3}))
        t = [(w, n) for w, n, _ in TIE]
        self.assertEqual((s["greedy"](t), s["tie_more"](t), s["fewest_tries"](t)), ({0, 1}, {1}, {0}))
        self.assertEqual(bench.bandit_pattern("tie", t, 0), "HHMT")          # vyloučení: levné zkratky trefí všechny brány, zbudou všechny



if __name__ == "__main__":
    unittest.main()
