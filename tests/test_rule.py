"""Scénář Skryté pravidlo (bench.py --rule). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
import contextlib
import io
import json
import os
import random
import runpy
import subprocess
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
from test_bench import fake_post, obedient, recording
from test_golden import Clock, fake_urlopen

LANG_ENV = "PICKBENCH_LANG"
GATES = ("gate_1", "gate_2", "gate_3")
CACHE = {}


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def cases(n=3000):
    """Úlohy ze semínka 1; velká sada se losuje jednou za běh modulu."""
    if n not in CACHE:
        CACHE[n] = bench.rule_cases(n)
    return CACHE[n]


def card(count, color, shape):
    return {"color": color, "shape": shape, "count": count}


def parse_card(text):
    """„2 red stars“ → karta; obrácení card_text."""
    count, color, shape = text.split()
    return card(int(count), color, shape[:-1] if int(count) > 1 else shape)


def log_of(state):
    """Deník ze stavu (obě podoby mají stejný deník) jako [(znak, správně?)]."""
    return [(e["matched_by"], e["result"] == "correct") for e in state["log"]]


def gate_by(state, feature):
    """Brána, která se se vzorem shoduje ve znaku feature: v labeled ze znaku brány, v matched_by z karet."""
    if "sample" not in state:
        return next(k for k in GATES if state[k]["matches_sample_by"] == feature)
    sample = parse_card(state["sample"])
    return next(k for k in GATES if parse_card(state[k])[feature] == sample[feature])


def solve(state):
    """Model, který odpoví správně v obou podobách: přečte deník a vybere bránu podle jediného kandidáta."""
    (rule,), _, _ = bench.rule_read(log_of(state))
    return gate_by(state, rule)


def last_correct(state):
    """Zopakuje znak posledního správného pokusu; bez správného pokusu gate_1."""
    hits = [f for f, ok in log_of(state) if ok]
    return gate_by(state, hits[-1]) if hits else "gate_1"


def least_frequent(state):
    """Zkratka: brána znaku, který se v deníku objevil nejméně často (shoda → pořadí color, shape, count)."""
    counts = Counter(f for f, _ in log_of(state))
    return gate_by(state, min(bench.RULE_FEATURES, key=lambda f: counts[f]))


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


C, W = True, False
SPEC_LOG = [("color", C)] * 3 + [("color", W), ("shape", W)]                   # příklad z rešerše, přepsaný na nové znění pravidla


class Reading(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(bench.RULE_FEATURES, ("color", "shape", "count"))
        self.assertEqual(bench.RULE_VALUES, {"color": ("red", "blue", "green"), "shape": ("circle", "star", "square"), "count": (1, 2, 3)})
        self.assertEqual(bench.RULE_CLASSES, ("repeat", "switch", "eliminate"))
        self.assertEqual((bench.RULE_STREAK, bench.RULE_LOG, bench.RULE_SHORT, bench.RULE_STICK, bench.RULE_TRIES), (3, (3, 10), 5, 0.8, 10000))
        self.assertEqual(bench.RULE_INSERT, 0.5)
        self.assertEqual(bench.RULE_LENGTHS, ("short", "long"))
        self.assertEqual(bench.RULE_FORMS, ("labeled", "matched_by"))
        self.assertIn(bench.RULE_FORM, bench.RULE_FORMS)

    def test_cards(self):
        self.assertEqual([bench.card_text(c) for c in (card(2, "red", "star"), card(1, "green", "circle"), card(3, "blue", "square"))],
                         ["2 red stars", "1 green circle", "3 blue squares"])
        self.assertEqual(bench.rule_shared(card(2, "red", "star"), card(1, "red", "circle")), ["color"])
        self.assertEqual(bench.rule_shared(card(2, "red", "star"), card(2, "blue", "star")), ["shape", "count"])
        self.assertEqual(bench.rule_shared(card(2, "red", "star"), card(1, "blue", "circle")), [])
        self.assertEqual(parse_card("3 blue squares"), card(3, "blue", "square"))

    def test_research_example(self):
        self.assertEqual(bench.rule_read(SPEC_LOG), ({"count"}, "color", "switch"))

    def test_log_ending_in_a_triple_is_undetermined(self):
        self.assertEqual(bench.rule_read([("color", C)] * 3), ({"shape", "count"}, "color", None))
        self.assertEqual(bench.rule_read([("color", C)] * 3 + [("shape", C)] * 3), ({"color", "count"}, "shape", None))

    def test_four_correct_in_a_row_across_a_change(self):
        self.assertEqual(bench.rule_read([("color", C)] * 3 + [("shape", C)]), ({"shape"}, "color", "repeat"))
        self.assertEqual(bench.rule_read([("color", C)] * 3 + [("shape", C)] * 2), ({"shape"}, "color", "repeat"))

    def test_streak_restarts_after_a_wrong_pick(self):
        log = [("color", C), ("color", C), ("shape", W), ("color", C), ("color", C)]
        self.assertEqual(bench.rule_read(log), ({"color"}, None, "repeat"))

    def test_elimination_and_switch(self):
        self.assertEqual(bench.rule_read([("color", W), ("shape", W), ("color", W)]), ({"count"}, None, "eliminate"))
        self.assertEqual(bench.rule_read([("color", W)] * 3), ({"shape", "count"}, None, None))
        self.assertEqual(bench.rule_read([("shape", C)] * 3 + [("shape", W)]), ({"color", "count"}, "shape", None))
        self.assertEqual(bench.rule_read([("shape", C)] * 3 + [("shape", W), ("count", W)]), ({"color"}, "shape", "switch"))
        self.assertEqual(bench.rule_read([(f, ok, "x", "y") for f, ok in SPEC_LOG]), ({"count"}, "color", "switch"))   # položka smí nést víc prvků


class Generator(unittest.TestCase):
    def test_cases_keep_the_guarantees(self):
        for log, sample, gates, top, cls, old in cases():
            with self.subTest(log=log, sample=sample, gates=gates):
                lo, hi = bench.RULE_LOG
                self.assertTrue(lo <= len(log) <= hi)
                for entry in log:                                                 # položka deníku je jen (znak, správně?), karty se nelosují
                    feature, ok = entry
                    self.assertIn(feature, bench.RULE_FEATURES)
                    self.assertIsInstance(ok, bool)
                shared = [bench.rule_shared(sample, g) for g in gates]
                self.assertTrue(all(len(s) == 1 for s in shared))                 # každá brána shoda v právě jednom znaku
                self.assertEqual(sorted(s[0] for s in shared), sorted(bench.RULE_FEATURES))      # znaky bran jsou permutace
                for c in (sample, *gates):
                    self.assertEqual(list(c), list(bench.RULE_FEATURES))
                    self.assertTrue(all(c[f] in bench.RULE_VALUES[f] for f in bench.RULE_FEATURES))
                candidates, old_feature, read_cls = bench.rule_read(log)
                self.assertEqual(candidates, {shared[top][0]})                    # odvozené pravidlo je jediné a je to pravidlo úlohy
                self.assertEqual(read_cls, cls)
                if old_feature is None:
                    self.assertIsNone(old)
                else:
                    self.assertEqual(shared[old], [old_feature])
                    self.assertNotEqual(old, top)

    def test_classes_and_answers_are_spread(self):
        c = cases()
        classes, tops = Counter(x[4] for x in c), Counter(x[3] for x in c)
        for cls in bench.RULE_CLASSES:
            self.assertTrue(0.25 <= classes[cls] / len(c) <= 0.42, (cls, classes))
        for top in range(3):
            self.assertTrue(0.25 <= tops[top] / len(c) <= 0.42, (top, tops))
        self.assertTrue(all(x[5] is not None for x in c if x[4] == "switch"))
        self.assertTrue(all(x[5] is None for x in c if x[4] == "eliminate"))
        for cls in bench.RULE_CLASSES:                                         # délka deníku nezávisí na třídě (Rozpory 9)
            sub = [x for x in c if x[4] == cls]
            short = sum(len(x[0]) <= bench.RULE_SHORT for x in sub) / len(sub)
            self.assertTrue(0.4 <= short <= 0.6, (cls, short))

    def test_control_strategies(self):
        states = [(bench.rule_state(x, "matched_by"), x) for x in cases()]

        def acc(pick, cls=None):
            sub = [(s, x) for s, x in states if cls is None or x[4] == cls]
            return sum(pick(s) == GATES[x[3]] for s, x in sub) / len(sub)
        self.assertEqual(acc(solve), 1)
        self.assertEqual(acc(last_correct, "repeat"), 1)
        self.assertEqual(acc(last_correct, "switch"), 0)
        self.assertTrue(all(last_correct(s) == GATES[x[5]] for s, x in states if x[4] == "switch"))   # v přepnutí vždy brána starého pravidla
        self.assertLessEqual(acc(last_correct, "eliminate"), 0.4)
        self.assertLess(acc(last_correct), 0.5)                                    # Rozpory 2
        self.assertLess(acc(lambda s: gate_by(s, s["log"][-1]["matched_by"])), 0.45)   # zopakuj znak posledního pokusu
        self.assertLess(acc(lambda s: "gate_1"), 0.4)

    def test_least_frequent_feature_is_balanced_in_switch(self):
        states = [(bench.rule_state(x, "labeled"), x) for x in cases()]

        def acc(cls):
            sub = [(s, x) for s, x in states if x[4] == cls]
            return sum(least_frequent(s) == GATES[x[3]] for s, x in sub) / len(sub)
        self.assertTrue(0.2 <= acc("switch") <= 0.45, acc("switch"))
        self.assertEqual(acc("eliminate"), 1)                                      # bez změny je nezkoušený znak totéž co vyloučení

    def test_first_pick_is_not_a_shortcut_in_switch(self):
        """Zkratka „znak prvního pokusu“: v krátkém switch, kde nejméně častý znak netrefí, je první pokus z definice
        budoucí pravidlo; v dlouhém deníku se ale zvýhodnit nesmí."""
        states = [(bench.rule_state(x, "labeled"), x) for x in cases() if x[4] == "switch"]

        def acc(long=None):
            sub = [(s, x) for s, x in states if long is None or (len(x[0]) > bench.RULE_SHORT) == long]
            return sum(gate_by(s, s["log"][0]["matched_by"]) == GATES[x[3]] for s, x in sub) / len(sub)
        self.assertLessEqual(acc(long=True), 0.45)
        self.assertLessEqual(acc(), 0.6)

    def test_rule_features_are_spread(self):
        """V podobě labeled brána jmenuje svůj znak: „vždy count“ nesmí ve třídě vycházet."""
        def shares(sub):
            rules = Counter(bench.rule_shared(x[1], x[2][x[3]])[0] for x in sub)
            return {f: rules[f] / len(sub) for f in bench.RULE_FEATURES}
        c = cases()
        for cls in ("repeat", "eliminate"):
            for f, share in shares([x for x in c if x[4] == cls]).items():
                self.assertTrue(0.25 <= share <= 0.42, (cls, f, share))
        switch = [x for x in c if x[4] == "switch"]
        for f, share in shares([x for x in switch if len(x[0]) > bench.RULE_SHORT]).items():
            self.assertTrue(0.2 <= share <= 0.45, ("switch long", f, share))
        self.assertLessEqual(max(shares(switch).values()), 0.5, shares(switch))

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.rule_cases(40), bench.rule_cases(40))
        self.assertEqual(bench.rule_cases(40), bench.rule_cases(40, seed=1))
        self.assertNotEqual(bench.rule_cases(40), bench.rule_cases(40, seed=2))

    def test_both_forms_carry_the_same_task_and_labeled_has_no_cards(self):
        for x in cases()[:300]:
            labeled, matched = bench.rule_state(x, "labeled"), bench.rule_state(x, "matched_by")
            with self.subTest(case=x):
                self.assertEqual(list(labeled), ["log", *GATES])
                self.assertEqual(list(matched), ["log", "sample", *GATES])
                self.assertEqual(labeled["log"], matched["log"])
                self.assertEqual(log_of(labeled), x[0])
                self.assertEqual(solve(labeled), solve(matched))
                self.assertEqual(solve(labeled), GATES[x[3]])
                sample = parse_card(matched["sample"])
                for k in GATES:
                    self.assertEqual(labeled[k], {"matches_sample_by": bench.rule_shared(sample, parse_card(matched[k]))[0]})
                self.assertEqual(bench.rule_state(x), bench.rule_state(x, bench.RULE_FORM))
        with self.assertRaises(ValueError):
            bench.rule_state(cases()[0], "cards")

    def test_bounded_search_and_broken_guarantee_raise(self):
        with mock.patch.object(bench, "RULE_TRIES", 0), self.assertRaises(RuntimeError):
            bench.rule_case(random.Random(1), "repeat", "short")
        for cls, length in (("guess", "short"), ("repeat", "medium")):
            with self.subTest(cls=cls, length=length), self.assertRaises(RuntimeError):
                bench.rule_case(random.Random(1), cls, length)
        with mock.patch.object(bench, "rule_gates", lambda rng, sample: [dict(sample)] * 3), self.assertRaises(RuntimeError):
            bench.rule_case(random.Random(1), "repeat", "long")                    # brány shodné se vzorem ve všech znacích


RED_STAR = card(2, "red", "star")
SPEC_CASE = (SPEC_LOG, RED_STAR, [card(2, "blue", "circle"), card(1, "red", "circle"), card(3, "green", "star")], 0, "switch", 1)
SPEC_STATE = {"log": [{"matched_by": "color", "result": "correct"}] * 3
              + [{"matched_by": "color", "result": "wrong"}, {"matched_by": "shape", "result": "wrong"}],
              "sample": "2 red stars", "gate_1": "2 blue circles", "gate_2": "1 red circle", "gate_3": "3 green stars"}
SPEC_LABELED = {"log": SPEC_STATE["log"], "gate_1": {"matches_sample_by": "count"},
                "gate_2": {"matches_sample_by": "color"}, "gate_3": {"matches_sample_by": "shape"}}
RULE_TEXT = ("A hidden rule decides which gate is right: the gate whose card matches the sample card by color, by shape, or by count. "
             "Each gate matches the sample in exactly one feature. The rule changes to another feature right after every three correct picks in a row. "
             "log lists earlier picks, oldest first: which feature the picked gate shared with the sample, and whether it was correct. "
             "Work out the current rule and choose the gate that matches the sample by it.")
LABELED_TEXT = ("A hidden rule decides which gate is right: the gate that matches the sample card by color, by shape, or by count. "
                "Each gate says which feature it shares with the sample. The rule changes to another feature right after every three correct picks in a row. "
                "log lists earlier picks, oldest first: which feature the picked gate shared with the sample, and whether it was correct. "
                "Work out the current rule and choose the gate that shares that feature with the sample.")
ALT_RULE = ("Which gate is right is set by a hidden rule: the right gate shares the color, the shape or the count with the sample card. "
            "Every gate shares exactly one feature with the sample. Right after any three correct picks in a row, the rule switches to a different feature. "
            "log holds the earlier picks from the oldest: the feature the picked gate had in common with the sample, and whether the pick was correct. "
            "Find the rule in force now and pick the gate that matches the sample by it.")
ALT_LABELED = ("A hidden rule picks the right gate: the one that shares a given feature (color, shape or count) with the sample card. "
               "Each gate says which feature it shares with the sample. After every three correct picks in a row the rule switches to a different feature. "
               "log holds the earlier picks from the oldest: the feature the picked gate shared with the sample, and whether it was correct. "
               "Find the rule in force now and pick the gate that shares that feature with the sample.")


class StateAndQuestion(unittest.TestCase):
    def test_spec_example_states(self):
        self.assertEqual(json.dumps(bench.rule_state(SPEC_CASE, "matched_by")), json.dumps(SPEC_STATE))
        self.assertEqual(json.dumps(bench.rule_state(SPEC_CASE, "labeled")), json.dumps(SPEC_LABELED))
        for state in (SPEC_STATE, SPEC_LABELED):
            self.assertEqual(solve(state), "gate_1")                                 # počtem (2) se se vzorem shoduje brána 1
            self.assertEqual(last_correct(state), "gate_2")                          # brána starého pravidla (barva)
        self.assertEqual(bench.rule_state(SPEC_CASE), bench.rule_state(SPEC_CASE, bench.RULE_FORM))
        with self.assertRaises(ValueError):
            bench.rule_state(SPEC_CASE, "full")

    def test_questions_and_their_alternative_wordings(self):
        criteria = {"gate_1": "Gate 1 (leftmost)", "gate_2": "Gate 2", "gate_3": "Gate 3 (rightmost)"}
        for form, key, text, alt in (("matched_by", "gate", RULE_TEXT, ALT_RULE), ("labeled", "gate_labeled", LABELED_TEXT, ALT_LABELED)):
            with self.subTest(form=form):
                q = bench.rule_question(form)
                self.assertEqual(list(q), [key])
                self.assertEqual(q[key]["type"], "choice")
                self.assertEqual(q[key]["instructions"], text)
                self.assertEqual(json.dumps(q[key]["criteria"]), json.dumps(criteria))
                self.assertEqual(bench.ALT[key], alt)
                self.assertEqual(bench.variant(q, 2)[key]["instructions"], alt)
                self.assertEqual(list(bench.variant(q, 1)[key]["criteria"]), ["gate_3", "gate_2", "gate_1"])
        self.assertEqual(bench.rule_question(), bench.rule_question(bench.RULE_FORM))
        with self.assertRaises(ValueError):
            bench.rule_question("full")
        with self.assertRaises(ValueError):
            bench.rule_question("cards")

    def test_state_stays_under_600_tokens(self):                                  # odhad: 4 znaky JSON na token; matched_by je nejdelší
        longest = max(len(json.dumps(bench.rule_state(c, "matched_by"))) for c in cases(500))
        self.assertLess(longest / 4, 600)


if __name__ == "__main__":
    unittest.main()
