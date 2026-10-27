"""Scénář Nejkratší fronta (bench.py --queue). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
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


def fewest(values):
    """Množina pruhů s nejmenší hodnotou: model, který jde po zkratce, zvolí kterýkoli z nich."""
    return {i for i, v in enumerate(values) if v == min(values)}


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
        self.assertEqual((bench.QUEUE_COUNT_SHARE, bench.QUEUE_TRIES), (0.5, 20000000))
        self.assertEqual([bench.queue_text_share(k) for k in (2, 3, 4, 8)], [1 / 2, 1 / 3, 1 / 3, 1 / 3])

    def test_lengths_and_classes(self):
        self.assertEqual([bench.queue_length(q) for q in EXAMPLE], [2, 1, 2])
        self.assertEqual([bench.queue_length(q, True) for q in EXAMPLE], [2, 3, 4])
        self.assertEqual(bench.queue_length(("tractor", "motorbike", "minibus", "pickup", "van"), True), 7)
        self.assertEqual([bench.queue_class(g) for g in (1, 2, 3, 4, 11)], ["gap1", "gap2-3", "gap2-3", "gap4+", "gap4+"])
        with self.assertRaises(ValueError):
            bench.queue_class(0)

    def test_shortest_text_and_fewest_vehicles(self):
        self.assertEqual(bench.queue_text(EXAMPLE), (1,))                                 # '["truck"]' má 9 znaků
        self.assertEqual(bench.queue_text((("car", "van"), ("bus", "car"))), (0, 1))       # shoda → všechny pruhy se shodou
        longer = (("motorbike", "tractor"), ("car", "van", "bus"))                         # 24 znaků proti 21: méně vozidel, delší text
        self.assertEqual((bench.queue_text(longer), bench.queue_count(longer)), ((1,), 0))
        self.assertEqual(bench.queue_count(EXAMPLE), 1)
        self.assertIsNone(bench.queue_count((("car",), ("van",), ("bus", "car"))))        # dvě fronty s jedním vozidlem
        self.assertEqual(bench.queue_heavy(EXAMPLE), (0,))                                # těžká vozidla 0, 1, 1
        self.assertEqual(bench.queue_heavy((("bus",), ("truck", "car"), ("van", "van", "van"))), (2,))
        self.assertEqual(bench.queue_heavy((("bus", "car"), ("tractor",), ("truck", "van"))), (0, 1, 2))   # shoda → všechny pruhy


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

    def test_shortest_text_is_balanced(self):              # zkratka trefí, když je správný pruh mezi VŠEMI pruhy s minimem (shoda)
        def share(c, cls):
            sub = [x for x in c if x[2] == cls]
            return sum(x[1] in fewest([len(json.dumps(list(q))) for q in x[0]]) for x in sub) / len(sub)
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
                heavy = sum(x[1] in fewest([sum(v in bench.QUEUE_WEIGHTS for v in q) for q in x[0]]) for x in sub)
                self.assertTrue(0.2 <= heavy / len(sub) <= 0.45, heavy / len(sub))

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


QUEUE_TEXT = ("A car comes to a toll gate with {k} lanes. Each lane_N lists the vehicles waiting in that lane. "
              "Choose the lane with the shortest queue.")
WEIGHTED_TEXT = QUEUE_TEXT.replace(" Choose", " A truck, a bus or a tractor counts as three vehicles; any other vehicle counts as one. Choose")
ALT_QUEUE = "The toll gate has {lanes} lanes; lane_N holds the vehicles that wait in lane N. Pick the lane whose queue is the shortest."
ALT_WEIGHTED = ALT_QUEUE.replace(" Pick", " A truck, a bus and a tractor each count as three vehicles, every other vehicle as one. Pick")


class StateAndQuestion(unittest.TestCase):
    def test_example_state(self):
        state = bench.queue_state(EXAMPLE)
        self.assertEqual(json.dumps(state), '{"lane_1": ["car", "car"], "lane_2": ["truck"], "lane_3": ["bus", "van"]}')
        self.assertEqual((solver(False)(state), solver(True)(state)), ("lane_2", "lane_1"))

    def test_questions_and_their_alternative_wordings(self):
        for weighted, key, text, alt in ((False, "lane", QUEUE_TEXT, ALT_QUEUE), (True, "lane_weighted", WEIGHTED_TEXT, ALT_WEIGHTED)):
            for lanes in range(2, 9):
                with self.subTest(weighted=weighted, lanes=lanes):
                    q = bench.queue_question(lanes, weighted)
                    self.assertEqual(list(q), [key])
                    self.assertEqual(q[key]["type"], "choice")
                    self.assertEqual(q[key]["instructions"], text.format(k=lanes))
                    self.assertEqual(json.dumps(q[key]["criteria"]), json.dumps(bench.position_criteria(bench.lane_keys(lanes), "Lane")))
                    self.assertEqual(bench.ALT[key], alt)
                    self.assertEqual(bench.variant(q, 2)[key]["instructions"], alt.format(lanes=lanes))
                    self.assertEqual(list(bench.variant(q, 1)[key]["criteria"]), bench.lane_keys(lanes)[::-1])
        self.assertEqual(bench.queue_question(4), bench.queue_question(4, False))

    def test_lookahead_still_takes_its_lane_count(self):                        # ALT_LANES má teď dvě větve; move bere tu první
        for lanes in (3, 8):
            for form, key in (("rows", "move"), ("blocked", "move_blocked")):
                q = bench.lookahead_question(lanes, form)
                self.assertEqual(bench.variant(q, 2)[key]["instructions"], bench.ALT[key].format(lanes=lanes))


class RunQueue(unittest.TestCase):
    def run_with(self, answer, *args, **kw):
        with mock.patch.object(bench, "post", fake_post(answer)):
            return bench.run_queue(*args, **kw)

    def test_perfect_model(self):
        for weighted in (False, True):
            with self.subTest(weighted=weighted):
                r = self.run_with(solver(weighted), "m", cases(120, 4, weighted), 4, weighted=weighted)
                self.assertEqual(r["acc"], 1)
                self.assertEqual(list(r["by_gap"]), list(bench.QUEUE_GAPS))
                self.assertEqual(sum(n for _, n in r["by_gap"].values()), 120)
                self.assertTrue(all(ok == n > 0 for ok, n in r["by_gap"].values()))
                zero = {"count": 0, "text": 0, "other": 0, "invalid": 0} if weighted else {"text": 0, "other": 0, "invalid": 0}
                self.assertEqual(json.dumps(r["errors"]), json.dumps(zero))          # i pořadí klíčů

    def test_shortest_text_model(self):
        c = cases(120, 4)
        r = self.run_with(lambda state: f"lane_{bench.queue_text(queues_of(state))[0] + 1}", "m", c, 4)
        wrong = sum(text[0] != answer for _, answer, _, text, _ in c)
        self.assertGreater(wrong, 0)
        self.assertEqual(r["errors"], {"text": wrong, "other": 0, "invalid": 0})
        self.assertEqual(r["acc"], (120 - wrong) / 120)

    def test_any_lane_tied_for_the_shortest_text_is_a_text_error(self):
        queues = (("motorbike", "motorbike"), ("car", "van", "bus"), ("bus", "car", "van"))   # správně pruh 1 (26 znaků); 2 a 3 mají 21
        case = (queues, 0, "gap1", bench.queue_text(queues), bench.queue_count(queues))
        errors = bench.queue_errors([case] * 3, ["lane_1", "lane_2", "lane_3"], bench.lane_keys(3))
        self.assertEqual(errors, {"text": 2, "other": 0, "invalid": 0})

    def test_counting_without_weights_in_a_weighted_run(self):           # nejkratší podle počtu má přednost před nejkratším textem
        c = cases(120, 4, True)
        r = self.run_with(solver(False), "m", c, 4, weighted=True)
        differ = sum(count != answer for _, answer, _, _, count in c)
        self.assertGreater(differ, 0)
        self.assertEqual(r["errors"], {"count": differ, "text": 0, "other": 0, "invalid": 0})
        self.assertEqual(r["acc"], (120 - differ) / 120)

    def test_invalid_answers(self):
        c = cases(30, 4)
        for answer in (None, "lane_9", "gate_1", ["lane_1"]):
            with self.subTest(answer=answer):
                r = self.run_with(lambda state: answer, "m", c, 4)
                self.assertEqual((r["acc"], r["errors"]), (0, {"text": 0, "other": 0, "invalid": 30}))

    def test_requests_carry_the_state_and_the_question(self):
        c, sent = bench.queue_cases(5, 6, True), []

        def post(model, payload):
            sent.append(payload)
            return {"answers": {next(iter(payload["questions"])): {"choice": "lane_1", "confidence": 0.5}}}, 10.0
        with mock.patch.object(bench, "post", post):
            bench.run_queue("m", c, 6, weighted=True)
        self.assertEqual(len(sent), 6)                                          # zahřátí a pět úloh
        self.assertEqual([p["state"] for p in sent], [bench.queue_state(c[0][0])] + [bench.queue_state(q) for q, *_ in c])
        self.assertTrue(all(p["questions"] == bench.queue_question(6, True) for p in sent))

    def test_mirror_and_inject(self):
        c = cases(40, 4)
        r = self.run_with(solver(False), "m", c, 4, mirror=True)
        self.assertEqual((r["stab"], r["acc"]), (1, 1))
        r = self.run_with(obedient(solver(False)), "m", c, 4, inject=True)
        self.assertEqual((r["inject"]["n"], r["inject"]["fooled"], r["inject"]["clean_acc"]), (10, 1, 1))
        self.assertEqual(r["acc"], 0.75)
        e = r["errors"]
        self.assertEqual((e["text"] + e["other"], e["invalid"]), (10, 0))


class Lines(unittest.TestCase):
    def test_note_by_gap(self):
        r = {"by_gap": {"gap1": [5, 10], "gap2-3": [9, 11], "gap4+": [12, 12]}}
        for lang, want in (("cs", "(rozdíl 1 5/10, rozdíl 2–3 9/11, rozdíl 4+ 12/12)"),
                           ("en", "(difference 1 5/10, difference 2–3 9/11, difference 4+ 12/12)")):
            with i18n.process(lang):
                self.assertEqual(bench.queue_note(r), want)

    def test_error_lines_in_both_languages(self):
        plain = {"errors": {"text": 4, "other": 2, "invalid": 1}}
        weighted = {"errors": {"count": 5, "text": 4, "other": 2, "invalid": 1}}
        pad = " " * 20
        self.assertEqual(printed("cs", bench.print_queue, plain), [pad + "chyby: nejkratší text 4, jiné 2, neplatné 1"])
        self.assertEqual(printed("en", bench.print_queue, plain), [pad + "errors: shortest text 4, other 2, invalid 1"])
        self.assertEqual(printed("cs", bench.print_queue, weighted),
                         [pad + "chyby: nejkratší podle počtu (bez vah) 5, nejkratší text 4, jiné 2, neplatné 1"])
        self.assertEqual(printed("en", bench.print_queue, weighted),
                         [pad + "errors: shortest by count (unweighted) 5, shortest text 4, other 2, invalid 1"])

    def test_lines_with_empty_classes(self):
        r = {"by_gap": {"gap1": [1, 1], "gap2-3": [0, 0], "gap4+": [0, 0]}, "errors": {"text": 0, "other": 0, "invalid": 0}}
        with i18n.process("en"):
            self.assertEqual(bench.queue_note(r), "(difference 1 1/1, difference 2–3 0/0, difference 4+ 0/0)")
        self.assertEqual(len(printed("en", bench.print_queue, r)), 1)


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
        a = bench.parse_args(["40", "--queue", "m1"])
        self.assertEqual((a.n, a.queue, a.weighted, a.lanes, a.models), (40, True, False, 4, ["m1"]))
        a = bench.parse_args(["--weighted", "--queue", "--lanes", "2", "m1"])
        self.assertEqual((a.queue, a.weighted, a.lanes), (True, True, 2))
        self.assertEqual(bench.parse_args(["m1"]).lanes, 3)

    def test_first_line_and_rows(self):
        for argv, cs, en in ((["1", "--queue", "m1"], "1 úloha u mýtné brány (fronty bez vah), 4 pruhy",
                              "1 task at a toll gate (unweighted queues), 4 lanes"),
                             (["7", "--queue", "--weighted", "--lanes", "2", "m1"], "7 úloh u mýtné brány (vážené fronty), 2 pruhy",
                              "7 tasks at a toll gate (weighted queues), 2 lanes"),
                             (["3", "--lanes", "5", "--queue", "m1"], "3 úlohy u mýtné brány (fronty bez vah), 5 pruhů",
                              "3 tasks at a toll gate (unweighted queues), 5 lanes")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[0].splitlines()[0], cs + "; sada je pro všechny modely stejná")
                self.assertEqual(self.cli(*argv, "--lang", "en")[0].splitlines()[0], en + ", the same for all models")
        self.assertTrue(self.cli("--queue", "m1")[0].startswith("100 úloh u mýtné brány"))          # výchozí počet úloh
        en = self.cli("6", "--queue", "m1", "--lang", "en")[0].splitlines()
        self.assertEqual(len(en), 5)
        self.assertRegex(en[3], r"^m1 .*   \(difference 1 \d+/\d+, difference 2–3 \d+/\d+, difference 4\+ \d+/\d+\)$")
        self.assertRegex(en[4], r"^ {20}errors: shortest text \d+, other \d+, invalid \d+$")
        en = self.cli("6", "--queue", "--weighted", "m1", "--lang", "en")[0].splitlines()
        self.assertRegex(en[4], r"^ {20}errors: shortest by count \(unweighted\) \d+, shortest text \d+, other \d+, invalid \d+$")

    def test_option_errors_in_both_languages(self):
        for argv in (["6", "--weighted", "m1"], ["--weighted", "--numbers", "m1"], ["--lookahead", "--weighted", "m1"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", "Volba --weighted jde jen s --queue."))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", "The --weighted option works only with --queue."))
        for argv, cs, en in ((["--queue", "--rows", "m1"], "Volby --rows a --blocked jdou jen s --lookahead.",
                              "The --rows and --blocked options work only with --lookahead."),
                             (["--queue", "--count", "m1"], "Volba --count jde jen s --memory.", "The --count option works only with --memory.")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", cs))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", en))
        for argv in (["--queue", "--numbers", "m1"], ["--queue", "--lookahead", "m1"], ["--queue", "--rule", "m1"],
                     ["--queue", "--lanes", "1", "m1"], ["--queue", "--lanes", "9", "m1"], ["--queue"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", bench.__doc__))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", bench.USAGE_EN))
        self.assertEqual(self.sent, [])

    def test_mirror_and_inject_work_with_queue(self):
        out, code = self.cli("8", "--queue", "--mirror", "--inject", "--deadline-ms", "9", "m1")
        self.assertIsNone(code)
        lines = out.splitlines()
        self.assertIn("stabilita", lines[2])
        self.assertIn("goodput", lines[2])
        self.assertTrue(lines[4].strip().startswith("přesnost variant: "), lines[4])
        self.assertTrue(lines[5].strip().startswith("trojský kůň: 2 úloh s pokynem"), lines[5])
        self.assertTrue(lines[6].strip().startswith("chyby: nejkratší text "), lines[6])

    def test_usage_names_the_options(self):
        for text in (bench.__doc__, bench.USAGE_EN):
            self.assertIn("python bench.py [N] --queue [--weighted] [--lanes K] [--mirror] [--inject] [--deadline-ms L] model1 model2 ...", text)
            self.assertIn("python bench.py 100 --queue --weighted tev1", text)
            self.assertIn("\n--queue:    ", text)
            self.assertIn("\n--weighted:  ", text)


def queue_body():
    return page_source().split("\nconst queue = {", 1)[1].split("\n};\n", 1)[0]


def method(body, name):                     # víceřádková metoda objektu scénáře, končí řádkem „  },“
    return re.search(r"\n  " + name + r"\(.*?\) \{.*?\n  \},", body, re.S).group(0)[1:-1]


class Page(unittest.TestCase):              # scénář ve stránce; počítání front, generátor a rozbor hlídá tests/test_parity.py (Queue)
    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_variant_fills_in_the_lanes_like_bench(self):        # ALT_LANES má dvě větve: fronta bere číslo z „toll gate with N lanes“, dva kroky z „a N-lane road“
        page = page_source()
        alt = re.search(r"\nconst ALT = \{.*?\n\};\n", page, re.S).group(0)
        lanes = re.search(r"\nconst ALT_LANES = [^\n]*\n", page).group(0)
        questions = ([bench.queue_question(k, w) for k in range(2, 9) for w in (False, True)]
                     + [bench.lookahead_question(k, "blocked") for k in (3, 8)]
                     + [bench.number_question(4), bench.memory_question(5), bench.sign_question(3), bench.rule_question("labeled")])   # otázky bez {lanes}: beze změny
        code = alt + lanes + page_function("variantOf") + "const Q = " + json.dumps(questions) + ";" + (
            "process.stdout.write(JSON.stringify(Q.map(q => [0, 1, 2].map(v => variantOf(JSON.parse(JSON.stringify(q)), v)))));")
        for q, by_variant in zip(questions, run_page_js(code)):
            for v, js in enumerate(by_variant):
                self.assertEqual(json.dumps(js), json.dumps(bench.variant(q, v)), (next(iter(q)), v))

    def test_scenario_is_offered_selected_and_wired(self):
        page = page_source()
        for value in ("queue", "queueWeighted"):
            self.assertEqual(page.count(f'<option value="{value}" data-i18n="opt.scenario.{value}"></option>'), 1)
        self.assertEqual(page.count(REGISTRY), 1)
        self.assertEqual(page.count("// ---------- scénář: nejkratší fronta ----------"), 1)
        self.assertEqual(page.count("|| v === 'queue' || v === 'queueWeighted'"), 1)      # bez „) {“: další části podmínku prodlouží
        # zahřívací úloha je pevná a bez pokynu: nebere z rnd, jinak by se řady posunuly podle řidiče
        self.assertEqual(page.count("  warmTask() { return this.task(queueWarm(LANES), null, null); },"), 1)
        self.assertEqual(page.count("  spawnRow(g, y) { g.rows.push(this.newRow(g, y, queueRow(LANES, this.weighted, rnd))); },"), 1)
        self.assertEqual(page.count("  randomTask() { return this.task(queueRow(LANES, this.weighted, rnd)); },"), 1)
        body = queue_body()
        for call in ("id: 'queue', lanesFree: true, weighted: false,", "rollInject([d.truth])", "queueState(d.queues)",
                     "injectText(INJECT_NOTE, laneKeys(LANES)[inject])", "variantOf(queueQuestion(LANES, this.weighted), v)",
                     "classTally(QUEUE_GAPS, ", "queueErrors(", "t(this.weighted ? 'queue.errorsWeighted' : 'queue.errors', ", "QUEUE_GAP_NAME[",
                     "laneOf: (r, move) => move,"):
            self.assertIn(call, body)                                                             # definice ze sdíleného bloku, ne opsané
        self.assertNotIn("SC.weighted", body)

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_weighted_queue_inherits_and_asks_lane_weighted(self):    # vážená fronta je zděděný objekt; otázka i odpověď pod klíčem lane_weighted
        page = page_source()
        self.assertEqual(page.count("const queueWeighted = { __proto__: queue, id: 'queueWeighted', weighted: true,"), 1)
        laneKeys = re.search(r"\nconst laneKeys = [^\n]*\n", page).group(0)
        code = laneKeys + "let LANES = 3; const proto = {" + method(queue_body(), "parse") + "};" + (
            "const plain = { __proto__: proto, weighted: false }, heavy = { __proto__: proto, weighted: true };"
            "const a = { lane: { choice: 'lane_2' } }, b = { lane_weighted: { choice: 'lane_3', confidence: 0.7 } };"
            "process.stdout.write(JSON.stringify([plain.parse(a).move, plain.parse(b).move, heavy.parse(a).move, heavy.parse(b)]));")
        self.assertEqual(run_page_js(code), [1, -1, -1, {"move": 2, "conf": 0.7}])

    def test_queue_rows_come_every_six_cells(self):               # fronta až 12 vozidel: rozestup 6; řada zmizí, až je budka pod plátnem
        page = page_source()
        self.assertEqual(page.count("const QUEUE_GAP = 6, QUEUE_SPAN = 4.6;"), 1)
        self.assertIn("  gap: QUEUE_GAP, keep: QUEUE_SPAN + 1.5,", queue_body())
        car_y, h, cell = (int(re.search(r"\b%s = (\d+)" % k, page).group(1)) for k in ("CAR_Y", "H", "CELL"))
        self.assertGreaterEqual(4.6 + 1.5, 4.6 + (h - car_y) / cell)                              # pod autem je (H − CAR_Y) / CELL polí

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_tally_keeps_the_task_and_the_choice(self):            # počítají se všechny odpovědi; neplatná je null
        laneKeys = re.search(r"\nconst laneKeys = [^\n]*\n", page_source()).group(0)
        code = laneKeys + "let LANES = 3; const base = { tally() {} }; const o = { __proto__: base," + method(queue_body(), "tally") + "};" + (
            "const g = { cases: [], choices: [] }, task = { queues: [['car'], ['bus', 'van'], ['truck']], answer: 0, cls: 'gap1', text: [0], count: 0, truth: 0, state: {} };"
            "[1, 0, -1].forEach(m => o.tally(g, task, m)); process.stdout.write(JSON.stringify(g));")
        case = {"queues": [["car"], ["bus", "van"], ["truck"]], "answer": 0, "cls": "gap1", "text": [0], "count": 0}
        self.assertEqual(run_page_js(code), {"cases": [case] * 3, "choices": ["lane_2", "lane_1", None]})

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_last_decision_lists_the_queues(self):                 # fronty s délkou podle otázky (s vahami vážená), volba a správný pruh
        from test_parity import run_js
        code = "(() => { const laneName = i => t('lane.name', { n: i + 1 }); const proto = {" + method(queue_body(), "describeLast") + "};" + (
            "const task = { queues: [['car', 'truck'], ['bus']], truth: 1 };"
            "return [{ __proto__: proto, weighted: false }, { __proto__: proto, weighted: true }].map(o => o.describeLast(task, { move: 0 })); })()")
        plain, heavy = run_js(code, None)
        self.assertEqual(plain.split("\n"), ["fronty:  1: car truck (2)  ·  2: bus (1)", "zvoleno: pruh 1  (správně: pruh 2)"])
        self.assertEqual(heavy.split("\n")[0], "fronty:  1: car truck (4)  ·  2: bus (3)")
        self.assertEqual(run_js(code, None, "en")[0].split("\n")[1], "chosen:  lane 1  (correct: lane 2)")


if __name__ == "__main__":
    unittest.main()
