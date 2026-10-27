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
