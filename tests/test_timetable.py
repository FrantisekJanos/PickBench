"""Scénář Jízdní řád (bench.py --timetable). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
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
KEY = "depot"                                         # klíč otázky (Volby po pokusu V5)
DEPOTS = ("alder", "birch", "cedar", "maple", "oak", "pine")
ROWS = ((10000, 81333, "birch"), (81334, 87678, "maple"), (87679, 99999, "pine"))
DECOY = (ROWS, (2, 0, 1), 81335, "express", ("economy", "cedar"), "maple", "small", "near", "decoy")     # příklad ze specu
APPLIES = (ROWS, (2, 0, 1), 81335, "economy", ("economy", "cedar"), "cedar", "small", "near", "applies")
CACHE = {}


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def cases(n=900):
    """Úlohy ze semínka 1; velká sada se losuje jednou za běh modulu."""
    if n not in CACHE:
        CACHE[n] = bench.timetable_cases(n)
    return CACHE[n]


def sorted_rows(state):
    """Řádky tabulky ze stavu jako [(zip_from, zip_to, depo)] seřazené podle PSČ (v obou podobách stejné)."""
    return sorted((r["zip_from"], r["zip_to"], r["depot"]) for r in state["routes"])


def route_index(rows, z):
    hit = [i for i, (a, b, _) in enumerate(rows) if a <= z <= b]
    assert len(hit) == 1, (rows, z)
    return hit[0]


def valid_exception(state):
    return next((e["depot"] for e in state["exceptions"] if e["service"] == state["parcel"]["service"]), None)


def solve(state):
    """Nezávislý řešitel: platná výjimka, jinak řádek, do jehož intervalu (oba kraje včetně) PSČ patří."""
    rows = sorted_rows(state)
    return valid_exception(state) or rows[route_index(rows, state["parcel"]["zip"])][2]


def never_exception(state):
    """Kontrolní model: výjimky nečte."""
    rows = sorted_rows(state)
    return rows[route_index(rows, state["parcel"]["zip"])][2]


def always_exception(state):
    """Kontrolní model: je-li ve stavu výjimka, bere ji bez ohledu na službu."""
    return state["exceptions"][0]["depot"] if state["exceptions"] else never_exception(state)


def exclusive(state):
    """Kontrolní model: hranice bez rovnosti, PSČ rovné zip_from / zip_to bere jako sousední řádek; platnou výjimku pozná."""
    if valid_exception(state):
        return valid_exception(state)
    rows, z = sorted_rows(state), state["parcel"]["zip"]
    i = route_index(rows, z)
    j = i - 1 if z == rows[i][0] else i + 1 if z == rows[i][1] else i
    return rows[j][2] if 0 <= j < len(rows) else rows[i][2]


def expected_hit(answer, picks):
    """Očekávaná trefa zkratky, která ukazuje na množinu dep (remíza = náhodná volba mezi nimi)."""
    return (answer in picks) / len(picks)


def nearest(rows, z, end):
    """Depa řádků s číselně nejbližším zip_from (end=0) nebo zip_to (end=1)."""
    best = min(abs(z - r[end]) for r in rows)
    return {r[2] for r in rows if abs(z - r[end]) == best}


def most_frequent(rows):
    counts = Counter(r[2] for r in rows)
    return {d for d, k in counts.items() if k == max(counts.values())}


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Basics(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(bench.TIMETABLE_DEPOTS, DEPOTS)
        self.assertEqual(bench.TIMETABLE_SERVICES, ("standard", "express", "economy", "cooled"))
        self.assertEqual(bench.TIMETABLE_EXC_SERVICES, ("express", "economy", "cooled"))
        self.assertEqual((bench.TIMETABLE_ZIP, bench.TIMETABLE_MIN_WIDTH, bench.TIMETABLE_INNER), ((10000, 99999), 300, 100))
        self.assertEqual(bench.TIMETABLE_SIZES, {"small": (3, 5), "medium": (6, 8), "large": (9, 12)})
        self.assertEqual((bench.TIMETABLE_POSITIONS, bench.TIMETABLE_RULES), (("inner", "near", "bound"), ("none", "applies", "decoy")))
        self.assertEqual((bench.TIMETABLE_FORMS, bench.TIMETABLE_TRIES), (("sorted", "shuffled"), 1000))
        self.assertEqual(bench.TIMETABLE_NEAR, 9)                                        # Volby po pokusu V2
        self.assertEqual(bench.TIMETABLE_FORM, "sorted")                                 # Volby po pokusu V1
        self.assertEqual(bench.TIMETABLE_POSITION_RULES, ("none", "decoy"))              # Volby po pokusu V4

    def test_row_and_solve_by_hand(self):
        spans = [(a, b) for a, b, _ in ROWS]
        self.assertEqual([bench.timetable_row(spans, z) for z in (10000, 81333, 81334, 87678, 87679, 99999)], [0, 0, 1, 1, 2, 2])
        for z in (9999, 100000):
            with self.subTest(z=z), self.assertRaises(RuntimeError):
                bench.timetable_row(spans, z)
        with self.assertRaises(RuntimeError):
            bench.timetable_row([(10000, 50000), (50000, 99999)], 50000)                  # PSČ ve dvou řádcích
        state = {"parcel": {"zip": 81334, "service": "express"},
                 "routes": [{"zip_from": a, "zip_to": b, "depot": d} for a, b, d in ROWS], "exceptions": []}
        self.assertEqual(bench.timetable_solve(state), "maple")                          # zip_from patří do řádku
        self.assertEqual(bench.timetable_solve({**state, "parcel": {"zip": 81333, "service": "express"}}), "birch")   # zip_to také
        self.assertEqual(bench.timetable_solve({**state, "exceptions": [{"service": "express", "depot": "oak"}]}), "oak")
        self.assertEqual(bench.timetable_solve({**state, "exceptions": [{"service": "cooled", "depot": "oak"}]}), "maple")
        self.assertEqual(bench.timetable_solve({**state, "routes": state["routes"][::-1]}), "maple")   # pořadí řádků nerozhoduje

    def test_table_covers_the_range_without_gaps(self):
        rng = random.Random(3)
        for n in range(3, 13):
            for _ in range(30):
                rows = bench.timetable_table(rng, n)
                with self.subTest(n=n, rows=rows):
                    self.assertIsInstance(rows, tuple)
                    self.assertEqual(len(rows), n)
                    self.assertEqual((rows[0][0], rows[-1][1]), (10000, 99999))
                    self.assertTrue(all(rows[i][1] + 1 == rows[i + 1][0] for i in range(n - 1)))
                    self.assertGreaterEqual(min(b - a + 1 for a, b, _ in rows), 300)
                    self.assertTrue(all(rows[i][2] != rows[i + 1][2] for i in range(n - 1)))
                    self.assertTrue({d for _, _, d in rows} <= set(DEPOTS))
        with mock.patch.object(bench, "TIMETABLE_TRIES", 0), self.assertRaises(RuntimeError):
            bench.timetable_table(random.Random(1), 5)


if __name__ == "__main__":
    unittest.main()
