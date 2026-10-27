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


if __name__ == "__main__":
    unittest.main()
