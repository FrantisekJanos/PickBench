"""Scénář Mýtné brány (bench.py --bandit). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
import contextlib
import io
import itertools
import json
import os
import random
import runpy
import sys
import time
import unittest
import urllib.request
from collections import Counter
from fractions import Fraction
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import bench
import i18n
from test_bench import fake_post, obedient
from test_golden import Clock, fake_urlopen

LANG_ENV = "PICKBENCH_LANG"
FORM = "totals"                                       # výchozí podoba stavu (Volby po pokusu)
EXPLORE = ((5, 9, 45), (4, 4, 90), (0, 6, 35), (1, 2, 70))       # příklad ze specu: správně gate_4 (jen 2 průjezdy)
EXPLOIT = ((6, 10, 70), (3, 4, 60), (5, 9, 50), (1, 3, 40))      # podíly 0,6 / 0,75 / 0,56 / 0,33: správně gate_2
TIE = ((2, 4, 50), (4, 8, 55), (1, 5, 20), (3, 9, 30))           # 2/4 a 4/8 ve shodě: správně gate_1 (méně průjezdů)
CACHE = {}


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def cases(n=900, k=4):
    """Úlohy ze semínka 1; velká sada se losuje jednou za běh modulu (900 úloh trvá asi 5 s na K)."""
    for (m, kk), got in CACHE.items():
        if kk == k and m >= n:
            return got[:n]                            # prvních n úloh velké sady = bandit_cases(n): stejné semínko, stejné pořadí
    CACHE[n, k] = bench.bandit_cases(n, k)
    return CACHE[n, k]


def by_hand(gates, answer, cls):
    """Ruční úloha s deníkem v pořadí bran (nejdřív výhry, pak prohry)."""
    log = tuple((i, True) for i, (w, _, _) in enumerate(gates) for _ in range(w)) + \
        tuple((i, False) for i, (w, n, _) in enumerate(gates) for _ in range(n - w))
    return gates, answer, cls, log


def totals_of(state):
    """Souhrn [(výhry, průjezdy)] ze stavu obou podob; pole note (trojský kůň) se přeskočí."""
    if "log" in state:
        drives = Counter(e["gate"] for e in state["log"])
        wins = Counter(e["gate"] for e in state["log"] if e["result"] == "win")
        return [(wins[g], drives[g]) for g in state["gates"]]
    keys = sorted((k for k in state if k.startswith("gate_")), key=lambda k: int(k[5:]))
    return [(state[k]["wins"], state[k]["drives"]) for k in keys]


def solve(state):
    """Nezávislý řešitel podle zadání: brána pod 3 průjezdy, jinak nejvyšší podíl (křížové násobení), při shodě méně průjezdů."""
    t = totals_of(state)
    fresh = [i for i, (_, n) in enumerate(t) if n < 3]
    if fresh:
        return f"gate_{fresh[0] + 1}"
    best = [i for i, (w, n) in enumerate(t) if all(w * m >= v * n for v, m in t)]
    return f"gate_{min(best, key=lambda i: t[i][1]) + 1}"


def greedy_tried(state):
    """Kontrolní model: pravidlo 1 vynechá, bere nejvyšší podíl mezi vyzkoušenými, při shodě nižší číslo brány."""
    t = totals_of(state)
    tried = [i for i, (_, n) in enumerate(t) if n >= 3]
    return f"gate_{max(tried, key=lambda i: (Fraction(*t[i]), -i)) + 1}"


def fewest_drives(state):
    """Kontrolní model: vždy průzkum, brána s nejméně průjezdy, při shodě nižší číslo."""
    t = totals_of(state)
    return f"gate_{min(range(len(t)), key=lambda i: (t[i][1], i)) + 1}"


def hit(picks, answer):
    """Očekávaná trefa zkratky, která ukazuje na množinu bran (remíza = náhodná volba mezi nimi)."""
    return (answer in picks) / len(picks)


def share(sub, name):
    return sum(hit(bench.BANDIT_SHORTCUTS[name]([(w, n) for w, n, _ in g]), a) for g, a, _, _ in sub) / len(sub)


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()



class Rule(unittest.TestCase):
    def test_constants(self):
        self.assertEqual((bench.BANDIT_GATES, bench.BANDIT_EXPLORE, bench.BANDIT_SPLIT, bench.BANDIT_TOTAL), ((4, 5), 3, ((3, 6), (9, 16)), 10))
        self.assertEqual((bench.BANDIT_MARGIN, bench.BANDIT_CHANCES), (Fraction(1, 10), tuple(range(10, 95, 5))))
        self.assertEqual((bench.BANDIT_CLASSES, bench.BANDIT_FORMS, bench.BANDIT_TRIES), (("explore", "exploit", "tie"), ("totals", "history"), 20000))
        self.assertEqual(bench.BANDIT_FRESH, ((0, 0), (0, 1), (0, 2), (1, 2), (1, 1), (2, 2)))
        self.assertEqual(bench.BANDIT_FORM, FORM)                                        # Volby po pokusu
        for k in bench.BANDIT_GATES:
            for cls in bench.BANDIT_CLASSES:
                with self.subTest(k=k, cls=cls):
                    patterns = bench.BANDIT_PATTERNS[k][cls]
                    self.assertTrue(all(len(p) == len(bench.BANDIT_WATCH[cls]) and set(p) <= set("HTM") for p in patterns))
                    self.assertTrue(all(isinstance(w, int) and 0 < w <= 30 for w in patterns.values()))

    def test_rule_matches_the_independent_check(self):              # spec: všechny souhrny pro K = 3 a 0–4 průjezdy (3 375)
        small = [(w, n) for n in range(5) for w in range(n + 1)]
        combos = list(itertools.product(small, repeat=3))
        self.assertEqual(len(combos), 3375)
        rng = random.Random(4)
        combos += [[(w, n) for n in (rng.randint(0, 16) for _ in range(k)) for w in (rng.randint(0, n),)] for k in (4, 5) for _ in range(3000)]
        for totals in combos:
            got = bench.bandit_answer(list(totals))
            self.assertEqual(got[0] if got else None, bench.bandit_check(list(totals)), totals)

    def test_rule_by_hand(self):
        self.assertEqual(bench.bandit_answer([(w, n) for w, n, _ in EXPLORE]), (3, "explore"))
        self.assertEqual(bench.bandit_answer([(w, n) for w, n, _ in EXPLOIT]), (1, "exploit"))
        self.assertEqual(bench.bandit_answer([(w, n) for w, n, _ in TIE]), (0, "tie"))
        self.assertEqual(bench.bandit_answer([(0, 0), (5, 9), (4, 8), (1, 3)]), (0, "explore"))       # 0/0 je brána k průzkumu
        self.assertEqual(bench.bandit_answer([(7, 12), (5, 8), (3, 9), (2, 4)]), (1, "exploit"))      # 5/8 > 7/12, ne podle výher
        self.assertEqual(bench.bandit_answer([(2, 4), (3, 6), (4, 8), (1, 5)]), (0, "tie"))   # shoda tří: rozhodnou průjezdy (generátor ji vyřadí odstupem)
        for totals in ([(1, 2), (0, 1), (5, 9), (3, 4)],                  # dvě brány pod 3 průjezdy
                       [(2, 4), (2, 4), (1, 5), (3, 9)]):                 # shoda dvou se stejnými průjezdy
            with self.subTest(totals=totals):
                self.assertIsNone(bench.bandit_answer(totals))
                self.assertIsNone(bench.bandit_check(totals))

    def test_shortcuts_by_hand(self):
        t = [(w, n) for w, n, _ in EXPLORE]
        s = bench.BANDIT_SHORTCUTS
        self.assertEqual((s["most_wins"](t), s["most_tries"](t), s["fewest_tries"](t), s["fewest_losses"](t), s["lowest_rate"](t)),
                         ({0}, {0}, {3}, {1}, {2}))
        self.assertEqual((s["greedy"](t), s["greedy_tried"](t), s["lowest_rate_tried"](t), s["tie_more"](t), s["exclusion"](t)), ({1}, {1}, {2}, {1}, {3}))
        t = [(w, n) for w, n, _ in TIE]
        self.assertEqual((s["greedy"](t), s["tie_more"](t), s["fewest_tries"](t)), ({0, 1}, {1}, {0}))
        self.assertEqual(bench.bandit_pattern("tie", t, 0), "HHMT")          # vyloučení: levné zkratky trefí všechny brány, zbudou všechny



class Generator(unittest.TestCase):
    def test_cases_keep_the_guarantees(self):
        for k in bench.BANDIT_GATES:
            for gates, answer, cls, log in cases(900, k):
                totals = [(w, n) for w, n, _ in gates]
                with self.subTest(k=k, gates=gates):
                    self.assertTrue(isinstance(gates, tuple) and isinstance(log, tuple) and len(gates) == k)
                    self.assertTrue(all(0 <= w <= n and c in bench.BANDIT_CHANCES for w, n, c in gates))
                    self.assertEqual(bench.bandit_answer(totals), (answer, cls))
                    self.assertEqual(bench.bandit_check(totals), answer)
                    fresh = [i for i, (_, n) in enumerate(totals) if n < 3]
                    self.assertEqual(fresh, [answer] if cls == "explore" else [])               # nejvýš jedna brána k průzkumu
                    if cls == "explore":
                        self.assertIn(totals[answer], bench.BANDIT_FRESH)
                    self.assertTrue(all(n <= 16 for _, n in totals))
                    self.assertLessEqual(sum(n for _, n in totals), 10 * k)
                    shares = sorted((Fraction(w, n) for w, n in totals if n >= 3), reverse=True)
                    if cls == "exploit":
                        self.assertGreaterEqual(shares[0] - shares[1], Fraction(1, 10))
                    if cls == "tie":
                        pair = [i for i, (w, n) in enumerate(totals) if n >= 3 and Fraction(w, n) == shares[0]]
                        self.assertEqual(len(pair), 2)
                        self.assertNotEqual(totals[pair[0]][1], totals[pair[1]][1])
                        self.assertLess(shares[0], 1)
                        self.assertGreaterEqual(shares[1] - shares[2], Fraction(1, 10))
                    self.assertEqual(Counter(log), Counter([(i, True) for i, (w, _) in enumerate(totals) for _ in range(w)] +
                                                           [(i, False) for i, (w, n) in enumerate(totals) for _ in range(n - w)]))

    def test_classes_and_answers_are_spread(self):
        for k, low, high in ((4, 0.18, 0.32), (5, 0.14, 0.26)):
            c = cases(900, k)
            classes, answers = Counter(x[2] for x in c), Counter(x[1] for x in c)
            for cls in bench.BANDIT_CLASSES:
                self.assertTrue(0.28 <= classes[cls] / len(c) <= 0.38, (k, classes))
            for gate in range(k):
                self.assertTrue(low <= answers[gate] / len(c) <= high, (k, answers))

    def test_cheap_shortcuts_stay_near_chance(self):             # spec: každá levná zkratka nejvýš 0,40 (K = 4) a 0,36 (K = 5)
        for k, top in ((4, 0.40), (5, 0.36)):
            c = cases(900, k)
            for cls in bench.BANDIT_CLASSES:
                sub = [x for x in c if x[2] == cls]
                for name in ("most_wins", "most_tries", "fewest_tries", "fewest_losses", "lowest_rate", "lowest_rate_tried", "exclusion"):
                    if cls == "explore" and name == "fewest_tries":
                        continue                                  # v explore je to přímo pravidlo 1 (test níž)
                    with self.subTest(k=k, cls=cls, shortcut=name):
                        self.assertLessEqual(share(sub, name), top)

    def test_partial_strategies(self):                           # poloviny pravidla: každá padá ve své třídě
        for k in bench.BANDIT_GATES:
            c = cases(900, k)
            sub = {cls: [x for x in c if x[2] == cls] for cls in bench.BANDIT_CLASSES}
            with self.subTest(k=k):
                self.assertEqual(share(sub["exploit"], "greedy"), 1)
                self.assertLessEqual(share(sub["explore"], "greedy"), 0.35)
                self.assertEqual(share(sub["tie"], "greedy"), 0.5)                     # dvojice ve shodě: polovina
                self.assertEqual(share(sub["explore"], "fewest_tries"), 1)
                self.assertLessEqual(max(share(sub["exploit"], "fewest_tries"), share(sub["tie"], "fewest_tries")), 0.35)
                self.assertEqual(share(sub["tie"], "tie_more"), 0)
                self.assertEqual(share(sub["exploit"], "tie_more"), 1)

    def test_log_order_is_not_a_shortcut(self):                  # spec: „poslední výhra“ a „poslední průjezd“ nejvýš 0,35 v každé třídě
        for k in bench.BANDIT_GATES:
            c = cases(900, k)
            for cls in bench.BANDIT_CLASSES:
                sub = [x for x in c if x[2] == cls]
                last_win = sum(([i for i, won in log if won] or [None])[-1] == answer for _, answer, _, log in sub) / len(sub)
                last_drive = sum(log[-1][0] == answer for _, answer, _, log in sub if log) / len(sub)
                with self.subTest(k=k, cls=cls):
                    self.assertLessEqual(max(last_win, last_drive), 0.35)

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.bandit_cases(20), bench.bandit_cases(20, 4, seed=1))
        self.assertEqual(bench.bandit_cases(20), cases(900)[:20])
        self.assertNotEqual(bench.bandit_cases(20), bench.bandit_cases(20, seed=2))
        self.assertNotEqual([x[0] for x in bench.bandit_cases(20)], [x[0][:4] for x in bench.bandit_cases(20, 5)])

    def test_bounded_search_and_bad_arguments_raise(self):
        with mock.patch.object(bench, "BANDIT_TRIES", 50), self.assertRaises(RuntimeError):
            bench.bandit_case(random.Random(1), 4, "explore", {"HHMMM": 1})        # nejvyšší i nejnižší podíl naráz: nesplnitelné
        for k, cls in ((3, "explore"), (6, "tie"), (4, "odd")):
            with self.subTest(k=k, cls=cls), self.assertRaises(RuntimeError):
                bench.bandit_case(random.Random(1), k, cls)
        with mock.patch.object(bench, "BANDIT_TRIES", 0), self.assertRaises(RuntimeError):
            bench.bandit_log(random.Random(1), EXPLORE, 3)

    def test_tie_shares(self):
        self.assertEqual(len(bench.BANDIT_TIE_SHARES), 21)
        self.assertEqual(bench.BANDIT_TIE_SHARES[:3], ((1, 8), (1, 7), (1, 6)))
        self.assertEqual(bench.BANDIT_TIE_SHARES[-1], (7, 8))
        for a, b in bench.BANDIT_TIE_SHARES:
            self.assertTrue(0 < a < b and Fraction(a, b).denominator == b and len(bench.bandit_tie_multiples(b)) >= 2, (a, b))


INS_TOTALS = ("Each toll gate pays a prize with its own hidden chance. For every gate, drives is how many times a car has "
              "gone through it and wins is how many of those drives won the prize. Choose the next gate by these rules, "
              "in order: 1. If a gate has fewer than 3 drives, choose it (there is at most one such gate). "
              "2. Otherwise choose the gate with the highest share of wins, wins divided by drives. "
              "3. If two gates share the highest share, choose the one with fewer drives.")
INS_HISTORY = ("Each toll gate pays a prize with its own hidden chance. gates lists all gates; log lists every earlier drive, "
               "oldest first: the gate the car went through and whether it won the prize. Choose the next gate by these rules, "
               "in order: 1. If a gate has fewer than 3 drives in log, choose it (there is at most one such gate). "
               "2. Otherwise choose the gate with the highest share of wins, its wins divided by its drives. "
               "3. If two gates share the highest share, choose the one with fewer drives.")
ALT_TOTALS = ("Every toll gate has its own unknown chance of paying a prize. drives tells how often a car has passed a gate "
              "and wins how often that paid. Pick the next gate: a gate passed fewer than 3 times comes first, and there is "
              "never more than one; if there is none, take the gate with the most wins per drive; when two gates are level "
              "on that, take the one passed fewer times.")
ALT_HISTORY = ("Every toll gate has its own unknown chance of paying a prize. gates names all gates and log holds every "
               "earlier drive from the oldest: which gate the car passed and whether that paid. Pick the next gate: a gate "
               "that appears fewer than 3 times in log comes first, and there is never more than one; if there is none, "
               "take the gate with the most wins per drive; when two gates are level on that, take the one passed fewer times.")


class StateAndQuestion(unittest.TestCase):
    def test_example_states(self):
        case = by_hand(EXPLORE, 3, "explore")
        state = bench.bandit_state(case)
        self.assertEqual(json.dumps(state), '{"gate_1": {"drives": 9, "wins": 5}, "gate_2": {"drives": 4, "wins": 4}, '
                                            '"gate_3": {"drives": 6, "wins": 0}, "gate_4": {"drives": 2, "wins": 1}}')
        self.assertEqual(bench.bandit_state(case, "totals"), state)
        history = bench.bandit_state(case, "history")
        self.assertEqual(list(history), ["gates", "log"])
        self.assertEqual(history["gates"], ["gate_1", "gate_2", "gate_3", "gate_4"])
        self.assertEqual(history["log"][:2], [{"gate": "gate_1", "result": "win"}] * 2)
        self.assertEqual(len(history["log"]), 21)
        fresh = by_hand(((0, 0, 50), (5, 9, 45), (4, 8, 60), (1, 3, 20)), 0, "explore")
        self.assertNotIn("gate_1", {e["gate"] for e in bench.bandit_state(fresh, "history")["log"]})    # brána bez průjezdu v deníku chybí
        with self.assertRaises(ValueError):
            bench.bandit_state(case, "log")                                   # podoby jsou totals a history
        for x in cases(60) + cases(60, 5):
            self.assertEqual(totals_of(bench.bandit_state(x, "history")), totals_of(bench.bandit_state(x)))   # deník nese týž souhrn
            self.assertEqual(solve(bench.bandit_state(x)), f"gate_{x[1] + 1}")

    def test_questions_and_their_alternative_wordings(self):
        for k in bench.BANDIT_GATES:
            for form, key, text, alt in (("totals", "toll", INS_TOTALS, ALT_TOTALS), ("history", "toll_log", INS_HISTORY, ALT_HISTORY)):
                with self.subTest(k=k, form=form):
                    q = bench.bandit_question(k, form)
                    self.assertEqual(list(q), [key])
                    self.assertEqual((q[key]["type"], q[key]["instructions"]), ("choice", text))
                    self.assertEqual(json.dumps(q[key]["criteria"]), json.dumps(bench.position_criteria(bench.gate_keys(k), "Gate")))
                    self.assertEqual(bench.ALT[key], alt)
                    self.assertEqual(bench.variant(q, 2)[key]["instructions"], alt)
                    self.assertEqual(list(bench.variant(q, 1)[key]["criteria"]), bench.gate_keys(k)[::-1])
        self.assertEqual(bench.bandit_question(4), bench.bandit_question(4, FORM))

    def test_state_stays_under_600_tokens(self):                 # odhad: 4 znaky JSON na token; deník při 5 branách je nejdelší
        longest = max(len(json.dumps(bench.bandit_state(x, "history"))) for x in cases(900, 5))
        self.assertLessEqual(longest, 2400)
        self.assertLessEqual(max(len(json.dumps(bench.bandit_state(x))) for x in cases(900, 5)), 200)


class RunBandit(unittest.TestCase):
    def run_with(self, answer, *args, **kw):
        with mock.patch.object(bench, "post", fake_post(answer)):
            return bench.run_bandit(*args, **kw)

    def test_errors_and_loss_by_hand(self):
        c = [by_hand(EXPLORE, 3, "explore")] * 5 + [by_hand(EXPLOIT, 1, "exploit")] * 4 + [by_hand(TIE, 0, "tie")] * 4
        choices = ["gate_4", "gate_2", "gate_1", "gate_3", None,              # správně, bez průzkumu, nejvíc výher, nejnižší podíl, neplatná
                   "gate_2", "gate_4", "gate_1", "gate_3",                    # správně, zbytečný průzkum, nejvíc výher, jiné
                   "gate_1", "gate_2", "gate_3", "lane_1"]                    # správně, shoda: víc průjezdů, nejnižší podíl, neplatná
        s = bench.bandit_summary(c, choices, bench.gate_keys(4))
        self.assertEqual(list(s), ["by_class", "errors", "loss"])
        self.assertEqual(s["by_class"], {"explore": [1, 5], "exploit": [1, 4], "tie": [1, 4]})
        self.assertEqual(json.dumps(s["errors"]), json.dumps({"no_explore": 1, "tie_more": 1, "over_explore": 1, "most_wins": 2,
                                                              "fewest_losses": 0, "lowest_rate": 2, "other": 1, "invalid": 2}))
        # ztráta = nejvyšší skrytá šance − šance zvolené; neplatná = největší ztráta v úloze
        model = [20, 0, 45, 55, 55] + [10, 30, 0, 20] + [5, 0, 35, 35]
        rule = [20] * 5 + [10] * 4 + [5] * 4
        greedy = [0] * 5 + [10] * 4 + [2.5] * 4                               # v tie hladová volba = průměr dvojice (5 a 0)
        rand = [30] * 5 + [15] * 4 + [(5 + 0 + 35 + 25) / 4] * 4
        for name, want in (("model", model), ("rule", rule), ("greedy", greedy), ("random", rand)):
            self.assertAlmostEqual(s["loss"][name], sum(want) / len(want), msg=name)
        self.assertEqual([bench.bandit_error_kind(by_hand(EXPLORE, 3, "explore"), p) for p in (3, 1, None)], [None, "no_explore", "invalid"])

    def test_perfect_model(self):
        for k in bench.BANDIT_GATES:
            for form in bench.BANDIT_FORMS:
                with self.subTest(k=k, form=form):
                    r = self.run_with(solve, "m", cases(90, k), form=form)
                    self.assertEqual(r["acc"], 1)
                    self.assertEqual(list(r["by_class"]), list(bench.BANDIT_CLASSES))
                    self.assertTrue(all(ok == n > 0 for ok, n in r["by_class"].values()))
                    self.assertEqual(sum(r["errors"].values()), 0)
                    self.assertEqual(r["loss"]["model"], r["loss"]["rule"])

    def test_control_models_fall_into_their_error_class(self):
        c = cases(300)
        r = self.run_with(greedy_tried, "m", c)                                # bez průzkumu: celá třída explore, v tie dvojice s víc průjezdy
        explore = sum(x[2] == "explore" for x in c)
        tie_wrong = sum(x[2] == "tie" and greedy_tried(bench.bandit_state(x)) != f"gate_{x[1] + 1}" for x in c)
        self.assertGreater(tie_wrong, 0)
        self.assertEqual(r["errors"], dict.fromkeys(bench.BANDIT_ERRORS, 0) | {"no_explore": explore, "tie_more": tie_wrong})
        r = self.run_with(fewest_drives, "m", c)                               # vždy průzkum: chyby jen zbytečný průzkum
        wrong = sum(fewest_drives(bench.bandit_state(x)) != f"gate_{x[1] + 1}" for x in c)
        self.assertEqual(r["by_class"]["explore"][0], explore)
        self.assertEqual(r["errors"], dict.fromkeys(bench.BANDIT_ERRORS, 0) | {"over_explore": wrong})

    def test_invalid_answers(self):
        c = cases(30)
        for answer in (None, "gate_9", "Gate 1", ["gate_1"], 2):
            with self.subTest(answer=answer):
                r = self.run_with(lambda state: answer, "m", c)
                self.assertEqual((r["acc"], r["errors"]["invalid"]), (0, 30))
                self.assertAlmostEqual(r["loss"]["model"], sum(max(z for *_, z in x[0]) - min(z for *_, z in x[0]) for x in c) / 30)

    def test_requests_carry_the_state_and_the_question(self):
        c = bench.bandit_cases(5, 5)
        for form in bench.BANDIT_FORMS:
            sent = []

            def post(model, payload):
                sent.append(payload)
                return {"answers": {next(iter(payload["questions"])): {"choice": "gate_1", "confidence": 0.5}}}, 10.0
            with mock.patch.object(bench, "post", post):
                bench.run_bandit("m", c, form=form)
            with self.subTest(form=form):
                self.assertEqual(len(sent), 6)                                  # zahřátí a pět úloh
                self.assertEqual([p["state"] for p in sent], [bench.bandit_state(c[0], form)] + [bench.bandit_state(x, form) for x in c])
                self.assertTrue(all(p["questions"] == bench.bandit_question(5, form) for p in sent))

    def test_mirror_and_inject(self):
        c = cases(40)
        r = self.run_with(solve, "m", c, mirror=True)
        self.assertEqual((r["stab"], r["acc"]), (1, 1))
        r = self.run_with(obedient(solve), "m", c, inject=True)
        self.assertEqual((r["inject"]["n"], r["inject"]["fooled"], r["inject"]["clean_acc"]), (10, 1, 1))
        self.assertEqual(r["acc"], 0.75)
        e = r["errors"]
        self.assertEqual((sum(e.values()) - e["invalid"], e["invalid"]), (10, 0))


class Lines(unittest.TestCase):
    def test_note_by_class(self):
        r = {"by_class": {"explore": [3, 10], "exploit": [9, 11], "tie": [5, 12]}}
        for lang, want in (("cs", "(průzkum 3/10, nejvyšší podíl 9/11, shoda podílů 5/12)"), ("en", "(explore 3/10, best share 9/11, tied share 5/12)")):
            with i18n.process(lang):
                self.assertEqual(bench.bandit_note(r), want)

    def test_lines_in_both_languages(self):
        r = {"errors": {"no_explore": 8, "tie_more": 4, "over_explore": 3, "most_wins": 2, "fewest_losses": 1, "lowest_rate": 0, "other": 5, "invalid": 6},
             "loss": {"model": 11.54, "rule": 14.6, "greedy": 9.25, "random": 21.4}}
        pad = " " * 20
        self.assertEqual(printed("cs", bench.print_bandit, r),
                         [pad + "chyby: bez průzkumu 8, shoda: víc průjezdů 4, zbytečný průzkum 3, nejvíc výher 2, nejméně proher 1, nejnižší podíl 0, jiné 5, neplatné 6",
                          pad + "ztráta jednoho kroku 11.5 p. b. (pravidlo 14.6, hladová volba 9.2, náhoda 21.4)"])
        self.assertEqual(printed("en", bench.print_bandit, r),
                         [pad + "errors: no exploration 8, tie: more drives 4, needless exploration 3, most wins 2, fewest losses 1, lowest share 0, other 5, invalid 6",
                          pad + "one-step loss 11.5 pp (rule 14.6, greedy pick 9.2, random 21.4)"])

    def test_lines_with_empty_classes(self):
        r = {"by_class": {"explore": [1, 1], "exploit": [0, 0], "tie": [0, 0]}, "errors": dict.fromkeys(bench.BANDIT_ERRORS, 0),
             "loss": {"model": 0.0, "rule": 0.0, "greedy": 25.0, "random": 12.5}}
        with i18n.process("en"):
            self.assertEqual(bench.bandit_note(r), "(explore 1/1, best share 0/0, tied share 0/0)")
        self.assertEqual(len(printed("en", bench.print_bandit, r)), 2)


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
        a = bench.parse_args(["40", "--bandit", "m1"])
        self.assertEqual((a.n, a.bandit, a.totals, a.history, a.lanes, a.lanes_given, a.models), (40, True, False, False, 4, False, ["m1"]))
        a = bench.parse_args(["--history", "--bandit", "--lanes", "5", "m1"])
        self.assertEqual((a.history, a.totals, a.lanes, a.lanes_given), (True, False, 5, True))
        self.assertTrue(bench.parse_args(["--bandit", "--totals", "m1"]).totals)

    def test_first_line_and_rows(self):
        form = {"totals": ("souhrn průjezdů", "drive totals"), "history": ("deník průjezdů", "drive log")}
        for argv, cs, en in ((["1", "--bandit", "m1"], "1 úloha u mýta (bandita, {}), 4 brány", "1 task at a toll (bandit, {}), 4 gates"),
                             (["7", "--bandit", "--lanes", "5", "--history", "m1"], "7 úloh u mýta (bandita, {}), 5 bran", "7 tasks at a toll (bandit, {}), 5 gates"),
                             (["3", "--totals", "--bandit", "m1"], "3 úlohy u mýta (bandita, {}), 4 brány", "3 tasks at a toll (bandit, {}), 4 gates")):
            words = form["history"] if "--history" in argv else form["totals"] if "--totals" in argv else form[FORM]
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[0].splitlines()[0], cs.format(words[0]) + "; sada je pro všechny modely stejná")
                self.assertEqual(self.cli(*argv, "--lang", "en")[0].splitlines()[0], en.format(words[1]) + ", the same for all models")
        self.assertTrue(self.cli("--bandit", "m1")[0].startswith("100 úloh u mýta"))       # výchozí počet úloh
        en = self.cli("6", "--bandit", "m1", "--lang", "en")[0].splitlines()
        self.assertEqual(len(en), 6)
        self.assertRegex(en[3], r"^m1 .*   \(explore \d+/\d+, best share \d+/\d+, tied share \d+/\d+\)$")
        self.assertRegex(en[4], r"^ {20}errors: no exploration \d+, tie: more drives \d+, needless exploration \d+, most wins \d+, "
                                r"fewest losses \d+, lowest share \d+, other \d+, invalid \d+$")
        self.assertRegex(en[5], r"^ {20}one-step loss \d+\.\d pp \(rule \d+\.\d, greedy pick \d+\.\d, random \d+\.\d\)$")
        self.assertEqual(len(self.cli("1", "--bandit", "m1")[0].splitlines()), 6)            # jediná úloha: prázdné třídy 0/0

    def test_option_errors_in_both_languages(self):
        lanes = ("Scénář Mýtné brány umí jen 4 nebo 5 bran (--lanes 4 nebo 5).", "The toll gate scenario supports only 4 or 5 gates (--lanes 4 or 5).")
        form = ("Volby --totals a --history jdou jen s --bandit.", "The --totals and --history options work only with --bandit.")
        for argv, (cs, en) in ((["6", "--bandit", "--lanes", "3", "m1"], lanes), (["--bandit", "--lanes", "6", "m1"], lanes),
                               (["--bandit", "--lanes", "2", "m1"], lanes), (["6", "--totals", "m1"], form), (["--history", "--queue", "m1"], form),
                               (["--bandit", "--weighted", "m1"], ("Volba --weighted jde jen s --queue.", "The --weighted option works only with --queue.")),
                               (["--bandit", "--rows", "m1"], ("Volby --rows a --blocked jdou jen s --lookahead.",
                                                               "The --rows and --blocked options work only with --lookahead."))):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", cs))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", en))
        for argv in (["--bandit", "--queue", "m1"], ["--bandit", "--rule", "m1"], ["--bandit", "--bet", "m1"], ["--bandit", "--timetable", "m1"],
                     ["--bandit", "--totals", "--history", "m1"], ["--bandit", "--lanes", "9", "m1"], ["--bandit"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", bench.__doc__))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", bench.USAGE_EN))
        self.assertEqual(self.sent, [])

    def test_both_forms_send_the_same_tasks(self):
        self.cli("6", "--bandit", "--totals", "m1")
        totals, self.sent = [json.loads(body) for body in self.sent], []
        self.cli("6", "--bandit", "--history", "m1")
        history = [json.loads(body) for body in self.sent]
        self.assertEqual((len(totals), len(history)), (7, 7))                   # zahřátí a šest úloh
        self.assertTrue(all(list(p["questions"]) == ["toll"] for p in totals))
        self.assertTrue(all(list(p["questions"]) == ["toll_log"] for p in history))
        self.assertEqual([totals_of(p["state"]) for p in totals], [totals_of(p["state"]) for p in history])

    def test_mirror_and_inject_work_with_bandit(self):
        out, code = self.cli("8", "--bandit", "--mirror", "--inject", "--deadline-ms", "9", "m1")
        self.assertIsNone(code)
        lines = out.splitlines()
        self.assertIn("stabilita", lines[2])
        self.assertIn("goodput", lines[2])
        self.assertTrue(lines[4].strip().startswith("přesnost variant: "), lines[4])
        self.assertTrue(lines[5].strip().startswith("trojský kůň: 2 úloh s pokynem"), lines[5])
        self.assertTrue(lines[6].strip().startswith("chyby: bez průzkumu "), lines[6])
        self.assertTrue(lines[7].strip().startswith("ztráta jednoho kroku "), lines[7])

    def test_usage_names_the_options(self):
        for text in (bench.__doc__, bench.USAGE_EN):
            self.assertIn("python bench.py [N] --bandit [--totals | --history] [--lanes K] [--mirror] [--inject] [--deadline-ms L] model1 model2 ...", text)
            self.assertIn("python bench.py 100 --bandit tev1", text)
            self.assertIn("\n--bandit:   ", text)
            self.assertIn("\n--totals, --history:  ", text)
            self.assertIn(FORM + ".", text)


if __name__ == "__main__":
    unittest.main()
