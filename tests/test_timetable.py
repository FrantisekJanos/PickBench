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


def position(rows, i, z):
    """Třída polohy PSČ z ve správném řádku i: vzdálenost od hranic, které mají souseda (okraj 10000 a 99999 se nepočítá)."""
    a, b, _ = rows[i]
    edges = [z - a] * (i > 0) + [b - z] * (i < len(rows) - 1)
    if min(z - a, b - z) >= bench.TIMETABLE_INNER:
        return "inner"
    if min(edges) == 0:
        return "bound"
    return "near" if 1 <= min(edges) <= bench.TIMETABLE_NEAR else None


class Generator(unittest.TestCase):
    def test_cases_keep_the_guarantees(self):
        for case in cases():
            rows, order, z, service, exception, answer, size, pos, rule = case
            with self.subTest(case=case):
                self.assertTrue(isinstance(rows, tuple) and isinstance(order, tuple))
                self.assertEqual(sorted(order), list(range(len(rows))))
                lo, hi = bench.TIMETABLE_SIZES[size]
                self.assertTrue(lo <= len(rows) <= hi)
                self.assertEqual((rows[0][0], rows[-1][1]), (10000, 99999))
                self.assertTrue(all(rows[k][1] + 1 == rows[k + 1][0] and rows[k][2] != rows[k + 1][2] for k in range(len(rows) - 1)))
                self.assertGreaterEqual(min(b - a + 1 for a, b, _ in rows), 300)
                i = route_index(rows, z)
                self.assertEqual(position(rows, i, z), pos)
                near = {rows[j][2] for j in (i - 1, i + 1) if 0 <= j < len(rows)}
                self.assertIn(service, bench.TIMETABLE_SERVICES)
                if rule == "none":
                    self.assertIsNone(exception)
                else:
                    self.assertIn(exception[0], bench.TIMETABLE_EXC_SERVICES)
                    self.assertNotIn(exception[1], near | {rows[i][2]})               # chyby výjimky a souseda se nepřekrývají
                    if rule == "applies":
                        self.assertEqual(service, exception[0])
                    else:
                        self.assertIn(service, bench.TIMETABLE_EXC_SERVICES)          # decoy: jiná nestandardní služba
                        self.assertNotEqual(service, exception[0])
                self.assertEqual(answer, exception[1] if rule == "applies" else rows[i][2])
                for form in bench.TIMETABLE_FORMS:
                    self.assertEqual(solve(bench.timetable_state(case, form)), answer)

    def test_classes_answers_and_sides_are_spread(self):
        c = cases()
        for k, order in ((6, bench.TIMETABLE_SIZES), (7, bench.TIMETABLE_POSITIONS), (8, bench.TIMETABLE_RULES)):
            counts = Counter(x[k] for x in c)
            for cls in order:
                self.assertTrue(0.25 <= counts[cls] / 900 <= 0.42, counts)
        answers = Counter(x[5] for x in c)
        for depot in DEPOTS:
            self.assertTrue(0.10 <= answers[depot] / 900 <= 0.25, answers)
        edge = [x for x in c if x[7] != "inner"]
        low = sum(x[2] - x[0][route_index(x[0], x[2])][0] <= bench.TIMETABLE_NEAR for x in edge)      # u zip_from, ne u zip_to
        self.assertTrue(0.4 <= low / len(edge) <= 0.6, low / len(edge))

    def test_table_shortcuts_stay_well_below_the_answer(self):
        for form in bench.TIMETABLE_FORMS:
            for pos in bench.TIMETABLE_POSITIONS:
                sub = [x for x in cases() if x[7] == pos and x[8] != "applies"]
                first = sum(expected_hit(x[5], {bench.timetable_state(x, form)["routes"][0]["depot"]}) for x in sub) / len(sub)
                frequent = sum(expected_hit(x[5], most_frequent(x[0])) for x in sub) / len(sub)
                with self.subTest(form=form, pos=pos):
                    self.assertLessEqual(first, 0.45)
                    self.assertLessEqual(frequent, 0.45)

    def test_a_boundary_is_one_of_two_rows(self):           # spec: u hranice je správně jeden ze dvou sousedních řádků, laťka 0,5
        near = [x for x in cases() if x[7] == "near" and x[8] != "applies"]
        for x in near:
            start, end = expected_hit(x[5], nearest(x[0], x[2], 0)), expected_hit(x[5], nearest(x[0], x[2], 1))
            self.assertEqual(start + end, 1, x)                                         # nejbližší začátek a konec se doplňují
        for end in (0, 1):
            share = sum(expected_hit(x[5], nearest(x[0], x[2], end)) for x in near) / len(near)
            self.assertTrue(0.35 <= share <= 0.65, (end, share))
        for pos, want in (("inner", 1), ("near", 1), ("bound", 0)):                     # hranice bez rovnosti padá jen na hranici
            sub = [x for x in cases() if x[7] == pos and x[8] != "applies"]
            hits = sum(exclusive(bench.timetable_state(x)) == x[5] for x in sub) / len(sub)
            self.assertEqual(hits, want, pos)

    def test_exception_is_balanced(self):
        with_exception = [x for x in cases() if x[4] is not None]
        for model in (always_exception, never_exception):
            share = sum(model(bench.timetable_state(x)) == x[5] for x in with_exception) / len(with_exception)
            self.assertTrue(0.4 <= share <= 0.6, (model.__name__, share))

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.timetable_cases(40), bench.timetable_cases(40, seed=1))
        self.assertNotEqual(bench.timetable_cases(40), bench.timetable_cases(40, seed=2))

    def test_bounded_search_and_broken_guarantee_raise(self):
        with mock.patch.object(bench, "TIMETABLE_TRIES", 0), self.assertRaises(RuntimeError):
            bench.timetable_case(random.Random(1), "small", "inner", "none")
        for size, pos, rule in (("huge", "inner", "none"), ("small", "edge", "none"), ("small", "inner", "maybe")):
            with self.subTest(size=size, pos=pos, rule=rule), self.assertRaises(RuntimeError):
                bench.timetable_case(random.Random(1), size, pos, rule)
        same = lambda rng, n: tuple((a, b, "oak") for a, b, _ in ROWS)                # sousední řádky se stejným depem
        with mock.patch.object(bench, "timetable_table", same), self.assertRaises(RuntimeError):
            bench.timetable_case(random.Random(1), "small", "inner", "none")


