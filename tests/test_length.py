"""bench.py --by-length: přesnost podle délky textu. Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
import contextlib
import io
import os
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
from test_golden import Clock, fake_urlopen
from test_bench import fake_post

LANG_ENV = "PICKBENCH_LANG"


def setUpModule():
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


def dataset():
    """Šest e-mailů o 3 až 8 slovech (předmět jedno slovo): třídy krátké 3–4, střední 5–6, dlouhé 7–8."""
    emails = [{"subject": "Hi", "body": " ".join(["word"] * k), "category": "a" if k % 2 else "b"} for k in range(2, 8)]
    return bench.validate_dataset({"categories": [{"key": "a", "label": "A", "description": "First."},
                                                  {"key": "b", "label": "B", "description": "Second."}], "emails": emails})


def right_from_five_words(ds):
    """Model, který odpoví správně u e-mailů aspoň o pěti slovech, jinak špatně."""
    by_body = {e["body"]: e["category"] for e in ds["emails"]}
    return fake_post(lambda s: by_body[s["body"]] if len(s["subject"].split()) + len(s["body"].split()) >= 5 else
                     ("b" if by_body[s["body"]] == "a" else "a"))


class Tally(unittest.TestCase):
    def test_email_words(self):
        self.assertEqual(bench.email_words({"subject": "Faktura za říjen", "body": "Dobrý den,\nposílám fakturu."}), 7)

    def test_emails_by_length(self):
        ds = dataset()
        with mock.patch.object(bench, "post", right_from_five_words(ds)):
            r = bench.run_emails("m", ds, bench.email_cases(ds), by_length=True)
        self.assertEqual(r["by_length"], {"short": [0, 2], "medium": [2, 2], "long": [2, 2]})
        self.assertEqual(r["length_ranges"], {"short": [3, 4], "medium": [5, 6], "long": [7, 8]})

    def test_repeated_emails_count_every_time(self):
        ds = dataset()
        with mock.patch.object(bench, "post", right_from_five_words(ds)):
            r = bench.run_emails("m", ds, bench.email_cases(ds, 12), by_length=True)
        self.assertEqual(r["by_length"], {"short": [0, 4], "medium": [4, 4], "long": [4, 4]})

    def test_public_by_length(self):
        ds = bench.load_public(ROOT / "tests" / "public-mini.json")
        post = lambda model, payload: ({"answers": {"label": {"choice": "zzz", "confidence": 0.7}}}, 10.0)
        with mock.patch.object(bench, "post", post):
            r = bench.run_public("m", ds, bench.public_cases(ds), by_length=True)
        self.assertEqual({c: n for c, (_, n) in r["by_length"].items()}, {"short": 4, "medium": 4, "long": 4})
        self.assertEqual({c: ok for c, (ok, _) in r["by_length"].items()}, {"short": 0, "medium": 0, "long": 0})
        self.assertEqual(r["length_ranges"], bench.metrics.length_classes([len(it["text"].split()) for it in ds["items"]])["ranges"])

    def test_without_the_option_lengths_are_not_counted(self):
        """Bez --by-length se délky nepočítají vůbec: souhrn klíče nemá a length_tally se nezavolá (kopie položek by ho shodila)."""
        ds, mini = dataset(), bench.load_public(ROOT / "tests" / "public-mini.json")
        post = lambda model, payload: ({"answers": {"label": {"choice": "zzz", "confidence": 0.7}}}, 10.0)
        with mock.patch.object(bench, "length_tally", side_effect=AssertionError("length_tally")):
            with mock.patch.object(bench, "post", right_from_five_words(ds)):
                r = bench.run_emails("m", ds, [dict(e) for e in bench.email_cases(ds)])
            with mock.patch.object(bench, "post", post):
                p = bench.run_public("m", mini, [dict(it) for it in bench.public_cases(mini)])
        self.assertNotIn("by_length", r)
        self.assertNotIn("by_length", p)

    def test_line_in_both_languages(self):
        r = {"by_length": {"short": [5, 7], "medium": [4, 6], "long": [0, 0]}, "length_ranges": {"short": [18, 24], "medium": [24, 26], "long": None}}
        self.assertEqual(printed("cs", lambda: print(bench.length_line(r))),
                         ["přesnost podle délky textu: krátké 18–24 slov 5/7, střední 24–26 slov 4/6, dlouhé — slov 0/0"])
        self.assertEqual(printed("en", lambda: print(bench.length_line(r))),
                         ["accuracy by text length: short 18–24 words 5/7, medium 24–26 words 4/6, long — words 0/0"])


class Cli(unittest.TestCase):
    def setUp(self):
        self.sent = []

    def cli(self, *argv):
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

    def test_lines_under_the_model(self):
        emails = str(ROOT / "datasets" / "cz-firemni-emaily.json")
        out, code = self.cli("20", "--emails", emails, "--by-length", "m1", "m2")
        self.assertIsNone(code)
        self.assertEqual(sum(line.strip().startswith("přesnost podle délky textu: krátké ") for line in out.splitlines()), 2)
        out, _ = self.cli("--public", str(ROOT / "tests" / "public-mini.json"), "--by-length", "m1", "--lang", "en")
        self.assertTrue(out.splitlines()[-1].strip().startswith("accuracy by text length: short "))
        self.assertNotIn("délky textu", self.cli("20", "--emails", emails, "m1")[0])

    def test_option_errors_in_both_languages(self):
        for argv in (["--by-length", "m1"], ["--numbers", "--by-length", "m1"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", "Volba --by-length jde jen s --emails nebo --public."))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", "The --by-length option works only with --emails or --public."))
        self.assertEqual(self.sent, [])

    def test_usage_names_the_option(self):
        for text in (bench.__doc__, bench.USAGE_EN):
            self.assertIn("\n--by-length:  ", text)
            self.assertIn("[--by-length]", text)


if __name__ == "__main__":
    unittest.main()
