"""Scénář Co se změnilo (bench.py --diff). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
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
NUMBERS = ((2408, 2144, 4943, 2486), (2409, 2144, 4943, 2486))          # num1 v pruhu 1, plain
NUMBERS_CASE = (*NUMBERS, 0, "num1", "plain")
WORDS_CASE = (("tape", "tape", "park", "tear"), ("cape", "tape", "park", "tear"), 0, "word1", "repeat")   # dvojče v pruhu 2 (stará hodnota)
CACHE = {}


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def cases(n, lanes=4):
    if (n, lanes) not in CACHE:
        CACHE[n, lanes] = bench.diff_cases(n, lanes)
    return CACHE[n, lanes]


def solver(state):
    """Správný model pro obě podoby stavu; pole note (trojský kůň) se nečte."""
    before, after = state["before"], state["after"]
    if isinstance(before, dict):
        return next(k for k in before if before[k] != after[k])
    return f"lane_{next(i for i, (b, a) in enumerate(zip(before, after)) if b != a) + 1}"


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Pairs(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(bench.DIFF_CLASSES, ("num1", "numnew", "word1", "wordnew"))
        self.assertEqual((bench.DIFF_MODES, bench.DIFF_FORMS), (("plain", "repeat"), ("dicts", "lists")))
        self.assertEqual((bench.DIFF_NUMBERS, bench.DIFF_TRIES), ((1000, 9999), 1000))
        self.assertIn(bench.DIFF_FORM, bench.DIFF_FORMS)
        self.assertTrue(2 <= bench.DIFF_LANES <= 8 and 0 < bench.DIFF_REPEAT_SHARE < 1)
        self.assertEqual([bench.diff_pool_pairs(k) for k in range(2, 9)], [2, 3, 3, 4, 4, 5, 5])

    def test_words_and_their_relatives(self):
        words = bench.DIFF_WORDS
        self.assertEqual((len(words), len(set(words))), (180, 180))
        self.assertTrue(all(len(w) == 4 and w.isalpha() and w.islower() for w in words))
        self.assertEqual((bench.diff_distance("wear", "pear"), bench.diff_distance(2408, 2409), bench.diff_distance(3181, 5744)), (1, 1, 4))
        for w in words:
            with self.subTest(word=w):
                self.assertTrue(bench.DIFF_NEIGHBOURS[w] and len(bench.DIFF_FAR[w]) >= 63)
                self.assertTrue(all(v in words and bench.diff_distance(w, v) == 1 for v in bench.DIFF_NEIGHBOURS[w]))
                self.assertTrue(all(v in words and bench.diff_distance(w, v) == 4 for v in bench.DIFF_FAR[w]))

    def test_pairs_keep_the_class_relation(self):
        rng = random.Random(1)
        for cls in bench.DIFF_CLASSES:
            pairs = [bench.diff_pair(rng, cls) for _ in range(2000)]
            for old, new in pairs:
                with self.subTest(cls=cls, old=old, new=new):
                    if cls in ("num1", "numnew"):
                        self.assertTrue(type(old) is int and type(new) is int and 1000 <= min(old, new) and max(old, new) <= 9999)
                    else:
                        self.assertTrue(old in bench.DIFF_WORDS and new in bench.DIFF_WORDS)
                    self.assertEqual(bench.diff_distance(old, new), {"num1": 1, "numnew": 4, "word1": 1, "wordnew": 4}[cls])
                    if cls == "num1":
                        self.assertEqual(abs(old - new), 1)
            self.assertTrue(0.4 <= sum(old < new for old, new in pairs) / 2000 <= 0.6, cls)     # pořadí ve dvojici 1 : 1
        with self.assertRaises(RuntimeError):
            bench.diff_pair(rng, "odd")


if __name__ == "__main__":
    unittest.main()
