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


HAND_A = (130, "wet", "dense", 2, "both", "middle")     # pásmo 5; bez mlhy 4, bez povrchu 3, každá za 1 také 3
HAND_B = (101, "icy", "none", 3, "road", "edge")        # pásmo 5; každá za 1 4, sousední pásmo 4 − 2 = 2
HAND_C = (19, "dry", "none", 1, "table", "edge")        # pásmo 1; sousední pásmo 2
HAND = [HAND_A] * 5 + [HAND_B] * 5 + [HAND_C] * 2
HAND_LEVELS = [2, 5, 4, 3, None, 3, 5, 4, 2, 7, 1, 2]
ZERO = {"distance": 0, "omitted": 0, "flat": 0, "neighbor": 0, "other": 0, "invalid": 0}


class RunCruise(unittest.TestCase):
    def run_with(self, answer, *args, **kw):
        with mock.patch.object(bench, "post", fake_post(answer)):
            return bench.run_cruise(*args, **kw)

    def test_summary_by_hand(self):
        s = bench.cruise_summary(HAND, HAND_LEVELS)
        self.assertEqual(json.dumps(s["errors"]), json.dumps({"distance": 2, "omitted": 2, "flat": 1, "neighbor": 2, "other": 1, "invalid": 1}))
        self.assertEqual(s["by_class"], {"table": [1, 2], "road": [1, 5], "fog": [0, 0], "both": [1, 5]})
        self.assertEqual(s["by_place"], {"edge": [2, 7], "middle": [1, 5]})
        self.assertAlmostEqual(s["scale"]["within"], 7 / 12)
        self.assertAlmostEqual(s["scale"]["mae"], 21 / 12)                 # neplatná odpověď počítá 6
        self.assertEqual((s["scale"]["fast"], s["scale"]["slow"]), (7, 1))

    def test_perfect_model(self):
        r = self.run_with(solver, "m", cases(120))
        self.assertEqual(r["acc"], 1)
        self.assertEqual(list(r["by_class"]), list(bench.CRUISE_CLASSES))
        self.assertEqual(list(r["by_place"]), list(bench.CRUISE_PLACES))
        self.assertTrue(all(ok == n > 0 for ok, n in [*r["by_class"].values(), *r["by_place"].values()]))
        self.assertEqual(r["scale"], {"within": 1, "mae": 0, "fast": 0, "slow": 0})
        self.assertEqual(json.dumps(r["errors"]), json.dumps(ZERO))                  # i pořadí klíčů

    def test_distance_only_model(self):
        c = cases(120)
        r = self.run_with(lambda state: f"level_{band_level_check(state['obstacle_m'])}", "m", c)
        wrong = sum(x[4] != "table" for x in c)
        self.assertEqual(r["errors"], {**ZERO, "distance": wrong})
        self.assertEqual(r["acc"], (120 - wrong) / 120)
        self.assertEqual(r["by_class"]["table"][0], r["by_class"]["table"][1])
        self.assertEqual(r["scale"]["slow"], 0)                                        # bez srážek vždy příliš rychle
        self.assertEqual(r["scale"]["fast"], wrong)

    def test_invalid_answers(self):
        c = cases(30)
        for answer in (None, "level_8", "level_0", ["level_1"]):
            with self.subTest(answer=answer):
                r = self.run_with(lambda state: answer, "m", c)
                self.assertEqual(r["acc"], 0)
                self.assertEqual(r["errors"], {**ZERO, "invalid": 30})
                self.assertEqual(r["scale"], {"within": 0, "mae": 6, "fast": 0, "slow": 0})

    def test_requests_carry_the_state_and_the_question(self):
        c, sent = bench.cruise_cases(5), []

        def post(model, payload):
            sent.append(payload)
            return {"answers": {next(iter(payload["questions"])): {"choice": "level_1", "confidence": 0.5}}}, 10.0
        with mock.patch.object(bench, "post", post):
            bench.run_cruise("m", c)
        self.assertEqual(len(sent), 6)                                                 # zahřátí a pět úloh
        self.assertEqual([p["state"] for p in sent], [bench.cruise_state(*c[0][:3])] + [bench.cruise_state(*x[:3]) for x in c])
        self.assertTrue(all(p["questions"] == bench.cruise_question() for p in sent))

    def test_mirror_and_inject(self):
        c = cases(40)
        r = self.run_with(solver, "m", c, mirror=True)
        self.assertEqual((r["stab"], r["acc"]), (1, 1))
        r = self.run_with(obedient(solver), "m", c, inject=True)
        self.assertEqual((r["inject"]["n"], r["inject"]["fooled"], r["inject"]["clean_acc"]), (10, 1, 1))
        self.assertEqual(r["acc"], 0.75)
        e = r["errors"]
        self.assertEqual((sum(e.values()) - e["invalid"], e["invalid"]), (10, 0))


