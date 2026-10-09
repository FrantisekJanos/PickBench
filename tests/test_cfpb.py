"""Přibalený vzorek stížností CFPB: sada e-mailů, 6 produktů × 50 stížností."""
import json
import re
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import bench
import fetch_datasets as fd
import i18n
import server

PATH = ROOT / "datasets" / "cfpb-complaints-300.json"
CZECH = re.compile(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„“]")


class ShippedCfpb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads(PATH.read_text(encoding="utf-8"))
        cls.ds = bench.validate_dataset(cls.raw)

    def test_six_products_fifty_complaints_each(self):
        keys = [cat["key"] for _, cat in fd.CFPB_CATEGORIES]
        self.assertEqual([c["key"] for c in self.ds["categories"]], keys)
        self.assertEqual(Counter(e["category"] for e in self.ds["emails"]), {k: 50 for k in keys})

    def test_subject_gives_nothing_away(self):
        self.assertEqual({e["subject"] for e in self.ds["emails"]}, {fd.CFPB_SUBJECT})

    def test_bodies_are_distinct_and_fit_the_limit(self):
        bodies = [e["body"] for e in self.ds["emails"]]
        self.assertEqual(len(set(bodies)), 300)
        for body in bodies:
            self.assertTrue(fd.CFPB_MIN_CHARS <= len(body) <= fd.CFPB_MAX_CHARS, len(body))
            self.assertEqual(body, " ".join(body.split()))                                    # žádné konce řádků ani dvojité mezery

    def test_categories_can_be_told_apart_in_the_game(self):
        self.assertEqual(len({c["letter"] for c in self.ds["categories"]}), 6)
        self.assertEqual(len({c["color"] for c in self.ds["categories"]}), 6)
        self.assertTrue(all(c["description"].isascii() for c in self.ds["categories"]))       # texty pro model anglicky

    def test_says_where_it_comes_from(self):
        self.assertIn("CC0", self.raw["license"])
        self.assertIn("consumerfinance.gov", self.raw["source"])
        self.assertIn("CFPB", self.ds["about"])
        for part in ("BEE-spoke-data/consumer-finance-complaints", "Hugging Face", "v únoru 2024", "CC0 podle karty",      # původ textů: kopie třetí strany
                     "úřad sám texty stížností přes své rozhraní dnes nevydává"):
            self.assertIn(part, self.ds["about"], part)
        self.assertIn("BEE-spoke-data/consumer-finance-complaints", self.raw["source"])
        self.assertEqual({k: self.raw["sample"][k] for k in ("n", "per_class", "max_chars")}, {"n": 300, "per_class": 50, "max_chars": fd.CFPB_MAX_CHARS})
        self.assertRegex(self.raw["sample"]["fetched"], r"^\d{4}-\d\d-\d\d$")

    def test_server_will_offer_it(self):
        self.assertTrue(server.DATASET_RE.fullmatch("/datasets/" + PATH.name))

    def test_descriptions_are_what_fetch_datasets_writes_in_either_language(self):
        """Hlavička přibaleného souboru včetně about a about_en je doslova to, co zapíše python fetch_datasets.py cfpb s --per-class 50, česky i s --lang en."""
        products = [product for product, _ in fd.CFPB_CATEGORIES]

        def page(p):                                                          # stránka zdroje: 100 řádků, šest sledovaných produktů dokola
            rows = [{"Product": products[i % 6], "Consumer complaint narrative": f"Complaint number {p * 100 + i}. " + "The company charged me XXXX twice. " * 4}
                    for i in range(100)]
            return json.dumps({"rows": [{"row": row} for row in rows], "num_rows_total": 400}).encode("utf-8")
        pages = {fd.hf_url("rows", fd.CFPB_MIRROR, "has-text", "train", p * 100): page(p) for p in range(4)}
        a = fd.parse_args(["--per-class", "50", "cfpb"])
        self.assertEqual(list(self.raw)[:3], ["name", "about", "about_en"])                   # anglický popis hned za českým
        self.assertEqual(CZECH.findall(self.raw["about_en"]), [])
        with mock.patch.object(fd, "HF_PAUSE", 0):                                            # mezi stránkami se v testu nečeká
            for lang in ("cs", "en"):
                with self.subTest(lang=lang), i18n.process(lang):
                    path, doc = fd.cfpb(lambda url, member=None: pages[url], a, self.fail, self.raw["sample"]["fetched"])
                    self.assertEqual(path, "datasets/" + PATH.name)
                    self.assertEqual(list(doc), list(self.raw))                               # stejná pole ve stejném pořadí
                    for field in ("name", "about", "about_en", "source", "license", "instructions", "sample", "categories"):
                        self.assertEqual(doc[field], self.raw[field], field)


if __name__ == "__main__":
    unittest.main()
