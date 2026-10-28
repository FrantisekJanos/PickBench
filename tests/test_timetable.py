"""Scénář Jízdní řád (bench.py --timetable). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
import contextlib
import io
import json
import os
import random
import re
import runpy
import shutil
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
from test_barrier import REGISTRY, page_function, page_source, run_page_js      # kus skriptu stránky v node

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


INSTRUCTIONS = ("Route the parcel to a depot. Each row of routes covers the ZIP codes from zip_from to zip_to, both included, "
                "and sends those parcels to its depot. exceptions override routes: a parcel whose service matches an exception "
                "goes to that exception's depot instead. Choose the depot for the parcel.")
ALT_TEXT = ("Find the depot this parcel goes to. If exceptions lists the parcel's service, that exception's depot wins. "
            "Otherwise take the routes row whose range from zip_from up to zip_to, including both ends, holds the parcel's zip.")


class Question(unittest.TestCase):
    def test_question_and_its_alternative_wording(self):
        q = bench.timetable_question()
        self.assertEqual(list(q), [KEY])
        self.assertEqual((q[KEY]["type"], q[KEY]["instructions"]), ("choice", INSTRUCTIONS))
        self.assertEqual(json.dumps(q[KEY]["criteria"]), json.dumps({d: f"Depot {d.capitalize()}" for d in DEPOTS}))
        self.assertEqual(bench.ALT[KEY], ALT_TEXT)
        self.assertEqual(bench.variant(q, 2)[KEY]["instructions"], ALT_TEXT)
        self.assertEqual(list(bench.variant(q, 1)[KEY]["criteria"]), list(DEPOTS)[::-1])

    def test_prompt_stays_under_600_tokens(self):          # odhad: 4 znaky JSON na token; cíl pro hru 600, limit tev1 2050
        q = json.dumps(bench.timetable_question())
        longest = max(len(json.dumps(bench.with_note(bench.timetable_state(x, form), "pine"))) for x in cases() for form in bench.TIMETABLE_FORMS)
        self.assertLess((longest + len(q)) / 4, 600)


class RunTimetable(unittest.TestCase):
    def run_with(self, answer, *args, **kw):
        with mock.patch.object(bench, "post", fake_post(answer)):
            return bench.run_timetable(*args, **kw)

    def test_errors_and_classes_by_hand(self):
        trap = DECOY[:4] + (("economy", "birch"),) + DECOY[5:]           # výjimka na depo souseda: výjimka navíc má přednost před sousedem
        c = [DECOY] * 7 + [APPLIES] * 3 + [trap]
        choices = ["maple", "birch", "cedar", "pine", "alder", None, "Depot Alder", "maple", "birch", "cedar", "birch"]
        s = bench.timetable_summary(c, choices)
        self.assertEqual(json.dumps(s["errors"]), json.dumps({"neighbor": 3, "missed": 1, "misapplied": 2, "other": 1, "invalid": 2}))
        self.assertEqual(s["by_size"], {"small": [2, 11], "medium": [0, 0], "large": [0, 0]})
        self.assertEqual(s["by_position"], {"inner": [0, 0], "near": [1, 8], "bound": [0, 0]})       # jen none a decoy (V4)
        self.assertEqual(s["by_rule"], {"none": [0, 0], "applies": [1, 3], "decoy": [1, 8]})
        self.assertEqual(bench.timetable_errors(c, choices), s["errors"])

    def test_perfect_model(self):
        c = cases(120)
        for form in bench.TIMETABLE_FORMS:
            with self.subTest(form=form):
                r = self.run_with(solve, "m", c, form=form)
                self.assertEqual(r["acc"], 1)
                self.assertEqual(list(r["by_size"]), list(bench.TIMETABLE_SIZES))
                self.assertEqual((list(r["by_position"]), list(r["by_rule"])), (list(bench.TIMETABLE_POSITIONS), list(bench.TIMETABLE_RULES)))
                self.assertEqual(sum(n for _, n in r["by_size"].values()), 120)
                self.assertEqual(sum(n for _, n in r["by_position"].values()), sum(x[8] in bench.TIMETABLE_POSITION_RULES for x in c))
                self.assertTrue(all(ok == n > 0 for part in ("by_size", "by_position", "by_rule") for ok, n in r[part].values()))
                self.assertEqual(json.dumps(r["errors"]), json.dumps({"neighbor": 0, "missed": 0, "misapplied": 0, "other": 0, "invalid": 0}))

    def test_control_models_fall_into_their_error_class(self):
        c = cases(300)
        for model, key, wrong in ((exclusive, "neighbor", lambda x: x[7] == "bound" and x[8] != "applies"),
                                  (never_exception, "missed", lambda x: x[8] == "applies"),
                                  (always_exception, "misapplied", lambda x: x[8] == "decoy")):
            with self.subTest(model=model.__name__):
                r = self.run_with(model, "m", c)
                n = sum(map(wrong, c))
                self.assertGreater(n, 0)
                self.assertEqual(r["errors"], {"neighbor": 0, "missed": 0, "misapplied": 0, "other": 0, "invalid": 0} | {key: n})
                self.assertEqual(r["acc"], (300 - n) / 300)

    def test_invalid_answers(self):
        c = cases(30)
        for answer in (None, "Depot Oak", "hub", ["oak"], 3):
            with self.subTest(answer=answer):
                r = self.run_with(lambda state: answer, "m", c)
                self.assertEqual((r["acc"], r["errors"]["invalid"]), (0, 30))

    def test_requests_carry_the_state_and_the_question(self):
        c = bench.timetable_cases(5)
        for form in bench.TIMETABLE_FORMS:
            sent = []

            def post(model, payload):
                sent.append(payload)
                return {"answers": {next(iter(payload["questions"])): {"choice": "oak", "confidence": 0.5}}}, 10.0
            with mock.patch.object(bench, "post", post):
                bench.run_timetable("m", c, form=form)
            with self.subTest(form=form):
                self.assertEqual(len(sent), 6)                                  # zahřátí a pět úloh
                self.assertEqual([p["state"] for p in sent], [bench.timetable_state(c[0], form)] + [bench.timetable_state(x, form) for x in c])
                self.assertTrue(all(p["questions"] == bench.timetable_question() for p in sent))

    def test_mirror_and_inject(self):
        c = cases(40)
        r = self.run_with(solve, "m", c, mirror=True)
        self.assertEqual((r["stab"], r["acc"]), (1, 1))
        r = self.run_with(obedient(solve), "m", c, inject=True)
        self.assertEqual((r["inject"]["n"], r["inject"]["fooled"], r["inject"]["clean_acc"]), (10, 1, 1))
        self.assertEqual(r["acc"], 0.75)
        e = r["errors"]
        self.assertEqual((e["neighbor"] + e["missed"] + e["misapplied"] + e["other"], e["invalid"]), (10, 0))


class Lines(unittest.TestCase):
    def test_note_by_size(self):
        r = {"by_size": {"small": [5, 10], "medium": [9, 11], "large": [3, 12]}}
        for lang, want in (("cs", "(3–5 řádků 5/10, 6–8 řádků 9/11, 9–12 řádků 3/12)"), ("en", "(3–5 rows 5/10, 6–8 rows 9/11, 9–12 rows 3/12)")):
            with i18n.process(lang):
                self.assertEqual(bench.timetable_note(r), want)

    def test_lines_in_both_languages(self):
        r = {"by_position": {"inner": [9, 10], "near": [6, 10], "bound": [2, 10]}, "by_rule": {"none": [8, 10], "applies": [7, 10], "decoy": [5, 10]},
             "errors": {"neighbor": 4, "missed": 3, "misapplied": 5, "other": 1, "invalid": 2}}
        pad = " " * 20
        self.assertEqual(printed("cs", bench.print_timetable, r),
                         [pad + "poloha: uvnitř 9/10, u hranice 6/10, na hranici 2/10; výjimka: žádná 8/10, platí 7/10, neplatí 5/10",
                          pad + "chyby: sousední řádek 4, výjimka přehlédnutá 3, výjimka navíc 5, jiné 1, neplatné 2"])
        self.assertEqual(printed("en", bench.print_timetable, r),
                         [pad + "position: inside 9/10, near a boundary 6/10, on a boundary 2/10; exception: none 8/10, applies 7/10, does not apply 5/10",
                          pad + "errors: neighboring row 4, exception missed 3, exception wrongly applied 5, other 1, invalid 2"])

    def test_lines_with_empty_classes(self):
        r = {"by_size": {"small": [1, 1], "medium": [0, 0], "large": [0, 0]},
             "by_position": {"inner": [0, 0], "near": [0, 0], "bound": [0, 0]}, "by_rule": {"none": [0, 0], "applies": [1, 1], "decoy": [0, 0]},
             "errors": {"neighbor": 0, "missed": 0, "misapplied": 0, "other": 0, "invalid": 0}}
        with i18n.process("en"):
            self.assertEqual(bench.timetable_note(r), "(3–5 rows 1/1, 6–8 rows 0/0, 9–12 rows 0/0)")
        self.assertEqual(len(printed("en", bench.print_timetable, r)), 2)


class Cli(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)                            # jazyk z prostředí vývojáře tu nesmí rozhodovat
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(LANG_ENV, None)
        self.sent = []

    def cli(self, *argv):
        """Pustí bench.py jako příkaz proti podstrčené Ollamě. Vrací (výstup, čím skončil: None nebo argument sys.exit)."""
        def urlopen(req, timeout=None):
            self.sent.append(req.data)
            return fake_urlopen(req, timeout)
        out, code = io.StringIO(), None
        with mock.patch.object(sys, "argv", ["bench.py", *argv]), mock.patch.object(urllib.request, "urlopen", urlopen), \
                mock.patch.object(time, "perf_counter", Clock()), contextlib.redirect_stdout(out):
            try:
                runpy.run_path(str(ROOT / "bench.py"), run_name="__main__")
            except SystemExit as e:
                code = e.code
        return out.getvalue(), code

    def test_parse(self):
        a = bench.parse_args(["40", "--timetable", "m1"])
        self.assertEqual((a.n, a.timetable, a.sorted, a.shuffled, a.lanes_given, a.models), (40, True, False, False, False, ["m1"]))
        a = bench.parse_args(["--shuffled", "--timetable", "m1"])
        self.assertEqual((a.timetable, a.sorted, a.shuffled), (True, False, True))
        self.assertTrue(bench.parse_args(["--timetable", "--sorted", "m1"]).sorted)

    def test_first_line_and_rows(self):
        form = {"sorted": ("řádky podle PSČ", "rows sorted by ZIP"), "shuffled": ("řádky zamíchané", "rows shuffled")}
        default = form[bench.TIMETABLE_FORM]
        for argv, (cs, en) in ((["1", "--timetable", "m1"], ("1 úloha se směrováním zásilek podle tabulky (3–12 řádků, {})",
                                                             "1 task routing a parcel by a table (3–12 rows, {})")),
                               (["7", "--timetable", "--shuffled", "m1"], ("7 úloh se směrováním zásilek podle tabulky (3–12 řádků, {})",
                                                                           "7 tasks routing a parcel by a table (3–12 rows, {})")),
                               (["3", "--sorted", "--timetable", "m1"], ("3 úlohy se směrováním zásilek podle tabulky (3–12 řádků, {})",
                                                                         "3 tasks routing a parcel by a table (3–12 rows, {})"))):
            words = form["shuffled"] if "--shuffled" in argv else form["sorted"] if "--sorted" in argv else default
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[0].splitlines()[0], cs.format(words[0]) + "; sada je pro všechny modely stejná")
                self.assertEqual(self.cli(*argv, "--lang", "en")[0].splitlines()[0], en.format(words[1]) + ", the same for all models")
        self.assertTrue(self.cli("--timetable", "m1")[0].startswith("100 úloh se směrováním zásilek"))      # výchozí počet úloh
        en = self.cli("6", "--timetable", "m1", "--lang", "en")[0].splitlines()
        self.assertEqual(len(en), 6)
        self.assertRegex(en[3], r"^m1 .*   \(3–5 rows \d+/\d+, 6–8 rows \d+/\d+, 9–12 rows \d+/\d+\)$")
        self.assertRegex(en[4], r"^ {20}position: inside \d+/\d+, near a boundary \d+/\d+, on a boundary \d+/\d+; "
                                r"exception: none \d+/\d+, applies \d+/\d+, does not apply \d+/\d+$")
        self.assertRegex(en[5], r"^ {20}errors: neighboring row \d+, exception missed \d+, exception wrongly applied \d+, other \d+, invalid \d+$")
        self.assertEqual(len(self.cli("1", "--timetable", "m1")[0].splitlines()), 6)          # jediná úloha: prázdné třídy 0/0

    def test_option_errors_in_both_languages(self):
        form_cs, form_en = "Volby --sorted a --shuffled jdou jen s --timetable.", "The --sorted and --shuffled options work only with --timetable."
        for argv, cs, en in (
                (["6", "--timetable", "--lanes", "3", "m1"], "Volba --lanes se s --timetable nekombinuje.",
                 "The --lanes option cannot be combined with --timetable."),
                (["6", "--shuffled", "m1"], form_cs, form_en),
                (["--sorted", "--queue", "m1"], form_cs, form_en),
                (["--timetable", "--rows", "m1"], "Volby --rows a --blocked jdou jen s --lookahead.", "The --rows and --blocked options work only with --lookahead."),
                (["--timetable", "--weighted", "m1"], "Volba --weighted jde jen s --queue.", "The --weighted option works only with --queue.")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", cs))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", en))
        for argv in (["--timetable", "--queue", "m1"], ["--timetable", "--bet", "m1"], ["--timetable", "--rule", "m1"],
                     ["--timetable", "--cruise", "m1"],
                     ["--timetable", "--sorted", "--shuffled", "m1"], ["--timetable", "--lanes", "9", "m1"], ["--timetable"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", bench.__doc__))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", bench.USAGE_EN))
        self.assertEqual(self.sent, [])

    def test_both_forms_send_the_same_tasks(self):
        self.cli("6", "--timetable", "--sorted", "m1")
        ordered, self.sent = [json.loads(body) for body in self.sent], []
        self.cli("6", "--timetable", "--shuffled", "m1")
        shuffled = [json.loads(body) for body in self.sent]
        self.assertEqual((len(ordered), len(shuffled)), (7, 7))                  # zahřátí a šest úloh
        self.assertTrue(all(list(p["questions"]) == [KEY] for p in ordered + shuffled))
        for a, b in zip((p["state"] for p in ordered), (p["state"] for p in shuffled)):
            self.assertEqual((a["parcel"], a["exceptions"]), (b["parcel"], b["exceptions"]))
            self.assertEqual(sorted_rows(a), sorted_rows(b))
            self.assertEqual([(r["zip_from"], r["zip_to"], r["depot"]) for r in a["routes"]], sorted_rows(a))
        self.assertNotEqual([p["state"]["routes"] for p in ordered], [p["state"]["routes"] for p in shuffled])

    def test_mirror_and_inject_work_with_timetable(self):
        out, code = self.cli("8", "--timetable", "--mirror", "--inject", "--deadline-ms", "9", "m1")
        self.assertIsNone(code)
        lines = out.splitlines()
        self.assertIn("stabilita", lines[2])
        self.assertIn("goodput", lines[2])
        self.assertTrue(lines[4].strip().startswith("přesnost variant: "), lines[4])
        self.assertTrue(lines[5].strip().startswith("trojský kůň: 2 úloh s pokynem"), lines[5])
        self.assertTrue(lines[6].strip().startswith("poloha: uvnitř "), lines[6])
        self.assertTrue(lines[7].strip().startswith("chyby: sousední řádek "), lines[7])

    def test_usage_names_the_options(self):
        for text in (bench.__doc__, bench.USAGE_EN):
            self.assertIn("python bench.py [N] --timetable [--sorted | --shuffled] [--mirror] [--inject] [--deadline-ms L] model1 model2 ...", text)
            self.assertIn("python bench.py 100 --timetable tev1", text)
            self.assertIn("\n--timetable:  ", text)
            self.assertIn("\n--sorted, --shuffled:  ", text)
            self.assertIn(bench.TIMETABLE_FORM + ".", text)


def timetable_body():
    return page_source().split("\nconst timetable = {", 1)[1].split("\n};\n", 1)[0]


class Page(unittest.TestCase):              # scénář ve stránce; tabulka, generátor a rozbor hlídá tests/test_parity.py (Timetable)
    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_variant_matches_bench(self):
        page = page_source()
        alt = re.search(r"\nconst ALT = \{.*?\n\};\n", page, re.S).group(0)
        lanes = re.search(r"\nconst ALT_LANES = [^\n]*\n", page).group(0)
        q = bench.timetable_question()
        code = alt + lanes + page_function("variantOf") + "const Q = " + json.dumps(q) + ";" + (
            "process.stdout.write(JSON.stringify([0, 1, 2].map(v => variantOf(JSON.parse(JSON.stringify(Q)), v))));")
        for v, js in enumerate(run_page_js(code)):
            self.assertEqual(json.dumps(js), json.dumps(bench.variant(q, v)), v)     # varianta 1 obrácené pořadí dep, 2 ALT.depot

    def test_scenario_is_offered_selected_and_wired(self):
        page = page_source()
        self.assertEqual(page.count('<option value="timetable" data-i18n="opt.scenario.timetable"></option>'), 1)
        self.assertEqual(page.count(REGISTRY), 1)
        self.assertEqual(page.count("// ---------- scénář: jízdní řád ----------"), 1)
        # zahřívací úloha je pevná a bez pokynu: nebere z rnd, jinak by se řady posunuly podle řidiče
        self.assertEqual(page.count("  warmTask() { return this.task(timetableWarm(), null, null); },"), 1)
        self.assertEqual(page.count("  spawnRow(g, y) { g.rows.push(this.newRow(g, y, timetableRow(rnd), 6)); },"), 1)
        self.assertEqual(page.count("  randomTask() { return this.task(timetableRow(rnd)); },"), 1)
        body = timetable_body()
        for call in ("rollInject([d.truth], 6)", "timetableState(d)", "injectText(INJECT_NOTE, TIMETABLE_DEPOTS[inject])",
                     "variantOf(timetableQuestion(), v)", "a.depot?.choice", "timetableSummary(", "panels: ['confPanel', 'legend']"):
            self.assertIn(call, body)                                                             # definice ze sdíleného bloku, ne opsané

    def test_lanes_are_fixed(self):                                # pruh = depo: vždy 6, volič zamčený, řady po 6 polích
        page = page_source()
        body = timetable_body()
        self.assertIn("  id: 'timetable', fixedLanes: 6, lanes: () => 6,", body)
        self.assertIn("  gap: TIMETABLE_GAP, keep: 4.5,", body)
        self.assertEqual(page.count("const TIMETABLE_GAP = 6;"), 1)
        self.assertEqual(page.count("if (SC.fixedLanes) { lanes.dataset.free ??= lanes.value; lanes.value = SC.fixedLanes; }"), 1)     # jádro beze změny

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_tally_keeps_the_task_and_the_choice(self):            # počítají se všechny odpovědi; neplatná je null
        tally = re.search(r"\n  tally\(g, task, move\) \{.*?\n  \},", timetable_body(), re.S).group(0)[1:-1]
        code = ("const TIMETABLE_DEPOTS = ['alder', 'birch', 'cedar', 'maple', 'oak', 'pine'];"
                "const base = { tally() {} }; const o = { __proto__: base," + tally + "};"
                "const g = { cases: [], choices: [] }, task = { rows: [[10000, 99999, 'oak']], zip: 12345, service: 'cooled', exception: null,"
                " answer: 'oak', size: 'small', pos: 'inner', rule: 'none', truth: 4, state: {} };"
                "[4, 0, -1].forEach(m => o.tally(g, task, m)); process.stdout.write(JSON.stringify(g));")
        case = {"rows": [[10000, 99999, "oak"]], "zip": 12345, "service": "cooled", "exception": None, "answer": "oak",
                "size": "small", "pos": "inner", "rule": "none"}
        self.assertEqual(run_page_js(code), {"cases": [case] * 3, "choices": ["oak", "alder", None]})

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_last_decision_names_depots_in_the_page_language(self):
        from test_parity import run_js
        page = page_source()
        method = re.search(r"\n  describeLast\(task, res\) \{.*?\n  \},", timetable_body(), re.S).group(0)[1:-1]
        consts = "".join(re.search(r"\nconst %s = [^\n]*\n" % n, page).group(0) for n in ("TIMETABLE_DEPOT_NAME", "TIMETABLE_SERVICE_NAME"))
        code = "(() => {" + consts + page_function("timetableDepot") + page_function("timetableService") + "const o = {" + method + "};" + (
            "return o.describeLast({ rows: [[10000, 81333, 'birch'], [81334, 99999, 'maple']], zip: 81335, service: 'express',"
            " exception: ['economy', 'cedar'], truth: 3 }, { move: 2 }); })()")
        cs, en = run_js(code, None), run_js(code, None, "en")
        self.assertEqual(cs.split("\n"), ["zásilka: PSČ 81335, služba expresní", "trasy:   10000–81333 Bříza, 81334–99999 Javor",
                                          "výjimky: úsporná → Cedr", "zvoleno: Cedr  (správně: Javor)",
                                          "model:   depa a služby dostává anglicky (alder, birch, …; standard, express, …)"])
        self.assertEqual(en.split("\n")[3], "chosen:     Cedar  (correct: Maple)")

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_board_marks_what_decided(self):                       # rozvržení cedule a zvýraznění po průjezdu: řádek s PSČ, nebo platná výjimka
        from test_parity import run_js, timetable_js
        code = "(() => {" + page_function("timetableBoard") + (
            "return arg.map(([c, result]) => timetableBoard({ ...c, result })); })()")
        twelve = bench.timetable_cases(200, seed=5)
        big = next(c for c in twelve if len(c[0]) == 12)
        small = next(c for c in twelve if len(c[0]) == 4)
        got = run_js(code, [[timetable_js(DECOY), "ok"], [timetable_js(APPLIES), "bad"], [timetable_js(DECOY), None],
                            [timetable_js(big), "ok"], [timetable_js(small), "late"]])
        decoy, applies, before, wide, four = got
        self.assertEqual(decoy["parcel"], {"zip": 81335, "service": "express"})
        self.assertEqual(decoy["exception"], {"service": "economy", "depot": "cedar"})
        self.assertEqual((decoy["mark"], decoy["struck"]), (1, True))          # PSČ 81335 je v řádku 81334–87678; výjimka pro jinou službu
        self.assertEqual((applies["mark"], applies["struck"]), ("exception", False))
        self.assertEqual((before["mark"], before["struck"]), (None, False))   # před průjezdem nic: divák vidí volbu modelu, ne řešení
        self.assertEqual(decoy["cols"], 1)
        self.assertEqual([(x["from"], x["to"], x["depot"]) for x in decoy["lines"]], list(ROWS))
        self.assertEqual(wide["cols"], 2)
        self.assertEqual([(x["col"], x["line"]) for x in wide["lines"]], [(0, i) for i in range(6)] + [(1, i) for i in range(6)])
        self.assertEqual([(x["from"], x["to"], x["depot"]) for x in wide["lines"]], list(big[0]))          # v pořadí PSČ, jako ve stavu
        want = "exception" if small[8] == "applies" else bench.timetable_row([r[:2] for r in small[0]], small[2])
        self.assertEqual((four["cols"], four["mark"], max(x["line"] for x in four["lines"])), (1, want, 3))

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_labels_name_the_outcome(self):             # štítek: trasa, výjimka, výjimka přehlédnutá / navíc, sousední řádek, jiné, neplatná, nestihl
        from test_parity import run_js, timetable_js
        page = page_source()
        consts = "".join(re.search(r"\nconst %s = [^\n]*\n" % n, page).group(0) for n in ("RESULT", "TIMETABLE_DEPOT_NAME", "TIMETABLE_SERVICE_NAME"))
        code = "(() => {" + consts + page_function("timetableDepot") + page_function("timetableService") + page_function("timetableLabel") + (
            "return arg.map(([c, result, chosen]) => timetableLabel({ ...c, result, chosen })); })()")
        d = list(bench.TIMETABLE_DEPOTS).index
        rows = [[timetable_js(DECOY), "ok", d("maple")], [timetable_js(APPLIES), "ok", d("cedar")], [timetable_js(APPLIES), "bad", d("maple")],
                [timetable_js(DECOY), "bad", d("cedar")], [timetable_js(DECOY), "bad", d("birch")], [timetable_js(DECOY), "bad", d("oak")],
                [timetable_js(DECOY), "bad", -1], [timetable_js(DECOY), "late", None]]
        cs, en = run_js(code, rows), run_js(code, rows, "en")
        self.assertEqual([x["text"] for x in cs], [
            "✓ Javor: PSČ v řádku 81334–87678", "✓ Cedr: platí výjimka úsporná → Cedr",
            "✗ výjimka přehlédnutá: zvolil Javor, platí výjimka úsporná → Cedr",
            "✗ výjimka navíc: zvolil Cedr, výjimka je jen pro službu úsporná; správně Javor",
            "✗ sousední řádek: zvolil Bříza, PSČ 81335 patří do 81334–87678 → Javor", "✗ zvolil Dub, správně Javor",
            "✗ neplatná odpověď", "⏱ nestihl"])
        self.assertEqual(en[4]["text"], "✗ neighboring row: picked Birch, ZIP 81335 belongs to 81334–87678 → Maple")
        self.assertEqual([x["color"] for x in cs], ["#3fb950"] * 2 + ["#f85149"] * 4 + ["#8b9aa8"] * 2)

    def test_drawing_writes_only_through_t(self):
        self.assertNotIn("fillText(", page_function("timetableBoard"))                         # rozvržení bez kreslení a bez textů
        draw = page_function("drawTimetable")
        self.assertEqual(re.findall(r"fillText\(\s*['\"`][^'\"`]*[A-Za-zÁ-ž]", draw), [])          # žádný doslovný text s písmeny
        for part in ("timetableBoard(r)", "timetableLabel(r)", "drawRowLabel(", "t('timetable.parcel'", "t('timetable.exception'",
                     "t('timetable.noException')", "timetableDepot(", "TIMETABLE_COLOR[", "strokeRect("):
            self.assertIn(part, draw)
        self.assertIn("service: timetableService(board.parcel.service)", draw)                  # služba na cedulce přeložená, ne anglické slovo modelu
        self.assertNotIn("t('timetable.parcel', board.parcel)", draw)
        self.assertIn("legend: [[null, 'legend.timetable.board'], [null, 'legend.timetable.exception'], [null, 'legend.timetable.gates'], ['#3fb950', 'legend.timetable.out']],",
                      timetable_body())


if __name__ == "__main__":
    unittest.main()
