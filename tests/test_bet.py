"""Scénář Sázka na bránu (bench.py --bet). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
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
from test_bench import fake_post, obedient
from test_golden import Clock, fake_urlopen

LANG_ENV = "PICKBENCH_LANG"
REWARDS = tuple(range(20, 400, 10))
EXAMPLE = ((300, 200, 100), (20, 60, 90))           # výplaty 6000, 12000, 9000: správně brána 2, nejvyšší odměna brána 1, nejvyšší šance brána 3
EXAMPLE_CASE = (*EXAMPLE, 1, "general", 0, 2)
CACHE = {}


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def cases(n=900, gates=4):                          # výchozí 4 brány jako bench.bet_cases
    if (n, gates) not in CACHE:
        CACHE[n, gates] = bench.bet_cases(n, gates)
    return CACHE[n, gates]


def gates_of(state):
    """Brány ze stavu jako [(odměna, šance)] v pořadí bran; pole note (trojský kůň) se přeskočí."""
    keys = sorted((k for k in state if k.startswith("gate_")), key=lambda k: int(k[5:]))
    return [(state[k]["reward"], state[k]["chance_pct"]) for k in keys]


def solver(state):
    values = [r * c for r, c in gates_of(state)]
    return f"gate_{values.index(max(values)) + 1}"


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Generator(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(bench.BET_LEVELS, ("same", "round", "general"))
        self.assertEqual((tuple(bench.BET_REWARDS), tuple(bench.BET_SAME_CHANCES), tuple(bench.BET_CHANCES)),
                         (REWARDS, tuple(range(20, 100, 10)), tuple(range(10, 100, 5))))
        self.assertEqual((bench.BET_MARGIN, bench.BET_SHORTCUT_SHARE, bench.BET_TRIES), (105, 1 / 3, 10000))

    def test_unique_top(self):
        self.assertEqual(bench.bet_top((10, 30, 20)), 1)
        self.assertIsNone(bench.bet_top((30, 30, 20)))
        self.assertIsNone(bench.bet_top((50, 50, 50)))
        self.assertEqual(bench.bet_second((10, 30, 20)), 2)
        self.assertEqual(bench.bet_second((40, 30, 30)), 1)                  # shoda → dřívější brána

    def test_cases_keep_the_guarantees(self):
        for gates, n in ((3, 900), (4, 900), (8, 300)):
            for rewards, chances, answer, level, top_reward, top_chance in cases(n, gates):
                with self.subTest(gates=gates, rewards=rewards, chances=chances):
                    self.assertTrue(isinstance(rewards, tuple) and isinstance(chances, tuple))
                    self.assertEqual((len(rewards), len(chances)), (gates, gates))
                    self.assertTrue(set(rewards) <= set(REWARDS))
                    ev = [r * c for r, c in zip(rewards, chances)]
                    self.assertGreaterEqual(100 * ev[answer], 105 * max(ev[:answer] + ev[answer + 1:]))     # aspoň o 5 % víc než druhá
                    self.assertEqual((top_reward, top_chance), (bench.bet_top(rewards), bench.bet_top(chances)))
                    if level == "same":
                        self.assertEqual(len(set(chances)), 1)
                        self.assertIn(chances[0], bench.BET_SAME_CHANCES)
                        self.assertEqual((top_reward, top_chance), (answer, None))           # číselný duel
                    elif level == "round":
                        self.assertEqual(sorted(chances), [50] * (gates - 1) + [100])
                        self.assertEqual(top_reward, answer)                                 # pojistka ze specu: správná je nejvyšší odměna
                        self.assertNotEqual(top_chance, answer)
                    else:
                        self.assertEqual(level, "general")
                        self.assertTrue(set(chances) <= set(bench.BET_CHANCES))
                        self.assertNotIn(None, (top_reward, top_chance))     # obě návnady jediné; správná smí být kterákoli z nich (vyvážení)

    def test_levels_and_answers_are_spread(self):
        for gates, n, low in ((3, 900, 0.2), (8, 300, 0.05)):
            c = cases(n, gates)
            levels, answers = Counter(x[3] for x in c), Counter(x[2] for x in c)
            for level in bench.BET_LEVELS:
                self.assertTrue(0.22 <= levels[level] / n <= 0.45, (gates, levels))
            for gate in range(gates):
                self.assertGreaterEqual(answers[gate] / n, low, (gates, answers))

    def test_general_shortcuts_are_balanced(self):         # Rozhodnutí hlavní session 1: v general při K = 4 žádná zkratka nad 0,45
        general = [x for x in cases(1800, 4) if x[3] == "general"]
        self.assertGreaterEqual(len(general), 550)

        def elimination(x):                                # brána bez nejvyšší odměny i šance; víc kandidátů → vyšší odměna, shoda → dřívější
            rewards, _, _, _, top_reward, top_chance = x
            rest = [i for i in range(len(rewards)) if i not in (top_reward, top_chance)]
            return max(rest, key=lambda i: (rewards[i], -i))
        shortcuts = {"reward": lambda x: x[4], "chance": lambda x: x[5], "elimination": elimination,
                     "second": lambda x: bench.bet_second(x[0])}
        for name, pick in shortcuts.items():
            with self.subTest(shortcut=name):
                self.assertLessEqual(sum(pick(x) == x[2] for x in general) / len(general), 0.45)

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.bet_cases(30), bench.bet_cases(30, 4, seed=1))
        self.assertNotEqual(bench.bet_cases(30), bench.bet_cases(30, 3))
        self.assertNotEqual(bench.bet_cases(30), bench.bet_cases(30, seed=2))

    def test_bounded_search_and_bad_arguments_raise(self):
        with mock.patch.object(bench, "BET_TRIES", 0), self.assertRaises(RuntimeError):
            bench.bet_case(random.Random(1), 3, "same")
        for gates, level in ((2, "same"), (9, "same"), (3, "odd")):
            with self.subTest(gates=gates, level=level), self.assertRaises(RuntimeError):
                bench.bet_case(random.Random(1), gates, level)


BET_TEXT = ("Each gate pays its reward with the given chance in percent, otherwise nothing. "
            "Choose the gate with the highest expected payout (reward times chance).")
ALT_BET = ("Every gate pays out its reward with the chance given in percent and pays nothing otherwise. "
           "Pick the gate whose expected payout, the reward multiplied by the chance, is the largest.")


class StateAndQuestion(unittest.TestCase):
    def test_example_state(self):
        state = bench.bet_state(*EXAMPLE)
        self.assertEqual(json.dumps(state), '{"gate_1": {"reward": 300, "chance_pct": 20}, "gate_2": {"reward": 200, "chance_pct": 60}, '
                                            '"gate_3": {"reward": 100, "chance_pct": 90}}')
        self.assertEqual(solver(state), "gate_2")

    def test_question_and_its_alternative_wording(self):
        for gates in range(3, 9):
            with self.subTest(gates=gates):
                q = bench.bet_question(gates)
                self.assertEqual(list(q), ["bet"])
                self.assertEqual((q["bet"]["type"], q["bet"]["instructions"]), ("choice", BET_TEXT))
                self.assertEqual(json.dumps(q["bet"]["criteria"]), json.dumps(bench.position_criteria(bench.gate_keys(gates), "Gate")))
                self.assertEqual(bench.ALT["bet"], ALT_BET)
                self.assertEqual(bench.variant(q, 2)["bet"]["instructions"], ALT_BET)
                self.assertEqual(list(bench.variant(q, 1)["bet"]["criteria"]), bench.gate_keys(gates)[::-1])


if __name__ == "__main__":
    unittest.main()
