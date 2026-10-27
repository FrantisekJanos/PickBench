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


class Generator(unittest.TestCase):
    def test_cases_keep_the_guarantees(self):
        for lanes, n in ((2, 2000), (4, 4000), (8, 2000)):
            for before, after, answer, cls, mode in cases(n, lanes):
                with self.subTest(lanes=lanes, before=before, after=after):
                    self.assertEqual((len(before), len(after)), (lanes, lanes))
                    self.assertEqual([i for i in range(lanes) if before[i] != after[i]], [answer])     # jediná správná odpověď
                    self.assertEqual(bench.diff_distance(before[answer], after[answer]),
                                     {"num1": 1, "numnew": 4, "word1": 1, "wordnew": 4}[cls])
                    if cls in ("num1", "numnew"):
                        self.assertTrue(all(type(v) is int and 1000 <= v <= 9999 for v in before + after))
                    else:
                        self.assertTrue(all(v in bench.DIFF_WORDS for v in before + after))
                    if cls == "num1":
                        self.assertEqual(abs(before[answer] - after[answer]), 1)
                    if mode == "plain":
                        self.assertEqual((len(set(before)), len(set(after))), (lanes, lanes))
                    else:
                        self.assertEqual(mode, "repeat")

    def test_classes_modes_and_answers_are_spread(self):
        for lanes, n in ((2, 2000), (4, 4000), (8, 2000)):
            c = cases(n, lanes)
            classes, modes = Counter(x[3] for x in c), Counter(x[4] for x in c)
            for cls in bench.DIFF_CLASSES:
                self.assertTrue(0.2 <= classes[cls] / n <= 0.3, (lanes, classes))
            self.assertLessEqual(abs(modes["repeat"] / n - bench.DIFF_REPEAT_SHARE), 0.05, (lanes, modes))
        answers = Counter(x[2] for x in cases(4000, 4))
        self.assertTrue(all(abs(answers[i] / 4000 - 0.25) <= 0.05 for i in range(4)), answers)

    def test_repeat_rows_have_twins(self):
        repeat = [x for x in cases(4000, 4) if x[4] == "repeat"]
        self.assertTrue(0.6 <= sum(bool(bench.diff_twins(x)) for x in repeat) / len(repeat) <= 0.8)
        self.assertFalse(any(bench.diff_twins(x) for x in cases(4000, 4) if x[4] == "plain"))

    def test_twins(self):
        self.assertEqual(bench.diff_twins(WORDS_CASE), {1})                          # stará hodnota tape i v pruhu 2
        self.assertEqual(bench.diff_twins((("lane", "wall", "near", "lane"), ("stem", "wall", "near", "lane"), 0, "wordnew", "repeat")), {3})
        self.assertEqual(bench.diff_twins((("bolt", "wall"), ("colt", "colt"), 0, "word1", "repeat")), {1})    # nová hodnota i v pruhu 2
        self.assertEqual(bench.diff_twins(NUMBERS_CASE), set())

    def test_single_snapshot_shortcuts_stay_at_chance(self):
        def repeated(row):
            count = Counter(row)
            return {i for i, v in enumerate(row) if count[v] > 1}

        def extreme(row, pick):
            return {i for i, v in enumerate(row) if v == pick(row)} if type(row[0]) is int else set()

        def lonely(row):
            return {i for i in range(len(row)) if not any(bench.diff_distance(row[j], row[i]) == 1 for j in range(len(row)))}

        shortcuts = {"first": lambda b, a: {0}, "last": lambda b, a: {len(a) - 1},
                     "length": lambda b, a: {i for i in range(len(a)) if len(json.dumps(b[i])) != len(json.dumps(a[i]))},
                     "repeated_after": lambda b, a: repeated(a), "repeated_before": lambda b, a: repeated(b),
                     "unique_after": lambda b, a: set(range(len(a))) - repeated(a),
                     "max_after": lambda b, a: extreme(a, max), "min_after": lambda b, a: extreme(a, min),
                     "max_before": lambda b, a: extreme(b, max), "lonely_after": lambda b, a: lonely(a),
                     "related_after": lambda b, a: set(range(len(a))) - lonely(a),
                     "first_text_after": lambda b, a: {i for i, v in enumerate(a) if str(v) == min(map(str, a))}}

        def hit(case, pick):                               # očekávaná trefa: z kandidátů náhodně, prázdná množina = hádání
            found = pick(case[0], case[1])
            return 1 / len(case[0]) if not found else (1 / len(found) if case[2] in found else 0)
        c = cases(4000, 4)
        for cls in bench.DIFF_CLASSES:
            sub = [x for x in c if x[3] == cls]
            for name, pick in shortcuts.items():
                with self.subTest(cls=cls, shortcut=name):
                    self.assertLessEqual(sum(hit(x, pick) for x in sub) / len(sub), 0.25 + 0.08)
        new_value = lambda b, a: {i for i in range(len(a)) if a[i] not in b}     # srovnání množin, ne zkratka: repeat ho láme
        for mode, low, high in (("plain", 1, 1), ("repeat", 0, 0.8)):
            sub = [x for x in c if x[4] == mode]
            with self.subTest(mode=mode):
                self.assertTrue(low <= sum(hit(x, new_value) for x in sub) / len(sub) <= high)

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.diff_cases(30), bench.diff_cases(30, bench.DIFF_LANES, seed=1))
        self.assertEqual(bench.diff_cases(30, 6, seed=3), bench.diff_cases(30, 6, seed=3))
        self.assertNotEqual(bench.diff_cases(30, 4), bench.diff_cases(30, 4, seed=2))

    def test_bounded_search_and_bad_arguments_raise(self):
        with mock.patch.object(bench, "DIFF_TRIES", 0), self.assertRaises(RuntimeError):
            bench.diff_case(random.Random(1), 4, "num1", "plain")
        for lanes, cls, mode in ((1, "num1", "plain"), (9, "num1", "plain"), (4, "odd", "plain"), (4, "num1", "odd")):
            with self.subTest(lanes=lanes, cls=cls, mode=mode), self.assertRaises(RuntimeError):
                bench.diff_case(random.Random(1), lanes, cls, mode)


if __name__ == "__main__":
    unittest.main()
