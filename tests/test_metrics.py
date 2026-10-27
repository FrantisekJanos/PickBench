import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import metrics

PAIRS = [("a", "a"), ("a", "a"), ("a", "b"), ("b", "b"), ("b", None), ("c", "a")]


class Classification(unittest.TestCase):
    def test_hand_example(self):
        m = metrics.classification(PAIRS)
        self.assertEqual((m["n"], m["invalid"], m["oos"]), (6, 1, None))
        self.assertAlmostEqual(m["acc"], 3 / 6)
        by = {c["label"]: c for c in m["classes"]}
        self.assertEqual({k: v["n"] for k, v in by.items()}, {"a": 3, "b": 2, "c": 1})
        self.assertAlmostEqual(by["a"]["precision"], 2 / 3)        # a zvoleno 3×, z toho 2× správně
        self.assertAlmostEqual(by["a"]["recall"], 2 / 3)
        self.assertAlmostEqual(by["a"]["f1"], 2 / 3)
        self.assertEqual((by["b"]["precision"], by["b"]["recall"], by["b"]["f1"]), (0.5, 0.5, 0.5))
        self.assertEqual((by["c"]["precision"], by["c"]["recall"], by["c"]["f1"]), (0.0, 0.0, 0.0))
        self.assertAlmostEqual(m["macro_f1"], (2 / 3 + 0.5 + 0.0) / 3)

    def test_classes_are_sorted_from_the_worst(self):
        self.assertEqual([c["label"] for c in metrics.classification(PAIRS)["classes"]], ["c", "b", "a"])
        tie = metrics.classification([("b", "x"), ("a", "x"), ("c", "x"), ("c", "x")])       # samé nuly: víc úloh napřed, pak abecedně
        self.assertEqual([c["label"] for c in tie["classes"]], ["c", "a", "b"])

    def test_confusions_from_the_most_frequent(self):
        m = metrics.classification(PAIRS + [("c", "a")])
        self.assertEqual(m["confusions"], [["c", "a", 2], ["a", "b", 1], ["b", None, 1]])

    def test_label_that_is_only_an_answer_is_not_a_class(self):
        m = metrics.classification([("a", "d"), ("a", "a"), ("b", "d")])
        self.assertEqual([c["label"] for c in m["classes"]], ["b", "a"])
        self.assertAlmostEqual(m["macro_f1"], (0.0 + 2 / 3) / 2)                              # a: P 1, R 1/2 → F1 2/3; d se nepočítá

    def test_invalid_answer_is_wrong_and_counted(self):
        m = metrics.classification([("a", None), ("a", None)])
        self.assertEqual((m["acc"], m["macro_f1"], m["invalid"]), (0.0, 0.0, 2))
        self.assertEqual(m["confusions"], [["a", None, 2]])

    def test_empty(self):
        m = metrics.classification([], oos="oos")
        self.assertEqual((m["n"], m["acc"], m["macro_f1"], m["invalid"], m["classes"], m["confusions"]), (0, None, None, 0, [], []))
        self.assertEqual(m["oos"], {"n": 0, "recall": None, "leak": None, "in_n": 0, "in_acc": None})


class OutOfScope(unittest.TestCase):
    PAIRS = [("x", "x"), ("x", "oos"), ("y", "y"), ("y", "x"), ("oos", "oos"), ("oos", "oos"), ("oos", "y"), ("oos", None)]

    def test_recall_leak_and_in_scope_accuracy(self):
        m = metrics.classification(self.PAIRS, oos="oos")
        self.assertEqual(m["oos"], {"n": 4, "recall": 2 / 4, "leak": 1 / 4, "in_n": 4, "in_acc": 2 / 4})
        self.assertAlmostEqual(m["acc"], 4 / 8)                                               # oos je plnohodnotná třída
        self.assertIn("oos", [c["label"] for c in m["classes"]])

    def test_without_oos_label_there_is_no_oos_block(self):
        self.assertIsNone(metrics.classification(self.PAIRS)["oos"])

    def test_oos_given_but_absent_from_the_sample(self):
        m = metrics.classification([("x", "x"), ("y", "oos")], oos="oos")
        self.assertEqual(m["oos"], {"n": 0, "recall": None, "leak": 1 / 2, "in_n": 2, "in_acc": 1 / 2})


class Goodput(unittest.TestCase):
    def test_right_and_on_time(self):
        results = [(True, 5), (True, 500), (False, 5), (True, 100)]
        self.assertEqual(metrics.goodput(results, 100), 2 / 4)                                # přesně na limitu se počítá
        self.assertEqual(metrics.goodput(results, 1000), 3 / 4)
        self.assertEqual(metrics.goodput(results, 1), 0.0)

    def test_no_deadline_or_no_data_is_none(self):
        self.assertIsNone(metrics.goodput([(True, 5)], None))
        self.assertIsNone(metrics.goodput([], 100))


