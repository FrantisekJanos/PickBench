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


class Generator(unittest.TestCase):
    def test_spec_example_has_lane_4_open(self):
        signs = [("only", "bus"), ("all_except", "truck"), ("limit_no", ("weight_t", 3.5)), ("limit_no", ("height_m", 3.5))]
        self.assertEqual(bench.sign_state(TRUCK, signs), {"vehicle": {"type": "truck", "weight_t": 7.5, "height_m": 3.2},
                                                          "lane_1": "buses only", "lane_2": "all vehicles except trucks",
                                                          "lane_3": "no vehicles over 3.5 t", "lane_4": "no vehicles higher than 3.5 m"})
        self.assertEqual([i for i, s in enumerate(signs) if bench.allowed(TRUCK, s)], [3])

    def test_exactly_one_lane_is_open_and_the_index_points_to_it(self):
        for lanes in range(2, 9):
            for vehicle, signs, top, cls in bench.sign_cases(500, lanes):
                self.assertEqual(len(signs), lanes)
                self.assertEqual([i for i, s in enumerate(signs) if bench.allowed(vehicle, s)], [top])
                self.assertEqual(signs[top][0], cls)

    def test_classes_are_spread_evenly(self):
        counts = {c: 0 for c in bench.SIGN_CLASSES}
        for _, _, _, cls in bench.sign_cases(600):
            counts[cls] += 1
        for cls, n in counts.items():
            self.assertTrue(60 <= n <= 140, (cls, n))

    def test_vehicles_stay_in_range_and_never_equal_a_limit(self):
        for vehicle, signs, _, _ in bench.sign_cases(500, 8):
            self.assertEqual(list(vehicle), ["type", "weight_t", "height_m"])
            _, weight, height = bench.VEHICLES[vehicle["type"]]
            self.assertTrue(weight[0] <= vehicle["weight_t"] <= weight[1], vehicle)
            self.assertTrue(height[0] <= vehicle["height_m"] <= height[1], vehicle)
            for dim in ("weight_t", "height_m"):
                self.assertEqual(vehicle[dim], round(vehicle[dim], 1))
                self.assertNotIn(vehicle[dim], bench.LIMITS[dim])
            for cls, param in signs:
                if cls.startswith("limit_"):
                    self.assertIn(param[1], bench.LIMITS[param[0]])
                    self.assertNotEqual(vehicle[param[0]], param[1])
                else:
                    self.assertIn(param, bench.VEHICLES)

    def test_limit_classes_always_have_a_closing_limit_sign(self):   # jinak by stačilo vybrat jedinou značku s číslem
        for lanes in range(2, 9):
            for vehicle, signs, top, cls in bench.sign_cases(300, lanes):
                if cls.startswith("limit_"):
                    closing = [s for i, s in enumerate(signs) if i != top and s[0].startswith("limit_")]
                    self.assertTrue(closing, (vehicle, signs))
                    self.assertTrue(all(not bench.allowed(vehicle, s) for s in closing))
                    self.assertTrue(bench.stopped_by_a_limit(vehicle), vehicle)
        self.assertFalse(bench.stopped_by_a_limit({"type": "motorcycle", "weight_t": 0.3, "height_m": 1.2}))
        self.assertTrue(bench.stopped_by_a_limit({"type": "car", "weight_t": 2.1, "height_m": 1.5}))

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.sign_cases(50, 4), bench.sign_cases(50, 4))
        self.assertEqual(bench.sign_cases(50, 4), bench.sign_cases(50, 4, seed=1))
        self.assertNotEqual(bench.sign_cases(50, 4), bench.sign_cases(50, 4, seed=2))

    def test_broken_guarantee_raises_instead_of_fixing(self):
        with mock.patch.object(bench, "allowed", lambda vehicle, sign: True):
            with self.assertRaises(RuntimeError):
                bench.sign_case(random.Random(1), 3)

    def test_state_with_eight_lanes_stays_under_600_tokens(self):     # odhad: 4 znaky JSON na token, ne skutečná tokenizace
        longest = max(len(json.dumps(bench.sign_state(vehicle, signs))) for vehicle, signs, _, _ in bench.sign_cases(500, 8))
        self.assertLess(longest / 4, 600)

    def test_question_and_its_alternative_wording(self):
        q = bench.sign_question(4)
        self.assertEqual(list(q), ["allowed"])
        self.assertEqual(q["allowed"]["type"], "choice")
        self.assertEqual(q["allowed"]["instructions"], "Each lane has a traffic sign. Exactly one lane is open to this vehicle. Choose that lane.")
        self.assertEqual(q["allowed"]["criteria"], {"lane_1": "Lane 1 (leftmost)", "lane_2": "Lane 2", "lane_3": "Lane 3", "lane_4": "Lane 4 (rightmost)"})
        self.assertEqual(bench.ALT["allowed"], "Every lane carries a sign. Only one of them lets this vehicle in. Pick that lane.")
        self.assertEqual(bench.variant(q, 2)["allowed"]["instructions"], bench.ALT["allowed"])
        self.assertEqual(list(bench.variant(q, 1)["allowed"]["criteria"]), ["lane_4", "lane_3", "lane_2", "lane_1"])


class Twin(unittest.TestCase):
    def test_twin_rewords_only_the_open_lane(self):
        for lanes in (2, 3, 8):
            for case in bench.sign_cases(200, lanes):
                vehicle, signs, top, cls = case
                t_vehicle, t_signs, t_top, t_cls = bench.sign_twin(case)
                self.assertEqual((t_vehicle, t_top), (vehicle, top))
                self.assertEqual(t_cls, bench.SIGN_PARTNER[cls])
                self.assertEqual(t_signs[top], (bench.SIGN_PARTNER[cls], signs[top][1]))
                self.assertEqual([i for i, s in enumerate(t_signs) if bench.allowed(vehicle, s)], [top])
                a, b = bench.sign_state(vehicle, signs), bench.sign_state(t_vehicle, t_signs)
                self.assertEqual([k for k in a if a[k] != b[k]], [f"lane_{top + 1}"])

    def test_twin_draws_nothing_and_leaves_the_task_alone(self):
        case = bench.sign_cases(1)[0]
        before = json.dumps(case)
        self.assertEqual(bench.sign_twin(case), bench.sign_twin(case))
        self.assertEqual(json.dumps(case), before)


if __name__ == "__main__":
    unittest.main()
