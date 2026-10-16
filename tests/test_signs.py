"""Scénář zákazy a výjimky (bench.py --signs, --pairs). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
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
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import bench
import i18n
from test_bench import fake_post, obedient, recording
from test_golden import Clock, fake_urlopen

LANG_ENV = "PICKBENCH_LANG"
TRUCK = {"type": "truck", "weight_t": 7.5, "height_m": 3.2}       # příklad ze specu


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def bare(state):
    """Stav bez schovaného pokynu, jako klíč slovníku."""
    return json.dumps({k: v for k, v in state.items() if k != "note"}, sort_keys=True)


def oracle(cases, lanes, twins=True):
    """Model, který zná správnou odpověď původních úloh (a s twins=True i dvojčat); jinak odpoví mimo nabídku."""
    keys, truth = bench.lane_keys(lanes), {}
    for case in cases:
        for vehicle, signs, top, _ in [case] + ([bench.sign_twin(case)] if twins else []):
            truth[bare(bench.sign_state(vehicle, signs))] = keys[top]
    return lambda state: truth.get(bare(state), "nonsense")


class Allowed(unittest.TestCase):
    def test_hand_checked_table(self):                                 # ověřeno ručně, nezávisle na generátoru
        car = {"type": "car", "weight_t": 1.5, "height_m": 1.6}
        light = {"type": "tractor", "weight_t": 3.4, "height_m": 2.5}
        heavy = {"type": "tractor", "weight_t": 3.6, "height_m": 2.9}
        table = [
            (TRUCK, ("only", "truck"), True), (TRUCK, ("only", "bus"), False),
            (car, ("except_only", "car"), True), (car, ("except_only", "motorcycle"), False),
            (TRUCK, ("no", "bus"), True), (TRUCK, ("no", "truck"), False),
            (car, ("all_except", "truck"), True), (car, ("all_except", "car"), False),
            (light, ("limit_no", ("weight_t", 3.5)), True), (heavy, ("limit_no", ("weight_t", 3.5)), False),      # 3.4 t pod mezí 3.5 t smí, 3.6 t ne
            (light, ("limit_up_to", ("weight_t", 3.5)), True), (heavy, ("limit_up_to", ("weight_t", 3.5)), False),
            (TRUCK, ("limit_no", ("height_m", 3.5)), True), (TRUCK, ("limit_no", ("height_m", 3)), False),
            (heavy, ("limit_up_to", ("height_m", 3)), True), (TRUCK, ("limit_up_to", ("height_m", 2.8)), False),
            (TRUCK, ("limit_no", ("weight_t", 3.5)), False),                                                     # 3.5 t, ne 3.5 m: jednotka rozhoduje
        ]
        for vehicle, sign, want in table:
            with self.subTest(vehicle=vehicle, sign=sign):
                self.assertIs(bench.allowed(vehicle, sign), want)

    def test_sign_texts(self):
        texts = [bench.sign_text(s) for s in (("only", "bus"), ("except_only", "motorcycle"), ("no", "tractor"), ("all_except", "truck"),
                                              ("limit_no", ("weight_t", 3.5)), ("limit_no", ("height_m", 4)),
                                              ("limit_up_to", ("weight_t", 12)), ("limit_up_to", ("height_m", 2.8)))]
        self.assertEqual(texts, ["buses only", "no entry except motorcycles", "no tractors", "all vehicles except trucks",
                                 "no vehicles over 3.5 t", "no vehicles higher than 4 m", "vehicles up to 12 t only", "vehicles up to 2.8 m high only"])
        self.assertEqual([bench.VEHICLES[k][0] for k in bench.VEHICLES], ["motorcycles", "cars", "tractors", "trucks", "buses"])

    def test_partners_share_the_condition(self):
        self.assertEqual(set(bench.SIGN_PARTNER), set(bench.SIGN_CLASSES))
        for cls, partner in bench.SIGN_PARTNER.items():
            self.assertEqual(bench.SIGN_PARTNER[partner], cls)
            param = ("weight_t", 5) if cls.startswith("limit_") else "car"
            for vehicle in (TRUCK, {"type": "car", "weight_t": 1.5, "height_m": 1.6}):
                self.assertEqual(bench.allowed(vehicle, (cls, param)), bench.allowed(vehicle, (partner, param)), (cls, vehicle))
        self.assertEqual(sorted(c for pair in bench.SIGN_PAIRS for c in pair), sorted(bench.SIGN_CLASSES))


if __name__ == "__main__":
    unittest.main()
