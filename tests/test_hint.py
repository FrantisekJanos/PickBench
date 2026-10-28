"""Scénář Rušivá nápověda (bench.py --hint). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
import contextlib
import io
import json
import os
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
from test_bench import fake_post, largest_gate
from test_golden import Clock, fake_urlopen
from test_barrier import REGISTRY, page_function, page_source, run_page_js      # kus skriptu stránky v node, registr scénářů
from test_queue import method                                                   # víceřádková metoda objektu scénáře

LANG_ENV = "PICKBENCH_LANG"
KEYS3 = ["gate_1", "gate_2", "gate_3"]
ROW_A, ROW_B = (10, 30, 20), (100, 98, 5)          # správně gate_2 (jasné), gate_1 (těsné)
CASES = [(ROW_A, 1, "clear", "none", None, 0), (ROW_A, 1, "clear", "congruent", 1, 0), (ROW_A, 1, "clear", "incongruent", 2, 0),
         (ROW_B, 0, "tight", "incongruent", 1, 1), (ROW_B, 0, "tight", "none", None, 1), (ROW_B, 0, "tight", "congruent", 0, 1)]
CACHE = {}


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def cases(n=900, lanes=3):
    if (n, lanes) not in CACHE:
        CACHE[n, lanes] = bench.hint_cases(n, lanes)
    return CACHE[n, lanes]


def follower(state):
    """Model, který jde za nápovědou; bez ní volí největší číslo."""
    return state.get(bench.HINT_FIELD) or largest_gate(state)


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Generator(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(bench.HINT_CONDS, ("none", "congruent", "incongruent"))
        self.assertEqual(bench.HINT_FIELD, "most_popular")

    def test_every_row_comes_three_times(self):
        for lanes in (2, 3, 4, 8):
            c = cases(900, lanes)
            with self.subTest(lanes=lanes):
                self.assertEqual(len(c), 2700)
                self.assertEqual(Counter(x[3] for x in c), {"none": 900, "congruent": 900, "incongruent": 900})
                rows = {}
                for values, answer, gap, cond, hint, row in c:
                    rows.setdefault(row, []).append((values, answer, gap, cond, hint))
                    self.assertIsInstance(values, tuple)
                    self.assertEqual(len(values), lanes)
                    self.assertEqual(values.count(max(values)), 1)
                    self.assertEqual(values.index(max(values)), answer)
                    if cond == "none":
                        self.assertIsNone(hint)
                    elif cond == "congruent":
                        self.assertEqual(hint, answer)
                    else:
                        self.assertTrue(0 <= hint < lanes and hint != answer)
                self.assertEqual(sorted(rows), list(range(900)))
                for trio in rows.values():
                    self.assertEqual(sorted(x[3] for x in trio), sorted(bench.HINT_CONDS))
                    self.assertEqual(len({x[:3] for x in trio}), 1)                 # stejná čísla, správná brána i třída
                numbers = [(list(x[0]), x[1], x[2]) for x in c if x[3] == "none"]
                self.assertEqual(sorted(numbers, key=str), sorted(bench.number_cases(900, lanes), key=str))    # řady jako --numbers

    def test_answers_gaps_and_wrong_hints_are_spread(self):
        c = cases(900, 3)
        none = [x for x in c if x[3] == "none"]
        answers, gaps = Counter(x[1] for x in none), Counter(x[2] for x in none)
        for gate in range(3):
            self.assertAlmostEqual(answers[gate] / 900, 1 / 3, delta=0.07)
        for gap in bench.GAPS:
            self.assertTrue(0.25 <= gaps[gap] / 900 <= 0.42, gaps)
        inc = [x for x in c if x[3] == "incongruent"]
        second = sum(x[0][x[4]] == sorted(x[0])[-2] for x in inc) / len(inc)
        self.assertTrue(0.4 <= second <= 0.6, second)

    def test_control_strategies(self):
        c = cases(900, 3)
        share = lambda pick, cond=None: (lambda sub: sum(pick(x) == x[1] for x in sub) / len(sub))(
            [x for x in c if cond is None or x[3] == cond])
        follow = lambda x: 0 if x[4] is None else x[4]
        anti = lambda x: 1 if x[4] == 0 else 0                             # nejnižší brána mimo nápovědu
        self.assertEqual((share(follow, "congruent"), share(follow, "incongruent")), (1, 0))
        self.assertTrue(0.4 <= share(anti, "incongruent") <= 0.6)
        self.assertLess(share(anti), 0.34)
        self.assertLess(share(lambda x: 0), 0.4)

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.hint_cases(30), bench.hint_cases(30, 3, seed=1))
        self.assertNotEqual(bench.hint_cases(30), bench.hint_cases(30, 3, seed=2))
        self.assertNotEqual(bench.hint_cases(30), bench.hint_cases(30, 4))

    def test_row_without_a_unique_largest_number_raises(self):
        with mock.patch.object(bench, "number_cases", lambda n, lanes, seed: [([50, 50, 1], 0, "tight")]), self.assertRaises(RuntimeError):
            bench.hint_cases(1)


class StateAndQuestion(unittest.TestCase):
    def test_state_without_a_hint_is_the_numbers_state(self):
        for values in ((10, 30, 20), (1, 2), tuple(range(1, 9))):
            with self.subTest(values=values):
                self.assertEqual(json.dumps(bench.hint_state(values, None)), json.dumps(dict(zip(bench.gate_keys(len(values)), values))))

    def test_hint_is_the_last_key(self):
        self.assertEqual(json.dumps(bench.hint_state((4120, 3987, 1203), 1)),
                         '{"gate_1": 4120, "gate_2": 3987, "gate_3": 1203, "most_popular": "gate_2"}')

    def test_question_is_the_numbers_question(self):
        for lanes in range(2, 9):
            with self.subTest(lanes=lanes):
                q = bench.hint_question(lanes)
                self.assertEqual(json.dumps(q), json.dumps(bench.number_question(lanes)))
                self.assertEqual(bench.variant(q, 2)["largest"]["instructions"], bench.ALT["largest"])
                self.assertEqual(list(bench.variant(q, 1)["largest"]["criteria"]), bench.gate_keys(lanes)[::-1])


class Summary(unittest.TestCase):
    def test_perfect_model(self):
        s = bench.hint_summary(CASES, [KEYS3[c[1]] for c in CASES], KEYS3)
        self.assertEqual(s["by_cond"], {"none": [2, 2], "congruent": [2, 2], "incongruent": [2, 2]})
        self.assertEqual(s["by_gap"], {"tight": [1, 1], "medium": [0, 0], "clear": [1, 1]})
        self.assertEqual((s["help"], s["hurt"], s["pull"], s["flipped"], s["pairs"]), (0, 0, 0, 0, 2))
        self.assertEqual(s["errors"], {"hint": 0, "other": 0, "invalid": 0})

    def test_hint_follower(self):                                         # nápověda, jinak gate_1
        s = bench.hint_summary(CASES, ["gate_1", "gate_2", "gate_3", "gate_2", "gate_1", "gate_1"], KEYS3)
        self.assertEqual(s["by_cond"], {"none": [1, 2], "congruent": [2, 2], "incongruent": [0, 2]})
        self.assertEqual((s["help"], s["hurt"], s["pull"], s["flipped"], s["pairs"]), (0.5, 0.5, 1, 1, 2))
        self.assertEqual(s["errors"], {"hint": 2, "other": 0, "invalid": 0})

    def test_hint_taken_for_a_decoy(self):                                # největší číslo mimo bránu nápovědy
        s = bench.hint_summary(CASES, ["gate_2", "gate_3", "gate_2", "gate_1", "gate_1", "gate_2"], KEYS3)
        self.assertEqual((s["help"], s["hurt"], s["pull"], s["flipped"]), (-1, 0, 0, 0))
        self.assertEqual(s["errors"], {"hint": 0, "other": 0, "invalid": 0})

    def test_invalid_answers(self):
        for bad in (None, "gate_9", "lane_1", 5, ["gate_1"]):
            with self.subTest(bad=bad):
                choices = [bad if c[3] == "incongruent" else KEYS3[c[1]] for c in CASES]
                s = bench.hint_summary(CASES, choices, KEYS3)
                self.assertEqual(s["errors"], {"hint": 0, "other": 0, "invalid": 2})
                self.assertEqual((s["hurt"], s["pull"], s["flipped"]), (1, 0, 0))
                self.assertEqual(s["by_gap"], {"tight": [0, 1], "medium": [0, 0], "clear": [0, 1]})


class RunHint(unittest.TestCase):
    def run_with(self, answer, *args, **kw):
        with mock.patch.object(bench, "post", fake_post(answer)):
            return bench.run_hint(*args, **kw)

    def test_perfect_model(self):
        r = self.run_with(largest_gate, "m", cases(40), 3)
        self.assertEqual(r["acc"], 1)
        self.assertEqual(r["by_cond"], {"none": [40, 40], "congruent": [40, 40], "incongruent": [40, 40]})
        self.assertEqual((r["help"], r["hurt"], r["pull"], r["flipped"], r["pairs"]), (0, 0, 0, 0, 40))
        self.assertEqual(sum(n for _, n in r["by_gap"].values()), 40)

    def test_hint_follower(self):
        r = self.run_with(follower, "m", cases(40), 3)
        self.assertEqual(r["by_cond"], {"none": [40, 40], "congruent": [40, 40], "incongruent": [0, 40]})
        self.assertEqual((r["help"], r["hurt"], r["pull"], r["flipped"]), (0, 1, 1, 40))
        self.assertEqual(r["errors"], {"hint": 40, "other": 0, "invalid": 0})
        self.assertAlmostEqual(r["acc"], 2 / 3)

    def test_requests_carry_the_state_and_the_question(self):
        c, sent = bench.hint_cases(5, 6), []

        def post(model, payload):
            sent.append(payload)
            return {"answers": {next(iter(payload["questions"])): {"choice": "gate_1", "confidence": 0.5}}}, 10.0
        with mock.patch.object(bench, "post", post):
            bench.run_hint("m", c, 6)
        self.assertEqual(len(sent), 16)                                   # zahřátí a 3 × 5 úloh
        self.assertEqual([p["state"] for p in sent], [bench.hint_state(c[0][0], None)] + [bench.hint_state(x[0], x[4]) for x in c])
        self.assertTrue(all(p["questions"] == bench.number_question(6) for p in sent))
        self.assertEqual(sum(bench.HINT_FIELD in p["state"] for p in sent), 10)

    def test_mirror(self):
        r = self.run_with(largest_gate, "m", cases(20), 3, mirror=True)
        self.assertEqual((r["stab"], r["variant_acc"]), (1, [1, 1, 1]))
        self.assertEqual(r["by_cond"]["incongruent"], [20, 20])


class Lines(unittest.TestCase):
    def test_note_by_condition(self):
        r = {"by_cond": {"none": [10, 12], "congruent": [12, 12], "incongruent": [3, 12]}}
        for lang, want in (("cs", "(bez nápovědy 10/12, shodná 12/12, konfliktní 3/12)"),
                           ("en", "(no hint 10/12, congruent 12/12, incongruent 3/12)")):
            with i18n.process(lang):
                self.assertEqual(bench.hint_note(r), want)

    def test_lines_in_both_languages(self):
        r = {"help": 0.12, "hurt": -0.034, "pull": 0.0, "errors": {"hint": 7, "other": 2, "invalid": 1}, "flipped": 5, "pairs": 100,
             "by_gap": {"tight": [3, 30], "medium": [20, 35], "clear": [33, 35]}}
        self.assertEqual(printed("cs", bench.print_hint, r), [
            " " * 20 + "nápověda: pomoc +12 pb, škoda -3 pb, tah +0 pb; konfliktní: chyby na nápovědu 7, jiné 2, neplatné 1; převráceno 5/100",
            " " * 20 + "konfliktní podle rozdílu: těsné 3/30, střední 20/35, jasné 33/35"])
        self.assertEqual(printed("en", bench.print_hint, r), [
            " " * 20 + "hint: help +12 pp, harm -3 pp, pull +0 pp; incongruent: errors on the hint 7, other 2, invalid 1; flipped 5/100",
            " " * 20 + "incongruent by gap: narrow 3/30, medium 20/35, clear 33/35"])

    def test_points_keep_the_sign(self):
        self.assertEqual([bench.pp(x) for x in (0.5, -1, 0.0, -0.004, 0.004, 0.125, float("nan"), None)],
                         ["+50", "-100", "+0", "+0", "+0", "+12", "—", "—"])


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
        a = bench.parse_args(["40", "--hint", "m1"])
        self.assertEqual((a.n, a.hint, a.lanes, a.models), (40, True, 3, ["m1"]))
        self.assertEqual(bench.parse_args(["--hint", "--lanes", "2", "m1"]).lanes, 2)

    def test_first_line_and_rows(self):
        cs_tail = " čísel, každá třikrát (bez nápovědy, nápověda na správnou a na špatnou bránu), "
        en_tail = " of numbers, each asked three times (no hint, hint on the right gate, hint on a wrong gate), "
        for argv, cs, en in ((["1", "--hint", "m1"], "1 řada" + cs_tail + "3 brány", "1 row" + en_tail + "3 gates"),
                             (["7", "--hint", "--lanes", "5", "m1"], "7 řad" + cs_tail + "5 bran", "7 rows" + en_tail + "5 gates"),
                             (["2", "--lanes", "2", "--hint", "m1"], "2 řady" + cs_tail + "2 brány", "2 rows" + en_tail + "2 gates")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[0].splitlines()[0], cs + "; sada je pro všechny modely stejná")
                self.assertEqual(self.cli(*argv, "--lang", "en")[0].splitlines()[0], en + ", the same for all models")
        self.assertTrue(self.cli("--hint", "m1")[0].startswith("100 řad čísel"))
        self.sent.clear()
        en = self.cli("6", "--hint", "m1", "--lang", "en")[0].splitlines()
        self.assertEqual(len(self.sent), 1 + 18)                         # zahřátí a 3 × 6 dotazů
        self.assertEqual(len(en), 6)
        self.assertRegex(en[3], r"^m1 .*   \(no hint \d+/6, congruent \d+/6, incongruent \d+/6\)$")
        self.assertRegex(en[4], r"^ {20}hint: help [+-]\d+ pp, harm [+-]\d+ pp, pull [+-]\d+ pp; incongruent: errors on the hint \d+, "
                                r"other \d+, invalid \d+; flipped \d+/6$")
        self.assertRegex(en[5], r"^ {20}incongruent by gap: narrow \d+/\d+, medium \d+/\d+, clear \d+/\d+$")

    def test_option_errors_in_both_languages(self):
        for argv, cs, en in ((["--hint", "--inject", "m1"], "Volba --inject se s --hint nekombinuje.",
                              "The --inject option cannot be combined with --hint."),
                             (["--hint", "--mirror", "--inject", "m1"], "Volba --inject se s --hint nekombinuje.",
                              "The --inject option cannot be combined with --hint."),
                             (["--hint", "--weighted", "m1"], "Volba --weighted jde jen s --queue.", "The --weighted option works only with --queue.")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", cs))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", en))
        for argv in (["--hint", "--numbers", "m1"], ["--hint", "--bet", "m1"], ["--hint", "--cruise", "m1"], ["--hint", "--timetable", "m1"],
                     ["--hint", "--lanes", "1", "m1"],
                     ["--hint", "--lanes", "9", "m1"], ["--hint"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", bench.__doc__))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", bench.USAGE_EN))
        self.assertEqual(self.sent, [])

    def test_mirror_works_with_hint(self):
        out, code = self.cli("4", "--hint", "--mirror", "--deadline-ms", "9", "m1")
        self.assertIsNone(code)
        lines = out.splitlines()
        self.assertIn("stabilita", lines[2])
        self.assertTrue(lines[4].strip().startswith("přesnost variant: "), lines[4])
        self.assertTrue(lines[5].strip().startswith("nápověda: pomoc "), lines[5])
        self.assertTrue(lines[6].strip().startswith("konfliktní podle rozdílu: "), lines[6])
        self.assertEqual(len(self.sent), 1 + 3 * 12)                     # zahřátí a 12 úloh ve třech variantách

    def test_usage_names_the_options(self):
        for text in (bench.__doc__, bench.USAGE_EN):
            self.assertIn("python bench.py [N] --hint [--lanes K] [--mirror] [--deadline-ms L] model1 model2 ...", text)
            self.assertIn("python bench.py 100 --hint tev1", text)
            self.assertIn("\n--hint:     ", text)


def hint_body():
    return page_source().split("\nconst hint = {", 1)[1].split("\n};\n", 1)[0]


class Page(unittest.TestCase):              # scénář ve stránce; čísla, trojice, stav a rozbor hlídá tests/test_parity.py (Hint)
    def test_scenario_is_offered_selected_and_wired(self):
        page = page_source()
        self.assertEqual(page.count('<option value="hint" data-i18n="opt.scenario.hint"></option>'), 1)
        self.assertEqual(page.count(REGISTRY), 1)
        self.assertEqual(page.count("// ---------- scénář: rušivá nápověda ----------"), 1)
        self.assertEqual(page.count("|| v === 'hint'"), 1)                                     # bez „) {“: další část podmínku prodlouží
        body = hint_body() + "\n"
        # trojice: nextRow bere řady z trojice a novou losuje až po třetí; zahřívací úloha je pevná a nebere z rnd
        for line in ("  nextRow(g) { if (!g.trio.length) g.trio = hintTrio(rnd, LANES, g.sets++); return g.trio.shift(); },",
                     "  spawnRow(g, y) { g.rows.push(this.newRow(g, y, this.nextRow(g))); },",
                     "  randomTask(g) { return this.task(this.nextRow(g)); },",
                     "  warmTask() { return this.task(hintWarm(LANES)); },",
                     "  questions(v = 0) { return variantOf(hintQuestion(LANES), v); },"):
            self.assertIn("\n" + line + "\n", body)
        for call in ("id: 'hint', lanesFree: true, noInject: true,", "state: hintState(d.values, d.hint)", "inject: null,",
                     "a.largest?.choice", "hintSummary(", "t('hint.line', ", "t('hint.byGap', ", "HINT_GAP_NAME[", "pointsText(",
                     "laneOf: (r, move) => move,"):
            self.assertIn(call, body)                                                              # definice ze sdíleného bloku, ne opsané
        for word in ("rollInject", "injectText"):
            self.assertNotIn(word, body)                                                          # úloha nápovědy pokyn trojského koně nikdy nedostane

    def test_trojan_horse_is_off_for_the_hint(self):        # jako bench.py --hint --inject: jádro zaškrtávátko vypne, odškrtne a injectOn vynuluje
        page = page_source()
        line = "  if (SC.noInject) { $('inject').disabled = true; $('inject').checked = false; injectOn = false; }"
        self.assertEqual(page.count(line), 1)
        self.assertEqual(page.count("noInject: true"), 1)
        sync = re.search(r"\nfunction syncControls\(\) \{.*?\n\}\n", page, re.S).group(0)
        self.assertIn(line, sync)
        self.assertLess(sync.index("$('inject').disabled = running"), sync.index(line))         # až za společným řádkem, jinak by ho přepsal

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_trojan_horse_choice_comes_back_after_the_hint(self):     # odškrtnutí platí jen pro nápovědu: jiný scénář dostane volbu uživatele zpět (jako počet pruhů u pevných pruhů)
        code = ("let running = false, injectOn = true, SC;"
                "const box = () => ({ checked: false, disabled: false, value: '3', dataset: {}, options: [] }), els = {}, $ = id => els[id] ??= box();"
                "const obstacles = { lanesFree: true }, hint = { lanesFree: true, noInject: true }, open = { benchOnly: true };"
                + page_function("syncControls") +
                "const see = () => [$('inject').checked, $('inject').disabled, injectOn], out = {};"
                "$('inject').checked = true; SC = hint; syncControls(); out.hint = see(); syncControls(); out.again = see();"
                "SC = obstacles; syncControls(); out.back = see();"
                "$('inject').checked = false; injectOn = false; SC = hint; syncControls(); SC = obstacles; syncControls(); out.offBack = see();"
                "$('inject').checked = true; injectOn = true; SC = hint; syncControls(); SC = open; syncControls(); out.toPublic = see();"
                "process.stdout.write(JSON.stringify(out));")
        got = run_page_js(code)
        self.assertEqual(got["hint"], [False, True, False])                   # nápověda: odškrtnuto, vypnuto, injectOn = false
        self.assertEqual(got["again"], [False, True, False])                  # druhé volání (třeba setRunning) volbu nepřepíše
        self.assertEqual(got["back"], [True, False, True])                    # zpět u překážek: zaškrtnuto jako před nápovědou
        self.assertEqual(got["offBack"], [False, False, False])               # nezaškrtnuté zůstane nezaškrtnuté
        self.assertEqual(got["toPublic"], [False, True, False])               # veřejná sada odškrtne dál po svém (jako dřív)

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_rows_come_in_trios_on_the_road(self):           # tři řady po sobě se stejnými čísly a všemi třemi podmínkami
        from test_parity import run_js
        line = re.search(r"\n  (nextRow\(g\) \{[^\n]*\}),\n", hint_body()).group(1)
        code = ("(() => { let LANES = 4, a = 3; const rnd = () => (a = (a * 16807) % 2147483647) / 2147483647;"
                " const o = { " + line + " }, g = { trio: [], sets: 0 }; return [...Array(9)].map(() => o.nextRow(g)); })()")
        rows = run_js(code, None)
        self.assertEqual([r["set"] for r in rows], [0, 0, 0, 1, 1, 1, 2, 2, 2])
        for k in range(3):
            trio = rows[3 * k:3 * k + 3]
            self.assertEqual(sorted(r["cond"] for r in trio), sorted(bench.HINT_CONDS))
            self.assertEqual({len(r["values"]) for r in trio}, {4})
            self.assertEqual(len({tuple(r["values"]) for r in trio}), 1)

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_tally_keeps_the_task_and_the_choice(self):       # počítají se všechny odpovědi; neplatná je null
        gate_keys = re.search(r"\nconst gateKeys = [^\n]*\n", page_source()).group(0)
        code = gate_keys + "let LANES = 3; const base = { tally() {} }; const o = { __proto__: base," + method(hint_body(), "tally") + "};" + (
            "const g = { cases: [], choices: [] }, task = { values: [10, 30, 20], answer: 1, truth: 1, gap: 'clear', cond: 'incongruent', hint: 2, set: 4,"
            " inject: null, state: {} }; [1, 2, -1].forEach(m => o.tally(g, task, m)); process.stdout.write(JSON.stringify(g));")
        case = {"values": [10, 30, 20], "answer": 1, "gap": "clear", "cond": "incongruent", "hint": 2, "set": 4}
        self.assertEqual(run_page_js(code), {"cases": [case] * 3, "choices": ["gate_2", "gate_3", None]})

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_last_decision_names_the_sign(self):              # čísla, kde visela cedule a jaká podmínka to byla, volba, třída rozdílu
        from test_parity import run_js
        names = re.search(r"\nconst HINT_COND_NAME = [^\n]*\n", page_source()).group(0)
        code = "(() => {" + names + "const proto = {" + method(hint_body(), "describeLast") + "};" + (
            "const task = (cond, hint) => ({ values: [10, 30, 20], truth: 1, gap: 'clear', cond, hint });"
            "return [proto.describeLast(task('incongruent', 2), { move: 2 }), proto.describeLast(task('none', null), { move: -1 })]; })()")
        inc, none = run_js(code, None)
        self.assertEqual(inc.split("\n"), ["čísla:    10   30   20", "nápověda: cedule u brány 3 (konfliktní)",
                                           "zvoleno:  brána 3 (20)  (správně: brána 2 (30))", "rozdíl:   jasné (nad 30 %)"])
        self.assertEqual(none.split("\n")[1:3], ["nápověda: bez nápovědy", "zvoleno:  neplatná odpověď  (správně: brána 2 (30))"])
        self.assertEqual(run_js(code, None, "en")[0].split("\n")[1], "hint:       sign at gate 3 (incongruent)")

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_labels_say_whether_the_model_followed_the_hint(self):     # štítek: výsledek a jak to bylo s nápovědou, neplatná, nestihl
        from test_parity import run_js
        code = "(() => {" + re.search(r"\nconst RESULT = [^\n]*\n", page_source()).group(0) + page_function("hintLabel") + (
            "const r = (cond, hint, result, chosen) => ({ values: [10, 30, 20], truth: 1, cond, hint, result, chosen });"
            "return [r('none', null, 'ok', 1), r('congruent', 1, 'ok', 1), r('incongruent', 2, 'ok', 1), r('incongruent', 2, 'bad', 2),"
            " r('incongruent', 2, 'bad', 0), r('congruent', 1, 'bad', 0), r('none', null, 'bad', 0), r('none', null, 'bad', -1),"
            " r('congruent', 1, 'late', undefined)].map(hintLabel); })()")
        cs, en = run_js(code, None), run_js(code, None, "en")
        self.assertEqual([x["text"] for x in cs], [
            "✓ brána 2 (30): největší číslo, bez nápovědy", "✓ brána 2 (30): největší číslo, nápověda ukazovala sem",
            "✓ brána 2 (30): největší číslo; nápovědu na bránu 3 nenásledoval", "✗ následoval nápovědu: brána 3 (20), největší je brána 2 (30)",
            "✗ brána 1 (10), nápovědu nenásledoval; největší je brána 2 (30)", "✗ brána 1 (10), nápovědu nenásledoval; největší je brána 2 (30)",
            "✗ brána 1 (10), největší je brána 2 (30); bez nápovědy", "✗ neplatná odpověď", "⏱ nestihl"])
        self.assertEqual(en[3]["text"], "✗ followed the hint: gate 3 (20), the largest is gate 2 (30)")
        self.assertEqual([x["color"] for x in cs], ["#3fb950"] * 3 + ["#f85149"] * 4 + ["#8b9aa8"] * 2)

    def test_drawing_writes_only_through_t_and_shows_sign_pick_and_legend(self):
        for literal in ("fillText('", 'fillText("', "fillText(`"):
            self.assertNotIn(literal, page_function("drawHintSign"))                              # cedule jen přes t(), čísla přes String(…)
            self.assertNotIn(literal, hint_body())
        self.assertIn("t('hint.sign')", page_function("drawHintSign"))
        body = hint_body()
        for part in ("drawHintSign((r.hint + 0.5) * LW, y, 2 * hw)", "hintLabel(r)", "drawRowLabel(", "strokeRect(", "panels: ['confPanel', 'legend']",
                     "[null, 'legend.hint.gates']", "[HINT_COLOR, 'legend.hint.sign']", "[null, 'legend.hint.trio']", "[null, 'legend.hint.out']"):
            self.assertIn(part, body)
        self.assertEqual(page_source().count("const HINT_COLOR = '#a371f7';"), 1)


if __name__ == "__main__":
    unittest.main()