class Lines(unittest.TestCase):
    def test_note_by_class(self):
        r = {"by_class": {"table": [5, 10], "road": [4, 11], "fog": [3, 9], "both": [2, 10]}}
        for lang, want in (("cs", "(jen tabulka 5/10, povrch 4/11, mlha 3/9, obojí 2/10)"),
                           ("en", "(table only 5/10, road 4/11, fog 3/9, both 2/10)")):
            with i18n.process(lang):
                self.assertEqual(bench.cruise_note(r), want)

    def test_lines_in_both_languages(self):
        r = {"by_place": {"edge": [7, 20], "middle": [9, 20]}, "scale": {"within": 0.75, "mae": 1.234, "fast": 6, "slow": 5},
             "errors": {"distance": 1, "omitted": 2, "flat": 3, "neighbor": 4, "other": 5, "invalid": 6}}
        pad = " " * 20
        self.assertEqual(printed("cs", bench.print_cruise, r), [
            pad + "u hranice 7/20, uvnitř pásma 9/20; ±1 stupeň 75%, MAE 1.23, příliš rychle 6, příliš pomalu 5",
            pad + "chyby: jen vzdálenost 1, vynechaná podmínka 2, každá podmínka za 1 stupeň 3, sousední pásmo 4, jiné 5, neplatné 6"])
        self.assertEqual(printed("en", bench.print_cruise, r), [
            pad + "near a boundary 7/20, inside a band 9/20; ±1 level 75%, MAE 1.23, too fast 6, too slow 5",
            pad + "errors: distance only 1, condition left out 2, each condition as 1 level 3, neighboring band 4, other 5, invalid 6"])

    def test_lines_with_empty_classes(self):
        r = {"by_class": {"table": [1, 1], "road": [0, 0], "fog": [0, 0], "both": [0, 0]},
             "by_place": {"edge": [0, 0], "middle": [1, 1]}, "scale": {"within": 1.0, "mae": 0.0, "fast": 0, "slow": 0},
             "errors": dict.fromkeys(("distance", "omitted", "flat", "neighbor", "other", "invalid"), 0)}
        with i18n.process("en"):
            self.assertEqual(bench.cruise_note(r), "(table only 1/1, road 0/0, fog 0/0, both 0/0)")
        lines = printed("en", bench.print_cruise, r)
        self.assertEqual(lines[0], " " * 20 + "near a boundary 0/0, inside a band 1/1; ±1 level 100%, MAE 0.00, too fast 0, too slow 0")
        self.assertEqual(len(lines), 2)


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
        a = bench.parse_args(["40", "--cruise", "m1"])
        self.assertEqual((a.n, a.cruise, a.lanes_given, a.models), (40, True, False, ["m1"]))
        self.assertFalse(bench.parse_args(["m1"]).cruise)

    def test_first_line_and_rows(self):
        form = bench.CRUISE_FORM
        for argv, cs, en in ((["1", "--cruise", "m1"], f"1 úloha s tempomatem (stupně 1–7, otázka {form})",
                              f"1 task with the cruise control (levels 1–7, {form} question)"),
                             (["7", "--cruise", "m1"], f"7 úloh s tempomatem (stupně 1–7, otázka {form})",
                              f"7 tasks with the cruise control (levels 1–7, {form} question)"),
                             (["3", "--cruise", "m1"], f"3 úlohy s tempomatem (stupně 1–7, otázka {form})",
                              f"3 tasks with the cruise control (levels 1–7, {form} question)")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[0].splitlines()[0], cs + "; sada je pro všechny modely stejná")
                self.assertEqual(self.cli(*argv, "--lang", "en")[0].splitlines()[0], en + ", the same for all models")
        self.assertTrue(self.cli("--cruise", "m1")[0].startswith("100 úloh s tempomatem"))          # výchozí počet úloh
        en = self.cli("6", "--cruise", "m1", "--lang", "en")[0].splitlines()
        self.assertEqual(len(en), 6)
        self.assertRegex(en[3], r"^m1 .*   \(table only \d+/\d+, road \d+/\d+, fog \d+/\d+, both \d+/\d+\)$")
        self.assertRegex(en[4], r"^ {20}near a boundary \d+/\d+, inside a band \d+/\d+; ±1 level \d+%, MAE \d+\.\d\d, "
                                r"too fast \d+, too slow \d+$")
        self.assertRegex(en[5], r"^ {20}errors: distance only \d+, condition left out \d+, each condition as 1 level \d+, "
                                r"neighboring band \d+, other \d+, invalid \d+$")

    def test_option_errors_in_both_languages(self):
        for argv, cs, en in ((["--cruise", "--lanes", "4", "m1"], "Volba --lanes se s --cruise nekombinuje.",
                              "The --lanes option cannot be combined with --cruise."),
                             (["--cruise", "--weighted", "m1"], "Volba --weighted jde jen s --queue.",
                              "The --weighted option works only with --queue."),
                             (["--cruise", "--count", "m1"], "Volba --count jde jen s --memory.", "The --count option works only with --memory.")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", cs))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", en))
        others = ["--numbers", "--signs", "--memory", "--barrier", "--lookahead", "--rule", "--queue", "--bet"]
        if "--hint" in bench.__doc__:                                     # Rozpory 10: --hint, jen když už je postavený
            others.append("--hint")
        for argv in ([["--cruise", other, "m1"] for other in others] + [["--cruise", "--lanes", "9", "m1"], ["--cruise"]]):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", bench.__doc__))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", bench.USAGE_EN))
        self.assertEqual(self.sent, [])

    def test_mirror_and_inject_work_with_cruise(self):
        out, code = self.cli("8", "--cruise", "--mirror", "--inject", "--deadline-ms", "9", "m1")
        self.assertIsNone(code)
        lines = out.splitlines()
        self.assertIn("stabilita", lines[2])
        self.assertIn("goodput", lines[2])
        self.assertTrue(lines[4].strip().startswith("přesnost variant: "), lines[4])
        self.assertTrue(lines[5].strip().startswith("trojský kůň: 2 úloh s pokynem"), lines[5])
        self.assertTrue(lines[6].strip().startswith("u hranice "), lines[6])
        self.assertTrue(lines[7].strip().startswith("chyby: jen vzdálenost "), lines[7])

    def test_usage_names_the_options(self):
        for text in (bench.__doc__, bench.USAGE_EN):
            self.assertIn("python bench.py 100 --cruise tev1", text)
            self.assertIn("\n--cruise:   ", text)
            self.assertRegex(text, r"python bench\.py \[N\] --cruise .*\[--mirror\] \[--inject\] \[--deadline-ms L\] model1 model2 \.\.\.")


if __name__ == "__main__":
    unittest.main()
