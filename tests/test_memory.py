"""Scénář Paměťová stopa (bench.py --memory). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
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
TEN = bench.MEMORY_SIGNS[:10] if hasattr(bench, "MEMORY_SIGNS") else []      # circle … hexagon; ručně sestavená historie
SPEC = ["circle", "square", "star", "triangle", "arrow", "moon"]              # historie z příkladu ve specu


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def gates_of(state):
    return [k for k in state if k.startswith("gate_")]


def history_of(state):
    """Historie ze stavu v obou podobách: seznam, nebo hodnoty slovníku se štítky (obojí nejstarší první)."""
    passed = state["signs_passed"]
    return list(passed.values()) if isinstance(passed, dict) else list(passed)


def recall(state):
    """Model, který odpoví správně v obou podobách: dohledá značku pod štítkem target, nebo steps_back kroků zpět."""
    passed = state["signs_passed"]
    target = passed[state["target"]] if "target" in state else passed[-state["steps_back"]]
    return next(k for k in gates_of(state) if state[k] == target)


def newest(state):
    """Model, který N nečte a volí bránu s nejnovější značkou (nejblíž konci historie)."""
    history = history_of(state)
    return max(gates_of(state), key=lambda k: history.index(state[k]))


def positions(history, signs):
    """Indexy značek bran v historii (0 = nejstarší)."""
    return [history.index(s) for s in signs]


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Generator(unittest.TestCase):
    def test_alphabet_and_constants(self):
        self.assertEqual(bench.MEMORY_SIGNS, ("circle", "square", "star", "triangle", "arrow", "moon",
                                              "heart", "cross", "diamond", "hexagon", "bell", "leaf"))
        self.assertTrue(all(" " not in s for s in bench.MEMORY_SIGNS))
        self.assertEqual(list(bench.MEMORY_STEPS), [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual((bench.MEMORY_LENGTH_MAX, bench.MEMORY_SPAN_SHARE), (12, 0.8))

    def test_spec_example_gates(self):                                  # triangle 3 kroky zpět (index 3); okno 4 značek začíná na indexu 0, 1 nebo 2
        windows = set()
        for seed in range(60):
            signs, top = bench.memory_gates(random.Random(seed), SPEC, 3, 4)
            self.assertEqual(signs[top], "triangle")
            windows.add(frozenset(signs))
        self.assertEqual(windows, {frozenset(SPEC[0:4]), frozenset(SPEC[1:5]), frozenset(SPEC[2:6])})

    def test_window_on_a_hand_built_history(self):
        # (kroků zpět, bran) → všechny platné začátky okna v TEN; za 60 semínek se musí objevit každý a žádný jiný
        table = [
            (4, 4, {3, 4, 5, 6}),                                      # hledaná na indexu 6, okno kdekoli kolem ní
            (8, 4, {0, 1, 2}),                                         # hledaná na indexu 2, okno nesmí začít před historií
            (1, 3, {7}),                                               # N=1: hledaná je nejnovější, okno končí na konci historie
            (2, 2, {7, 8}),
            (10, 3, {0}),                                              # N=L: hledaná je nejstarší, okno začíná na začátku
            (4, 10, {0}),                                              # okno je celá historie
        ]
        for steps, gates, starts in table:
            seen = set()
            for seed in range(60):
                with self.subTest(steps=steps, gates=gates, seed=seed):
                    signs, top = bench.memory_gates(random.Random(seed), TEN, steps, gates)
                    pos = sorted(positions(TEN, signs))
                    self.assertEqual(pos, list(range(pos[0], pos[0] + gates)))         # souvislý úsek historie
                    self.assertEqual(signs[top], TEN[10 - steps])
                    seen.add(pos[0])
            self.assertEqual(seen, starts, (steps, gates))

    def test_cases_keep_the_guarantees(self):
        for gates in range(2, 9):
            for history, steps, signs, top in bench.memory_cases(500, gates):
                with self.subTest(gates=gates, history=history, steps=steps):
                    self.assertIn(steps, bench.MEMORY_STEPS)
                    self.assertTrue(max(steps, gates) <= len(history) <= 12)
                    self.assertEqual(len(set(history)), len(history))                 # bez opakování
                    self.assertTrue(set(history) <= set(bench.MEMORY_SIGNS))
                    self.assertEqual(len(signs), gates)
                    self.assertEqual(len(set(signs)), gates)
                    self.assertTrue(set(signs) <= set(history))
                    self.assertEqual(signs[top], history[len(history) - steps])
                    self.assertEqual(signs.count(history[len(history) - steps]), 1)

    def test_gates_are_a_window_around_the_target(self):               # spec (a)
        for gates in range(2, 9):
            for history, steps, signs, _ in bench.memory_cases(500, gates):
                pos = sorted(positions(history, signs))
                with self.subTest(gates=gates, history=history, steps=steps):
                    self.assertEqual(pos, list(range(pos[0], pos[0] + gates)))
                    self.assertIn(len(history) - steps, pos)

    def test_target_position_in_the_window_is_spread(self):           # spec (b): 1. až 4. od nejstarší, každá aspoň 10 %
        counts = Counter(sorted(positions(h, g)).index(len(h) - s) for h, s, g, _ in bench.memory_cases(2000, 4))
        self.assertEqual(sorted(counts), [0, 1, 2, 3])
        for k, n in counts.items():
            self.assertGreaterEqual(n / 2000, 0.10, (k, n))

    def test_strategies_without_counting_stay_near_chance(self):      # spec (c): strategie, které steps_back nečtou
        # Neprojde-li tento test, implementace se zastaví a zapíše to do reportu; generátor se neladí potichu.
        for gates in (3, 4, 8):
            cases, rng = bench.memory_cases(4000, gates), random.Random(1)

            def kth(k):                                                # vždy k-tá nejstarší značka okna
                return lambda h, s: sorted(range(gates), key=lambda i: h.index(s[i]))[k]

            def newest_sign(h, s):                                     # nejnovější značka historie, když je na branách; jinak náhodně
                return s.index(h[-1]) if h[-1] in s else rng.randrange(gates)

            def both_neighbours(h, s):                                 # značka, jejíž oba sousedé z historie jsou na branách; jinak náhodně
                p = set(positions(h, s))
                return rng.choice([i for i, x in enumerate(s) if h.index(x) - 1 in p and h.index(x) + 1 in p] or range(gates))

            strategies = {f"k={k + 1}": kth(k) for k in range(gates)} | {"newest": newest_sign, "neighbours": both_neighbours}
            for name, pick in strategies.items():
                acc = sum(pick(h, s) == top for h, _, s, top in cases) / len(cases)
                with self.subTest(gates=gates, strategy=name, acc=acc):
                    self.assertLessEqual(acc - 1 / gates, 0.15)

    def test_steps_are_spread_evenly(self):
        counts = Counter(steps for _, steps, _, _ in bench.memory_cases(800))
        self.assertEqual(sorted(counts), [1, 2, 3, 4, 5, 6, 7, 8])
        for steps, n in counts.items():
            self.assertTrue(60 <= n <= 140, (steps, n))

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.memory_cases(50, 4), bench.memory_cases(50, 4))
        self.assertEqual(bench.memory_cases(50), bench.memory_cases(50, 4, seed=1))
        self.assertNotEqual(bench.memory_cases(50, 4), bench.memory_cases(50, 4, seed=2))

    def test_broken_guarantee_raises_instead_of_fixing(self):
        for history, steps, gates in ((["moon", "moon", "star"], 1, 3),      # opakování v historii
                                      (["circle", "square"], 1, 3),          # méně značek než bran
                                      (SPEC, 1, 0),                          # žádná brána
                                      (SPEC, 7, 3), (SPEC, 0, 3)):           # N mimo 1…L
            with self.subTest(history=history, steps=steps, gates=gates):
                with self.assertRaises(RuntimeError):
                    bench.memory_gates(random.Random(1), history, steps, gates)


if __name__ == "__main__":
    unittest.main()
