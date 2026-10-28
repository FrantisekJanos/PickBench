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



class Generator(unittest.TestCase):
    def test_cases_keep_the_guarantees(self):
        for k in bench.BANDIT_GATES:
            for gates, answer, cls, log in cases(900, k):
                totals = [(w, n) for w, n, _ in gates]
                with self.subTest(k=k, gates=gates):
                    self.assertTrue(isinstance(gates, tuple) and isinstance(log, tuple) and len(gates) == k)
                    self.assertTrue(all(0 <= w <= n and c in bench.BANDIT_CHANCES for w, n, c in gates))
                    self.assertEqual(bench.bandit_answer(totals), (answer, cls))
                    self.assertEqual(bench.bandit_check(totals), answer)
                    fresh = [i for i, (_, n) in enumerate(totals) if n < 3]
                    self.assertEqual(fresh, [answer] if cls == "explore" else [])               # nejvýš jedna brána k průzkumu
                    if cls == "explore":
                        self.assertIn(totals[answer], bench.BANDIT_FRESH)
                    self.assertTrue(all(n <= 16 for _, n in totals))
                    self.assertLessEqual(sum(n for _, n in totals), 10 * k)
                    shares = sorted((Fraction(w, n) for w, n in totals if n >= 3), reverse=True)
                    if cls == "exploit":
                        self.assertGreaterEqual(shares[0] - shares[1], Fraction(1, 10))
                    if cls == "tie":
                        pair = [i for i, (w, n) in enumerate(totals) if n >= 3 and Fraction(w, n) == shares[0]]
                        self.assertEqual(len(pair), 2)
                        self.assertNotEqual(totals[pair[0]][1], totals[pair[1]][1])
                        self.assertLess(shares[0], 1)
                        self.assertGreaterEqual(shares[1] - shares[2], Fraction(1, 10))
                    self.assertEqual(Counter(log), Counter([(i, True) for i, (w, _) in enumerate(totals) for _ in range(w)] +
                                                           [(i, False) for i, (w, n) in enumerate(totals) for _ in range(n - w)]))

    def test_classes_and_answers_are_spread(self):
        for k, low, high in ((4, 0.18, 0.32), (5, 0.14, 0.26)):
            c = cases(900, k)
            classes, answers = Counter(x[2] for x in c), Counter(x[1] for x in c)
            for cls in bench.BANDIT_CLASSES:
                self.assertTrue(0.28 <= classes[cls] / len(c) <= 0.38, (k, classes))
            for gate in range(k):
                self.assertTrue(low <= answers[gate] / len(c) <= high, (k, answers))

    def test_cheap_shortcuts_stay_near_chance(self):             # spec: každá levná zkratka nejvýš 0,40 (K = 4) a 0,36 (K = 5)
        for k, top in ((4, 0.40), (5, 0.36)):
            c = cases(900, k)
            for cls in bench.BANDIT_CLASSES:
                sub = [x for x in c if x[2] == cls]
                for name in ("most_wins", "most_tries", "fewest_tries", "fewest_losses", "lowest_rate", "lowest_rate_tried", "exclusion"):
                    if cls == "explore" and name == "fewest_tries":
                        continue                                  # v explore je to přímo pravidlo 1 (test níž)
                    with self.subTest(k=k, cls=cls, shortcut=name):
                        self.assertLessEqual(share(sub, name), top)

    def test_partial_strategies(self):                           # poloviny pravidla: každá padá ve své třídě
        for k in bench.BANDIT_GATES:
            c = cases(900, k)
            sub = {cls: [x for x in c if x[2] == cls] for cls in bench.BANDIT_CLASSES}
            with self.subTest(k=k):
                self.assertEqual(share(sub["exploit"], "greedy"), 1)
                self.assertLessEqual(share(sub["explore"], "greedy"), 0.35)
                self.assertEqual(share(sub["tie"], "greedy"), 0.5)                     # dvojice ve shodě: polovina
                self.assertEqual(share(sub["explore"], "fewest_tries"), 1)
                self.assertLessEqual(max(share(sub["exploit"], "fewest_tries"), share(sub["tie"], "fewest_tries")), 0.35)
                self.assertEqual(share(sub["tie"], "tie_more"), 0)
                self.assertEqual(share(sub["exploit"], "tie_more"), 1)

    def test_log_order_is_not_a_shortcut(self):                  # spec: „poslední výhra“ a „poslední průjezd“ nejvýš 0,35 v každé třídě
        for k in bench.BANDIT_GATES:
            c = cases(900, k)
            for cls in bench.BANDIT_CLASSES:
                sub = [x for x in c if x[2] == cls]
                last_win = sum(([i for i, won in log if won] or [None])[-1] == answer for _, answer, _, log in sub) / len(sub)
                last_drive = sum(log[-1][0] == answer for _, answer, _, log in sub if log) / len(sub)
                with self.subTest(k=k, cls=cls):
                    self.assertLessEqual(max(last_win, last_drive), 0.35)

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.bandit_cases(20), bench.bandit_cases(20, 4, seed=1))
        self.assertEqual(bench.bandit_cases(20), cases(900)[:20])
        self.assertNotEqual(bench.bandit_cases(20), bench.bandit_cases(20, seed=2))
        self.assertNotEqual([x[0] for x in bench.bandit_cases(20)], [x[0][:4] for x in bench.bandit_cases(20, 5)])

    def test_bounded_search_and_bad_arguments_raise(self):
        with mock.patch.object(bench, "BANDIT_TRIES", 50), self.assertRaises(RuntimeError):
            bench.bandit_case(random.Random(1), 4, "explore", {"HHMMM": 1})        # nejvyšší i nejnižší podíl naráz: nesplnitelné
        for k, cls in ((3, "explore"), (6, "tie"), (4, "odd")):
            with self.subTest(k=k, cls=cls), self.assertRaises(RuntimeError):
                bench.bandit_case(random.Random(1), k, cls)
        with mock.patch.object(bench, "BANDIT_TRIES", 0), self.assertRaises(RuntimeError):
            bench.bandit_log(random.Random(1), EXPLORE, 3)

    def test_tie_shares(self):
        self.assertEqual(len(bench.BANDIT_TIE_SHARES), 21)
        self.assertEqual(bench.BANDIT_TIE_SHARES[:3], ((1, 8), (1, 7), (1, 6)))
        self.assertEqual(bench.BANDIT_TIE_SHARES[-1], (7, 8))
        for a, b in bench.BANDIT_TIE_SHARES:
            self.assertTrue(0 < a < b and Fraction(a, b).denominator == b and len(bench.bandit_tie_multiples(b)) >= 2, (a, b))


if __name__ == "__main__":
    unittest.main()
