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


if __name__ == "__main__":
    unittest.main()
