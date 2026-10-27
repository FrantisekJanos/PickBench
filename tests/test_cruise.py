"""Scénář Tempomat (bench.py --cruise). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
import contextlib
import io
import json
import math
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
BANDS = (20, 40, 60, 100, 150, 220)
CACHE = {}


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def band_level_check(m):
    """Nezávislé čtení tabulky ze zadání (intervaly včetně hranic); přesně jeden řádek musí sedět."""
    rows = [(0, 19, 1), (20, 39, 2), (40, 59, 3), (60, 99, 4), (100, 149, 5), (150, 219, 6), (220, 10 ** 6, 7)]
    hit = [level for lo, hi, level in rows if lo <= m <= hi]
    assert len(hit) == 1, (m, hit)
    return hit[0]


def cases(n=3000):
    """Úlohy ze semínka 1; velké sady se losují jednou za běh modulu."""
    if n not in CACHE:
        CACHE[n] = bench.cruise_cases(n)
    return CACHE[n]


def solver(state):
    """Model, který přečte tabulku a odečte obě srážky."""
    return f"level_{bench.cruise_level(state['obstacle_m'], state['road'], state['fog'])}"


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Basics(unittest.TestCase):
    def test_constants(self):
        self.assertEqual((bench.CRUISE_TOP, bench.CRUISE_BANDS, bench.CRUISE_DIST, bench.CRUISE_EDGE), (7, BANDS, (8, 300), 3))
        self.assertEqual(bench.CRUISE_ROAD, {"dry": 0, "wet": 1, "icy": 2})
        self.assertEqual(bench.CRUISE_FOG, {"none": 0, "light": 1, "dense": 2})
        self.assertEqual((bench.CRUISE_CLASSES, bench.CRUISE_PLACES), (("table", "road", "fog", "both"), ("edge", "middle")))
        self.assertIn(bench.CRUISE_FORM, ("choice", "score"))
        self.assertEqual(list(bench.CRUISE_WEIGHTS), list(bench.CRUISE_CLASSES))
        self.assertEqual(sum(bench.CRUISE_WEIGHTS["road"].values()), sum(bench.CRUISE_WEIGHTS["fog"].values()))      # zrcadlové třídy

    def test_bands_match_the_table(self):
        for m in range(0, 401):
            with self.subTest(m=m):
                self.assertEqual(bench.band_level(m), band_level_check(m))
        for m, level in ((19, 1), (20, 2), (39, 2), (40, 3), (59, 3), (60, 4), (99, 4), (100, 5), (149, 5), (150, 6), (219, 6), (220, 7)):
            self.assertEqual(bench.band_level(m), level, m)
        self.assertEqual(bench.cruise_level(97, "icy", "none"), 2)
        self.assertEqual(bench.cruise_level(130, "wet", "dense"), 2)

    def test_band_ranges(self):
        self.assertEqual([bench.band_range(k) for k in range(1, 8)],
                         [(8, 19), (20, 39), (40, 59), (60, 99), (100, 149), (150, 219), (220, 300)])

    def test_edge_and_neighbor(self):
        self.assertEqual([bench.near_edge(m) for m in (8, 16, 17, 19, 20, 22, 23, 97, 103, 222, 223, 300)],
                         [False, False, True, True, True, True, False, True, False, True, False, False])
        self.assertEqual([bench.neighbor_level(m) for m in (19, 20, 98, 101, 217, 222)], [2, 1, 5, 4, 7, 6])


def cheap_shortcuts():
    """Levné zkratky, které pravidla nečtou (spec, oddíl Zkratky): stálý stupeň, jen podmínky, hrubá vzdálenost ve čtyřech podobách."""
    pen = lambda c: bench.CRUISE_ROAD[c[1]] + bench.CRUISE_FOG[c[2]]
    clip = lambda x: max(1, min(7, x))
    out = {f"always_{k}": (lambda k: lambda c: k)(k) for k in range(1, 8)}
    out["conditions_only"] = lambda c: clip(7 - pen(c))
    for lo, hi in ((2, 5), (3, 6), (2, 6), (3, 5)):
        out[f"rough_{lo}{hi}"] = (lambda lo, hi: lambda c: clip((lo if c[0] < 100 else hi) - pen(c)))(lo, hi)
    return out


class Generator(unittest.TestCase):
    def test_cases_keep_the_guarantees(self):
        for m, road, fog, answer, cls, place in cases():
            with self.subTest(m=m, road=road, fog=fog):
                self.assertIsInstance(m, int)
                self.assertTrue(8 <= m <= 300)
                self.assertTrue(1 <= answer <= 7)
                self.assertEqual(answer, band_level_check(m) - bench.CRUISE_ROAD[road] - bench.CRUISE_FOG[fog])
                self.assertIn(cls, bench.CRUISE_CLASSES)

    def test_conditions_match_the_class(self):
        for m, road, fog, answer, cls, place in cases():
            with self.subTest(cls=cls, road=road, fog=fog):
                self.assertEqual((road != "dry", fog != "none"),
                                 {"table": (False, False), "road": (True, False), "fog": (False, True), "both": (True, True)}[cls])

    def test_classes_and_places_are_spread(self):
        c = cases()
        classes = Counter(x[4] for x in c)
        for cls in bench.CRUISE_CLASSES:
            sub = [x for x in c if x[4] == cls]
            with self.subTest(cls=cls):
                self.assertTrue(0.22 <= classes[cls] / len(c) <= 0.28, classes)
                self.assertTrue(0.45 <= sum(x[5] == "edge" for x in sub) / len(sub) <= 0.55)

    def test_edge_tasks_lie_near_a_boundary(self):
        for m, *_, place in cases():
            with self.subTest(m=m, place=place):
                self.assertEqual(any(b - 3 <= m <= b + 2 for b in BANDS), place == "edge")
                if place == "edge":
                    self.assertLessEqual(min(abs(m - b) for b in BANDS), 3)

    def test_cheap_shortcuts_are_balanced(self):
        c = cases()
        for cls in ("road", "fog", "both"):
            sub = [x for x in c if x[4] == cls]
            for name, guess in cheap_shortcuts().items():
                with self.subTest(cls=cls, shortcut=name):
                    self.assertLessEqual(sum(guess(x) == x[3] for x in sub) / len(sub), 0.35)

    def test_distance_only_and_a_left_out_condition(self):
        c = cases()
        for cls, want in (("table", 1.0), ("road", 0.0), ("fog", 0.0), ("both", 0.0)):
            sub = [x for x in c if x[4] == cls]
            self.assertEqual(sum(band_level_check(x[0]) == x[3] for x in sub) / len(sub), want, cls)
        for x in (x for x in c if x[4] == "both"):                                   # vynechaná mlha / vynechaný povrch v both nikdy netrefí
            band, p, f = band_level_check(x[0]), bench.CRUISE_ROAD[x[1]], bench.CRUISE_FOG[x[2]]
            self.assertNotIn(x[3], (band - p, band - f), x)

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.cruise_cases(30), bench.cruise_cases(30, seed=1))
        self.assertNotEqual(bench.cruise_cases(30), bench.cruise_cases(30, seed=2))
        self.assertEqual(bench.cruise_cases(30), cases()[:30])                       # další úloha nemění ty předchozí

    def test_broken_guarantees_and_bad_arguments_raise(self):
        with mock.patch.dict(bench.CRUISE_WEIGHTS, {"both": {(1, 1, 1): 1}}), self.assertRaises(RuntimeError):
            bench.cruise_case(random.Random(1), "both", "middle")                   # stupeň 1 − 2 je pod 1
        for cls, place in (("floor", "edge"), ("table", "near")):
            with self.subTest(cls=cls, place=place), self.assertRaises(RuntimeError):
                bench.cruise_case(random.Random(1), cls, place)


CRUISE_TEXT = ("Choose the cruise control speed level, from 1 (slowest) to 7 (fastest). Start from obstacle_m, the distance in meters "
               "to the nearest obstacle ahead: under 20 m is level 1, 20 to 39 m level 2, 40 to 59 m level 3, 60 to 99 m level 4, "
               "100 to 149 m level 5, 150 to 219 m level 6, 220 m or more level 7. Then go one level lower on a wet road and two "
               "levels lower on an icy road. Then go one level lower in light fog and two levels lower in dense fog.")
ALT_SPEED = ("The cruise control has seven speed levels; 1 is the slowest and 7 the fastest. obstacle_m is how many meters ahead the "
             "nearest obstacle is, and it sets the starting level: level 1 below 20 m, level 2 from 20 m, level 3 from 40 m, level 4 "
             "from 60 m, level 5 from 100 m, level 6 from 150 m and level 7 from 220 m. A wet road takes one level off and an icy "
             "road two. Light fog takes one level off and dense fog two. Pick the level that is left.")
LEVELS = {"level_1": "Level 1 (slowest)", "level_2": "Level 2", "level_3": "Level 3", "level_4": "Level 4", "level_5": "Level 5",
          "level_6": "Level 6", "level_7": "Level 7 (fastest)"}


class StateAndQuestion(unittest.TestCase):
    def test_example_state(self):
        state = bench.cruise_state(97, "icy", "none")
        self.assertEqual(json.dumps(state), '{"obstacle_m": 97, "road": "icy", "fog": "none"}')
        self.assertEqual(solver(state), "level_2")

    def test_question_and_its_alternative_wordings(self):
        q = bench.cruise_question()
        self.assertEqual(list(q), ["speed"])
        self.assertEqual((q["speed"]["type"], q["speed"]["instructions"]), ("choice", CRUISE_TEXT))
        self.assertEqual(json.dumps(q["speed"]["criteria"]), json.dumps(LEVELS))
        self.assertEqual(bench.CRUISE_INS, CRUISE_TEXT)
        self.assertEqual(bench.ALT["speed"], ALT_SPEED)
        self.assertEqual(bench.variant(q, 2)["speed"]["instructions"], ALT_SPEED)
        self.assertEqual(list(bench.variant(q, 1)["speed"]["criteria"]), list(LEVELS)[::-1])

    def test_choice_level(self):
        self.assertEqual(bench.level_keys(), list(LEVELS))
        self.assertEqual([bench.choice_level(f"level_{k}") for k in range(1, 8)], list(range(1, 8)))
        for bad in (None, "level_0", "level_8", "level_03", "3", 3, ["level_1"], "Level 3"):
            with self.subTest(bad=bad):
                self.assertIsNone(bench.choice_level(bad))


if __name__ == "__main__":
    unittest.main()
