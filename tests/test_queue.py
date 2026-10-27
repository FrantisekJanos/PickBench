"""Scénář Nejkratší fronta (bench.py --queue). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
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
EXAMPLE = (("car", "car"), ("truck",), ("bus", "van"))   # bez vah 2, 1, 2 (správně pruh 2); s vahami 2, 3, 4 (správně pruh 1)
CACHE = {}


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def cases(n=900, lanes=4, weighted=False):
    """Úlohy ze semínka 1; velké sady se losují jednou za běh modulu."""
    if (n, lanes, weighted) not in CACHE:
        CACHE[n, lanes, weighted] = bench.queue_cases(n, lanes, weighted)
    return CACHE[n, lanes, weighted]


def queues_of(state):
    """Fronty ze stavu v pořadí pruhů; pole note (trojský kůň) se přeskočí."""
    keys = sorted((k for k in state if k.startswith("lane_")), key=lambda k: int(k[5:]))
    return tuple(tuple(state[k]) for k in keys)


def solver(weighted):
    """Model, který spočítá délky front (s vahami nebo bez) a zvolí nejkratší."""
    def answer(state):
        lengths = [bench.queue_length(q, weighted) for q in queues_of(state)]
        return f"lane_{lengths.index(min(lengths)) + 1}"
    return answer


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Basics(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(bench.QUEUE_VEHICLES, ("car", "van", "bus", "truck", "motorbike", "tractor", "minibus", "pickup"))
        self.assertEqual(bench.QUEUE_WEIGHTS, {"bus": 3, "truck": 3, "tractor": 3})
        self.assertEqual((bench.QUEUE_SIZE, bench.QUEUE_GAPS), ((1, 12), ("gap1", "gap2-3", "gap4+")))
        self.assertEqual((bench.QUEUE_COUNT_SHARE, bench.QUEUE_TRIES), (0.5, 2000000))
        self.assertEqual([bench.queue_text_share(k) for k in (2, 3, 4, 8)], [1 / 2, 1 / 3, 1 / 3, 1 / 3])

    def test_lengths_and_classes(self):
        self.assertEqual([bench.queue_length(q) for q in EXAMPLE], [2, 1, 2])
        self.assertEqual([bench.queue_length(q, True) for q in EXAMPLE], [2, 3, 4])
        self.assertEqual(bench.queue_length(("tractor", "motorbike", "minibus", "pickup", "van"), True), 7)
        self.assertEqual([bench.queue_class(g) for g in (1, 2, 3, 4, 11)], ["gap1", "gap2-3", "gap2-3", "gap4+", "gap4+"])
        with self.assertRaises(ValueError):
            bench.queue_class(0)

    def test_shortest_text_and_fewest_vehicles(self):
        self.assertEqual(bench.queue_text(EXAMPLE), 1)                                    # '["truck"]' má 9 znaků
        self.assertEqual(bench.queue_text((("car", "van"), ("bus", "car"))), 0)            # shoda → dřívější pruh
        longer = (("motorbike", "tractor"), ("car", "van", "bus"))                         # 24 znaků proti 21: méně vozidel, delší text
        self.assertEqual((bench.queue_text(longer), bench.queue_count(longer)), (1, 0))
        self.assertEqual(bench.queue_count(EXAMPLE), 1)
        self.assertIsNone(bench.queue_count((("car",), ("van",), ("bus", "car"))))        # dvě fronty s jedním vozidlem
        self.assertEqual(bench.queue_heavy(EXAMPLE), 0)                                   # těžká vozidla 0, 1, 1
        self.assertEqual(bench.queue_heavy((("bus",), ("truck", "car"), ("van", "van", "van"))), 2)
        self.assertEqual(bench.queue_heavy((("bus", "car"), ("tractor",), ("truck", "van"))), 0)   # shoda → dřívější pruh


class Generator(unittest.TestCase):
    def test_cases_keep_the_guarantees(self):
        for lanes, n in ((2, 300), (4, 900), (8, 120)):
            for weighted in (False, True):
                for queues, answer, cls, text, count in cases(n, lanes, weighted):
                    with self.subTest(lanes=lanes, weighted=weighted, queues=queues):
                        self.assertIsInstance(queues, tuple)
                        self.assertEqual(len(queues), lanes)
                        self.assertTrue(all(isinstance(q, tuple) and 1 <= len(q) <= 12 and set(q) <= set(bench.QUEUE_VEHICLES)
                                            for q in queues))
                        lengths = [bench.queue_length(q, weighted) for q in queues]
                        rest = sorted(lengths[:answer] + lengths[answer + 1:])
                        self.assertLess(lengths[answer], rest[0])                         # jediná nejkratší fronta
                        self.assertEqual(bench.queue_class(rest[0] - lengths[answer]), cls)
                        self.assertEqual((text, count), (bench.queue_text(queues), bench.queue_count(queues)))
                        self.assertIsNotNone(count)
                        if not weighted:
                            self.assertEqual(count, answer)                               # bez vah je délka počet vozidel

    def test_classes_and_answers_are_spread(self):
        for weighted in (False, True):
            c = cases(900, 4, weighted)
            classes, answers = Counter(x[2] for x in c), Counter(x[1] for x in c)
            for cls in bench.QUEUE_GAPS:
                self.assertTrue(0.25 <= classes[cls] / 900 <= 0.42, (weighted, classes))
            for lane in range(4):
                self.assertGreaterEqual(answers[lane] / 900, 0.15, (weighted, answers))

    def test_shortest_text_is_balanced(self):
        def share(c, cls):
            sub = [x for x in c if x[2] == cls]
            return sum(x[3] == x[1] for x in sub) / len(sub)
        for weighted in (False, True):
            for cls in ("gap1", "gap2-3"):
                with self.subTest(weighted=weighted, cls=cls):
                    self.assertTrue(0.2 <= share(cases(900, 4, weighted), cls) <= 0.45)
                    self.assertTrue(0.3 <= share(cases(300, 2, weighted), cls) <= 0.7)      # dva pruhy: polovina (Rozpory 4)
        self.assertGreaterEqual(share(cases(900, 4), "gap4+"), 0.95)                       # rozdíl 4+ bez vah vyvážit nejde (spec)
        weighted = cases(900, 4, True)                                                     # nejméně těžkých vozidel (Rozhodnutí hlavní session 2)
        for cls in ("gap1", "gap2-3"):
            sub = [x for x in weighted if x[2] == cls]
            with self.subTest(heavy=cls):
                self.assertTrue(0.2 <= sum(bench.queue_heavy(x[0]) == x[1] for x in sub) / len(sub) <= 0.45)

    def test_fewest_vehicles_differ_in_half_of_the_weighted_tasks(self):
        c = cases(900, 4, True)
        self.assertTrue(0.4 <= sum(count != answer for _, answer, _, _, count in c) / 900 <= 0.6)

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.queue_cases(30, 4), bench.queue_cases(30))
        self.assertEqual(bench.queue_cases(30), bench.queue_cases(30, 4, False, seed=1))
        self.assertNotEqual(bench.queue_cases(30), bench.queue_cases(30, seed=2))
        self.assertNotEqual(bench.queue_cases(30), bench.queue_cases(30, weighted=True))

    def test_bounded_search_and_bad_arguments_raise(self):
        with mock.patch.object(bench, "QUEUE_TRIES", 0), self.assertRaises(RuntimeError):
            bench.queue_case(random.Random(1), 4, "gap1")
        for lanes, cls in ((1, "gap1"), (9, "gap1"), (4, "gap0")):
            with self.subTest(lanes=lanes, cls=cls), self.assertRaises(RuntimeError):
                bench.queue_case(random.Random(1), lanes, cls)


if __name__ == "__main__":
    unittest.main()