class State(unittest.TestCase):
    def test_spec_example_state(self):
        routes = ('"routes": [{"zip_from": 10000, "zip_to": 81333, "depot": "birch"}, {"zip_from": 81334, "zip_to": 87678, "depot": "maple"}, '
                  '{"zip_from": 87679, "zip_to": 99999, "depot": "pine"}]')
        self.assertEqual(json.dumps(bench.timetable_state(DECOY, "sorted")),
                         '{"parcel": {"zip": 81335, "service": "express"}, ' + routes + ', "exceptions": [{"service": "economy", "depot": "cedar"}]}')
        shuffled = bench.timetable_state(DECOY, "shuffled")
        self.assertEqual([r["depot"] for r in shuffled["routes"]], ["pine", "birch", "maple"])      # pořadí z order
        self.assertEqual(bench.timetable_state(DECOY), bench.timetable_state(DECOY, bench.TIMETABLE_FORM))
        self.assertEqual(bench.timetable_state(DECOY[:4] + (None, "maple", "small", "near", "none"))["exceptions"], [])
        for state in (bench.timetable_state(DECOY, "sorted"), shuffled):
            self.assertEqual((solve(state), bench.timetable_solve(state)), ("maple", "maple"))
        self.assertEqual(bench.timetable_solve(bench.timetable_state(APPLIES)), "cedar")
        with self.assertRaises(ValueError):
            bench.timetable_state(DECOY, "random")

    def test_both_forms_carry_the_same_task(self):
        for x in cases()[:300]:
            a, b = bench.timetable_state(x, "sorted"), bench.timetable_state(x, "shuffled")
            with self.subTest(case=x):
                self.assertEqual(list(a), ["parcel", "routes", "exceptions"])
                self.assertEqual((a["parcel"], a["exceptions"]), (b["parcel"], b["exceptions"]))
                self.assertEqual([(r["zip_from"], r["zip_to"], r["depot"]) for r in a["routes"]], list(x[0]))     # sorted = podle PSČ
                self.assertEqual(sorted_rows(b), list(x[0]))
                self.assertEqual(bench.timetable_solve(b), x[5])


if __name__ == "__main__":
    unittest.main()
