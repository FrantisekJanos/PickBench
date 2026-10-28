"""Scénář Co se změnilo (bench.py --diff). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
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
from test_barrier import REGISTRY, page_function, page_source, run_page_js      # kus skriptu stránky v node, registr scénářů
from test_queue import method                                                   # víceřádková metoda objektu scénáře

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


CHANGED = ("A camera took two photos of the same row of lanes, first before and then after. Each lane shows one value, "
           "a number or a word. Exactly one lane shows a different value in after than in before. Choose that lane.")
CHANGED_LIST = ("A camera took two photos of the same row of lanes, first before and then after. Each lane shows one value, "
                "a number or a word. The lists before and after hold the lane values from left to right; the first item is lane 1. "
                "Exactly one lane shows a different value in after than in before. Choose that lane.")
ALT_CHANGED = ("Two photos show the same row of lanes, before first and after second, with one value per lane. "
               "In after, the value of exactly one lane differs from its value in before. Pick that lane.")
ALT_CHANGED_LIST = ("Two photos show the same row of lanes, before first and after second; each one lists the lane values from left "
                    "to right, so its first item belongs to lane 1. In after, the value of exactly one lane differs from its value "
                    "in before. Pick that lane.")


class StateAndQuestion(unittest.TestCase):
    def test_example_state_in_both_forms(self):
        self.assertEqual(json.dumps(bench.diff_state(*NUMBERS, "dicts")),
                         '{"before": {"lane_1": 2408, "lane_2": 2144, "lane_3": 4943, "lane_4": 2486}, '
                         '"after": {"lane_1": 2409, "lane_2": 2144, "lane_3": 4943, "lane_4": 2486}}')
        self.assertEqual(json.dumps(bench.diff_state(*NUMBERS, "lists")),
                         '{"before": [2408, 2144, 4943, 2486], "after": [2409, 2144, 4943, 2486]}')
        self.assertEqual(json.dumps(bench.diff_state(*WORDS_CASE[:2], "lists")),
                         '{"before": ["tape", "tape", "park", "tear"], "after": ["cape", "tape", "park", "tear"]}')
        self.assertEqual(bench.diff_state(*NUMBERS), bench.diff_state(*NUMBERS, bench.DIFF_FORM))
        for form in bench.DIFF_FORMS:
            self.assertEqual(solver(bench.diff_state(*NUMBERS, form)), "lane_1")

    def test_questions_and_their_alternative_wordings(self):
        for lanes in range(2, 9):
            for form, key, text, alt in (("dicts", "changed", CHANGED, ALT_CHANGED), ("lists", "changed_list", CHANGED_LIST, ALT_CHANGED_LIST)):
                with self.subTest(lanes=lanes, form=form):
                    q = bench.diff_question(lanes, form)
                    self.assertEqual(list(q), [key])
                    self.assertEqual((q[key]["type"], q[key]["instructions"]), ("choice", text))
                    self.assertEqual(json.dumps(q[key]["criteria"]), json.dumps(bench.position_criteria(bench.lane_keys(lanes), "Lane")))
                    self.assertEqual(bench.ALT[key], alt)
                    self.assertEqual(bench.variant(q, 2)[key]["instructions"], alt)
                    self.assertEqual(list(bench.variant(q, 1)[key]["criteria"]), bench.lane_keys(lanes)[::-1])
        self.assertEqual(bench.diff_question(4), bench.diff_question(4, bench.DIFF_FORM))


class RunDiff(unittest.TestCase):
    def run_with(self, answer, *args, **kw):
        with mock.patch.object(bench, "post", fake_post(answer)):
            return bench.run_diff(*args, **kw)

    def test_summary_by_hand(self):
        s = bench.diff_summary([NUMBERS_CASE] * 6 + [WORDS_CASE] * 3,
                               ["lane_1", "lane_2", "lane_3", None, "lane_9", ["lane_1"], "lane_2", "lane_1", "lane_4"], bench.lane_keys(4))
        self.assertEqual(s["by_class"], {"num1": [1, 6], "numnew": [0, 0], "word1": [1, 3], "wordnew": [0, 0]})
        self.assertEqual(s["by_mode"], {"plain": [1, 6], "repeat": [1, 3]})
        self.assertEqual(json.dumps(s["errors"]), json.dumps({"twin": 1, "neighbor": 1, "other": 2, "invalid": 3}))   # i pořadí klíčů

    def test_summary_of_one_task(self):
        s = bench.diff_summary([WORDS_CASE], ["lane_1"], bench.lane_keys(4))
        self.assertEqual(s["by_class"], {"num1": [0, 0], "numnew": [0, 0], "word1": [1, 1], "wordnew": [0, 0]})
        self.assertEqual(s["errors"], {"twin": 0, "neighbor": 0, "other": 0, "invalid": 0})

    def test_perfect_model(self):
        for form in bench.DIFF_FORMS:
            with self.subTest(form=form):
                r = self.run_with(solver, "m", cases(120), 4, form=form)
                self.assertEqual(r["acc"], 1)
                self.assertEqual(list(r["by_class"]), list(bench.DIFF_CLASSES))
                self.assertEqual(list(r["by_mode"]), list(bench.DIFF_MODES))
                self.assertEqual(sum(n for _, n in r["by_class"].values()), 120)
                self.assertTrue(all(ok == n > 0 for ok, n in [*r["by_class"].values(), *r["by_mode"].values()]))
                self.assertEqual(r["errors"], {"twin": 0, "neighbor": 0, "other": 0, "invalid": 0})

    def test_invalid_answers(self):
        for answer in (None, "lane_9", "gate_1", ["lane_1"]):
            with self.subTest(answer=answer):
                r = self.run_with(lambda state: answer, "m", cases(30), 4)
                self.assertEqual((r["acc"], r["errors"]), (0, {"twin": 0, "neighbor": 0, "other": 0, "invalid": 30}))

    def test_requests_carry_the_state_and_the_question(self):
        c = bench.diff_cases(5, 6)
        for form in bench.DIFF_FORMS:
            sent = []

            def post(model, payload):
                sent.append(payload)
                return {"answers": {next(iter(payload["questions"])): {"choice": "lane_1", "confidence": 0.5}}}, 10.0
            with self.subTest(form=form), mock.patch.object(bench, "post", post):
                bench.run_diff("m", c, 6, form=form)
                self.assertEqual(len(sent), 6)                                  # zahřátí a pět úloh
                self.assertEqual([p["state"] for p in sent], [bench.diff_state(*c[0][:2], form)] + [bench.diff_state(*x[:2], form) for x in c])
                self.assertTrue(all(p["questions"] == bench.diff_question(6, form) for p in sent))

    def test_mirror_and_inject(self):
        for form in bench.DIFF_FORMS:
            with self.subTest(form=form):
                r = self.run_with(solver, "m", cases(40), 4, mirror=True, form=form)
                self.assertEqual((r["stab"], r["acc"]), (1, 1))
                r = self.run_with(obedient(solver), "m", cases(40), 4, inject=True, form=form)
                self.assertEqual((r["inject"]["n"], r["inject"]["fooled"], r["inject"]["clean_acc"]), (10, 1, 1))
                self.assertEqual(r["acc"], 0.75)
                e = r["errors"]
                self.assertEqual((e["twin"] + e["neighbor"] + e["other"], e["invalid"]), (10, 0))


class Lines(unittest.TestCase):
    def test_note_by_class(self):
        r = {"by_class": {"num1": [5, 10], "numnew": [9, 10], "word1": [3, 10], "wordnew": [10, 10]}}
        for lang, want in (("cs", "(číslo o 1 5/10, jiné číslo 9/10, slovo o písmeno 3/10, jiné slovo 10/10)"),
                           ("en", "(number off by 1 5/10, other number 9/10, word off by a letter 3/10, other word 10/10)")):
            with i18n.process(lang):
                self.assertEqual(bench.diff_note(r), want)

    def test_line_in_both_languages(self):
        r = {"by_mode": {"plain": [19, 20], "repeat": [12, 20]}, "errors": {"twin": 5, "neighbor": 2, "other": 1, "invalid": 1}}
        self.assertEqual(printed("cs", bench.print_diff, r),
                         [" " * 20 + "řady bez opakování 19/20, s opakováním 12/20; chyby: stejná hodnota v jiném pruhu 5, sousední pruh 2, jiné 1, neplatné 1"])
        self.assertEqual(printed("en", bench.print_diff, r),
                         [" " * 20 + "rows without repeats 19/20, with repeats 12/20; errors: same value in another lane 5, next lane 2, other 1, invalid 1"])

    def test_lines_with_one_task(self):
        r = {"by_class": {"num1": [0, 0], "numnew": [0, 0], "word1": [1, 1], "wordnew": [0, 0]},
             "by_mode": {"plain": [0, 0], "repeat": [1, 1]}, "errors": {"twin": 0, "neighbor": 0, "other": 0, "invalid": 0}}
        with i18n.process("en"):
            self.assertEqual(bench.diff_note(r), "(number off by 1 0/0, other number 0/0, word off by a letter 1/1, other word 0/0)")
        self.assertEqual(printed("en", bench.print_diff, r),
                         [" " * 20 + "rows without repeats 0/0, with repeats 1/1; errors: same value in another lane 0, next lane 0, other 0, invalid 0"])


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
        a = bench.parse_args(["40", "--diff", "m1"])
        self.assertEqual((a.n, a.diff, a.dicts, a.lists, a.lanes, a.models), (40, True, False, False, 8, ["m1"]))
        a = bench.parse_args(["--lists", "--diff", "--lanes", "2", "m1"])
        self.assertEqual((a.diff, a.lists, a.lanes), (True, True, 2))
        self.assertEqual(bench.parse_args(["m1"]).lanes, 3)

    def test_first_line_and_rows(self):
        for argv, cs, en in ((["1", "--diff", "m1"], "1 úloha se dvěma snímky řady (zápis seznamy), 8 pruhů",
                              "1 task with two snapshots of a row (written as lists), 8 lanes"),
                             (["7", "--diff", "--lists", "--lanes", "2", "m1"], "7 úloh se dvěma snímky řady (zápis seznamy), 2 pruhy",
                              "7 tasks with two snapshots of a row (written as lists), 2 lanes"),
                             (["3", "--lanes", "5", "--diff", "--dicts", "m1"], "3 úlohy se dvěma snímky řady (zápis slovníky), 5 pruhů",
                              "3 tasks with two snapshots of a row (written as objects), 5 lanes")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[0].splitlines()[0], cs + "; sada je pro všechny modely stejná")
                self.assertEqual(self.cli(*argv, "--lang", "en")[0].splitlines()[0], en + ", the same for all models")
        self.assertTrue(self.cli("--diff", "m1")[0].startswith("100 úloh se dvěma snímky řady"))           # výchozí počet úloh
        en = self.cli("6", "--diff", "m1", "--lang", "en")[0].splitlines()
        self.assertEqual(len(en), 5)
        self.assertRegex(en[3], r"^m1 .*   \(number off by 1 \d+/\d+, other number \d+/\d+, word off by a letter \d+/\d+, other word \d+/\d+\)$")
        self.assertRegex(en[4], r"^ {20}rows without repeats \d+/\d+, with repeats \d+/\d+; errors: same value in another lane \d+, "
                                r"next lane \d+, other \d+, invalid \d+$")

    def test_option_errors_in_both_languages(self):
        for argv in (["6", "--lists", "m1"], ["--dicts", "--numbers", "m1"], ["--bet", "--lists", "m1"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", "Volby --dicts a --lists jdou jen s --diff."))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", "The --dicts and --lists options work only with --diff."))
        for argv, cs, en in ((["--diff", "--weighted", "m1"], "Volba --weighted jde jen s --queue.", "The --weighted option works only with --queue."),
                             (["--diff", "--count", "m1"], "Volba --count jde jen s --memory.", "The --count option works only with --memory.")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", cs))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", en))
        for argv in (["--diff", "--bet", "m1"], ["--diff", "--queue", "m1"], ["--diff", "--numbers", "m1"], ["--diff", "--dicts", "--lists", "m1"],
                     ["--diff", "--hint", "m1"], ["--diff", "--cruise", "m1"], ["--diff", "--timetable", "m1"],
                     ["--diff", "--lanes", "1", "m1"], ["--diff", "--lanes", "9", "m1"], ["--diff"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", bench.__doc__))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", bench.USAGE_EN))
        self.assertEqual(self.sent, [])

    def test_the_form_reaches_the_model(self):
        keys = bench.lane_keys(bench.DIFF_LANES)
        for argv, dicts in ((["2", "--diff", "--dicts", "m1"], True), (["2", "--diff", "m1"], False), (["2", "--diff", "--lists", "m1"], False)):
            with self.subTest(argv=argv):
                self.sent.clear()
                self.assertIsNone(self.cli(*argv)[1])
                states = [json.loads(body)["state"] for body in self.sent]
                states = [s for s in states if isinstance(s, dict) and {"before", "after"} <= set(s)]
                self.assertEqual(len(states), 3)                                 # zahřátí a dvě úlohy
                for state in states:
                    for snapshot in (state["before"], state["after"]):
                        if dicts:
                            self.assertIsInstance(snapshot, dict)
                            self.assertEqual(list(snapshot), keys)
                        else:
                            self.assertIsInstance(snapshot, list)
                            self.assertEqual(len(snapshot), bench.DIFF_LANES)

    def test_mirror_and_inject_work_with_diff(self):
        out, code = self.cli("8", "--diff", "--mirror", "--inject", "--deadline-ms", "9", "m1")
        self.assertIsNone(code)
        lines = out.splitlines()
        self.assertIn("stabilita", lines[2])
        self.assertIn("goodput", lines[2])
        self.assertTrue(lines[4].strip().startswith("přesnost variant: "), lines[4])
        self.assertTrue(lines[5].strip().startswith("trojský kůň: 2 úloh s pokynem"), lines[5])
        self.assertTrue(lines[6].strip().startswith("řady bez opakování "), lines[6])

    def test_usage_names_the_options(self):
        for text in (bench.__doc__, bench.USAGE_EN):
            self.assertIn("python bench.py [N] --diff [--dicts | --lists] [--lanes K] [--mirror] [--inject] [--deadline-ms L] model1 model2 ...", text)
            self.assertIn("python bench.py 100 --diff tev1", text)
            self.assertIn("\n--diff:     ", text)
            self.assertIn("\n--dicts, --lists:  ", text)


def diff_body():
    return page_source().split("\nconst diff = {", 1)[1].split("\n};\n", 1)[0]


class Page(unittest.TestCase):              # scénář ve stránce; generátor, stav a rozbor hlídá tests/test_parity.py (Diff)
    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_variant_matches_bench(self):                    # zrcadla otázek obou nových scénářů jako bench.variant; ostatní otázky beze změny
        page = page_source()
        alt = re.search(r"\nconst ALT = \{.*?\n\};\n", page, re.S).group(0)
        lanes = re.search(r"\nconst ALT_LANES = [^\n]*\n", page).group(0)
        questions = ([bench.diff_question(k, "lists") for k in range(2, 9)] + [bench.hint_question(k) for k in (2, 3, 8)]
                     + [bench.queue_question(4, True), bench.lookahead_question(5, "blocked"), bench.memory_question(5), bench.rule_question("labeled")])
        code = alt + lanes + page_function("variantOf") + "const Q = " + json.dumps(questions) + ";" + (
            "process.stdout.write(JSON.stringify(Q.map(q => [0, 1, 2].map(v => variantOf(JSON.parse(JSON.stringify(q)), v)))));")
        for q, by_variant in zip(questions, run_page_js(code)):
            for v, js in enumerate(by_variant):
                self.assertEqual(json.dumps(js), json.dumps(bench.variant(q, v)), (next(iter(q)), v))


if __name__ == "__main__":
    unittest.main()
