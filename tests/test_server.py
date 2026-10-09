import contextlib
import http.client
import io
import json
import os
import re
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
import i18n
import providers
import server
import store


def setUpModule():
    """Testy českých vět na jazyce vývojáře nezávisí: PICKBENCH_LANG se na dobu modulu odstraní z prostředí a po něm vrátí."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(i18n.ENV, None)


class DatasetEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "datasets").mkdir()
        (root / "datasets" / "a.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"name": "Sada A"}).encode())
        (root / "datasets" / "broken.json").write_text("{nope", encoding="utf-8")
        (root / "datasets" / "s mezerou.json").write_text("{}", encoding="utf-8")
        (root / "secret.py").write_text("x = 1", encoding="utf-8")
        cls.old_root, server.ROOT = server.ROOT, root
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.ROOT = cls.old_root
        cls.tmp.cleanup()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def test_list_reads_name_even_with_bom_and_skips_bad_filenames(self):
        status, body = self.get("/datasets")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["datasets"],
                         [{"file": "a.json", "name": "Sada A"}, {"file": "broken.json", "name": "broken"}])

    def test_file_is_served(self):
        status, body = self.get("/datasets/a.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8-sig"))["name"], "Sada A")

    def test_paths_outside_datasets_are_404(self):
        for path in ("/datasets/..%2fsecret.py", "/datasets/../secret.py", "/datasets/a.json/..",
                     "/datasets/missing.json", "/datasets/a.txt", "/datasets/s%20mezerou.json"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)

    def test_missing_folder_gives_empty_list(self):
        with tempfile.TemporaryDirectory() as empty:
            keep, server.ROOT = server.ROOT, Path(empty)
            try:
                self.assertEqual(server.list_datasets(), [])
            finally:
                server.ROOT = keep


class GpuEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def gpu_with(self, run):
        """GET /gpu s podstrčeným spouštěním nvidia-smi. Vrací (status, JSON)."""
        old, server.gpu._run = server.gpu._run, run
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
            c.request("GET", "/gpu")
            r = c.getresponse()
            body = json.loads(r.read())
            c.close()
            return r.status, body
        finally:
            server.gpu._run = old

    def test_gpu_state(self):
        status, body = self.gpu_with(lambda cmd, timeout: "71, 38.60, 1282, P4, 5200, 93\n")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"available": True, "temp_c": 71, "power_w": 38.6, "clock_mhz": 1282,
                                "pstate": "P4", "mem_mb": 5200, "util_pct": 93})

    def test_not_available_value_is_null(self):
        status, body = self.gpu_with(lambda cmd, timeout: "71, [N/A], 1282, P4, 5200, 93\n")
        self.assertEqual((status, body["available"], body["power_w"]), (200, True, None))

    def test_machine_without_nvidia_smi(self):
        def missing(cmd, timeout):
            raise FileNotFoundError("nvidia-smi")
        self.assertEqual(self.gpu_with(missing), (200, {"available": False}))

    def test_other_gpu_paths_are_404(self):
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        c.request("GET", "/gpu/")
        self.assertEqual(c.getresponse().status, 404)
        c.close()


class WithServer(unittest.TestCase):
    """Server na volném portu s databází v dočasné složce."""

    LANG = None                                      # jazyk požadavků třídy (hlavička X-PickBench-Lang); None = bez hlavičky, jako klienti před překladem

    def lang_header(self):
        return {i18n.HEADER: self.LANG} if self.LANG else {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.db"
        old = server.STORE
        self.addCleanup(setattr, server, "STORE", old)
        server.STORE = store.Store(self.db, now=lambda: "2026-10-06T21:00:00")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def call(self, method, path, body=None, raw=None, headers=None):
        """Vrací (status, JSON odpovědi). body = objekt pro JSON, raw = bajty těla tak, jak jsou."""
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        c.request(method, path, body=data, headers={**self.lang_header(), **(headers or {})})
        r = c.getresponse()
        out = json.loads(r.read())
        c.close()
        return r.status, out


class StoreEndpoints(WithServer):
    def test_results_add_list_and_delete(self):
        self.assertEqual(self.call("GET", "/results"), (200, {"results": []}))
        first = {"cells": {"driver": "Pravidla (ideální řidič)", "n": 12}, "settings": {"seed": "7"}}
        self.assertEqual(self.call("POST", "/results", first),
                         (200, {"id": 1, "ts": "2026-10-06T21:00:00", "data": first}))
        self.call("POST", "/results", {"cells": {"n": 3}})
        status, body = self.call("GET", "/results")
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in body["results"]], [2, 1])
        self.assertEqual(body["results"][1]["data"], first)
        self.assertEqual(self.call("DELETE", "/results"), (200, {"deleted": 2}))
        self.assertEqual(self.call("GET", "/results"), (200, {"results": []}))

    def results_ids(self):
        return [r["id"] for r in self.call("GET", "/results")[1]["results"]]

    def test_delete_one_result_by_id(self):
        for i in range(3):
            self.call("POST", "/results", {"cells": {"n": i}})
        self.assertEqual(self.call("DELETE", "/results/2"), (200, {"deleted": 1}))
        self.assertEqual(self.results_ids(), [3, 1])
        self.assertEqual(self.call("DELETE", "/results"), (200, {"deleted": 2}))      # hromadné mazání je beze změny
        self.assertEqual(self.results_ids(), [])

    def test_delete_missing_result_is_404_with_a_sentence(self):
        self.call("POST", "/results", {"a": 1})
        for rid in ("2", "9" * 30, "9" * 40):                # větší než jakékoli číslo v SQLite
            with self.subTest(rid=rid):
                status, body = self.call("DELETE", "/results/" + rid)
                self.assertEqual(status, 404)
                self.assertIn("není", body["error"])
        self.assertEqual(self.call("DELETE", "/results/1"), (200, {"deleted": 1}))
        self.assertEqual(self.call("DELETE", "/results/1")[0], 404)                    # podruhé už tam není

    def test_delete_result_with_a_bad_id_is_400_and_nothing_is_deleted(self):
        bad = ("abc", "-1", "+1", "0", "00", "", "1/2", "1.5", "1x", "1?x=1", "%31", "%D9%A1",
               "1" * 41, "1" * 5000)                         # příliš dlouhé číslo: int() by u tisíců číslic hlásil chybu a spojení by spadlo bez odpovědi
        for rid in bad:
            with self.subTest(rid=rid[:12]):
                self.call("POST", "/results", {"a": 1})
                status, body = self.call("DELETE", "/results/" + rid)
                self.assertEqual(status, 400)
                self.assertIn("číslo výsledku", body["error"])
        self.assertEqual(len(self.results_ids()), len(bad))

    def test_delete_one_result_from_another_web_page_is_refused_and_nothing_is_deleted(self):
        self.call("POST", "/results", {"a": 1})
        port = self.httpd.server_address[1]
        for headers in ({"Origin": "http://evil.example"}, {"Origin": "null"}, {"Host": "evil.example"},
                        {"Host": f"evil.example:{port}", "Origin": f"http://evil.example:{port}"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.call("DELETE", "/results/1", headers=headers), (403, {"error": "požadavek z cizí stránky"}))
        self.assertEqual(self.results_ids(), [1])
        self.assertEqual(self.call("DELETE", "/results/1", headers={"Origin": f"http://127.0.0.1:{port}"}), (200, {"deleted": 1}))

    def test_settings_and_queue_are_kept_separately(self):
        self.assertEqual(self.call("GET", "/settings"), (200, {"settings": None}))
        self.assertEqual(self.call("GET", "/queue"), (200, {"queue": None}))
        self.assertEqual(self.call("POST", "/settings", {"scenario": "numbers", "lanes": 4}), (200, {"ok": True}))
        self.assertEqual(self.call("POST", "/queue", {"batch": 1, "steps": [{"state": "wait"}]}), (200, {"ok": True}))
        self.call("POST", "/settings", {"scenario": "obstacles"})
        self.assertEqual(self.call("GET", "/settings"), (200, {"settings": {"scenario": "obstacles"}}))
        self.assertEqual(self.call("GET", "/queue"), (200, {"queue": {"batch": 1, "steps": [{"state": "wait"}]}}))

    def test_bad_bodies_are_400_and_nothing_is_stored(self):
        for path in ("/results", "/settings", "/queue", "/unload"):
            for name, kwargs in (("not json", {"raw": b"{nope"}), ("list", {"body": [1, 2]}), ("text", {"body": "x"}),
                                 ("empty", {"raw": b""}), ("not utf-8", {"raw": b'{"a": "\xff"}'}),
                                 ("too big", {"raw": b" " * (server.MAX_BODY + 1)})):
                with self.subTest(path=path, body=name):
                    status, body = self.call("POST", path, **kwargs)
                    self.assertEqual(status, 400)
                    self.assertIn("tělo požadavku", body["error"])
        self.assertEqual(self.call("GET", "/results")[1], {"results": []})
        self.assertEqual(self.call("GET", "/settings")[1], {"settings": None})

    def test_body_at_the_size_limit_is_accepted(self):
        pad = "x" * (server.MAX_BODY - len(json.dumps({"pad": ""})))
        self.assertEqual(self.call("POST", "/settings", {"pad": pad})[0], 200)

    def test_database_trouble_is_503_and_the_game_still_loads(self):
        self.db.write_bytes(b"toto neni databaze " * 200)
        for method, path, body in (("GET", "/results", None), ("POST", "/results", {"a": 1}), ("DELETE", "/results", None),
                                   ("DELETE", "/results/1", None), ("GET", "/settings", None), ("POST", "/settings", {}),
                                   ("GET", "/queue", None), ("POST", "/queue", {})):
            with self.subTest(method=method, path=path):
                status, out = self.call(method, path, body)
                self.assertEqual(status, 503)
                self.assertIn("neukládají", out["error"])
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        c.request("GET", "/")
        r = c.getresponse()
        self.assertEqual((r.status, r.read()[:15]), (200, b"<!doctype html>"))
        c.close()
        self.assertEqual(self.call("GET", "/datasets")[0], 200)

    def test_unknown_paths_and_methods_are_404(self):
        for method, path in (("POST", "/nic"), ("POST", "/results/1"), ("DELETE", "/settings"), ("DELETE", "/queue"),
                             ("DELETE", "/settings/1"), ("GET", "/results/"), ("GET", "/results/1"), ("GET", "/unload")):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.call(method, path, {} if method == "POST" else None)[0], 404)

    def test_requests_from_another_web_page_are_refused(self):         # cizí stránka v prohlížeči nesmí přepsat frontu ani číst výsledky
        port = self.httpd.server_address[1]
        foreign = ({"Origin": "http://evil.example"}, {"Origin": "null"}, {"Host": "evil.example"},
                   {"Host": f"evil.example:{port}", "Origin": f"http://evil.example:{port}"},      # DNS rebinding
                   {"Origin": f"https://127.0.0.1:{port}"}, {"Host": ""})
        for headers in foreign:
            for method, path in (("POST", "/settings"), ("POST", "/results"), ("POST", "/queue"), ("POST", "/unload"),
                                 ("POST", "/decide"), ("DELETE", "/results"), ("DELETE", "/results/1"), ("GET", "/results"), ("GET", "/datasets")):
                with self.subTest(headers=headers, method=method, path=path):
                    status, body = self.call(method, path, {"model": "x"} if method == "POST" else None, headers=headers)
                    self.assertEqual((status, body), (403, {"error": "požadavek z cizí stránky"}))
        self.assertEqual(self.call("GET", "/settings")[1], {"settings": None})
        self.assertEqual(self.call("GET", "/results")[1], {"results": []})
        own = ({}, {"Origin": f"http://127.0.0.1:{port}"}, {"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"},
               {"Host": f"LOCALHOST:{port}"}, {"Host": f"[::1]:{port}"}, {"Host": "127.0.0.1"})
        for headers in own:
            with self.subTest(headers=headers):
                self.assertEqual(self.call("POST", "/settings", {"a": 1}, headers=headers), (200, {"ok": True}))
                self.assertEqual(self.call("GET", "/settings", headers=headers), (200, {"settings": {"a": 1}}))

    def test_no_response_may_be_framed_by_another_page(self):          # hra v rámu cizí stránky = kliknutí naslepo na Vyprázdnit frontu
        for method, path, headers in (("GET", "/", {}), ("GET", "/settings", {}), ("GET", "/nic", {}),
                                      ("POST", "/queue", {"Origin": "http://evil.example"})):
            with self.subTest(method=method, path=path, headers=headers):
                c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
                c.request(method, path, body=b"{}" if method == "POST" else None, headers=headers)
                r = c.getresponse()
                r.read()
                c.close()
                self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
                self.assertEqual(r.getheader("Content-Security-Policy"), "frame-ancestors 'none'")

    def test_parallel_posts_all_land(self):
        def work(t):
            for i in range(10):
                self.call("POST", "/results", {"thread": t, "i": i})
        threads = [threading.Thread(target=work, args=(t,)) for t in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = self.call("GET", "/results")[1]["results"]
        self.assertEqual(sorted(r["id"] for r in got), list(range(1, 61)))


class OllamaEndpoints(WithServer):
    def with_ollama(self, fake):
        old = server.ollama
        self.addCleanup(setattr, server, "ollama", old)
        server.ollama = fake

    def test_unload_sends_keep_alive_zero_without_a_prompt(self):
        calls = []

        def fake(path, payload=None, timeout=300):
            calls.append((path, payload, timeout))
            return {"model": payload["model"], "response": "", "done": True, "done_reason": "unload"}
        self.with_ollama(fake)
        self.assertEqual(self.call("POST", "/unload", {"model": "tev1:latest"}), (200, {"unloaded": "tev1:latest"}))
        self.assertEqual(calls, [("/api/generate", {"model": "tev1:latest", "keep_alive": 0, "stream": False}, 30)])

    def test_unload_of_a_missing_model_is_502_with_ollamas_words(self):
        def fake(path, payload=None, timeout=300):
            raise urllib.error.HTTPError("http://x", 404, "Not Found", {}, io.BytesIO(b'{"error":"model \'x\' not found"}'))
        self.with_ollama(fake)
        status, body = self.call("POST", "/unload", {"model": "x"})
        self.assertEqual(status, 502)
        self.assertEqual(body["error"], 'Ollama 404: {"error":"model \'x\' not found"}')

    def test_unload_without_ollama_is_502(self):
        def fake(path, payload=None, timeout=300):
            raise ConnectionRefusedError("odmítnuto")
        self.with_ollama(fake)
        status, body = self.call("POST", "/unload", {"model": "tev1"})
        self.assertEqual((status, body["error"]), (502, "Ollama: odmítnuto"))

    def test_unload_needs_a_model_name(self):
        calls = []
        self.with_ollama(lambda *a, **k: calls.append(a))
        for body in ({}, {"model": ""}, {"model": 5}, {"model": None}):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", "/unload", body), (400, {"error": "chybí jméno modelu"}))
        self.assertEqual(calls, [])

    def test_decide_still_proxies_and_adds_the_model_time(self):
        seen = []

        def fake(path, payload=None, timeout=300):
            seen.append((path, payload))
            return {"answers": {"target": {"choice": "lane_1"}}}
        self.with_ollama(fake)
        status, body = self.call("POST", "/decide", {"model": "tev1", "state": {}, "questions": {}})
        self.assertEqual(status, 200)
        self.assertEqual(seen, [("/v1/systemone", {"model": "tev1", "state": {}, "questions": {}})])
        self.assertEqual(body["answers"], {"target": {"choice": "lane_1"}})
        self.assertGreaterEqual(body["_ollama_ms"], 0)


class NotFinite(WithServer):
    def test_nan_and_infinity_are_refused_with_400(self):
        for raw in (b'{"cells": {"avg": NaN}}', b'{"x": Infinity}', b'{"x": [-Infinity]}', b'{"x": 1e999}'):
            for path in ("/results", "/settings", "/queue"):
                with self.subTest(raw=raw, path=path):
                    status, body = self.call("POST", path, raw=raw)
                    self.assertEqual(status, 400)
                    self.assertIn("NaN", body["error"])
        self.assertEqual(self.call("GET", "/results"), (200, {"results": []}))
        self.assertEqual(self.call("GET", "/settings"), (200, {"settings": None}))

    def test_ordinary_numbers_still_pass(self):
        data = {"cells": {"avg": 171.9, "n": 200, "neg": -3, "exp": 1e3, "text": "NaN"}}
        self.assertEqual(self.call("POST", "/results", data)[0], 200)
        self.assertEqual(self.call("GET", "/results")[1]["results"][0]["data"], data)

    def test_broken_json_keeps_its_own_message(self):
        status, body = self.call("POST", "/results", raw=b"{nope")
        self.assertEqual((status, body["error"]), (400, "tělo požadavku není JSON"))


class PublicDatasets(unittest.TestCase):
    """Veřejné sady ve složce datasets/public/."""

    def serve(self, files=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "datasets").mkdir()
        (root / "datasets" / "a.json").write_text(json.dumps({"name": "Sada A"}), encoding="utf-8")
        (root / "secret.py").write_text("x = 1", encoding="utf-8")
        if files is not None:
            (root / "datasets" / "public").mkdir()
            for name, body in files.items():
                (root / "datasets" / "public" / name).write_bytes(body)
        old = server.ROOT
        self.addCleanup(setattr, server, "ROOT", old)
        server.ROOT = root
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.port = httpd.server_address[1]

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    FILES = {"banking77.json": b"\xef\xbb\xbf" + json.dumps({"name": "BANKING77 (vzorek)", "items": []}).encode(),
             "broken.json": b"{nope", "list.json": b"[1, 2]", "s mezerou.json": b"{}", "stav.txt": b"poznamky", "horizon.log": b"log"}

    def test_list_names_public_sets_and_skips_what_is_not_a_set(self):
        self.serve(self.FILES)
        status, body = self.get("/datasets")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"datasets": [{"file": "a.json", "name": "Sada A"}],
                                            "public": [{"file": "public/banking77.json", "name": "BANKING77 (vzorek)"},
                                                       {"file": "public/broken.json", "name": "broken"},
                                                       {"file": "public/list.json", "name": "list"}]})

    def test_without_the_folder_the_list_is_empty(self):
        self.serve()
        self.assertEqual(json.loads(self.get("/datasets")[1])["public"], [])
        self.assertEqual(self.get("/datasets/public/banking77.json")[0], 404)

    def test_file_is_served(self):
        self.serve(self.FILES)
        status, body = self.get("/datasets/public/banking77.json")
        self.assertEqual((status, json.loads(body.decode("utf-8-sig"))["name"]), (200, "BANKING77 (vzorek)"))
        self.assertEqual(self.get("/datasets/a.json")[0], 200)                                # dnešní sady beze změny

    def test_paths_outside_the_folder_are_404(self):
        self.serve(self.FILES)
        for path in ("/datasets/public/..%2f..%2fsecret.py", "/datasets/public/../a.json", "/datasets/public/../../secret.py",
                     "/datasets/public/stav.txt", "/datasets/public/horizon.log", "/datasets/public/s%20mezerou.json",
                     "/datasets/public/missing.json", "/datasets/public/", "/datasets/public", "/datasets/public/public/banking77.json"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)


JEV_KEY = "tajny-klic-JEV-7f3a9c"                    # poznatelné hodnoty: testy je hledají ve všem, co server pošle, vypíše nebo uloží
CF_ACCOUNT = "0123456789abcdef0123456789abcdef"
CF_TOKEN = "tajny-token-CF-55aa77"
SECRETS = {"TYPESAFE_API_KEY": JEV_KEY, "CLOUDFLARE_ACCOUNT_ID": CF_ACCOUNT, "CLOUDFLARE_API_TOKEN": CF_TOKEN}
QUESTION = {"move": {"type": "choice", "instructions": "Which lane?", "criteria": {"left": "move left", "stay": None}}}
ANSWER = {"model": "x", "answers": {"move": {"choice": "left", "confidence": 0.9}}, "usage": {"input_tokens": 120, "output_tokens": 1}}
REAL_TRANSPORT = providers.https_transport           # skutečný transport, než ho testy nahradí podstrčeným


def ask(model, **extra):
    return {"model": model, "state": {"lane": "clear"}, "questions": QUESTION, **extra}


class Hosted(WithServer):
    """Hostované modely na serveru. Síť je zakázaná: transport i zkušební spojení jsou podstrčené."""

    def setUp(self):
        super().setUp()
        self.sent, self.script, self.ollama_calls = [], [(200, {}, json.dumps(ANSWER).encode())], []
        self.patch(os.environ, None, env=SECRETS)
        self.patch(providers, "BUDGET", providers.Budget(env={}))
        self.patch(providers, "_sleep", lambda seconds: None)
        self.patch(providers, "https_transport", self.transport)
        self.patch(providers, "tls_connect", lambda host, timeout: self.sent.append(("connect", host)))
        self.patch(server, "ollama", self.ollama)
        self.local = ["tev1:latest", "nimble:9b-q4_K_M"]

    def patch(self, owner, name, value=None, env=None):
        patcher = mock.patch.dict(owner, env, clear=False) if env is not None else mock.patch.object(owner, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        if env is not None:
            for key in ("PICKBENCH_TEST_PROVIDER", "PICKBENCH_MAX_USD", "PICKBENCH_MAX_HOSTED", *SECRETS):
                if key not in env:
                    os.environ.pop(key, None)

    def no_keys(self, **env):
        self.patch(os.environ, None, env=env)

    def transport(self, host, path, headers, body, timeout):
        self.sent.append((host, path, dict(headers), json.loads(body)))
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, BaseException):
            raise step
        return step

    def ollama(self, path, payload=None, timeout=300):
        self.ollama_calls.append((path, payload))
        if self.local is None:
            raise ConnectionRefusedError("odmítnuto")
        if path == "/api/tags":
            return {"models": [{"name": name} for name in self.local]}
        if path == "/api/show":
            return {"capabilities": ["decision"]}
        return {"answers": {"move": {"choice": "stay"}}, "usage": {"input_tokens": 50}}

    def raw(self, method, path, body=None, headers=None):
        """Odpověď doslova: stavový řádek, hlavičky i tělo jako jeden text."""
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        c.request(method, path, body=None if body is None else json.dumps(body).encode(), headers={**self.lang_header(), **(headers or {})})
        r = c.getresponse()
        text = f"{r.status} {r.reason}\n{r.getheaders()}\n" + r.read().decode("utf-8", errors="replace")
        c.close()
        return text

    # ---------- /models ----------
    def test_models_without_keys_are_exactly_as_today(self):
        self.no_keys()
        self.assertEqual(self.call("GET", "/models"), (200, {"models": ["tev1:latest", "nimble:9b-q4_K_M"]}))
        self.local = None
        self.assertEqual(self.call("GET", "/models"), (502, {"error": "Ollama nedostupná: odmítnuto"}))

    def test_models_with_keys_add_the_hosted_list_and_keep_the_local_shape(self):
        status, body = self.call("GET", "/models")
        self.assertEqual((status, body["models"]), (200, ["tev1:latest", "nimble:9b-q4_K_M"]))    # pole jmen jako dnes
        self.assertEqual(body["hosted"], [
            {"name": "jev:jev-1.13.0", "provider": "jev", "label": "Jev", "who": "TypeSafe AI", "usd_per_m": 0.042},
            {"name": "cf:clef-flash", "provider": "cf", "label": "Cloudflare", "who": "Cloudflare Workers AI", "usd_per_m": 0.09},
            {"name": "cf:clef", "provider": "cf", "label": "Cloudflare", "who": "Cloudflare Workers AI", "usd_per_m": 0.24}])
        self.assertNotIn("ollama_error", body)

    def test_only_models_with_a_key_are_offered(self):
        self.no_keys(TYPESAFE_API_KEY=JEV_KEY, CLOUDFLARE_API_TOKEN=CF_TOKEN)                  # Cloudflare bez ID účtu
        self.assertEqual([m["name"] for m in self.call("GET", "/models")[1]["hosted"]], ["jev:jev-1.13.0"])

    def test_unreachable_ollama_does_not_hide_the_hosted_models(self):
        self.local = None
        status, body = self.call("GET", "/models")
        self.assertEqual((status, body["models"], len(body["hosted"]), body["ollama_error"]), (200, [], 3, "Ollama nedostupná: odmítnuto"))

    def test_local_model_named_like_a_hosted_one_is_not_offered(self):  # /decide by ho poslal k poskytovateli, ne Ollamě
        self.local = ["tev1:latest", "cf:mistni", "jev:latest"]
        self.assertEqual(self.call("GET", "/models")[1]["models"], ["tev1:latest"])

    def test_test_provider_is_offered_only_in_test_mode(self):
        self.no_keys()
        self.assertNotIn("hosted", self.call("GET", "/models")[1])
        self.no_keys(PICKBENCH_TEST_PROVIDER="1")
        self.assertEqual([m["name"] for m in self.call("GET", "/models")[1]["hosted"]], ["test:echo", "test:drahy"])

    # ---------- /decide ----------
    def test_decide_sends_a_hosted_model_to_its_provider_and_nothing_to_ollama(self):
        status, body = self.call("POST", "/decide", ask("jev:jev-1.13.0"))
        self.assertEqual(status, 200)
        self.assertEqual(self.sent, [("api.typesafe.ai", "/v1/systemone", {"Content-Type": "application/json", "Authorization": "Bearer " + JEV_KEY},
                                      {"model": "jev-1.13.0", "state": {"lane": "clear"}, "questions": QUESTION})])
        self.assertEqual(self.ollama_calls, [])
        self.assertEqual((body["answers"], body["usage"], body["_provider"], body["_retries"], body["_waited_ms"]),
                         (ANSWER["answers"], ANSWER["usage"], "jev", 0, 0.0))
        self.assertGreaterEqual(body["_ms"], 0)
        self.assertAlmostEqual(body["_usd"], 120 * 0.042 / 1_000_000)
        self.assertEqual((body["_spend"]["requests"], body["_spend"]["answered"], body["_spend"]["max_usd"]), (1, 1, 1.0))
        self.assertNotIn("_ollama_ms", body)
        self.assertNotIn("_model_ms", body)

    def test_decide_unwraps_cloudflare_and_passes_the_model_time_when_there_is_one(self):
        self.script = [(200, {"server-timing": "model;dur=41.5"}, json.dumps({"result": ANSWER, "success": True}).encode())]
        status, body = self.call("POST", "/decide", ask("cf:clef-flash"))
        self.assertEqual((status, body["answers"], body["_provider"], body["_model_ms"]), (200, ANSWER["answers"], "cf", 41.5))
        self.assertEqual(self.sent[0][:2], ("api.cloudflare.com", f"/client/v4/accounts/{CF_ACCOUNT}/ai/run/@cf/cloudflare/clef-flash"))

    def test_decide_keeps_local_models_on_ollama(self):
        for model in ("tev1:latest", "nimble:9b-q4_K_M", "test:echo"):       # test:echo bez testovacího režimu je obyčejné jméno
            with self.subTest(model=model):
                self.ollama_calls.clear()
                payload = {"model": model, "state": {}, "questions": {}}
                status, body = self.call("POST", "/decide", payload)
                self.assertEqual((status, self.ollama_calls, body["_provider"]), (200, [("/v1/systemone", payload)], "ollama"))
                self.assertEqual(body["_ms"], body["_ollama_ms"])
        self.assertEqual(self.sent, [])
        self.assertEqual(providers.BUDGET.state()["requests"], 0)

    def test_the_page_cannot_choose_the_address_or_the_model(self):
        for model in ("cf:cokoli", "cf:../../user/tokens", "cf:clef-flash/../../../user", "jev:https://evil.example/x", "jev:jev-latest", "cf:", "jev: "):
            with self.subTest(model=model):
                status, body = self.call("POST", "/decide", ask(model, url="https://evil.example", host="evil.example", path="/x"))
                self.assertEqual(status, 400)
                self.assertIn("není v seznamu hostovaných modelů", body["error"])
        self.assertEqual((self.sent, self.ollama_calls), ([], []))
        self.call("POST", "/decide", ask("cf:clef", url="https://evil.example", host="evil.example", path="/x", stream=True,
                                         headers={"Authorization": "x"}, account="jiny"))
        host, path, headers, body = self.sent[0]
        self.assertEqual((host, path), ("api.cloudflare.com", f"/client/v4/accounts/{CF_ACCOUNT}/ai/run/@cf/cloudflare/clef"))
        self.assertEqual(sorted(body), ["model", "questions", "state"])
        self.assertEqual(headers["Authorization"], "Bearer " + CF_TOKEN)

    def test_hosted_request_goes_out_whatever_set_it_names(self):     # pravidlo o sadách je zrušené; pole scenario ze staré stránky server ignoruje
        tries = ({}, {"scenario": "cfpb-complaints-300.json"}, {"scenario": "moje-sada.json"}, {"scenario": "../x"}, {"scenario": 5})
        for count, extra in enumerate(tries, 1):
            with self.subTest(extra=extra):
                status, body = self.call("POST", "/decide", ask("jev:jev-1.13.0", **extra))
                self.assertEqual((status, body["answers"]), (200, ANSWER["answers"]))
                self.assertEqual(len(self.sent), count)                  # dotaz odešel
                self.assertEqual(self.sent[-1][3], {"model": "jev-1.13.0", "state": {"lane": "clear"}, "questions": QUESTION})   # pole scenario k poskytovateli nejde
                self.assertEqual((providers.BUDGET.state()["requests"], body["_spend"]["requests"]), (count, count))

    def test_provider_errors_are_502_with_a_sentence(self):
        self.script = [(400, {}, b'{"error": "criteria must contain 2-26 candidates"}')]
        self.assertEqual(self.call("POST", "/decide", ask("jev:jev-1.13.0")),
                         (502, {"error": 'Jev 400: {"error": "criteria must contain 2-26 candidates"}', "code": "provider", "upstream_status": 400}))
        self.script = [(401, {}, b'{"error": "bad key"}')]
        self.assertEqual(self.call("POST", "/decide", ask("cf:clef")),
                         (502, {"error": "Cloudflare 401: klíč neplatí nebo nemá oprávnění (proměnné prostředí: CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN)",
                                "code": "provider", "upstream_status": 401}))
        self.script, self.sent = [(529, {}, b"overloaded")], []
        status, body = self.call("POST", "/decide", ask("jev:jev-1.13.0"))
        self.assertEqual((status, len(self.sent)), (502, 4))
        self.assertTrue(body["error"].startswith("Jev 529: overloaded"))

    def test_decide_errors_carry_a_code_and_the_upstream_status(self):      # stránka druh chyby pozná podle nich, ne podle věty
        with mock.patch.object(server, "ollama", side_effect=urllib.error.HTTPError("http://x", 400, "Bad Request", {}, io.BytesIO(b"too many"))):
            status, body = self.call("POST", "/decide", ask("tev1"))
        self.assertEqual((status, body), (502, {"error": "Ollama 400: too many", "code": "ollama", "upstream_status": 400}))
        with mock.patch.object(server, "ollama", side_effect=ConnectionRefusedError("odmítnuto")):
            status, body = self.call("POST", "/decide", ask("tev1"))
        self.assertEqual((status, body), (502, {"error": "Ollama: odmítnuto", "code": "ollama_unreachable", "upstream_status": None}))
        self.patch(providers, "BUDGET", providers.Budget(max_usd=1, max_requests=0, env={}))
        status, body = self.call("POST", "/decide", ask("jev:jev-1.13.0"))
        self.assertEqual((status, body["code"], body["upstream_status"], "_spend" in body), (429, "budget", None, True))
        self.no_keys()
        status, body = self.call("POST", "/decide", ask("jev:jev-1.13.0"))                    # dotaz se neposlal: chybí klíč
        self.assertEqual((status, body["code"], body["upstream_status"]), (400, "refused", None))

    def test_retries_are_reported_and_not_counted_into_the_time(self):
        self.script = [(429, {"retry-after": "2"}, b""), (200, {}, json.dumps(ANSWER).encode())]
        status, body = self.call("POST", "/decide", ask("jev:jev-1.13.0"))
        self.assertEqual((status, body["_retries"], len(self.sent)), (200, 1, 2))
        self.assertGreaterEqual(body["_waited_ms"], 2000)
        self.assertLess(body["_ms"], 1000)

    def test_missing_key_is_400_and_names_the_variable(self):
        self.no_keys(CLOUDFLARE_ACCOUNT_ID=CF_ACCOUNT)
        status, body = self.call("POST", "/decide", ask("cf:clef"))
        self.assertEqual(status, 400)
        self.assertIn("CLOUDFLARE_API_TOKEN", body["error"])
        self.assertEqual(self.sent, [])

    def test_program_fault_in_the_hosted_path_is_a_sentence_not_a_traceback(self):
        def broken(host, path, headers, body, timeout):
            raise ValueError(f"Invalid header value b'Bearer {JEV_KEY}'")        # tak mluví http.client o hlavičce, která se mu nelíbí
        self.patch(providers, "https_transport", broken)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status, body = self.call("POST", "/decide", ask("jev:jev-1.13.0"))
        self.assertEqual(status, 502)
        self.assertEqual(body["error"], "hostovaný model: ValueError: Invalid header value b'Bearer ***'")
        self.assertEqual(err.getvalue(), "")

    def test_hosted_requests_reuse_one_connection_although_each_runs_in_a_new_thread(self):
        made = []

        class Connection:                                                # náhrada http.client.HTTPSConnection: žádná síť
            def __init__(self, host, timeout=None):
                made.append(self)
                self.host, self.count = host, 0

            def request(self, method, path, body=None, headers=None):
                self.count += 1

            def getresponse(self):
                return mock.Mock(status=200, read=lambda: json.dumps(ANSWER).encode(), getheaders=lambda: [])

            def close(self):
                pass
        self.patch(providers, "https_transport", REAL_TRANSPORT)
        self.patch(http.client, "HTTPSConnection", Connection)
        providers._pool.clear()
        self.addCleanup(providers._pool.clear)
        for _ in range(4):                                               # každý požadavek obslouží server v novém vlákně
            self.assertEqual(self.call("POST", "/decide", ask("jev:jev-1.13.0"))[0], 200)
        self.assertEqual([(c.host, c.count) for c in made], [("api.typesafe.ai", 4)])       # jedno spojení pro všechny čtyři: latence nenese TLS handshake

    # ---------- strop útraty ----------
    def test_spend_reports_the_counter_and_the_limits(self):
        self.assertEqual(self.call("GET", "/spend"), (200, {"requests": 0, "answered": 0, "tokens": 0, "usd": 0.0, "retries": 0, "unpriced": 0,
                                                             "max_requests": 5000, "max_usd": 1.0}))
        self.call("POST", "/decide", ask("cf:clef"))
        status, body = self.call("GET", "/spend")
        self.assertEqual((body["requests"], body["answered"], body["tokens"]), (1, 1, 120))
        self.assertAlmostEqual(body["usd"], 120 * 0.24 / 1_000_000)

    def test_limit_refuses_with_429_and_sends_nothing_more(self):
        self.patch(providers, "BUDGET", providers.Budget(max_requests=2, env={}))
        for _ in range(2):
            self.assertEqual(self.call("POST", "/decide", ask("jev:jev-1.13.0"))[0], 200)
        status, body = self.call("POST", "/decide", ask("jev:jev-1.13.0"))
        self.assertEqual(status, 429)
        self.assertEqual(body["error"], "Jev: strop útraty — odesláno 2 hostovaných dotazů, limit je 2 (PICKBENCH_MAX_HOSTED); další dotaz se neposlal")
        self.assertEqual((body["_spend"]["requests"], len(self.sent)), (2, 2))
        self.assertEqual(self.call("POST", "/decide", ask("cf:clef"))[0], 429)                 # jedno počítadlo pro všechny poskytovatele
        self.assertEqual(self.call("POST", "/decide", {"model": "tev1:latest", "state": {}, "questions": {}})[0], 200)   # lokální modely strop nezastaví

    def test_the_page_cannot_switch_the_limit_off_or_raise_it(self):
        self.patch(providers, "BUDGET", providers.Budget(max_requests=1, max_usd=0.5, env={}))
        self.call("POST", "/decide", ask("jev:jev-1.13.0"))
        tries = (("POST", "/spend", {"max_requests": 100000, "max_usd": 1000}), ("DELETE", "/spend", None), ("GET", "/spend?max_usd=1000", None),
                 ("POST", "/settings", {"max_usd": 1000, "PICKBENCH_MAX_USD": "1000", "PICKBENCH_MAX_HOSTED": "100000"}),
                 ("POST", "/decide", ask("jev:jev-1.13.0", max_usd=1000, max_requests=100000, budget=None, _spend={"max_requests": 100000})))
        for method, path, body in tries:
            with self.subTest(path=path, method=method):
                self.call(method, path, body)
        state = self.call("GET", "/spend")[1]
        self.assertEqual((state["max_requests"], state["max_usd"], state["requests"], len(self.sent)), (1, 0.5, 1, 1))
        self.assertEqual(self.call("POST", "/decide", ask("jev:jev-1.13.0"))[0], 429)

    def test_dollar_limit_with_the_test_provider(self):
        self.no_keys(PICKBENCH_TEST_PROVIDER="1")
        self.patch(providers, "https_transport", mock.Mock(side_effect=AssertionError("síť")))
        codes = [self.call("POST", "/decide", ask("test:drahy"))[0] for _ in range(40)]
        self.assertEqual(sorted(set(codes)), [200, 429])
        self.assertLess(codes.index(429), 30)                            # výchozí strop 1 $, test:drahy stojí 1000 $ za milion tokenů
        self.assertEqual(codes[codes.index(429):], [429] * (40 - codes.index(429)))
        self.assertGreaterEqual(self.call("GET", "/spend")[1]["usd"], 1.0)

    # ---------- /unload, /net ----------
    def test_unload_of_a_hosted_model_succeeds_without_calling_ollama(self):
        for model in ("jev:jev-1.13.0", "cf:clef", "cf:cokoli"):
            self.assertEqual(self.call("POST", "/unload", {"model": model}), (200, {"unloaded": model}))
        self.assertEqual(self.ollama_calls, [])
        self.call("POST", "/unload", {"model": "tev1:latest"})
        self.assertEqual(self.ollama_calls, [("/api/generate", {"model": "tev1:latest", "keep_alive": 0, "stream": False})])

    def test_net_estimate(self):
        status, body = self.call("POST", "/net", {"model": "cf:clef"})
        self.assertEqual((status, body["provider"], self.sent), (200, "cf", [("connect", "api.cloudflare.com")]))
        self.assertGreaterEqual(body["connect_ms"], 0)
        self.assertEqual(self.call("POST", "/net", {"model": "tev1:latest"}), (400, {"error": "odhad sítě je jen pro hostované modely"}))
        self.assertEqual(self.call("POST", "/net", {})[0], 400)
        self.assertEqual(self.call("POST", "/net", {"model": "cf:cokoli"})[0], 400)
        self.assertEqual(self.call("GET", "/net")[0], 404)

        def refuse(host, timeout):
            raise OSError(f"no route to {host} for {CF_TOKEN}")
        self.patch(providers, "tls_connect", refuse)
        self.assertEqual(self.call("POST", "/net", {"model": "cf:clef"}),
                         (502, {"error": "Cloudflare: spojení se serverem api.cloudflare.com se nepodařilo (OSError)"}))

    def test_net_estimate_answers_with_the_same_sentence_when_the_tls_context_cannot_be_built(self):
        def broken():
            raise ssl.SSLError(f"nelze načíst certifikáty pro {CF_TOKEN}")
        self.patch(providers, "_tls_context", None)                      # mezipaměť kontextu se po testu vrátí; skutečný kontext ani spojení se nestaví
        self.patch(ssl, "create_default_context", broken)
        self.assertEqual(self.call("POST", "/net", {"model": "cf:clef"}),
                         (502, {"error": "Cloudflare: spojení se serverem api.cloudflare.com se nepodařilo (SSLError)"}))      # jako u refuse() v test_net_estimate
        self.assertEqual(self.sent, [])

    # ---------- brána Host / Origin ----------
    def test_another_web_page_cannot_spend_money(self):
        port = self.httpd.server_address[1]
        for headers in ({"Origin": "http://evil.example"}, {"Host": f"evil.example:{port}", "Origin": f"http://evil.example:{port}"}, {"Origin": "null"}):
            for method, path, body in (("POST", "/decide", ask("jev:jev-1.13.0")), ("POST", "/net", {"model": "cf:clef"}),
                                       ("GET", "/spend", None), ("GET", "/models", None)):
                with self.subTest(headers=headers, path=path):
                    self.assertEqual(self.call(method, path, body, headers=headers), (403, {"error": "požadavek z cizí stránky"}))
        self.assertEqual((self.sent, providers.BUDGET.state()["requests"]), ([], 0))

    # ---------- klíče ----------
    def test_no_response_log_or_stored_row_ever_carries_a_key(self):
        leak = f"denied for {JEV_KEY} on account {CF_ACCOUNT} with {CF_TOKEN}"
        steps = [(200, {}, json.dumps(ANSWER).encode()), (200, {}, json.dumps({"result": ANSWER}).encode()), (200, {}, leak.encode()),
                 (200, {}, json.dumps({"success": False, "errors": [{"message": leak}]}).encode()),
                 (400, {}, leak.encode()), (401, {}, leak.encode()), (403, {}, leak.encode()), (422, {}, leak.encode()),
                 (429, {"retry-after": "9999"}, leak.encode()), (500, {}, leak.encode()), (529, {}, leak.encode()),
                 ConnectionResetError(leak), TimeoutError(leak), OSError(leak), ValueError(leak), RuntimeError(leak)]
        seen, err, out = [], io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            for step in steps:
                for model in ("jev:jev-1.13.0", "cf:clef-flash", "cf:clef"):
                    self.script = [step]
                    seen.append(self.raw("POST", "/decide", ask(model)))
            self.patch(providers, "tls_connect", mock.Mock(side_effect=OSError(leak)))
            for method, path, body in (("GET", "/", None), ("GET", "/models", None), ("GET", "/spend", None),
                                       ("GET", "/datasets", None), ("GET", "/nic", None), ("POST", "/nic", {}),
                                       ("POST", "/net", {"model": "jev:jev-1.13.0"}), ("POST", "/net", {"model": "cf:clef"}),
                                       ("POST", "/unload", {"model": "cf:clef"}), ("POST", "/decide", ask("cf:cokoli")),
                                       ("POST", "/results", {"cells": {"driver": "jev:jev-1.13.0"}}), ("POST", "/settings", {"model": "cf:clef"}),
                                       ("POST", "/queue", {"batch": 1, "steps": []}),
                                       ("GET", "/results", None), ("GET", "/settings", None), ("GET", "/queue", None)):
                seen.append(self.raw(method, path, body))
            self.patch(providers, "BUDGET", providers.Budget(max_requests=0, env={}))
            seen.append(self.raw("POST", "/decide", ask("jev:jev-1.13.0")))
        self.assertGreater(len(seen), 60)
        self.assertTrue(any(text.startswith("200 ") for text in seen) and any(text.startswith("502 ") for text in seen)
                        and any(text.startswith("429 ") for text in seen) and any(text.startswith("400 ") for text in seen))
        stored = self.db.read_bytes().decode("utf-8", errors="replace")
        for name, secret in SECRETS.items():
            for where, text in (("odpověď", "\n".join(seen)), ("stderr", err.getvalue()), ("stdout", out.getvalue()), ("databáze", stored)):
                with self.subTest(name=name, where=where):
                    self.assertNotIn(secret, text)
                    self.assertNotIn(secret[:12], text)                  # ani začátek klíče


CZECH = re.compile(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„“]")
OFFERED = "jev:jev-1.13.0, cf:clef-flash, cf:clef"                      # nabídka s klíči ze SECRETS
EN = {i18n.HEADER: "en"}
BAD_KEY = {"cs": "Jev 401: klíč neplatí nebo nemá oprávnění (proměnné prostředí: TYPESAFE_API_KEY)",
           "en": "Jev 401: the key is not valid or lacks permission (environment variables: TYPESAFE_API_KEY)"}
TEST_WHO = {"cs": "zkušební poskytovatel (nic neodchází)", "en": "test provider (nothing leaves this computer)"}


def keep_tests(*inherited):
    """Podtřída si nechá přípravu rodiče (setUp, podstrčený transport), ale z jeho testů jen jmenované: ostatní by běžely podruhé."""
    def trim(cls):
        for name in dir(cls):
            if name.startswith("test_") and name not in vars(cls) and name not in inherited:
                setattr(cls, name, None)
        return cls
    return trim


def no_ollama(path, payload=None, timeout=300):
    """Podstrčená Ollama, která neběží; hláška systému bez českého textu."""
    raise ConnectionRefusedError("connection refused")


def full_ollama(path, payload=None, timeout=300):
    """Podstrčená Ollama, která dotaz odmítne stavem 400 (tělo odpovědi jde přečíst jen jednou, proto pokaždé nová chyba)."""
    raise urllib.error.HTTPError("http://x", 400, "Bad Request", {}, io.BytesIO(b"too many"))


@keep_tests()
class Language(Hosted):
    """Jazyk požadavku: hlavička X-PickBench-Lang: en dá anglickou větu; bez hlavičky (i s neznámou hodnotou) přijde česká, doslova jako před překladem."""

    def both(self, method, path, status, czech, english, body=None, raw=None, headers=None, **fields):
        """Pošle požadavek bez hlavičky jazyka, s neznámou, prázdnou a českou hodnotou a anglicky. english=None: věta je cizí text
        (tělo odpovědi poskytovatele nebo Ollamy) a musí být v obou jazycích stejná. Ostatní pole odpovědi (fields) na jazyce nezávisí."""
        ask = lambda lang=None: self.call(method, path, body, raw, {**(headers or {}), **({} if lang is None else {i18n.HEADER: lang})})
        want = (status, {"error": czech, **fields})
        for lang in (None, "de", "", "cs"):
            self.assertEqual(ask(lang), want, lang)
        got = ask("en")
        self.assertEqual(ask("EN"), got)                                 # hodnota hlavičky se čte bez ohledu na velikost písmen
        self.assertEqual(ask(), want)                                    # po anglickém požadavku další bez hlavičky zase česky
        self.assertEqual((got[0], {**got[1], "error": czech}), want)     # stav HTTP, code, upstream_status a ostatní pole jsou stejná
        self.assertEqual(got[1]["error"], czech if english is None else english)
        self.assertEqual(CZECH.findall(got[1]["error"].replace(str(self.db), "")), [])      # cesta k databázi je cizí text
        if english is not None:                                          # ani český úlomek bez diakritiky, který by kontrola diakritikou nenašla
            self.assertNotEqual(english, czech)

    def test_results_and_the_database(self):
        self.both("DELETE", "/results/0", 400, "číslo výsledku musí být kladné celé číslo", "the result number must be a positive integer")
        self.both("DELETE", "/results/7", 404, "výsledek č. 7 v databázi není", "result no. 7 is not in the database")
        self.db.write_bytes(b"toto neni databaze " * 200)
        cs = f"Databáze {self.db} nejde použít (file is not a database); výsledky, nastavení a fronta se neukládají."
        en = f"The server cannot use the database {self.db} (file is not a database); it does not save the results, the settings or the queue."
        for method, path, body in (("GET", "/results", None), ("POST", "/results", {"a": 1}), ("DELETE", "/results", None),
                                   ("DELETE", "/results/1", None), ("GET", "/queue", None), ("POST", "/settings", {})):
            with self.subTest(method=method, path=path):
                self.both(method, path, 503, cs, en, body=body)

    def test_unload_and_net(self):
        self.both("POST", "/unload", 400, "chybí jméno modelu", "the model name is missing", body={})
        self.both("POST", "/net", 400, "odhad sítě je jen pro hostované modely", "the network estimate is only for hosted models", body={"model": "tev1:latest"})
        self.both("POST", "/net", 400, f"Cloudflare: model „cf:cokoli“ není v seznamu hostovaných modelů ({OFFERED})",
                  f'Cloudflare: the model "cf:cokoli" is not in the list of hosted models ({OFFERED})', body={"model": "cf:cokoli"})
        self.patch(providers, "tls_connect", mock.Mock(side_effect=OSError("no route")))
        self.both("POST", "/net", 502, "Cloudflare: spojení se serverem api.cloudflare.com se nepodařilo (OSError)",
                  "Cloudflare: the connection to the server api.cloudflare.com failed (OSError)", body={"model": "cf:clef"})

    def test_request_bodies(self):
        cases = ((b"", "tělo požadavku chybí", "the HTTP request body is missing"),
                 (b" " * (server.MAX_BODY + 1), f"tělo požadavku je větší než {server.MAX_BODY} bajtů",
                  f"the HTTP request body is larger than {server.MAX_BODY} bytes"),
                 (b"{nope", "tělo požadavku není JSON", "the HTTP request body is not JSON"),
                 (b'{"a": "\xff"}', "tělo požadavku není JSON", "the HTTP request body is not JSON"),
                 (b'{"x": NaN}', "tělo požadavku obsahuje NaN nebo Infinity; ty v JSON být nesmí",
                  "the HTTP request body contains NaN or Infinity; JSON does not allow them"),
                 (b"[1, 2]", "tělo požadavku musí být objekt JSON", "the HTTP request body must be a JSON object"))
        for raw, cs, en in cases:
            for path in ("/settings", "/net")[:1 if len(raw) > 100 else 2]:
                with self.subTest(raw=raw[:12], path=path):
                    self.both("POST", path, 400, cs, en, raw=raw)
        self.assertEqual(self.call("GET", "/settings"), (200, {"settings": None}))

    def test_another_web_page(self):
        for method, path in (("GET", "/results"), ("POST", "/decide"), ("DELETE", "/results/1")):
            with self.subTest(method=method, path=path):
                self.both(method, path, 403, "požadavek z cizí stránky", "the HTTP request came from another web page",
                          body={"model": "x"} if method == "POST" else None, headers={"Origin": "http://evil.example"})

    def test_not_found_is_english_in_both_languages_as_before(self):
        for method, path in (("GET", "/nic"), ("POST", "/nic"), ("DELETE", "/settings")):
            with self.subTest(method=method, path=path):
                self.both(method, path, 404, "not found", None, body={} if method == "POST" else None)

    def test_models_without_ollama(self):
        self.patch(server, "ollama", no_ollama)
        status, body = self.call("GET", "/models")                       # s klíči: nabídka hostovaných modelů zůstává a věta jde vedle ní
        self.assertEqual((status, body["ollama_error"], len(body["hosted"])), (200, "Ollama nedostupná: connection refused", 3))
        self.assertEqual(self.call("GET", "/models", headers=EN), (200, {**body, "ollama_error": "Ollama unreachable: connection refused"}))
        self.no_keys()
        self.both("GET", "/models", 502, "Ollama nedostupná: connection refused", "Ollama unreachable: connection refused")

    def test_models_describe_the_test_provider_in_the_language_of_the_request(self):
        hosted = lambda lang=None: self.call("GET", "/models", headers={} if lang is None else {i18n.HEADER: lang})[1]["hosted"]
        real = hosted()
        self.assertEqual([m["who"] for m in real], ["TypeSafe AI", "Cloudflare Workers AI", "Cloudflare Workers AI"])
        self.assertEqual(hosted("en"), real)                             # Jev a Cloudflare jsou vlastní jména
        self.no_keys(PICKBENCH_TEST_PROVIDER="1")
        cs, en, again, unknown = hosted(), hosted("en"), hosted(), hosted("de")
        self.assertEqual([m["who"] for m in cs], [TEST_WHO["cs"]] * 2)
        self.assertEqual([m["who"] for m in en], [TEST_WHO["en"]] * 2)
        self.assertEqual((again, unknown), (cs, cs))                     # po anglickém dotazu dotaz bez hlavičky zase česky
        self.assertEqual([{**m, "who": None} for m in en], [{**m, "who": None} for m in cs])      # jména, ceny a popisky se nemění

    def test_decide_requests_that_are_not_sent(self):
        self.patch(providers, "BUDGET", providers.Budget(max_requests=0, env={}))
        self.both("POST", "/decide", 429, "Jev: strop útraty — odesláno 0 hostovaných dotazů, limit je 0 (PICKBENCH_MAX_HOSTED); další dotaz se neposlal",
                  "Jev: spend cap — 0 hosted requests sent, the cap is 0 (PICKBENCH_MAX_HOSTED); PickBench did not send the next request",
                  body=ask("jev:jev-1.13.0"), code="budget", upstream_status=None, _spend=providers.BUDGET.state())
        self.patch(providers, "BUDGET", providers.Budget(max_usd=0, env={}))
        self.both("POST", "/decide", 429, "Cloudflare: strop útraty — utraceno 0.0000 $, limit je 0 $ (PICKBENCH_MAX_USD); další dotaz se neposlal",
                  "Cloudflare: spend cap — $0.0000 spent, the cap is $0 (PICKBENCH_MAX_USD); PickBench did not send the next request",
                  body=ask("cf:clef"), code="budget", upstream_status=None, _spend=providers.BUDGET.state())
        self.patch(providers, "BUDGET", providers.Budget(env={}))
        big = ask("jev:jev-1.13.0", state={"text": "x" * providers.MAX_BODY})
        size = len(json.dumps({"model": "jev-1.13.0", "state": big["state"], "questions": QUESTION}).encode())
        self.both("POST", "/decide", 400, f"Jev: dotaz má {size} bajtů; hostovanému modelu se posílá nejvýš 64000",
                  f"Jev: the request is {size} bytes long; PickBench sends at most 64000 to a hosted model", body=big, code="refused", upstream_status=None)
        self.both("POST", "/decide", 400, f"Cloudflare: model „cf:cokoli“ není v seznamu hostovaných modelů ({OFFERED})",
                  f'Cloudflare: the model "cf:cokoli" is not in the list of hosted models ({OFFERED})', body=ask("cf:cokoli"), code="refused", upstream_status=None)
        self.no_keys(CLOUDFLARE_ACCOUNT_ID=CF_ACCOUNT)
        self.both("POST", "/decide", 400, "Cloudflare: model cf:clef potřebuje proměnnou prostředí CLOUDFLARE_API_TOKEN, která není nastavená",
                  "Cloudflare: the model cf:clef needs the environment variable CLOUDFLARE_API_TOKEN, which is not set",
                  body=ask("cf:clef"), code="refused", upstream_status=None)
        self.no_keys(TYPESAFE_API_KEY="a b")
        self.both("POST", "/decide", 400, "Jev: proměnná prostředí TYPESAFE_API_KEY obsahuje znaky, které v ní být nesmí (mezera, konec řádku, znak mimo ASCII)",
                  "Jev: the environment variable TYPESAFE_API_KEY contains characters that it must not contain (a space, a line break, a non-ASCII character)",
                  body=ask("jev:jev-1.13.0"), code="refused", upstream_status=None)
        self.assertEqual((self.sent, self.ollama_calls), ([], []))

    def test_decide_provider_errors(self):
        jev, envelope = "jev:jev-1.13.0", json.dumps({"success": False, "errors": [{"message": "No such model"}]}).encode()
        cases = (([(401, {}, b'{"error": "bad key"}')], jev, 401, BAD_KEY["cs"], BAD_KEY["en"]),
                 ([TimeoutError("timed out")], jev, None, "Jev: poskytovatel neodpověděl do 30 s", "Jev: the provider did not answer within 30 s"),
                 ([(529, {}, b"overloaded")], jev, 529, "Jev 529: overloaded (po 4 pokusech)", "Jev 529: overloaded (after 4 attempts)"),
                 ([ConnectionResetError("reset")], jev, None, "Jev: spojení se nepodařilo po 4 pokusech (ConnectionResetError: reset)",
                  "Jev: the connection failed after 4 attempts (ConnectionResetError: reset)"),
                 ([(429, {"retry-after": "9999"}, b"")], jev, 429, "Jev 429: poskytovatel žádá počkat 9999 s (Retry-After), víc než 60 s; dotaz se neopakuje",
                  "Jev 429: the provider asks to wait 9999 s (Retry-After), more than 60 s; PickBench does not retry the request"),
                 ([(200, {}, b"<html>gateway</html>")], jev, 200, "Jev 200: odpověď není objekt JSON: <html>gateway</html>",
                  "Jev 200: the answer is not a JSON object: <html>gateway</html>"),
                 ([(200, {}, envelope)], "cf:clef", 200, 'Cloudflare 200: poskytovatel hlásí chybu: [{"message": "No such model"}]',
                  'Cloudflare 200: the provider reports an error: [{"message": "No such model"}]'),
                 ([(400, {}, b'{"error": "criteria must contain 2-26 candidates"}')], jev, 400,      # tělo odpovědi poskytovatele je cizí text
                  'Jev 400: {"error": "criteria must contain 2-26 candidates"}', None))
        for script, model, upstream, cs, en in cases:
            with self.subTest(cs=cs):
                self.script = script
                self.both("POST", "/decide", 502, cs, en, body=ask(model), code="provider", upstream_status=upstream)

    def test_decide_program_fault_and_ollama(self):
        def broken(host, path, headers, body, timeout):
            raise ValueError("Invalid header value {type} {error} {")       # složené závorky v cizím textu: věta se doplňuje jedním průchodem
        self.patch(providers, "https_transport", broken)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):                              # chyba programu má kód internal a žádný stav poskytovatele
            self.both("POST", "/decide", 502, "hostovaný model: ValueError: Invalid header value {type} {error} {",
                      "hosted model: ValueError: Invalid header value {type} {error} {", body=ask("jev:jev-1.13.0"), code="internal", upstream_status=None)
        self.assertEqual(err.getvalue(), "")
        self.patch(server, "ollama", full_ollama)                          # věta od Ollamy je cizí text: v obou jazycích stejná
        self.both("POST", "/decide", 502, "Ollama 400: too many", None, body=ask("tev1"), code="ollama", upstream_status=400)
        self.both("POST", "/unload", 502, "Ollama 400: too many", None, body={"model": "tev1"})
        self.patch(server, "ollama", no_ollama)
        self.both("POST", "/decide", 502, "Ollama: connection refused", None, body=ask("tev1"), code="ollama_unreachable", upstream_status=None)
        self.both("POST", "/unload", 502, "Ollama: connection refused", None, body={"model": "tev1"})

    def test_console_language_does_not_change_what_a_client_without_the_header_gets(self):
        with mock.patch.dict(os.environ, {i18n.ENV: "en"}):                # PICKBENCH_LANG řídí jen výpisy serveru do konzole
            self.assertEqual(i18n.lang(), "en")
            self.both("POST", "/unload", 400, "chybí jméno modelu", "the model name is missing", body={})
            self.script = [(401, {}, b"")]
            self.both("POST", "/decide", 502, BAD_KEY["cs"], BAD_KEY["en"], body=ask("jev:jev-1.13.0"), code="provider", upstream_status=401)

    # ---------- souběh ----------
    def together(self, lang_of, request):
        """Dva klienti naráz, každý 20 požadavků: jeden s hlavičkou en, druhý bez ní. Vrací odpovědi {jazyk: [...]}."""
        got = {lang: [] for lang in lang_of}

        def client(lang):
            for _ in range(20):
                got[lang].append(request({} if lang is None else {i18n.HEADER: lang}))
        threads = [threading.Thread(target=client, args=(lang,)) for lang in lang_of]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return got

    def test_two_requests_at_once_in_two_languages_get_the_provider_sentence_each_in_its_own(self):
        barrier = threading.Barrier(2, timeout=10)                         # bariéra je uvnitř serveru: oba požadavky jsou v obsluze naráz

        def both_inside(host, path, headers, body, timeout):
            barrier.wait()                                                 # věta chyby se skládá až potom (providers.post)
            return 401, {}, b""
        self.patch(providers, "https_transport", both_inside)
        got = self.together((None, "en"), lambda headers: self.call("POST", "/decide", ask("jev:jev-1.13.0"), headers=headers))
        for lang, code in ((None, "cs"), ("en", "en")):
            self.assertEqual(got[lang], [(502, {"error": BAD_KEY[code], "code": "provider", "upstream_status": 401})] * 20, code)

    def test_two_requests_at_once_in_two_languages_get_the_server_sentence_each_in_its_own(self):
        barrier = threading.Barrier(2, timeout=10)

        def both_inside(path, payload=None, timeout=300):
            barrier.wait()                                                 # věta chyby se skládá až potom (server._models)
            raise ConnectionRefusedError("connection refused")
        self.no_keys()
        self.patch(server, "ollama", both_inside)
        got = self.together((None, "en"), lambda headers: self.call("GET", "/models", headers=headers))
        self.assertEqual(got[None], [(502, {"error": "Ollama nedostupná: connection refused"})] * 20)
        self.assertEqual(got["en"], [(502, {"error": "Ollama unreachable: connection refused"})] * 20)

    # ---------- klíče ----------
    def test_program_fault_is_cleaned_before_it_is_cut_in_both_languages(self):      # řez uprostřed klíče by nechal jeho začátek, který už scrub nepozná
        text = [""]

        def broken(host, path, headers, body, timeout):
            raise ValueError(text[0])
        self.patch(providers, "https_transport", broken)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for headers, start in (({}, "hostovaný model: ValueError: "), (EN, "hosted model: ValueError: ")):      # úvod věty je v každém jazyce jinak dlouhý
                for pad in range(300 - len(start) - len(JEV_KEY) - 1, 300 - len(start) + 2):
                    with self.subTest(start=start, pad=pad):
                        text[0] = "x" * pad + JEV_KEY + " end"
                        raw = self.raw("POST", "/decide", ask("jev:jev-1.13.0"), headers=headers)
                        error = json.loads(raw.split("\n", 2)[2])["error"]
                        self.assertTrue(error.startswith(start + "xxx"), error[:40])
                        self.assertLessEqual(len(error), 300)
                        self.assertNotIn(JEV_KEY[:4], raw)
        self.assertEqual(err.getvalue(), "")

    def test_program_fault_in_the_hosted_path_is_an_english_sentence_not_a_traceback(self):      # anglické dvojče testu třídy Hosted
        def broken(host, path, headers, body, timeout):
            raise ValueError(f"Invalid header value b'Bearer {JEV_KEY}'")
        self.patch(providers, "https_transport", broken)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status, body = self.call("POST", "/decide", ask("jev:jev-1.13.0"), headers=EN)
        self.assertEqual((status, body), (502, {"error": "hosted model: ValueError: Invalid header value b'Bearer ***'", "code": "internal",
                                                "upstream_status": None}))
        self.assertEqual(err.getvalue(), "")

    def test_net_estimate_answers_in_english_without_a_key(self):          # anglická dvojčata testů odhadu sítě třídy Hosted
        def refuse(host, timeout):
            raise OSError(f"no route to {host} for {CF_TOKEN}")
        self.patch(providers, "tls_connect", refuse)
        self.assertEqual(self.call("POST", "/net", {"model": "cf:clef"}, headers=EN),
                         (502, {"error": "Cloudflare: the connection to the server api.cloudflare.com failed (OSError)"}))

        def broken():
            raise ssl.SSLError(f"cannot load the certificates for {CF_TOKEN}")
        self.patch(providers, "_tls_context", None)
        self.patch(ssl, "create_default_context", broken)
        self.assertEqual(self.call("POST", "/net", {"model": "cf:clef"}, headers=EN),
                         (502, {"error": "Cloudflare: the connection to the server api.cloudflare.com failed (SSLError)"}))
        self.assertEqual(self.sent, [])


@keep_tests("test_no_response_log_or_stored_row_ever_carries_a_key")
class KeysInEnglish(Hosted):
    """Test úniku klíče, který nejde pustit dvakrát v jedné přípravě (vyčerpá strop útraty), podruhé: každý požadavek nese hlavičku X-PickBench-Lang: en."""

    LANG = "en"

    def test_every_request_of_this_class_is_answered_in_english(self):
        self.assertEqual(unittest.TestLoader().getTestCaseNames(type(self)),      # zděděný test úniku tu opravdu běží (po přejmenování v rodiči by tiše zmizel)
                         ["test_every_request_of_this_class_is_answered_in_english", "test_no_response_log_or_stored_row_ever_carries_a_key"])
        self.assertEqual(self.call("POST", "/unload", {}), (400, {"error": "the model name is missing"}))
        self.script = [(401, {}, b"")]
        self.assertIn(BAD_KEY["en"], self.raw("POST", "/decide", ask("jev:jev-1.13.0")))


class Banner(unittest.TestCase):
    """Řádky, které server vypíše při startu: jazyk procesu (PICKBENCH_LANG), klíče nikdy."""

    def lines(self, lang=None, **env):
        """server.banner() s danými proměnnými prostředí (klíče a zkušební poskytovatel jen ty jmenované) a s čistým počítadlem útraty."""
        with mock.patch.dict(os.environ, env), mock.patch.object(providers, "BUDGET", providers.Budget(env={})):
            for key in ("PICKBENCH_TEST_PROVIDER", i18n.ENV, *SECRETS):
                if key not in env:
                    os.environ.pop(key, None)
            if lang is not None:
                os.environ[i18n.ENV] = lang
            return server.banner()

    def test_czech_lines_are_as_before(self):
        first = f"PickBench běží na http://127.0.0.1:{server.PORT}  (Ollama: {server.OLLAMA}, databáze: {server.STORE.path})  — Ctrl+C ukončí"
        hosted = "Hostované modely: jev:jev-1.13.0, cf:clef-flash, cf:clef — strop útraty 1 $ a 5000 dotazů na běh serveru (PICKBENCH_MAX_USD, PICKBENCH_MAX_HOSTED)"
        self.assertEqual(self.lines(), [first])                            # bez klíčů se hostované modely nevypisují
        self.assertEqual(self.lines(**SECRETS), [first, hosted])
        self.assertEqual(self.lines("cs", **SECRETS), [first, hosted])
        self.assertEqual(self.lines(PICKBENCH_TEST_PROVIDER="1")[1].split(" — ")[0], "Hostované modely: test:echo, test:drahy")

    def test_english_lines_follow_the_environment(self):
        first = f"PickBench is running at http://127.0.0.1:{server.PORT}  (Ollama: {server.OLLAMA}, database: {server.STORE.path})  — Ctrl+C stops it"
        hosted = "Hosted models: jev:jev-1.13.0, cf:clef-flash, cf:clef — spend cap $1 and 5000 requests per server run (PICKBENCH_MAX_USD, PICKBENCH_MAX_HOSTED)"
        self.assertEqual(self.lines("en"), [first])
        self.assertEqual(self.lines(" EN ", **SECRETS), [first, hosted])
        self.assertEqual(CZECH.findall(hosted + first.replace(server.STORE.path, "")), [])

    def test_no_line_carries_a_key(self):
        for lang in ("cs", "en"):
            text = "\n".join(self.lines(lang, PICKBENCH_TEST_PROVIDER="1", **SECRETS))
            self.assertIn("jev:jev-1.13.0, cf:clef-flash, cf:clef, test:echo, test:drahy", text)
            for name, secret in SECRETS.items():
                with self.subTest(lang=lang, name=name):
                    self.assertNotIn(secret, text)
                    self.assertNotIn(secret[:12], text)

    def test_unknown_language_ends_with_a_sentence_before_the_port_opens(self):
        with self.assertRaises(SystemExit) as ctx:
            self.lines("xx")
        self.assertEqual(ctx.exception.code, "Neznámý jazyk „xx“: --lang a PICKBENCH_LANG berou cs nebo en. / "
                                             'Unknown language "xx": --lang and PICKBENCH_LANG accept cs or en.')      # věta = nenulový kód ukončení
        main = Path(server.__file__).read_text(encoding="utf-8").split('if __name__ == "__main__":')[1]
        self.assertLess(main.index("banner()"), main.index("ThreadingHTTPServer("))      # výpis (a s ním kontrola jazyka) stojí před otevřením portu


if __name__ == "__main__":
    unittest.main()