class Calibration(unittest.TestCase):               # Brierovo skóre a práh jistoty (bench.py --defer, panel Kalibrace ve stránce)
    PAIRS = [(0.9, True), (0.8, False), (0.6, True), (0.95, True)]

    def test_brier(self):
        self.assertAlmostEqual(metrics.brier(self.PAIRS), (0.1 ** 2 + 0.8 ** 2 + 0.4 ** 2 + 0.05 ** 2) / 4)
        self.assertEqual(metrics.brier([(1.0, True), (0.0, False)]), 0.0)
        self.assertEqual(metrics.brier([(1.0, False)]), 1.0)
        self.assertIsNone(metrics.brier([]))

    def test_coverage_above_the_threshold(self):
        c = metrics.coverage(self.PAIRS, 5, 0.8)                      # pátá úloha jistotu nemá: jde pod práh
        self.assertEqual((c["threshold"], c["n"], c["covered"], c["handed"]), (0.8, 5, 3, 2))   # jistota rovná prahu je nad prahem
        self.assertAlmostEqual(c["coverage"], 3 / 5)
        self.assertAlmostEqual(c["acc"], 2 / 3)                       # 0.9 správně, 0.8 špatně, 0.95 správně
        high = metrics.coverage(self.PAIRS, 4, 0.96)
        self.assertEqual((high["covered"], high["coverage"], high["acc"], high["handed"]), (0, 0.0, None, 4))
        self.assertEqual(metrics.coverage([], 0, 0.5), {"threshold": 0.5, "n": 0, "covered": 0, "coverage": None, "acc": None, "handed": 0})

    def test_threshold_one_keeps_only_full_confidence(self):
        self.assertEqual(metrics.coverage([(1.0, True), (0.999, True)], 2, 1.0)["covered"], 1)


class Lengths(unittest.TestCase):                   # třídy délky textu (bench.py --by-length, řádek ve stránce)
    def test_thirds_by_rank(self):
        self.assertEqual(metrics.LENGTH_CLASSES, ("short", "medium", "long"))
        got = metrics.length_classes([5, 1, 9, 3, 7, 2])               # pořadí podle délky: 1, 2 | 3, 5 | 7, 9
        self.assertEqual(got["classes"], ["medium", "short", "long", "medium", "long", "short"])
        self.assertEqual(got["ranges"], {"short": [1, 2], "medium": [3, 5], "long": [7, 9]})

    def test_ties_go_by_the_order_in_the_dataset(self):
        got = metrics.length_classes([4, 4, 4, 4])                     # hranice 4 // 3 = 1 a 8 // 3 = 2
        self.assertEqual(got["classes"], ["short", "medium", "long", "long"])
        self.assertEqual(got["ranges"], {"short": [4, 4], "medium": [4, 4], "long": [4, 4]})

    def test_small_sets_leave_classes_empty(self):
        self.assertEqual(metrics.length_classes([]), {"classes": [], "ranges": {"short": None, "medium": None, "long": None}})
        self.assertEqual(metrics.length_classes([3]), {"classes": ["long"], "ranges": {"short": None, "medium": None, "long": [3, 3]}})
        self.assertEqual(metrics.length_classes([3, 8])["classes"], ["medium", "long"])


class Barrier(unittest.TestCase):                   # AUROC a nejlepší práh otázky noul; případy z tests/test_barrier.py (Quality)
    def test_auroc(self):
        self.assertEqual(metrics.auroc([0.9, 0.8, 0.2, 0.1], [True, True, False, False]), 1)
        self.assertEqual(metrics.auroc([0.1, 0.2, 0.8, 0.9], [True, True, False, False]), 0)
        self.assertEqual(metrics.auroc([0.5, 0.5, 0.5], [True, False, True]), 0.5)                 # shody jako polovina
        self.assertEqual(metrics.auroc([0.9, 0.4, 0.4, 0.1], [True, True, False, False]), 0.875)
        self.assertEqual(metrics.auroc([0.9, None, 0.1, None], [True, True, False, False]), 1)     # neplatné se vynechají
        self.assertIsNone(metrics.auroc([0.9, 0.8], [True, True]))                                 # chybí úlohy „stůj“
        self.assertIsNone(metrics.auroc([0.9, None], [True, False]))                               # jediná „stůj“ je neplatná
        self.assertIsNone(metrics.auroc([], []))

    def test_best_threshold(self):
        self.assertEqual(metrics.best_threshold([0.9, 0.6, 0.4, 0.2], [True, True, True, False]), (0.4, 1))
        self.assertEqual(metrics.best_threshold([0.7, 0.3], [True, False]), (0.5, 1))                  # shoda: nejblíž 0,5
        self.assertEqual(metrics.best_threshold([0.8, 0.6], [True, False]), (0.8, 1))
        self.assertIsNone(metrics.best_threshold([], []))                                     # bez úloh: jako bestThreshold ve stránce (null)
        self.assertEqual(metrics.best_threshold([0.25, 0.75, 0.625, 0.125], [True, True, False, False]), (0.25, 0.75))   # stejně daleko: nižší
        self.assertEqual(metrics.best_threshold([None, 0.9], [True, True]), (0.5, 0.5))                 # neplatná je chyba; jen úlohy jeď
        self.assertEqual(metrics.best_threshold([None, None], [True, False]), (0.5, 0))
        self.assertEqual(metrics.best_threshold([0.8] * 4, [True, True, True, False]), (0.5, 0.5))      # vždy jeď: vyváženě 50 %
        self.assertEqual(metrics.best_threshold([0.9, 0.3, 0.3, 0.6], [True, True, True, False]), (0.9, 2 / 3))

    def test_bench_uses_these_functions(self):      # bench.py je po přesunu jen přejmenovává; stejný výpočet má stránka (tests/test_parity.py)
        import bench
        self.assertIs(bench.barrier_auroc, metrics.auroc)
        self.assertIs(bench.barrier_threshold, metrics.best_threshold)


if __name__ == "__main__":
    unittest.main()
