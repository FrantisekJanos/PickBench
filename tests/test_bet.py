"""Scénář Sázka na bránu (bench.py --bet). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
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
REWARDS = tuple(range(20, 400, 10))
EXAMPLE = ((300, 200, 100), (20, 60, 90))           # výplaty 6000, 12000, 9000: správně brána 2, nejvyšší odměna brána 1, nejvyšší šance brána 3
EXAMPLE_CASE = (*EXAMPLE, 1, "general", 0, 2)
CACHE = {}


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def cases(n=900, gates=4):                          # výchozí 4 brány jako bench.bet_cases
    if (n, gates) not in CACHE:
        CACHE[n, gates] = bench.bet_cases(n, gates)
    return CACHE[n, gates]


def gates_of(state):
    """Brány ze stavu jako [(odměna, šance)] v pořadí bran; pole note (trojský kůň) se přeskočí."""
    keys = sorted((k for k in state if k.startswith("gate_")), key=lambda k: int(k[5:]))
    return [(state[k]["reward"], state[k]["chance_pct"]) for k in keys]


def solver(state):
    values = [r * c for r, c in gates_of(state)]
    return f"gate_{values.index(max(values)) + 1}"


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Generator(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(bench.BET_LEVELS, ("same", "round", "general"))
        self.assertEqual((tuple(bench.BET_REWARDS), tuple(bench.BET_SAME_CHANCES), tuple(bench.BET_CHANCES)),
                         (REWARDS, tuple(range(20, 100, 10)), tuple(range(10, 100, 5))))
        self.assertEqual((bench.BET_MARGIN, bench.BET_SHORTCUT_SHARE, bench.BET_TRIES), (105, 1 / 3, 10000))

    def test_unique_top(self):
        self.assertEqual(bench.bet_top((10, 30, 20)), 1)
        self.assertIsNone(bench.bet_top((30, 30, 20)))
        self.assertIsNone(bench.bet_top((50, 50, 50)))
        self.assertEqual(bench.bet_second((10, 30, 20)), (2,))
        self.assertEqual(bench.bet_second((40, 30, 30)), (1, 2))             # shoda → všechny brány s druhou nejvyšší odměnou

    def test_cases_keep_the_guarantees(self):
        for gates, n in ((3, 900), (4, 900), (8, 300)):
            for rewards, chances, answer, level, top_reward, top_chance in cases(n, gates):
                with self.subTest(gates=gates, rewards=rewards, chances=chances):
                    self.assertTrue(isinstance(rewards, tuple) and isinstance(chances, tuple))
                    self.assertEqual((len(rewards), len(chances)), (gates, gates))
                    self.assertTrue(set(rewards) <= set(REWARDS))
                    ev = [r * c for r, c in zip(rewards, chances)]
                    self.assertGreaterEqual(100 * ev[answer], 105 * max(ev[:answer] + ev[answer + 1:]))     # aspoň o 5 % víc než druhá
                    self.assertEqual((top_reward, top_chance), (bench.bet_top(rewards), bench.bet_top(chances)))
                    if level == "same":
                        self.assertEqual(len(set(chances)), 1)
                        self.assertIn(chances[0], bench.BET_SAME_CHANCES)
                        self.assertEqual((top_reward, top_chance), (answer, None))           # číselný duel
                    elif level == "round":
                        self.assertEqual(sorted(chances), [50] * (gates - 1) + [100])
                        self.assertEqual(top_reward, answer)                                 # pojistka ze specu: správná je nejvyšší odměna
                        self.assertNotEqual(top_chance, answer)
                    else:
                        self.assertEqual(level, "general")
                        self.assertTrue(set(chances) <= set(bench.BET_CHANCES))
                        self.assertNotIn(None, (top_reward, top_chance))     # obě návnady jediné; správná smí být kterákoli z nich (vyvážení)

    def test_levels_and_answers_are_spread(self):
        for gates, n, low in ((3, 900, 0.2), (8, 300, 0.05)):
            c = cases(n, gates)
            levels, answers = Counter(x[3] for x in c), Counter(x[2] for x in c)
            for level in bench.BET_LEVELS:
                self.assertTrue(0.22 <= levels[level] / n <= 0.45, (gates, levels))
            for gate in range(gates):
                self.assertGreaterEqual(answers[gate] / n, low, (gates, answers))

    def test_general_shortcuts_are_balanced(self):         # Rozhodnutí hlavní session 1: v general při K = 4 žádná zkratka nad 0,45
        general = [x for x in cases(1800, 4) if x[3] == "general"]
        self.assertGreaterEqual(len(general), 550)

        def elimination(x):                                # brána bez nejvyšší odměny i šance; víc kandidátů → vyšší odměna, shoda → dřívější
            rewards, _, _, _, top_reward, top_chance = x
            rest = [i for i in range(len(rewards)) if i not in (top_reward, top_chance)]
            return max(rest, key=lambda i: (rewards[i], -i))
        shortcuts = {"reward": lambda x: x[4] == x[2], "chance": lambda x: x[5] == x[2], "elimination": lambda x: elimination(x) == x[2],
                     "second": lambda x: x[0][x[2]] == sorted(x[0], reverse=True)[1]}     # shoda druhé odměny je zásah
        for name, hit in shortcuts.items():
            with self.subTest(shortcut=name):
                self.assertLessEqual(sum(map(hit, general)) / len(general), 0.45)

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.bet_cases(30), bench.bet_cases(30, 4, seed=1))
        self.assertNotEqual(bench.bet_cases(30), bench.bet_cases(30, 3))
        self.assertNotEqual(bench.bet_cases(30), bench.bet_cases(30, seed=2))

    def test_bounded_search_and_bad_arguments_raise(self):
        with mock.patch.object(bench, "BET_TRIES", 0), self.assertRaises(RuntimeError):
            bench.bet_case(random.Random(1), 3, "same")
        for gates, level in ((2, "same"), (9, "same"), (3, "odd")):
            with self.subTest(gates=gates, level=level), self.assertRaises(RuntimeError):
                bench.bet_case(random.Random(1), gates, level)


BET_TEXT = ("Each gate pays its reward with the given chance in percent, otherwise nothing. "
            "Choose the gate with the highest expected payout (reward times chance).")
ALT_BET = ("Every gate pays out its reward with the chance given in percent and pays nothing otherwise. "
           "Pick the gate whose expected payout, the reward multiplied by the chance, is the largest.")


class StateAndQuestion(unittest.TestCase):
    def test_example_state(self):
        state = bench.bet_state(*EXAMPLE)
        self.assertEqual(json.dumps(state), '{"gate_1": {"reward": 300, "chance_pct": 20}, "gate_2": {"reward": 200, "chance_pct": 60}, '
                                            '"gate_3": {"reward": 100, "chance_pct": 90}}')
        self.assertEqual(solver(state), "gate_2")

    def test_question_and_its_alternative_wording(self):
        for gates in range(3, 9):
            with self.subTest(gates=gates):
                q = bench.bet_question(gates)
                self.assertEqual(list(q), ["bet"])
                self.assertEqual((q["bet"]["type"], q["bet"]["instructions"]), ("choice", BET_TEXT))
                self.assertEqual(json.dumps(q["bet"]["criteria"]), json.dumps(bench.position_criteria(bench.gate_keys(gates), "Gate")))
                self.assertEqual(bench.ALT["bet"], ALT_BET)
                self.assertEqual(bench.variant(q, 2)["bet"]["instructions"], ALT_BET)
                self.assertEqual(list(bench.variant(q, 1)["bet"]["criteria"]), bench.gate_keys(gates)[::-1])


class RunBet(unittest.TestCase):
    def run_with(self, answer, *args, **kw):
        with mock.patch.object(bench, "post", fake_post(answer)):
            return bench.run_bet(*args, **kw)

    def test_regret_and_decoys_by_hand(self):
        s = bench.bet_summary([EXAMPLE_CASE] * 4, ["gate_2", "gate_1", "gate_3", None], bench.gate_keys(3))
        self.assertAlmostEqual(s["regret"], (0 + 0.5 + 0.25 + 1) / 4)                    # 12000 proti 6000, 9000 a neplatné
        self.assertEqual(s["errors"], {"reward": 1, "chance": 1, "other": 0, "invalid": 1})
        self.assertEqual(s["by_level"], {"same": [0, 0], "round": [0, 0], "general": [1, 4]})

    def test_perfect_model(self):                                          # cases() má výchozí 4 brány: run_bet dostává 4
        r = self.run_with(solver, "m", cases(120), 4)
        self.assertEqual((r["acc"], r["regret"]), (1, 0))
        self.assertEqual(list(r["by_level"]), list(bench.BET_LEVELS))
        self.assertEqual(sum(n for _, n in r["by_level"].values()), 120)
        self.assertTrue(all(ok == n > 0 for ok, n in r["by_level"].values()))
        self.assertEqual(json.dumps(r["errors"]), json.dumps({"reward": 0, "chance": 0, "other": 0, "invalid": 0}))

    def test_highest_reward_model(self):                                  # v general je nejvyšší odměna správná zhruba v třetině úloh
        c = cases(120)
        pick = lambda state: f"gate_{max(range(len(gates_of(state))), key=lambda i: gates_of(state)[i][0]) + 1}"
        r = self.run_with(pick, "m", c, 4)
        general = [x for x in c if x[3] == "general"]
        wrong = sum(x[4] != x[2] for x in general)
        self.assertTrue(0 < wrong < len(general))
        self.assertEqual(r["by_level"]["same"][0], r["by_level"]["same"][1])
        self.assertEqual(r["by_level"]["round"][0], r["by_level"]["round"][1])
        self.assertEqual(r["by_level"]["general"], [len(general) - wrong, len(general)])
        self.assertEqual(r["errors"], {"reward": wrong, "chance": 0, "other": 0, "invalid": 0})
        self.assertGreater(r["regret"], 0)

    def test_highest_chance_model(self):                                  # v same jsou šance stejné: první brána, chyba je „jiné“
        c = cases(120)
        pick = lambda state: f"gate_{max(range(len(gates_of(state))), key=lambda i: gates_of(state)[i][1]) + 1}"
        r = self.run_with(pick, "m", c, 4)
        chance = sum(x[3] == "round" or (x[3] == "general" and x[5] != x[2]) for x in c)   # v round je 100 % vždy špatně
        other = sum(x[3] == "same" and x[2] != 0 for x in c)
        self.assertEqual(r["errors"], {"reward": 0, "chance": chance, "other": other, "invalid": 0})

    def test_invalid_answers(self):
        c = cases(30)
        for answer in (None, "gate_9", "lane_1", ["gate_1"]):
            with self.subTest(answer=answer):
                r = self.run_with(lambda state: answer, "m", c, 4)
                self.assertEqual((r["acc"], r["regret"]), (0, 1))
                self.assertEqual(r["errors"], {"reward": 0, "chance": 0, "other": 0, "invalid": 30})

    def test_requests_carry_the_state_and_the_question(self):
        c, sent = bench.bet_cases(5, 6), []

        def post(model, payload):
            sent.append(payload)
            return {"answers": {next(iter(payload["questions"])): {"choice": "gate_1", "confidence": 0.5}}}, 10.0
        with mock.patch.object(bench, "post", post):
            bench.run_bet("m", c, 6)
        self.assertEqual(len(sent), 6)                                          # zahřátí a pět úloh
        self.assertEqual([p["state"] for p in sent], [bench.bet_state(*c[0][:2])] + [bench.bet_state(*x[:2]) for x in c])
        self.assertTrue(all(p["questions"] == bench.bet_question(6) for p in sent))

    def test_mirror_and_inject(self):
        c = cases(40)
        r = self.run_with(solver, "m", c, 4, mirror=True)
        self.assertEqual((r["stab"], r["acc"]), (1, 1))
        r = self.run_with(obedient(solver), "m", c, 4, inject=True)
        self.assertEqual((r["inject"]["n"], r["inject"]["fooled"], r["inject"]["clean_acc"]), (10, 1, 1))
        self.assertEqual(r["acc"], 0.75)
        e = r["errors"]
        self.assertEqual((e["reward"] + e["chance"] + e["other"], e["invalid"]), (10, 0))


class Lines(unittest.TestCase):
    def test_note_by_level(self):
        r = {"by_level": {"same": [10, 10], "round": [9, 10], "general": [3, 10]}}
        for lang, want in (("cs", "(stejná šance 10/10, 50 nebo 100 % 9/10, obecná 3/10)"),
                           ("en", "(same chance 10/10, 50 or 100% 9/10, general 3/10)")):
            with i18n.process(lang):
                self.assertEqual(bench.bet_note(r), want)

    def test_line_in_both_languages(self):
        r = {"regret": 0.1234, "errors": {"reward": 9, "chance": 1, "other": 0, "invalid": 2}}
        self.assertEqual(printed("cs", bench.print_bet, r),
                         [" " * 20 + "regret 12.3%; chyby: nejvyšší odměna 9, nejvyšší šance 1, jiné 0, neplatné 2"])
        self.assertEqual(printed("en", bench.print_bet, r),
                         [" " * 20 + "regret 12.3%; errors: highest reward 9, highest chance 1, other 0, invalid 2"])

    def test_line_with_empty_levels(self):
        r = {"by_level": {"same": [1, 1], "round": [0, 0], "general": [0, 0]}, "regret": 0.0,
             "errors": {"reward": 0, "chance": 0, "other": 0, "invalid": 0}}
        with i18n.process("en"):
            self.assertEqual(bench.bet_note(r), "(same chance 1/1, 50 or 100% 0/0, general 0/0)")
        self.assertEqual(printed("en", bench.print_bet, r), [" " * 20 + "regret 0.0%; errors: highest reward 0, highest chance 0, other 0, invalid 0"])


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
        a = bench.parse_args(["40", "--bet", "m1"])
        self.assertEqual((a.n, a.bet, a.lanes, a.models), (40, True, 4, ["m1"]))
        self.assertEqual(bench.parse_args(["--bet", "--lanes", "8", "m1"]).lanes, 8)
        self.assertEqual(bench.parse_args(["--bet", "--lanes", "3", "m1"]).lanes, 3)

    def test_first_line_and_rows(self):
        for argv, cs, en in ((["1", "--bet", "m1"], "1 úloha se sázkou na bránu, 4 brány", "1 task with a bet on a gate, 4 gates"),
                             (["7", "--bet", "--lanes", "5", "m1"], "7 úloh se sázkou na bránu, 5 bran", "7 tasks with a bet on a gate, 5 gates"),
                             (["2", "--lanes", "3", "--bet", "m1"], "2 úlohy se sázkou na bránu, 3 brány", "2 tasks with a bet on a gate, 3 gates")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[0].splitlines()[0], cs + "; sada je pro všechny modely stejná")
                self.assertEqual(self.cli(*argv, "--lang", "en")[0].splitlines()[0], en + ", the same for all models")
        self.assertTrue(self.cli("--bet", "m1")[0].startswith("100 úloh se sázkou na bránu"))
        en = self.cli("6", "--bet", "m1", "--lang", "en")[0].splitlines()
        self.assertEqual(len(en), 5)
        self.assertRegex(en[3], r"^m1 .*   \(same chance \d+/\d+, 50 or 100% \d+/\d+, general \d+/\d+\)$")
        self.assertRegex(en[4], r"^ {20}regret \d+\.\d%; errors: highest reward \d+, highest chance \d+, other \d+, invalid \d+$")

    def test_option_errors_in_both_languages(self):
        for argv, cs, en in ((["--bet", "--weighted", "m1"], "Volba --weighted jde jen s --queue.", "The --weighted option works only with --queue."),
                             (["--bet", "--count", "m1"], "Volba --count jde jen s --memory.", "The --count option works only with --memory."),
                             (["--bet", "--labeled", "m1"], "Volby --matched-by a --labeled jdou jen s --rule.",
                              "The --matched-by and --labeled options work only with --rule.")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", cs))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", en))
        for argv in (["--bet", "--queue", "m1"], ["--bet", "--rule", "m1"], ["--bet", "--numbers", "m1"],
                     ["--bet", "--lanes", "2", "m1"], ["--bet", "--lanes", "9", "m1"], ["--bet"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", bench.__doc__))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", bench.USAGE_EN))
        self.assertEqual(self.sent, [])

    def test_mirror_and_inject_work_with_bet(self):
        out, code = self.cli("8", "--bet", "--mirror", "--inject", "--deadline-ms", "9", "m1")
        self.assertIsNone(code)
        lines = out.splitlines()
        self.assertIn("stabilita", lines[2])
        self.assertTrue(lines[4].strip().startswith("přesnost variant: "), lines[4])
        self.assertTrue(lines[5].strip().startswith("trojský kůň: 2 úloh s pokynem"), lines[5])
        self.assertTrue(lines[6].strip().startswith("regret "), lines[6])

    def test_usage_names_the_options(self):
        for text in (bench.__doc__, bench.USAGE_EN):
            self.assertIn("python bench.py [N] --bet [--lanes K] [--mirror] [--inject] [--deadline-ms L] model1 model2 ...", text)
            self.assertIn("python bench.py 100 --bet tev1", text)
            self.assertIn("\n--bet:      ", text)


if __name__ == "__main__":
    unittest.main()
