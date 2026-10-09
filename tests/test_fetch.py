"""Stahování veřejných sad: bez sítě, nad podstrčenými odpověďmi stejného tvaru, jaký vrací zdroj."""
import ast
import http.client
import io
import json
import os
import random
import re
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from collections import Counter
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
import bench
import fetch_datasets as fd
import i18n
import server

MASSIVE_SAMPLE = (Path(__file__).parent / "massive-sample.jsonl").read_bytes()      # vymyšlené řádky stejného tvaru jako 1.1/data/en-US.jsonl
LANG_ENV = "PICKBENCH_LANG"
CZECH = re.compile(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„“]")


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní a po něm vrátí."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(i18n.ENV, None)


def args(*argv):
    return fd.parse_args([*argv, "x"])


def massive_tar(sample=MASSIVE_SAMPLE, name="1.1/data/en-US.jsonl"):
    """Archiv .tar.gz stejného tvaru jako zdroj: průvodní soubor, jiný jazyk před en-US a velký nestlačitelný soubor za ním."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for member, body in (("1.1/NOTICE.md", b"# NOTICE\n"), ("1.1/data/af-ZA.jsonl", b'{"id": "0", "locale": "af-ZA"}\n'),
                             (name, sample), ("1.1/data/zu-ZA.jsonl", random.Random(1).randbytes(300000))):
            info = tarfile.TarInfo(member)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def banking_csv(classes=77, per=4):
    lines = ["text,category"]
    for c in range(classes):
        for i in range(per):
            lines.append(f'"Question {i} about topic {c}, with a comma",{"Topic" if c == 0 else "topic"}_{c:02d}{"?" if c == 1 else ""}')
    return ("\n".join(lines) + "\n").encode("utf-8")


def clinc_json(intents=150, per=2, oos=30):
    test = [[f"say {i} for intent {c}", f"intent_{c:03d}"] for c in range(intents) for i in range(per)]
    return json.dumps({"train": [], "val": [], "oos_train": [], "oos_val": [], "test": test,
                       "oos_test": [[f"unrelated {i}", "oos"] for i in range(oos)]}).encode("utf-8")


class FakeGet:
    """Náhrada http_get: adresa → bajty, nebo výjimka. Pamatuje si, na co se kdo ptal (a který soubor z archivu chtěl)."""

    def __init__(self, pages):
        self.pages, self.urls, self.members = pages, [], []

    def __call__(self, url, member=None):
        self.urls.append(url)
        if member is not None:
            self.members.append(member)
        page = self.pages.get(url)
        if page is None:
            raise fd.FetchError(f"Stažení se nepodařilo (HTTP 404): {url}")
        if isinstance(page, Exception):
            raise page
        return page


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class FakeStream(FakeResponse):
    """Odpověď, kterou jde číst jen proudem: po kouscích a bez návratu zpět. Počítá, kolik bajtů si čtenář vzal."""

    def __init__(self, body):
        self.left, self.taken = io.BytesIO(body), 0

    def read(self, n=-1):
        chunk = self.left.read(n)
        self.taken += len(chunk)
        return chunk


class HttpGet(unittest.TestCase):
    def opener(self, answers):
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, request.get_header("User-agent"), timeout))
            answer = answers[min(len(calls) - 1, len(answers) - 1)]
            if isinstance(answer, Exception):
                raise answer
            return answer if isinstance(answer, FakeResponse) else FakeResponse(answer)
        return opener, calls

    @staticmethod
    def status(code):
        return urllib.error.HTTPError("http://x", code, "x", {}, io.BytesIO(b""))

    def test_returns_the_body_and_identifies_itself(self):
        opener, calls = self.opener([b"ahoj"])
        self.assertEqual(fd.http_get("http://x/a", opener=opener, sleep=self.fail), b"ahoj")
        self.assertEqual(calls, [("http://x/a", fd.AGENT, 120)])

    def test_retries_a_busy_server_with_growing_pauses(self):
        opener, calls = self.opener([self.status(503), urllib.error.URLError("timed out"), TimeoutError("read"),
                                     http.client.IncompleteRead(b"pul"), b"ok"])      # i spojení utnuté uprostřed těla
        naps = []
        self.assertEqual(fd.http_get("http://x/a", pause=2, opener=opener, sleep=naps.append), b"ok")
        self.assertEqual((len(calls), naps), (5, [2, 4, 6, 8]))

    def test_gives_up_with_one_sentence(self):
        opener, calls = self.opener([self.status(500)])
        naps = []
        with self.assertRaises(fd.FetchError) as e:
            fd.http_get("http://x/a", tries=3, opener=opener, sleep=naps.append)
        self.assertEqual(str(e.exception), "Stažení se nepodařilo (HTTP 500): http://x/a")
        self.assertEqual((len(calls), naps), (3, [5, 10]))

    def test_too_many_requests_waits_minutes_not_seconds(self):
        opener, calls = self.opener([self.status(429), self.status(429), b"ok"])
        naps = []
        self.assertEqual(fd.http_get("http://x/a", opener=opener, sleep=naps.append), b"ok")
        self.assertEqual(naps, [60, 120])

    def test_a_missing_page_is_not_retried(self):
        opener, calls = self.opener([self.status(404)])
        with self.assertRaises(fd.FetchError) as e:
            fd.http_get("http://x/a", opener=opener, sleep=self.fail)
        self.assertEqual((len(calls), str(e.exception)), (1, "Stažení se nepodařilo (HTTP 404): http://x/a"))

    def test_member_is_read_from_the_stream_and_reading_stops_there(self):
        archive = massive_tar()
        stream = FakeStream(archive)
        opener, calls = self.opener([stream])
        self.assertEqual(fd.http_get("http://x/a.tar.gz", opener=opener, sleep=self.fail, member="/data/en-US.jsonl"), MASSIVE_SAMPLE)
        self.assertLess(stream.taken, len(archive) // 2)                                      # velký soubor za hledaným se už nestahuje

    def test_archive_without_the_member_is_one_sentence_and_is_not_retried(self):
        opener, calls = self.opener([FakeStream(massive_tar(name="1.1/data/en-GB.jsonl"))])
        with self.assertRaises(fd.FetchError) as e:
            fd.http_get("http://x/a.tar.gz", opener=opener, sleep=self.fail, member="/data/en-US.jsonl")
        self.assertEqual((len(calls), str(e.exception)),
                         (1, "V archivu není soubor končící /data/en-US.jsonl — zdroj změnil tvar: http://x/a.tar.gz"))

    def test_cut_or_broken_archive_is_tried_again(self):
        archive = massive_tar()
        opener, calls = self.opener([FakeStream(archive[:300]), FakeStream(b"<html>toto neni archiv</html>"), FakeStream(archive)])
        naps = []
        self.assertEqual(fd.http_get("http://x/a.tar.gz", pause=2, opener=opener, sleep=naps.append, member="/data/en-US.jsonl"), MASSIVE_SAMPLE)
        self.assertEqual((len(calls), naps), (3, [2, 4]))                                     # spojení utnuté uprostřed archivu i odpověď, která archiv není
        opener, calls = self.opener([FakeStream(archive[:300]), FakeStream(archive[:300])])
        with self.assertRaises(fd.FetchError) as e:
            fd.http_get("http://x/a.tar.gz", tries=2, opener=opener, sleep=naps.append, member="/data/en-US.jsonl")
        self.assertRegex(str(e.exception), r"^Stažení se nepodařilo \(.+\): http://x/a\.tar\.gz$")


class Stratified(unittest.TestCase):
    ITEMS = [{"label": label, "i": i} for label, k in (("a", 60), ("b", 30), ("c", 9), ("d", 1)) for i in range(k)]

    def pick(self, n, seed=1):
        return fd.stratified(self.ITEMS, lambda it: it["label"], n, seed)

    def test_each_layer_gets_its_share(self):
        got = self.pick(10)
        self.assertEqual(Counter(it["label"] for it in got), {"a": 6, "b": 3, "c": 1})
        self.assertEqual(len({(it["label"], it["i"]) for it in got}), 10)                     # žádná položka dvakrát

    def test_remainders_go_to_the_largest_fractions(self):
        self.assertEqual(Counter(it["label"] for it in self.pick(25)), {"a": 15, "b": 8, "c": 2})      # podíly 15 / 7,5 / 2,25 / 0,25 → zbylý kus dostane b
        for n in (1, 33, 50, 99):
            self.assertEqual(len(self.pick(n)), n)

    def test_same_seed_same_sample_other_seed_other_sample(self):
        self.assertEqual(self.pick(20), self.pick(20))
        self.assertNotEqual(self.pick(20), self.pick(20, seed=2))

    def test_asking_for_everything_returns_everything(self):
        self.assertEqual(self.pick(100), self.ITEMS)
        self.assertEqual(self.pick(5000), self.ITEMS)

    def test_equal_layers_split_evenly(self):
        items = [{"label": f"k{c}", "i": i} for c in range(7) for i in range(10)]
        counts = Counter(it["label"] for it in fd.stratified(items, lambda it: it["label"], 17, 3))
        self.assertEqual(sorted(counts.values()), [2, 2, 2, 2, 3, 3, 3])


class Banking(unittest.TestCase):
    def fetch(self, body=None, *argv):
        return fd.banking77(FakeGet({fd.BANKING_URL: banking_csv() if body is None else body}), args(*argv), self.fail, "2026-10-06")

    def test_sample_is_a_valid_public_set(self):
        path, doc = self.fetch(None, "--n", "154")
        self.assertEqual(path, "datasets/public/banking77.json")
        ds = bench.validate_public(doc)
        self.assertEqual((len(ds["labels"]), len(ds["items"]), ds["oos"]), (77, 154, None))
        self.assertEqual(set(Counter(it["label"] for it in ds["items"]).values()), {2})
        self.assertEqual(doc["sample"], {"n": 154, "of": 308, "seed": 1, "full": False, "fetched": "2026-10-06"})
        self.assertEqual(doc["name"], "BANKING77 (vzorek 154 z 308, semínko 1)")
        self.assertIn("CC-BY-4.0", doc["license"])
        self.assertIn("PolyAI-LDN/task-specific-datasets", doc["source"])
        self.assertIn({"key": "topic_00"}, doc["labels"])                                     # jméno „Topic_00“ se od klíče liší jen velkým písmenem: bez popisu
        self.assertIn({"key": "topic_01", "description": "topic 01?"}, doc["labels"])              # jméno nese víc než klíč (otazník): zůstává jako popis
        self.assertEqual(sum("description" in label for label in doc["labels"]), 1)              # štítek jde do promptu jednou
        self.assertEqual([fd.option(name) for name in ("card_arrival", "Billing Error", "Fees & Charges", "Spokojený")],
                         [{"key": "card_arrival"}, {"key": "billing_error"}, {"key": "fees_charges", "description": "Fees & Charges"},
                          {"key": "spokojen", "description": "Spokojený"}])
        self.assertEqual(fd.slug(" Fees & Charges! "), "fees_charges")
        self.assertEqual(doc["items"][0]["text"][:9], "Question ")

    def test_full_keeps_everything_under_another_name(self):
        path, doc = self.fetch(None, "--full")
        self.assertEqual((path, len(doc["items"]), doc["sample"]["full"]), ("datasets/public/banking77-full.json", 308, True))
        self.assertEqual(doc["name"], "BANKING77 (celá testovací část, 308 úloh)")

    def test_seed_changes_the_sample(self):
        a, b = self.fetch(None, "--n", "100")[1], self.fetch(None, "--n", "100", "--seed", "2")[1]
        self.assertNotEqual(a["items"], b["items"])
        self.assertEqual(self.fetch(None, "--n", "100")[1], a)

    def test_changed_columns_are_one_sentence(self):
        with self.assertRaises(fd.FetchError) as e:
            self.fetch(b"utterance,intent\nhi,greet\n")
        self.assertEqual(str(e.exception), "BANKING77: soubor nemá sloupce text a category — zdroj změnil tvar.")

    def test_body_that_is_not_utf8_is_one_sentence(self):
        with self.assertRaises(fd.FetchError) as e:
            self.fetch("text,category\nčau,pozdrav\n".encode("cp1250"))
        self.assertEqual(str(e.exception), "BANKING77: soubor není text v UTF-8 — zdroj změnil tvar.")

    def test_row_without_the_second_column_is_one_sentence(self):
        with self.assertRaises(fd.FetchError) as e:
            self.fetch(b"text,category\nhi\n")
        self.assertEqual(str(e.exception), "BANKING77: řádek nemá text a category jako text — zdroj změnil tvar.")

    def test_body_that_csv_cannot_read_is_one_sentence(self):
        with self.assertRaises(fd.FetchError) as e:
            self.fetch(b'text,category\n"' + b"x" * 140000 + b'",topic\n')                 # pole delší, než modul csv dovolí → csv.Error, ne ValueError
        self.assertEqual(str(e.exception), "BANKING77: soubor nejde přečíst jako CSV — zdroj změnil tvar.")

    def test_unexpected_number_of_intents_is_one_sentence(self):
        with self.assertRaises(fd.FetchError) as e:
            self.fetch(banking_csv(classes=76))
        self.assertEqual(str(e.exception), "BANKING77: čekáno 77 záměrů, zdroj jich má 76.")


class Clinc(unittest.TestCase):
    def fetch(self, body=None, *argv):
        return fd.clinc150(FakeGet({fd.CLINC_URL: clinc_json() if body is None else body}), args(*argv), self.fail, "2026-10-06")

    def test_sample_has_out_of_scope_as_a_full_label(self):
        path, doc = self.fetch(None, "--n", "110")
        self.assertEqual(path, "datasets/public/clinc150.json")
        ds = bench.validate_public(doc)
        self.assertEqual((len(ds["labels"]), ds["labels"][-1]["key"], ds["oos"], len(ds["items"])), (151, "oos", "oos", 110))
        counts = Counter(it["label"] for it in ds["items"])
        self.assertEqual(counts["oos"], 10)                                                   # 30 z 330 → 10 ze 110
        self.assertEqual(doc["sample"]["of"], 330)
        self.assertIn("CC-BY-3.0", doc["license"])
        self.assertIn("out of scope", ds["labels"][-1]["description"])
        self.assertEqual(ds["labels"][0], {"key": "intent_000", "description": None})          # záměry bez popisu; popis má jen oos
        self.assertEqual(sum("description" in label for label in doc["labels"]), 1)

    def test_full(self):
        path, doc = self.fetch(None, "--full")
        self.assertEqual((path, len(doc["items"])), ("datasets/public/clinc150-full.json", 330))

    def test_broken_answers_are_one_sentence(self):
        for body, message in ((b"<html>", "CLINC150: odpověď není JSON — zdroj změnil tvar."),
                              (b'{"test": []}', "CLINC150: soubor nemá seznamy test a oos_test — zdroj změnil tvar."),
                              (clinc_json(intents=149), "CLINC150: čekáno 150 záměrů a oos, zdroj má 149 záměrů."),
                              (json.dumps({"test": [["a", "x"]], "oos_test": [["b"]]}).encode(), "CLINC150: řádek nemá tvar [text, záměr] — zdroj změnil tvar.")):
            with self.subTest(message=message):
                with self.assertRaises(fd.FetchError) as e:
                    self.fetch(body)
                self.assertEqual(str(e.exception), message)


class Massive(unittest.TestCase):
    def fetch(self, body=None, *argv):
        self.get = FakeGet({fd.MASSIVE_URL: MASSIVE_SAMPLE if body is None else body})
        return fd.massive(self.get, args(*argv), self.fail, "2026-10-06")

    def test_sample_has_eighteen_bare_scenario_keys(self):
        path, doc = self.fetch(None, "--n", "18")
        self.assertEqual(path, "datasets/public/massive-en-us.json")
        self.assertEqual((self.get.urls, self.get.members), ([fd.MASSIVE_URL], ["/data/en-US.jsonl"]))      # z archivu jen soubor en-US
        ds = bench.validate_public(doc)
        self.assertEqual(doc["labels"], [{"key": name} for name in fd.MASSIVE_SCENARIOS])        # bez popisu: štítek jde do promptu jednou
        self.assertEqual((len(ds["labels"]), len(ds["items"]), ds["oos"]), (18, 18, None))
        self.assertEqual(set(Counter(it["label"] for it in ds["items"]).values()), {1})
        self.assertEqual(doc["sample"], {"n": 18, "of": 36, "seed": 1, "full": False, "fetched": "2026-10-06"})      # jen část test, bez prázdné věty
        self.assertEqual(doc["name"], "MASSIVE en-US (vzorek 18 z 36, semínko 1)")
        self.assertIn("CC BY 4.0", doc["license"])
        self.assertIn("alexa/massive", doc["source"])
        self.assertIn("FitzGerald", doc["about"])
        self.assertIn("SLURP", doc["about"])

    def test_full_keeps_the_whole_test_partition_and_nothing_else(self):
        path, doc = self.fetch(None, "--full")
        self.assertEqual((path, len(doc["items"]), doc["sample"]["full"]), ("datasets/public/massive-en-us-full.json", 36, True))
        texts = [it["text"] for it in doc["items"]]
        self.assertIn("wake me up at six tomorrow", texts)
        self.assertFalse([t for t in texts if t.startswith("training row") or t.startswith("dev row") or t != t.strip() or not t])

    def test_broken_answers_are_one_sentence(self):
        def row(**change):
            return (json.dumps({"id": "1", "locale": "en-US", "partition": "test", "scenario": "alarm", "utt": "wake me", **change}) + "\n").encode("utf-8")
        shape = "MASSIVE: řádky části test nemají pole utt a scenario jako text — zdroj změnil tvar."
        for body, message in ((b"<html>", "MASSIVE: soubor en-US.jsonl není JSON Lines — zdroj změnil tvar."),
                              (b"[1]\n", "MASSIVE: soubor en-US.jsonl není JSON Lines — zdroj změnil tvar."),
                              (row(utt=None), shape), (row(scenario=3), shape), (row(partition="train"), shape),
                              (MASSIVE_SAMPLE + row(scenario="shopping"), "MASSIVE: čekáno 18 známých scénářů, zdroj se liší v: shopping."),
                              (b"\n".join(line for line in MASSIVE_SAMPLE.split(b"\n") if b'"weather"' not in line),
                               "MASSIVE: čekáno 18 známých scénářů, zdroj se liší v: weather.")):
            with self.subTest(message=message, body=body[:20]):
                with self.assertRaises(fd.FetchError) as e:
                    self.fetch(body)
                self.assertEqual(str(e.exception), message)


class WriteJson(unittest.TestCase):
    def test_writes_utf8_and_creates_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "datasets" / "public" / "a.json"
            fd.write_json(path, {"name": "žluťoučký"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"name": "žluťoučký"})
            self.assertIn("žluťoučký", path.read_text(encoding="utf-8"))
            self.assertEqual([p.name for p in path.parent.iterdir()], ["a.json"])

    def test_failure_leaves_nothing_behind_and_keeps_the_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.json"
            fd.write_json(path, {"v": 1})
            with self.assertRaises(TypeError):
                fd.write_json(path, {"v": object()})
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["a.json"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"v": 1})


class Main(unittest.TestCase):
    def run_main(self, argv, pages):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        out, code = [], 0
        try:
            fd.main(argv, get=FakeGet(pages), out=out.append, root=Path(self.tmp.name), today="2026-10-06")
        except SystemExit as e:
            code = e.code
        files = sorted(p.relative_to(self.tmp.name).as_posix() for p in Path(self.tmp.name).rglob("*") if p.is_file())
        return out, code, files

    def test_two_sets_are_written_where_the_server_looks(self):
        out, code, files = self.run_main(["--n", "110", "banking77", "clinc150"], {fd.BANKING_URL: banking_csv(), fd.CLINC_URL: clinc_json()})
        self.assertEqual(code, 0)
        self.assertEqual(files, ["datasets/public/banking77.json", "datasets/public/clinc150.json"])
        self.assertEqual(out, ["banking77: uloženo 110 úloh do datasets/public/banking77.json",
                               "clinc150: uloženo 110 úloh do datasets/public/clinc150.json"])
        for name in files:
            self.assertTrue(server.DATASET_RE.fullmatch("/datasets/" + Path(name).name), name)
            bench.load_public(Path(self.tmp.name) / name)

    def test_massive_is_written_next_to_the_others(self):
        out, code, files = self.run_main(["--n", "18", "massive"], {fd.MASSIVE_URL: MASSIVE_SAMPLE})
        self.assertEqual((code, files, out), (0, ["datasets/public/massive-en-us.json"], ["massive: uloženo 18 úloh do datasets/public/massive-en-us.json"]))
        self.assertTrue(server.DATASET_RE.fullmatch("/datasets/massive-en-us.json"))
        bench.load_public(Path(self.tmp.name) / "datasets" / "public" / "massive-en-us.json")

    def test_one_failure_does_not_stop_the_others_and_exits_nonzero(self):
        out, code, files = self.run_main(["banking77", "clinc150"], {fd.CLINC_URL: clinc_json()})
        self.assertEqual(code, 1)
        self.assertIn("datasets/public/clinc150.json", files)
        self.assertNotIn("datasets/public/banking77.json", files)
        self.assertEqual(out[0], f"banking77: Stažení se nepodařilo (HTTP 404): {fd.BANKING_URL}")
        self.assertFalse([f for f in files if f.endswith(".part")])

    def test_unknown_set_or_no_set_prints_usage(self):
        for argv in (["nic"], [], ["--n", "0", "banking77"]):
            with self.subTest(argv=argv):
                out, code, files = self.run_main(argv, {})
                self.assertEqual((code, files, out), (fd.__doc__, [], []))

    def test_all_means_every_known_set(self):
        self.assertEqual(fd.parse_args(["all"]).names, list(fd.SETS))
        a = fd.parse_args(["banking77", "--n", "50", "--seed", "7", "--full"])
        self.assertEqual((a.names, a.n, a.seed, a.full, a.per_class), (["banking77"], 50, 7, True, 50))


fd.HF_PAUSE = 0                                      # v testech se mezi stránkami nečeká


def hf_body(rows, total):
    return json.dumps({"features": [], "rows": [{"row_idx": i, "row": r, "truncated_cells": []} for i, r in enumerate(rows)],
                       "num_rows_total": total, "num_rows_per_page": 100, "partial": False}).encode("utf-8")


def horizon_row(i, **change):
    row = {"text": f"Aplikace mi už po{i}. spadla při placení.", "lang": "Czech", "task": "app store review issue type, in the domain of banking",
           "candidate_labels": ["Billing Error", "Login Issues", "App Crash"], "gold_labels": ["App Crash"],
           "hypothesis_template": "The user is complaining about {}.", "kind": "short", "text_origin": "qwen-generated"}
    row.update(change)
    return row


class HuggingFace(unittest.TestCase):
    def test_url(self):
        self.assertEqual(fd.hf_url("rows", "a/b", "default", "train", 200),
                         "https://datasets-server.huggingface.co/rows?dataset=a%2Fb&config=default&split=train&offset=200&length=100")
        self.assertTrue(fd.hf_url("filter", "a/b", "c", "s", 0, "\"lang\"='Czech'").endswith("&length=100&where=%22lang%22%3D%27Czech%27"))

    def test_page_returns_rows_and_total(self):
        url = fd.hf_url("rows", "a/b", "c", "s", 0)
        self.assertEqual(fd.hf_page(FakeGet({url: hf_body([{"x": 1}, {"x": 2}], 7)}), "rows", "a/b", "c", "s", 0), ([{"x": 1}, {"x": 2}], 7))

    def test_changed_shape_is_one_sentence(self):
        url = fd.hf_url("rows", "a/b", "c", "s", 0)
        for body in (b"<html>", b"[]", b'{"rows": []}', b'{"rows": [{"nic": 1}], "num_rows_total": 1}', b'{"rows": {}, "num_rows_total": 1}'):
            with self.subTest(body=body):
                with self.assertRaises(fd.FetchError) as e:
                    fd.hf_page(FakeGet({url: body}), "rows", "a/b", "c", "s", 0)
                self.assertRegex(str(e.exception), r"^a/b: .*zdroj změnil tvar\.$")


class Horizon(unittest.TestCase):
    WHERE = "\"lang\"='Czech'"

    def url(self, endpoint, offset):
        return fd.hf_url(endpoint, fd.HORIZON, "default", "train", offset, self.WHERE if endpoint == "filter" else None)

    def test_keeps_only_czech_support_rows_with_two_to_ten_labels(self):
        rows = [horizon_row(1),
                horizon_row(2, lang="German"),
                horizon_row(3, gold_labels=["App Crash", "Login Issues"]),
                horizon_row(4, candidate_labels=[f"L{i}" for i in range(11)], gold_labels=["L1"]),      # jedenáct štítků je přes mez
                horizon_row(5, candidate_labels=["App Crash"]),
                horizon_row(6, task="news headline section"),
                horizon_row(7, text="The app crashed again during payment."),
                horizon_row(8, kind="passage", text_origin="fineweb"),
                horizon_row(9, gold_labels=["Refund"]),
                horizon_row(10, candidate_labels=["App crash", "App Crash"]),
                horizon_row(11, task="urgency", kind="tax_short", candidate_labels=["low", "medium", "high", "critical"], gold_labels=["high"]),
                horizon_row(12, task="customer message topic", kind="tax_short", gold_labels=["Topic 7"],            # deset štítků se ještě bere
                            candidate_labels=["Legit: Bill Inquiry"] + [f"Topic {i}" for i in range(1, 10)]),
                horizon_row(1)]
        get = FakeGet({self.url("filter", 0): hf_body(rows, len(rows))})
        path, doc = fd.horizon_cs(get, args(), self.fail, "2026-10-06")
        self.assertEqual(path, "datasets/public/horizon-cs.json")
        ds = bench.validate_public(doc)
        self.assertEqual((ds["labels"], ds["oos"]), (None, None))
        self.assertEqual(sorted(it["label"] for it in ds["items"]), ["app_crash", "high", "topic_7"])
        first = next(it for it in doc["items"] if it["label"] == "app_crash")
        self.assertEqual(first["options"], [{"key": "billing_error"}, {"key": "login_issues"}, {"key": "app_crash"}])      # bez popisu: štítek jde do promptu jednou
        self.assertEqual(first["task"], "app store review issue type, in the domain of banking")
        ten = next(it for it in doc["items"] if it["label"] == "topic_7")
        self.assertEqual(len(ten["options"]), 10)
        self.assertEqual(ten["options"][:2], [{"key": "legit_bill_inquiry", "description": "Legit: Bill Inquiry"}, {"key": "topic_1"}])      # jméno nese víc než klíč: popis zůstává
        self.assertIn("ODC-BY 1.0", doc["license"])
        self.assertIn("člověk je neověřil", doc["about"])
        self.assertIn("2–10", doc["about"])
        self.assertEqual(doc["sample"]["of"], 3)

    def test_reads_every_page_of_the_filter(self):
        pages = {self.url("filter", off): hf_body([horizon_row(off + i) for i in range(100 if off < 200 else 30)], 230) for off in (0, 100, 200)}
        get = FakeGet(pages)
        path, doc = fd.horizon_cs(get, args("--n", "50"), self.fail, "2026-10-06")
        self.assertEqual(get.urls, [self.url("filter", 0), self.url("filter", 100), self.url("filter", 200)])
        self.assertEqual((len(doc["items"]), doc["sample"]["of"]), (50, 230))
        del pages[self.url("filter", 100)]                                                    # filtr odpoví na první stránku a pak přestane:
        pages.update(self.scan_pages(40))                                                     # nasbírané zůstává a dočte se náhodnými stránkami
        get, said = FakeGet(pages), []
        path, doc = fd.horizon_cs(get, args("--n", "150"), said.append, "2026-10-06")
        self.assertEqual(get.urls[:3], [self.url("filter", 0), self.url("filter", 100), self.url("rows", 0)])
        self.assertEqual(len(doc["items"]), 150)
        self.assertIn("čtu náhodné stránky", said[0])

    def scan_pages(self, n_pages, per_page=2):
        """Celá sada bez filtru: na každé stránce per_page českých řádků podpory, zbytek řecky."""
        return {self.url("rows", p * 100): hf_body([horizon_row(p * 100 + i, lang="Czech" if i < per_page else "Greek") for i in range(100)], n_pages * 100)
                for p in range(n_pages)}

    def test_without_the_filter_it_reads_random_pages_until_it_has_enough(self):
        get, said = FakeGet(self.scan_pages(40)), []
        path, doc = fd.horizon_cs(get, args("--n", "10"), said.append, "2026-10-06")
        self.assertEqual(get.urls[:2], [self.url("filter", 0), self.url("rows", 0)])
        self.assertEqual(len(get.urls), 1 + 5)                                                # 2 úlohy na stránku → 5 stránek
        self.assertEqual(len(set(get.urls)), 6)
        self.assertEqual((path, len(doc["items"])), ("datasets/public/horizon-cs.json", 10))
        self.assertIn("čtu náhodné stránky", said[0])
        again = FakeGet(self.scan_pages(40))
        fd.horizon_cs(again, args("--n", "10"), lambda s: None, "2026-10-06")
        self.assertEqual(again.urls, get.urls)                                                # stejné semínko, stejné stránky
        other = FakeGet(self.scan_pages(40))
        fd.horizon_cs(other, args("--n", "10", "--seed", "2"), lambda s: None, "2026-10-06")
        self.assertNotEqual(other.urls, get.urls)

    def test_without_the_filter_full_reads_every_page(self):
        get = FakeGet(self.scan_pages(7))
        path, doc = fd.horizon_cs(get, args("--full"), lambda s: None, "2026-10-06")
        self.assertEqual((path, len(doc["items"]), len(get.urls)), ("datasets/public/horizon-cs-full.json", 14, 8))

    def test_a_page_that_fails_midway_keeps_what_was_collected(self):
        pages = self.scan_pages(40)
        get = FakeGet(pages)
        fd.horizon_cs(get, args("--n", "10"), lambda s: None, "2026-10-06")
        del pages[get.urls[3]]                                                                 # třetí čtená stránka přestane odpovídat
        said = []
        path, doc = fd.horizon_cs(FakeGet(pages), args("--n", "10"), said.append, "2026-10-06")
        self.assertEqual(len(doc["items"]), 4)
        self.assertIn("ukládám 4 úloh", said[-1])

    def test_the_filter_stopping_and_the_fallback_failing_at_once_keeps_what_the_filter_gave(self):
        pages = {self.url("filter", 0): hf_body([horizon_row(i) for i in range(100)], 230)}      # druhá stránka filtru i první dotaz náhradního čtení chybí
        get, said = FakeGet(pages), []
        path, doc = fd.horizon_cs(get, args("--n", "150"), said.append, "2026-10-06")
        self.assertEqual(get.urls, [self.url("filter", 0), self.url("filter", 100), self.url("rows", 0)])
        self.assertEqual((path, len(doc["items"])), ("datasets/public/horizon-cs.json", 100))
        self.assertIn("čtu náhodné stránky", said[0])
        self.assertIn("čtení skončilo dřív", said[-1])
        self.assertIn("ukládám 100 úloh", said[-1])

    def test_the_first_page_failing_is_an_error(self):
        with self.assertRaises(fd.FetchError):
            fd.horizon_cs(FakeGet({}), args(), lambda s: None, "2026-10-06")

    def test_missing_field_or_nothing_left_is_one_sentence(self):
        row = horizon_row(1)
        del row["gold_labels"]
        for rows, message in (([row], "Horizon-Labs: řádek nemá pole gold_labels — zdroj změnil tvar."),
                              ([horizon_row(1, lang="Greek")], "Horizon-Labs: žádný český řádek zákaznické podpory se 2–10 štítky — zdroj změnil obsah.")):
            with self.subTest(message=message):
                with self.assertRaises(fd.FetchError) as e:
                    fd.horizon_cs(FakeGet({self.url("filter", 0): hf_body(rows, 1)}), args(), self.fail, "2026-10-06")
                self.assertEqual(str(e.exception), message)

    def test_slug(self):
        self.assertEqual([fd.slug(s) for s in ("App Crash", "billing or payment", "card_block", " Fees & Charges! ", "Žádost")],
                         ["app_crash", "billing_or_payment", "card_block", "fees_charges", "dost"])


def cfpb_row(i, product, text=None):
    return {"Date received": "2023-12-18", "Product": product, "Complaint ID": 1000 + i,
            "Consumer complaint narrative": text if text is not None else f"Complaint number {i} about {product}. " + "The company charged me XXXX twice and refuses to fix it. " * 3}


class Cfpb(unittest.TestCase):
    PRODUCTS = ["Mortgage", "Credit card", "Checking or savings account", "Student loan", "Vehicle loan or lease", "Debt collection"]

    def pages(self, n_pages, rows_of):
        return {fd.hf_url("rows", fd.CFPB_MIRROR, "has-text", "train", p * 100): hf_body(rows_of(p), n_pages * 100) for p in range(n_pages)}

    def mixed(self, p):
        """Stránka: 60 řádků šesti sledovaných produktů dokola a 40 řádků produktu, který se nebere."""
        return ([cfpb_row(p * 100 + i, self.PRODUCTS[i % 6]) for i in range(60)]
                + [cfpb_row(p * 100 + 60 + i, "Credit reporting or other personal consumer reports") for i in range(40)])

    def test_sample_is_a_valid_email_set_with_equal_classes(self):
        get = FakeGet(self.pages(8, self.mixed))
        path, doc = fd.cfpb(get, args("--per-class", "20"), self.fail, "2026-10-06")
        self.assertEqual(path, "datasets/cfpb-complaints-120.json")
        ds = bench.validate_dataset(doc)
        self.assertEqual([c["key"] for c in ds["categories"]], ["mortgage", "credit_card", "bank_account", "student_loan", "vehicle_loan", "debt_collection"])
        self.assertEqual(Counter(e["category"] for e in ds["emails"]), {c["key"]: 20 for c in ds["categories"]})
        self.assertEqual({e["subject"] for e in ds["emails"]}, {"Consumer complaint"})
        self.assertEqual(len({e["body"] for e in ds["emails"]}), 120)
        self.assertEqual(len(get.urls), 2)                                                    # 10 z každé třídy na stránku → stačí dvě
        self.assertEqual(get.urls[0], fd.hf_url("rows", fd.CFPB_MIRROR, "has-text", "train", 0))
        self.assertIn("CC0", doc["license"])
        self.assertIn("BEE-spoke-data/consumer-finance-complaints", doc["source"])
        for part in ("BEE-spoke-data/consumer-finance-complaints", "Hugging Face", "v únoru 2024", "CC0 podle karty",      # původ textů říká i popis sady
                     "úřad sám texty stížností přes své rozhraní dnes nevydává", "stažen 2026-10-06"):
            self.assertIn(part, doc["about"], part)
        self.assertEqual(doc["sample"], {"n": 120, "per_class": 20, "seed": 1, "fetched": "2026-10-06", "max_chars": 1200})
        self.assertEqual(len({c["letter"] for c in ds["categories"]}), 6)

    def test_pages_after_the_first_come_in_seeded_order(self):
        def order(seed):
            get = FakeGet(self.pages(8, lambda p: self.mixed(p)[:6]))                         # jen 1 z každé třídy na stránku → projde všech 8
            try:
                fd.cfpb(get, args("--per-class", "8", "--seed", str(seed)), self.fail, "2026-10-06")
            except fd.FetchError:
                self.fail("osm stránek má stačit")
            return get.urls
        self.assertEqual(order(1), order(1))
        self.assertNotEqual(order(1), order(2))
        self.assertEqual(len(set(order(1))), 8)

    def test_long_text_is_cut_short_text_and_duplicates_are_skipped(self):
        long = "word " * 600
        rows = ([cfpb_row(1, "Mortgage", long), cfpb_row(2, "Mortgage", "too short"), cfpb_row(3, "Mortgage", None), cfpb_row(3, "Mortgage", None),
                 cfpb_row(4, "Mortgage", "  spaces \n\n and\tnew lines   " + "x" * 120)]
                + [cfpb_row(10 + i, p) for i, p in enumerate(self.PRODUCTS[1:] * 3)])
        path, doc = fd.cfpb(FakeGet(self.pages(1, lambda p: rows)), args("--per-class", "3"), self.fail, "2026-10-06")
        bodies = [e["body"] for e in doc["emails"] if e["category"] == "mortgage"]
        self.assertEqual(len(bodies), 3)
        self.assertTrue(bodies[0].endswith("word …") and len(bodies[0]) <= fd.CFPB_MAX_CHARS)
        self.assertEqual(bodies[2], "spaces and new lines " + "x" * 120)
        self.assertEqual(fd.clip("a  b\n c", 10), "a b c")
        self.assertEqual(fd.clip("abcdefghij klm", 12), "abcdefghij …")
        self.assertEqual(fd.clip("abcdefghijklmno", 12), "abcdefghijk…")

    def test_not_enough_complaints_is_one_sentence(self):
        with self.assertRaises(fd.FetchError) as e:
            fd.cfpb(FakeGet(self.pages(2, lambda p: [cfpb_row(p * 100 + i, "Mortgage") for i in range(100)])), args("--per-class", "5"), self.fail, "2026-10-06")
        self.assertEqual(str(e.exception), "CFPB: po 2 stránkách chybí stížnosti tříd credit_card, bank_account, student_loan, vehicle_loan, debt_collection.")

    def test_missing_field_is_one_sentence(self):
        with self.assertRaises(fd.FetchError) as e:
            fd.cfpb(FakeGet(self.pages(1, lambda p: [{"Product": "Mortgage"}])), args(), self.fail, "2026-10-06")
        self.assertEqual(str(e.exception), "CFPB: řádek nemá pole Consumer complaint narrative — zdroj změnil tvar.")

    def test_row_whose_product_or_text_is_not_text_is_skipped(self):
        def with_text(i, text):
            return {**cfpb_row(i, "Mortgage"), "Consumer complaint narrative": text}
        odd = [cfpb_row(900, ["Mortgage"]), cfpb_row(901, {"name": "Mortgage"}), cfpb_row(902, None), cfpb_row(903, 7),      # seznam a slovník nejdou použít jako klíč
               with_text(904, ["The company charged me XXXX twice and refuses to fix it."] * 5), with_text(905, {"text": "x" * 200}),
               with_text(906, None), with_text(907, 12345)]
        rows = odd + [cfpb_row(i, p) for i, p in enumerate(self.PRODUCTS * 2)]
        path, doc = fd.cfpb(FakeGet(self.pages(1, lambda p: rows)), args("--per-class", "2"), self.fail, "2026-10-06")
        self.assertEqual(Counter(e["category"] for e in doc["emails"]), {c["key"]: 2 for c in doc["categories"]})
        self.assertTrue(all(e["body"].startswith("Complaint number ") for e in doc["emails"]))      # jen řádky, které mají obojí jako text
        with self.assertRaises(fd.FetchError) as e:                                           # samé takové řádky: obvyklá věta o chybějících třídách
            fd.cfpb(FakeGet(self.pages(1, lambda p: odd)), args("--per-class", "2"), self.fail, "2026-10-06")
        self.assertEqual(str(e.exception), "CFPB: po 1 stránkách chybí stížnosti tříd mortgage, credit_card, bank_account, student_loan, vehicle_loan, debt_collection.")


class AllSets(unittest.TestCase):
    def test_all_five_sets_are_known(self):
        self.assertEqual(list(fd.SETS), ["banking77", "clinc150", "massive", "horizon-cs", "cfpb"])
        for name in fd.SETS:
            self.assertIn(f"  {name} ", fd.__doc__)

    def test_main_writes_the_cfpb_sample_next_to_the_other_email_sets(self):
        pages = Cfpb().pages(8, Cfpb().mixed)
        with tempfile.TemporaryDirectory() as tmp:
            out = []
            fd.main(["--per-class", "20", "cfpb"], get=FakeGet(pages), out=out.append, root=Path(tmp), today="2026-10-06")
            self.assertEqual(out, ["cfpb: uloženo 120 úloh do datasets/cfpb-complaints-120.json"])
            self.assertTrue(server.DATASET_RE.fullmatch("/datasets/cfpb-complaints-120.json"))
            bench.load_dataset(Path(tmp) / "datasets" / "cfpb-complaints-120.json")


def web(pages):
    """Podstrčená síť pod skutečným http_get: adresa → bajty, jinak HTTP 404. Věty chyb tak staví sám nástroj, v jazyce běhu."""
    def opener(request, timeout):
        page = pages.get(request.full_url)
        if page is None:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b""))
        return FakeStream(page)
    return lambda url, member=None: fd.http_get(url, opener=opener, sleep=lambda seconds: None, member=member)


def all_pages():
    """Všech pět zdrojů najednou: stačí na python fetch_datasets.py all."""
    rows = [horizon_row(i) for i in range(30)]
    return {fd.BANKING_URL: banking_csv(), fd.CLINC_URL: clinc_json(), fd.MASSIVE_URL: massive_tar(),
            Horizon().url("filter", 0): hf_body(rows, len(rows)), **Cfpb().pages(8, Cfpb().mixed)}


# Popisy sad česky, doslova jak je nástroj zapisoval před překladem (bez dovětku o rozsahu a datu stažení).
ABOUT_CS = {
    "banking77": "BANKING77 (PolyAI): anglické dotazy klientů internetové banky, úkolem je poznat záměr ze 77 možných. Testovací část sady. "
                 "Citace: Casanueva et al., Efficient Intent Detection with Dual Sentence Encoders, 2020.",
    "clinc150": "CLINC150 (Larson et al., An Evaluation Dataset for Intent Classification and Out-of-Scope Prediction, 2019): anglické věty pro hlasového "
                "asistenta, 150 záměrů a k nim věty „mimo rozsah“ (štítek oos), které žádnému záměru nepatří. Testovací část sady (data_full.json: test a oos_test).",
    "massive-en-us": "MASSIVE 1.1 (Amazon; FitzGerald et al., MASSIVE: A 1M-Example Multilingual Natural Language Understanding Dataset with 51 Typologically-Diverse "
                     "Languages, 2022), anglická část en-US: věty pro hlasového asistenta, úkolem je poznat, kterému z 18 scénářů (oblastí) věta patří. Testovací část "
                     "sady (partition test). Anglické věty pocházejí ze sady SLURP (Bastianelli et al., 2020). Štítky jsou holá jména scénářů bez popisu. Stejnou úlohu "
                     "(18 scénářů, 350 vět téže části) měřili autoři modelu Nimble, ti ale dali každému scénáři větu popisu.",
    "horizon-cs": "Horizon-Labs multilingual-zeroshot-synthetic, české řádky typu zákaznická podpora: krátká česká zpráva a k ní 2–10 štítků (většinou anglických), "
                  "ze kterých se vybírá jeden; každá úloha má vlastní nabídku. Texty i štítky napsal jazykový model (Qwen) a člověk je neověřil — podle autorů "
                  "s asi 7 % odvozených štítků nesouhlasil jejich vlastní model a pod češtinou je občas veden jiný jazyk. Sada je trénovací, ne testovací; "
                  "vybrané řádky: jazyk Czech, druh short nebo tax_short, jeden správný štítek.",
}
ABOUT_CFPB_CS = ("Stížnosti spotřebitelů z databáze amerického úřadu CFPB (Consumer Complaint Database), anglicky: 20 stížností z každého ze šesti finančních "
                 "produktů; úkolem je poznat, kterého produktu se stížnost týká. Text je zkrácený nejvýš na 1200 znaků a předmět je u všech stejný. Úřad osobní "
                 "údaje nahrazuje znaky XXXX. Původ textů: kopie databáze na Hugging Face (BEE-spoke-data/consumer-finance-complaints), stažená z úřadu v únoru "
                 "2024, CC0 podle karty kopie; úřad sám texty stížností přes své rozhraní dnes nevydává. Vzorek byl z kopie stažen 2026-10-06.")
TITLES = {"banking77": "BANKING77", "clinc150": "CLINC150", "massive-en-us": "MASSIVE en-US", "horizon-cs": "Horizon-Labs česky"}
POOLS = {"banking77": 308, "clinc150": 330, "massive-en-us": 36, "horizon-cs": 30}      # kolik úloh dává zdroj z all_pages()


class Language(unittest.TestCase):
    """--lang a PICKBENCH_LANG ve fetch_datasets.py: hlášky a věty chyb anglicky, česky beze změny, jazyk po běhu nezůstane
    a soubor sady na jazyku běhu nezávisí. Každý běh dostává podstrčený zdroj (get); na síť nesmí žádný."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ)                                 # jazyk z prostředí vývojáře tu nesmí rozhodovat
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(LANG_ENV, None)

    def cli(self, *argv, pages=None, get=None):
        """Pustí fd.main nad podstrčenou sítí. Vrací (řádky výstupu, čím skončil: None nebo argument sys.exit, {soubor: obsah v bajtech})."""
        with tempfile.TemporaryDirectory() as tmp:
            lines, code = [], None
            try:
                fd.main(list(argv), get=get or web(pages or {}), out=lines.append, root=Path(tmp), today="2026-10-06")
            except SystemExit as e:
                code = e.code
            files = {p.relative_to(tmp).as_posix(): p.read_bytes() for p in Path(tmp).rglob("*") if p.is_file()}
        return lines, code, files

    def both(self, *argv, **kw):
        """Stejný příkaz česky a anglicky (--lang en na konci)."""
        return self.cli(*argv, **kw), self.cli(*argv, "--lang", "en", **kw)

    def test_lang_option_may_stand_anywhere(self):
        pages = {fd.BANKING_URL: banking_csv()}
        czech, code, files = self.cli("--n", "110", "banking77", pages=pages)
        self.assertEqual((czech, code), (["banking77: uloženo 110 úloh do datasets/public/banking77.json"], None))
        english = ["banking77: saved 110 tasks to datasets/public/banking77.json"]
        for argv in (["--lang", "en", "--n", "110", "banking77"], ["--n", "110", "--lang", "en", "banking77"], ["--n", "110", "banking77", "--lang", "en"],
                     ["--n", "110", "banking77", "--lang=en"], ["--lang=en", "banking77", "--n", "110"]):
            self.assertEqual(self.cli(*argv, pages=pages), (english, None, files), argv)      # i soubor je stejný jako česky
        self.assertEqual(self.cli("--n", "110", "--lang", "cs", "banking77", pages=pages), (czech, None, files))
        self.assertEqual(self.cli("--n", "1", "banking77", pages=pages)[0], ["banking77: uloženo 1 úloh do datasets/public/banking77.json"])      # česky beze změny
        self.assertEqual(self.cli("--n", "1", "banking77", "--lang", "en", pages=pages)[0], ["banking77: saved 1 task to datasets/public/banking77.json"])

    def test_unknown_language_ends_with_a_sentence_before_any_download(self):
        for argv in (["banking77", "--lang", "de"], ["--lang=de", "banking77"], ["banking77", "--lang"]):
            with self.subTest(argv=argv):
                get = FakeGet({fd.BANKING_URL: banking_csv()})
                lines, code, files = self.cli(*argv, get=get)
                self.assertEqual((lines, files, get.urls), ([], {}, []))
                self.assertIsInstance(code, str)                             # věta = nenulový kód ukončení
                self.assertRegex(code, r"cs.*en")
        self.assertEqual(self.cli("banking77", "--lang", "de", get=FakeGet({}))[1],      # jazyk ještě není znám: věta v obou
                         'Neznámý jazyk „de“: --lang a PICKBENCH_LANG berou cs nebo en. / Unknown language "de": --lang and PICKBENCH_LANG accept cs or en.')

    def test_environment_variable_sets_the_language_and_the_option_beats_it(self):
        argv, pages = ("--n", "110", "banking77"), {fd.BANKING_URL: banking_csv()}
        czech, english = self.both(*argv, pages=pages)
        self.assertNotEqual(english[0], czech[0])
        with mock.patch.dict(os.environ, {LANG_ENV: "en"}):
            self.assertEqual(self.cli(*argv, pages=pages), english)
            self.assertEqual(self.cli(*argv, "--lang", "cs", pages=pages), czech)
            self.assertEqual(self.cli(), ([], fd.USAGE_EN, {}))
        with mock.patch.dict(os.environ, {LANG_ENV: "de"}):
            get = FakeGet(pages)
            lines, code, files = self.cli(*argv, get=get)
            self.assertEqual((lines, files, get.urls), ([], {}, []))
            self.assertIn('"de"', code)
            self.assertEqual(self.cli(*argv, "--lang", "en", pages=pages), english)      # volba proměnnou přebíjí, i když je v ní nesmysl

    def test_language_does_not_stay_after_the_run(self):
        self.assertEqual(i18n.lang(), "cs")
        for argv in (["--n", "110", "banking77"],                            # doběhl
                     [],                                                     # skončil nápovědou
                     ["clinc150"]):                                          # skončil kódem 1: zdroj neodpověděl
            with self.subTest(argv=argv):
                self.cli(*argv, "--lang", "en", pages={fd.BANKING_URL: banking_csv()})
                self.assertEqual(i18n.lang(), "cs")
        during = []

        def broken(url, member=None):
            during.append(i18n.lang())
            raise ValueError("chyba programu")
        with self.assertRaises(ValueError):                                  # výjimka uprostřed stahování
            self.cli("banking77", "--lang", "en", get=broken)
        self.assertEqual(during, ["en"])                                     # za běhu platil jazyk volby
        self.assertEqual(i18n.lang(), "cs")

    def test_no_text_is_composed_when_the_module_is_imported(self):          # konstanta s textem by zůstala v jazyce, ve kterém se modul načetl
        tree = ast.parse(Path(fd.__file__).read_text(encoding="utf-8"))
        top = [node for node in tree.body if not isinstance(node, (ast.FunctionDef, ast.ClassDef))]      # co se vykoná při importu: příkazy modulu
        top += [node for cls in tree.body if isinstance(cls, ast.ClassDef) for node in cls.body if not isinstance(node, ast.FunctionDef)]      # a těla tříd
        calls = [ast.unparse(n.func) for node in top for n in ast.walk(node) if isinstance(n, ast.Call)]
        self.assertEqual([name for name in calls if name.startswith("i18n.")], [])
        defaults = [d for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) for d in node.args.defaults + node.args.kw_defaults if d is not None]
        self.assertEqual([ast.unparse(d) for d in defaults if "i18n" in ast.unparse(d)], [])      # ani jako výchozí hodnota parametru

    def test_usage_follows_the_language(self):
        for argv in ([], ["nic"], ["--n", "0", "banking77"], ["--per-class", "0", "cfpb"]):
            with self.subTest(argv=argv):
                get = FakeGet({})
                self.assertEqual(self.cli(*argv, get=get), ([], fd.__doc__, {}))
                self.assertEqual(self.cli(*argv, "--lang", "en", get=get), ([], fd.USAGE_EN, {}))
                self.assertEqual(get.urls, [])
        self.assertTrue(fd.USAGE_EN.startswith("Downloads public datasets for PickBench "))
        self.assertEqual(CZECH.findall(fd.USAGE_EN), [])
        self.assertEqual(len(fd.USAGE_EN.splitlines()), len(fd.__doc__.splitlines()))
        self.assertEqual(sorted(re.findall(r"\d+", fd.USAGE_EN)), sorted(re.findall(r"\d+", fd.__doc__)))      # počty, meze a výchozí hodnoty
        for text in (fd.__doc__, fd.USAGE_EN):                               # každá sada a každá volba je v obou nápovědách
            for word in (*(f"  {name} " for name in fd.SETS), "  all ", "[--n N]", "[--seed S]", "[--full]", "--per-class K", "--lang cs|en", LANG_ENV,
                         "datasets/public/", "datasets/cfpb-complaints-<", "datasets-server.huggingface.co", "HTTP 429", "bench.validate_public"):
                self.assertIn(word, text)
            self.assertEqual(text.count(" → "), 5)                           # pět sad: co je vstup a co se vybírá

    def test_main_prints_english_messages_and_writes_the_same_files(self):
        (lines_cs, code_cs, files_cs), (lines_en, code_en, files_en) = self.both("--n", "18", "--per-class", "20", "all", pages=all_pages())
        self.assertEqual((code_cs, code_en), (None, None))
        saved = [("banking77", 18, "datasets/public/banking77.json"), ("clinc150", 18, "datasets/public/clinc150.json"),
                 ("massive", 18, "datasets/public/massive-en-us.json"), ("horizon-cs", 18, "datasets/public/horizon-cs.json"),
                 ("cfpb", 120, "datasets/cfpb-complaints-120.json")]
        self.assertEqual(lines_cs, [f"{name}: uloženo {n} úloh do {path}" for name, n, path in saved])
        self.assertEqual(lines_en, [f"{name}: saved {n} tasks to {path}" for name, n, path in saved])
        self.assertEqual(sorted(files_cs), sorted(path for _, _, path in saved))
        self.assertEqual(files_en, files_cs)                                 # soubor sady na jazyku běhu nezávisí: bajt po bajtu stejný
        for path, raw in files_cs.items():
            with self.subTest(path=path):
                doc = json.loads(raw)
                self.assertEqual(list(doc)[:3], ["name", "about", "about_en"])      # anglický popis hned za českým
                self.assertTrue(CZECH.search(doc["about"]))
                self.assertEqual(CZECH.findall(doc["about_en"]), [])
                self.assertEqual(sorted(re.findall(r"\d+", doc["about_en"])), sorted(re.findall(r"\d+", doc["about"])))      # počty, roky a datum
                (bench.validate_dataset if "emails" in doc else bench.validate_public)(doc)

    def test_czech_descriptions_and_names_read_as_before_in_both_runs(self):
        for flags, scope_of, name_of in (
                (["--n", "18"], "Rozsah: vzorek 18 z {pool}, semínko 1; staženo 2026-10-06.", "{title} (vzorek 18 z {pool}, semínko 1)"),
                (["--full"], "Rozsah: celá testovací část, {pool} úloh; staženo 2026-10-06.", "{title} (celá testovací část, {pool} úloh)")):
            czech, english = self.both(*flags, "banking77", "clinc150", "massive", "horizon-cs", pages=all_pages())
            self.assertEqual(english[2], czech[2])
            for name, about in ABOUT_CS.items():
                with self.subTest(name=name, flags=flags):
                    doc = json.loads(czech[2][f"datasets/public/{name}{'-full' if '--full' in flags else ''}.json"])
                    self.assertEqual(doc["about"], about + " " + scope_of.format(pool=POOLS[name]))
                    self.assertEqual(doc["name"], name_of.format(title=TITLES[name], pool=POOLS[name]))      # název sady se nepřekládá
        czech, english = self.both("--per-class", "20", "cfpb", pages=all_pages())
        self.assertEqual(english[2], czech[2])
        doc = json.loads(czech[2]["datasets/cfpb-complaints-120.json"])
        self.assertEqual((doc["name"], doc["about"]), ("CFPB stížnosti (120)", ABOUT_CFPB_CS))

    def test_english_descriptions_say_what_the_czech_ones_do(self):
        facts = {      # co anglický popis musí nést: co sada obsahuje, počty, zdroj a citace
            "banking77": ("BANKING77 (PolyAI): English queries", "online bank", "intent out of 77", "The test split of the dataset.",
                          "Citation: Casanueva et al., Efficient Intent Detection with Dual Sentence Encoders, 2020."),
            "clinc150": ("CLINC150 (Larson et al., An Evaluation Dataset for Intent Classification and Out-of-Scope Prediction, 2019): English sentences",
                         "voice assistant", "150 intents", '"out of scope" sentences (the oos label)', "belong to no intent",
                         "The test split of the dataset (data_full.json: test and oos_test)."),
            "massive-en-us": ("MASSIVE 1.1 (Amazon; FitzGerald et al., MASSIVE: A 1M-Example Multilingual Natural Language Understanding Dataset with 51 "
                              "Typologically-Diverse Languages, 2022), the English part en-US:", "voice assistant", "which of the 18 scenarios (domains)",
                              "The test split of the dataset (partition test).", "SLURP dataset (Bastianelli et al., 2020)", "bare scenario names without a description",
                              "(18 scenarios, 350 sentences of the same split)", "Nimble model", "every scenario a one-sentence description"),
            "horizon-cs": ("Horizon-Labs multilingual-zeroshot-synthetic, the Czech customer support rows:", "a short Czech message", "2–10 labels (mostly English)",
                           "every task has its own list.", "A language model (Qwen) wrote", "no human verified them", "about 7% of the derived labels",
                           "now and then a row filed under Czech is in another language", "a training set, not a test set",
                           "language Czech, kind short or tax_short, one correct label."),
        }
        for flags, scope in ((["--n", "18"], "Scope: a sample of 18 out of {pool}, seed 1; downloaded 2026-10-06."),
                             (["--full"], "Scope: the whole test split, {pool} tasks; downloaded 2026-10-06.")):
            files = self.cli(*flags, "banking77", "clinc150", "massive", "horizon-cs", pages=all_pages())[2]
            for name, parts in facts.items():
                with self.subTest(name=name, flags=flags):
                    about = json.loads(files[f"datasets/public/{name}{'-full' if '--full' in flags else ''}.json"])["about_en"]
                    self.assertTrue(about.endswith(" " + scope.format(pool=POOLS[name])), about)
                    for part in parts:
                        self.assertIn(part, about)
                    self.assertEqual(CZECH.findall(about), [])
        about = json.loads(self.cli("--per-class", "20", "cfpb", pages=all_pages())[2]["datasets/cfpb-complaints-120.json"])["about_en"]
        for part in ("Consumer complaints from the database of the US agency CFPB (Consumer Complaint Database), in English: 20 complaints for each of the six "
                     "financial products", "which product the complaint is about", "at most 1200 characters", "the subject is the same for all of them",
                     "replaces personal data with the characters XXXX", "a copy of the database on Hugging Face (BEE-spoke-data/consumer-finance-complaints)",
                     "downloaded from the agency in February 2024", "CC0 according to the card of the copy",
                     "the agency itself does not release the complaint texts through its API today",
                     "fetch_datasets.py downloaded the sample from the copy on 2026-10-06."):
            self.assertIn(part, about)
        one = json.loads(self.cli("--per-class", "1", "cfpb", pages=all_pages())[2]["datasets/cfpb-complaints-6.json"])
        self.assertIn("in English: 1 complaint for each of the six", one["about_en"])      # jednotné číslo
        self.assertIn("anglicky: 1 stížností z každého", one["about"])                     # česky beze změny

    def test_error_sentences_in_both_languages(self):
        opener = lambda answers: HttpGet().opener(answers)[0]
        quiet = lambda seconds: None
        status = HttpGet.status

        def massive_row(**change):
            return (json.dumps({"id": "1", "locale": "en-US", "partition": "test", "scenario": "alarm", "utt": "wake me", **change}) + "\n").encode("utf-8")

        def banking(body):
            return lambda: fd.banking77(FakeGet({fd.BANKING_URL: body}), args(), self.fail, "2026-10-06")

        def clinc(body):
            return lambda: fd.clinc150(FakeGet({fd.CLINC_URL: body}), args(), self.fail, "2026-10-06")

        def massive(body):
            return lambda: fd.massive(FakeGet({fd.MASSIVE_URL: body}), args(), self.fail, "2026-10-06")

        def horizon(rows):
            return lambda: fd.horizon_cs(FakeGet({Horizon().url("filter", 0): hf_body(rows, 1)}), args(), self.fail, "2026-10-06")

        def cfpb(rows, *argv):
            return lambda: fd.cfpb(FakeGet(Cfpb().pages(1, lambda p: rows)), args(*argv), self.fail, "2026-10-06")
        no_gold = horizon_row(1)
        del no_gold["gold_labels"]
        page = fd.hf_url("rows", "a/b", "c", "s", 0)
        cases = [
            (lambda: fd.http_get("http://x/a.tar.gz", opener=opener([FakeStream(massive_tar(name="1.1/data/en-GB.jsonl"))]), sleep=quiet, member="/data/en-US.jsonl"),
             "V archivu není soubor končící /data/en-US.jsonl — zdroj změnil tvar: http://x/a.tar.gz",
             "The archive has no file ending in /data/en-US.jsonl — the source changed its format: http://x/a.tar.gz"),
            (lambda: fd.http_get("http://x/a", tries=2, opener=opener([status(500)]), sleep=quiet),
             "Stažení se nepodařilo (HTTP 500): http://x/a", "The download failed (HTTP 500): http://x/a"),
            (lambda: fd.http_get("http://x/a", tries=1, opener=opener([urllib.error.URLError("timed out")]), sleep=quiet),      # hláška systému zůstává, jak přišla
             "Stažení se nepodařilo (timed out): http://x/a", "The download failed (timed out): http://x/a"),
            (lambda: fd.as_json(b"<html>", "CLINC150"), "CLINC150: odpověď není JSON — zdroj změnil tvar.", "CLINC150: the answer is not JSON — the source changed its format."),
            (banking("text,category\nčau,pozdrav\n".encode("cp1250")), "BANKING77: soubor není text v UTF-8 — zdroj změnil tvar.",
             "BANKING77: the file is not UTF-8 text — the source changed its format."),
            (banking(b'text,category\n"' + b"x" * 140000 + b'",topic\n'), "BANKING77: soubor nejde přečíst jako CSV — zdroj změnil tvar.",
             "BANKING77: fetch_datasets.py cannot read the file as CSV — the source changed its format."),
            (banking(b"utterance,intent\nhi,greet\n"), "BANKING77: soubor nemá sloupce text a category — zdroj změnil tvar.",
             "BANKING77: the file has no text and category columns — the source changed its format."),
            (banking(b"text,category\nhi\n"), "BANKING77: řádek nemá text a category jako text — zdroj změnil tvar.",
             "BANKING77: a row does not have text and category as text — the source changed its format."),
            (banking(banking_csv(classes=76)), "BANKING77: čekáno 77 záměrů, zdroj jich má 76.", "BANKING77: expected 77 intents, the source has 76."),
            (clinc(b'{"test": []}'), "CLINC150: soubor nemá seznamy test a oos_test — zdroj změnil tvar.",
             "CLINC150: the file has no test and oos_test lists — the source changed its format."),
            (clinc(json.dumps({"test": [["a", "x"]], "oos_test": [["b"]]}).encode()), "CLINC150: řádek nemá tvar [text, záměr] — zdroj změnil tvar.",
             "CLINC150: a row does not have the format [text, intent] — the source changed its format."),
            (clinc(clinc_json(intents=149)), "CLINC150: čekáno 150 záměrů a oos, zdroj má 149 záměrů.", "CLINC150: expected 150 intents and oos, the source has 149 intents."),
            (clinc(clinc_json(intents=1)), "CLINC150: čekáno 150 záměrů a oos, zdroj má 1 záměrů.", "CLINC150: expected 150 intents and oos, the source has 1 intent."),
            (massive(b"<html>"), "MASSIVE: soubor en-US.jsonl není JSON Lines — zdroj změnil tvar.", "MASSIVE: the file en-US.jsonl is not JSON Lines — the source changed its format."),
            (massive(massive_row(utt=None)), "MASSIVE: řádky části test nemají pole utt a scenario jako text — zdroj změnil tvar.",
             "MASSIVE: the rows of the test split do not have the fields utt and scenario as text — the source changed its format."),
            (massive(MASSIVE_SAMPLE + massive_row(scenario="shopping")), "MASSIVE: čekáno 18 známých scénářů, zdroj se liší v: shopping.",
             "MASSIVE: expected 18 known scenarios, the source differs in: shopping."),
            (lambda: fd.hf_page(FakeGet({page: b'{"rows": []}'}), "rows", "a/b", "c", "s", 0), "a/b: odpověď nemá rows a num_rows_total — zdroj změnil tvar.",
             "a/b: the answer has no rows and num_rows_total — the source changed its format."),
            (horizon([no_gold]), "Horizon-Labs: řádek nemá pole gold_labels — zdroj změnil tvar.", "Horizon-Labs: a row has no field gold_labels — the source changed its format."),
            (cfpb([{"Product": "Mortgage"}]), "CFPB: řádek nemá pole Consumer complaint narrative — zdroj změnil tvar.",
             "CFPB: a row has no field Consumer complaint narrative — the source changed its format."),
            (horizon([horizon_row(1, lang="Greek")]), "Horizon-Labs: žádný český řádek zákaznické podpory se 2–10 štítky — zdroj změnil obsah.",
             "Horizon-Labs: no Czech customer support row with 2–10 labels — the source changed its content."),
            (cfpb([cfpb_row(i, "Mortgage") for i in range(100)], "--per-class", "5"),
             "CFPB: po 1 stránkách chybí stížnosti tříd credit_card, bank_account, student_loan, vehicle_loan, debt_collection.",
             "CFPB: after 1 page, complaints of these classes are missing: credit_card, bank_account, student_loan, vehicle_loan, debt_collection."),
            (lambda: fd.cfpb(FakeGet(Cfpb().pages(2, lambda p: [cfpb_row(p * 100 + i, "Mortgage") for i in range(100)])), args("--per-class", "5"), self.fail, "2026-10-06"),
             "CFPB: po 2 stránkách chybí stížnosti tříd credit_card, bank_account, student_loan, vehicle_loan, debt_collection.",
             "CFPB: after 2 pages, complaints of these classes are missing: credit_card, bank_account, student_loan, vehicle_loan, debt_collection."),
        ]
        used, real = set(), i18n.t

        def spy(key, /, **values):
            used.add(key)
            return real(key, **values)
        with mock.patch.object(i18n, "t", spy):
            for call, cs, en in cases:
                with self.subTest(cs=cs):
                    for lang, want in (("cs", cs), ("en", en)):
                        with i18n.process(lang), self.assertRaises(fd.FetchError) as ctx:
                            call()
                        self.assertEqual(str(ctx.exception), want)
                    self.assertEqual(CZECH.findall(en), [])
        errors = {key for key in i18n.TEXTS if key.startswith("fetch.err.")}
        self.assertEqual(len(errors), 20)
        self.assertEqual(errors - used, {"fetch.err.write", "fetch.err.interrupted"})      # každá věta FetchError je tu vyvolaná; tyhle dvě říká main (test níže)

    def test_error_from_the_validator_keeps_the_name_of_the_set_and_follows_the_language(self):      # věta validátoru za jménem sady: žádné slovo navíc
        def broken():
            return fd.public_doc("BANKING77", "česky", "in English", "", "CC0", "Pick one.", [{"text": "t", "label": "a"}], 1, args(), "2026-10-06",
                                 labels=[{"key": "a"}, {"key": "b"}])
        for lang, want in (("cs", "BANKING77: Neplatná veřejná sada: chybí source nebo license"), ("en", "BANKING77: Invalid public dataset: source or license is missing")):
            with i18n.process(lang), self.assertRaises(fd.FetchError) as ctx:
                broken()
            self.assertEqual(str(ctx.exception), want)

    def test_sentences_of_the_run_follow_the_language(self):
        pages = {fd.CLINC_URL: clinc_json()}
        (lines_cs, code_cs, files_cs), (lines_en, code_en, files_en) = self.both("banking77", "clinc150", pages=pages)      # jedna sada selže, druhá se uloží
        self.assertEqual((code_cs, code_en), (1, 1))
        self.assertEqual(lines_cs, [f"banking77: Stažení se nepodařilo (HTTP 404): {fd.BANKING_URL}", "clinc150: uloženo 330 úloh do datasets/public/clinc150.json"])
        self.assertEqual(lines_en, [f"banking77: The download failed (HTTP 404): {fd.BANKING_URL}", "clinc150: saved 330 tasks to datasets/public/clinc150.json"])
        self.assertEqual((sorted(files_cs), files_en), (["datasets/public/clinc150.json"], files_cs))
        with mock.patch.object(fd, "write_json", side_effect=OSError(28, "No space left on device")):
            (lines_cs, code_cs, _), (lines_en, code_en, _) = self.both("clinc150", pages=pages)
        self.assertEqual((lines_cs, code_cs), (["clinc150: soubor nejde zapsat ([Errno 28] No space left on device)"], 1))
        self.assertEqual((lines_en, code_en), (["clinc150: fetch_datasets.py cannot write the file ([Errno 28] No space left on device)"], 1))      # hláška systému zůstává

        def ctrl_c(url, member=None):
            raise KeyboardInterrupt
        (lines_cs, code_cs, files_cs), (lines_en, code_en, files_en) = self.both("banking77", "clinc150", get=ctrl_c)
        self.assertEqual((lines_cs, code_cs, files_cs), ([], "Přerušeno.", {}))
        self.assertEqual((lines_en, code_en, files_en), ([], "Interrupted.", {}))

    def test_horizon_progress_messages_in_both_languages(self):
        horizon = Horizon()
        failed = lambda url: {"cs": f"Stažení se nepodařilo (HTTP 404): {url}", "en": f"The download failed (HTTP 404): {url}"}
        no_filter = failed(horizon.url("filter", 0))
        (cs, _, files_cs), (en, _, files_en) = self.both("--n", "10", "horizon-cs", pages=horizon.scan_pages(40))      # filtr neodpovídá: náhodné stránky
        self.assertEqual(cs, [f"Horizon-Labs: filtr podle jazyka neodpovídá ({no_filter['cs']}); čtu náhodné stránky celé sady, dokud nenasbírám 10 úloh, "
                              "nejvýš 400 stránek — může to trvat hodinu.", "horizon-cs: uloženo 10 úloh do datasets/public/horizon-cs.json"])
        self.assertEqual(en, [f"Horizon-Labs: the language filter does not answer ({no_filter['en']}); reading random pages of the whole dataset to collect 10 tasks, "
                              "at most 400 pages — this may take an hour.", "horizon-cs: saved 10 tasks to datasets/public/horizon-cs.json"])
        self.assertEqual(files_en, files_cs)
        english = list(en)
        (cs, _, _), (en, _, _) = self.both("--full", "horizon-cs", pages=horizon.scan_pages(7))      # s --full všechny stránky
        self.assertEqual(cs[0], f"Horizon-Labs: filtr podle jazyka neodpovídá ({no_filter['cs']}); čtu náhodné stránky celé sady, všechny — potrvá to mnoho hodin.")
        self.assertEqual(en[0], f"Horizon-Labs: the language filter does not answer ({no_filter['en']}); reading all pages of the whole dataset "
                                "in random order — this will take many hours.")
        english += en
        (cs, _, _), (en, _, _) = self.both("--n", "200", "horizon-cs", pages=horizon.scan_pages(60))      # každá padesátá stránka se ohlásí
        self.assertEqual(cs[1:], ["Horizon-Labs: čtu 50. stránku, zatím 98 úloh…", "horizon-cs: uloženo 120 úloh do datasets/public/horizon-cs.json"])
        self.assertEqual(en[1:], ["Horizon-Labs: reading page 50, 98 tasks so far…", "horizon-cs: saved 120 tasks to datasets/public/horizon-cs.json"])
        english += en
        pages = horizon.scan_pages(40)
        probe = FakeGet(pages)
        fd.horizon_cs(probe, args("--n", "10"), lambda s: None, "2026-10-06")
        gone = probe.urls[3]                                                 # třetí čtená stránka přestane odpovídat
        del pages[gone]
        (cs, _, files_cs), (en, _, files_en) = self.both("--n", "10", "horizon-cs", pages=pages)
        self.assertEqual(cs[1:], [f"Horizon-Labs: čtení skončilo dřív ({failed(gone)['cs']}); ukládám 4 úloh, které se stihly nasbírat.",
                                  "horizon-cs: uloženo 4 úloh do datasets/public/horizon-cs.json"])
        self.assertEqual(en[1:], [f"Horizon-Labs: reading ended early ({failed(gone)['en']}); saving the 4 tasks collected so far.",
                                  "horizon-cs: saved 4 tasks to datasets/public/horizon-cs.json"])
        self.assertEqual(files_en, files_cs)
        english += en
        self.assertEqual(CZECH.findall("\n".join(english)), [])


if __name__ == "__main__":
    unittest.main()
