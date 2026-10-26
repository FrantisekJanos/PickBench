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


if __name__ == "__main__":
    unittest.main()
