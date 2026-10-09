import ast
import contextlib
import csv
import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
import bench
import hwbench
import i18n
import providers

LANG_ENV = "PICKBENCH_LANG"
CZECH = re.compile(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„“]")


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní a po něm vrátí."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(i18n.ENV, None)


class FakePost:
    """Náhrada bench.post: pamatuje si dotazy, latence bere z řady ms (poslední hodnota se opakuje)."""

    def __init__(self, ms=(10.0,), tokens=50, fail=None):
        self.calls, self.ms, self.tokens, self.fail = [], list(ms), tokens, fail

    def __call__(self, model, payload):
        self.calls.append(payload)
        if self.fail:
            self.fail(payload)
        ms = self.ms[min(len(self.calls) - 1, len(self.ms) - 1)]
        words = len(payload["state"].get("background", "").split())
        return {"answers": {k: {"choice": "lane_1", "confidence": 0.9} for k in payload["questions"]},
                "usage": {"input_tokens": self.tokens + words}}, ms


class FakeClock:
    """Hodiny, které při každém čtení poskočí o step sekund."""

    def __init__(self, step):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


class FakeSampler:
    def __init__(self, samples):
        self.samples, self.started = samples, False

    def start(self):
        self.started = True

    def stop(self):
        return self.samples


class BrokenFile:
    """Soubor CSV, kterému selže zápis (fail="write"), nebo až vyprázdnění vyrovnávací paměti (fail="flush") — plný disk."""

    def __init__(self, fail):
        self.fail = fail

    def write(self, text):
        if self.fail == "write":
            raise OSError(28, "No space left on device")

    def flush(self):
        if self.fail == "flush":
            raise OSError(28, "No space left on device")

    def close(self):
        pass


class Helpers(unittest.TestCase):
    def test_percentile_matches_the_game(self):
        values = list(range(1, 101))
        self.assertEqual([hwbench.percentile(values, p) for p in (.5, .9, .95, .99)], [51, 91, 96, 100])
        self.assertEqual(hwbench.percentile([7], .99), 7)
        self.assertTrue(hwbench.percentile([], .5) != hwbench.percentile([], .5))

    def test_payloads_are_the_bench_cases(self):
        p = hwbench.payloads(5)
        self.assertEqual(len(p), 5)
        self.assertEqual(p[0]["questions"], bench.lane_question(3))
        lane, dists = bench.make_cases(5)[0]
        self.assertEqual(p[0]["state"]["car_is_in"], f"lane_{lane + 1}")
        self.assertEqual(p[0]["state"]["lane_1"], bench.describe(dists[0]))

    def test_multi_question_copies_one_question(self):
        q = hwbench.multi_question(4)
        self.assertEqual(list(q), ["q1", "q2", "q3", "q4"])
        self.assertTrue(all(v == bench.lane_question(3)["target"] for v in q.values()))

    def test_pad_state(self):
        state = {"car_is_in": "lane_1"}
        padded = hwbench.pad_state(state, 250)
        self.assertEqual(len(padded["background"].split()), 250)
        self.assertEqual(padded["car_is_in"], "lane_1")
        self.assertEqual(hwbench.pad_state(state, 0), state)
        self.assertEqual(hwbench.pad_state(state, -40), state)
        self.assertNotIn("background", state)                      # původní stav se nemění
        self.assertNotRegex(padded["background"], r"lane|obstacle|clear|blocked")


class Tests(unittest.TestCase):
    def test_latency(self):
        post = FakePost(ms=[999.0] + [float(i) for i in range(1, 101)])      # první je zahřátí
        r = hwbench.run_latency("m", 100, post)
        self.assertEqual(len(post.calls), 101)
        self.assertEqual((r["n"], r["p50"], r["p90"], r["p95"], r["p99"], r["max"]), (100, 51, 91, 96, 100, 100))
        self.assertAlmostEqual(r["rate"], 1000 * 100 / 5050)

    def test_cold_stops_the_model_before_each_round(self):
        log = []
        post = FakePost(ms=[900.0, 10, 11, 12, 13, 14, 700.0, 10, 11, 12, 13, 14, 800.0, 10, 11, 12, 13, 14])
        seen = post.__call__
        r = hwbench.run_cold("m", lambda model, payload: (log.append("post"), seen(model, payload))[1],
                             stop=lambda model: log.append("stop " + model))
        self.assertEqual(log, (["stop m"] + ["post"] * 6) * 3)
        self.assertEqual((r["cold"], r["warm"], r["colds"]), (800.0, 12, [900.0, 700.0, 800.0]))

    def test_concurrency_levels(self):
        post = FakePost()
        rows = hwbench.run_concurrency("m", 16, post=post, clock=FakeClock(2.0))
        self.assertEqual([(r["clients"], r["n"]) for r in rows], [(1, 16), (2, 16), (4, 16), (8, 16)])
        self.assertTrue(all(r["rate"] == 8.0 and r["p95"] == 10.0 for r in rows))     # 16 dotazů za 2 s
        self.assertEqual(len(post.calls), 1 + 4 * 16)

    def test_concurrency_error_in_a_thread_is_raised(self):
        def fail(payload):
            if len(post.calls) > 3:
                raise ConnectionRefusedError("down")
        post = FakePost(fail=fail)
        with self.assertRaises(ConnectionRefusedError):
            hwbench.run_concurrency("m", 16, post=post)

    def test_concurrency_client_threads_are_daemon(self):
        inner, seen, caller = FakePost(), [], threading.current_thread()

        def post(model, payload):
            if threading.current_thread() is not caller:     # zahřívací dotaz jde z volajícího vlákna
                seen.append(threading.current_thread().daemon)
            return inner(model, payload)
        hwbench.run_concurrency("m", 8, levels=(1, 2), post=post)
        self.assertEqual(len(seen), 2 * 8)
        self.assertTrue(all(seen))                           # po Ctrl+C nesmí proces dál posílat dotazy z klientských vláken

    def test_questions(self):
        post = FakePost(ms=[5.0] + [20.0] * 3 + [40.0] * 3 + [80.0] * 3)
        rows = hwbench.run_questions("m", levels=(1, 2, 4), reps=3, post=post)
        self.assertEqual(rows, [{"questions": 1, "ms": 20.0, "per_question": 20.0},
                                {"questions": 2, "ms": 40.0, "per_question": 20.0},
                                {"questions": 4, "ms": 80.0, "per_question": 20.0}])
        self.assertEqual([len(c["questions"]) for c in post.calls], [1, 1, 1, 1, 2, 2, 2, 4, 4, 4])

    def test_questions_level_refused_by_the_server_keeps_the_measured_levels(self):
        def fail(payload):
            if len(payload["questions"]) > 4:                # server odmítne příliš dlouhý prompt (víc otázek = delší prompt)
                raise urllib.error.HTTPError("http://x", 400, "prompt too long", {}, None)
        post = FakePost(fail=fail)
        rows = hwbench.run_questions("m", reps=2, post=post)
        self.assertEqual(rows, [{"questions": 1, "ms": 10.0, "per_question": 10.0},
                                {"questions": 2, "ms": 10.0, "per_question": 5.0},
                                {"questions": 4, "ms": 10.0, "per_question": 2.5},
                                {"questions": 8, "ms": None, "per_question": None}])
        self.assertEqual(max(len(c["questions"]) for c in post.calls), 8)        # 16 otázek se už nezkouší

    def test_questions_other_errors_are_not_reported_as_too_long(self):
        def fail(payload):
            if len(payload["questions"]) > 1:                # zahřátí a první stupeň projdou, chyba nastane až potom
                raise urllib.error.URLError("connection refused")
        with self.assertRaises(urllib.error.URLError):
            hwbench.run_questions("m", reps=2, post=FakePost(fail=fail))

    def test_questions_server_fault_is_not_reported_as_too_long(self):
        def fail(payload):
            if len(post.calls) > 1:                          # zahřátí projde, server selže u prvního měřeného dotazu
                raise urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, None)
        post = FakePost(fail=fail)
        with self.assertRaises(urllib.error.HTTPError) as e:
            hwbench.run_questions("m", reps=2, post=post)
        self.assertEqual(e.exception.code, 503)

    def test_cloudflare_status_400_is_a_refusal(self):
        def fail(payload):
            if len(payload["questions"]) > 1:
                raise providers.ProviderError("cf", 400, '{"errors":[{"message":"prompt too long"}]}')
        rows = hwbench.run_questions("cf:x", reps=2, post=FakePost(fail=fail))
        self.assertEqual(rows, [{"questions": 1, "ms": 10.0, "per_question": 10.0},
                                {"questions": 2, "ms": None, "per_question": None}])

    def test_cloudflare_other_status_is_not_reported_as_too_long(self):
        def fail(payload):
            if len(payload["questions"]) > 1:
                raise providers.ProviderError("cf", 429, '{"errors":[{"message":"rate limited"}]}')
        with self.assertRaises(RuntimeError) as e:
            hwbench.run_questions("cf:x", reps=2, post=FakePost(fail=fail))
        self.assertTrue(str(e.exception).startswith("Cloudflare 429"))

    def test_context_pads_to_the_target_and_reports_real_tokens(self):
        post = FakePost(tokens=150)
        rows = hwbench.run_context("m", sizes=(100, 500, 1000), reps=2, post=post)
        self.assertEqual([(r["target"], r["tokens"], r["fit"]) for r in rows], [(100, 150, True), (500, 500, True), (1000, 1000, True)])
        self.assertNotIn("background", post.calls[1]["state"])       # dotaz je už bez vycpávky delší než cíl

    def test_context_level_refused_by_the_server_ends_the_model(self):
        def fail(payload):
            if len(payload["state"].get("background", "").split()) > 1500:
                raise urllib.error.HTTPError("http://x", 400, "context too long", {}, None)
        rows = hwbench.run_context("m", reps=2, post=FakePost(tokens=150, fail=fail))
        self.assertEqual([(r["target"], r["fit"]) for r in rows], [(100, True), (500, True), (1000, True), (2000, False)])

    def test_context_other_errors_are_not_reported_as_too_long(self):
        def fail(payload):
            if "background" in payload["state"]:             # zahřátí projde, chyba nastane až v měřené smyčce
                raise urllib.error.URLError("connection refused")
        with self.assertRaises(urllib.error.URLError):
            hwbench.run_context("m", reps=2, post=FakePost(fail=fail))

    def test_context_server_fault_is_not_reported_as_too_long(self):
        def fail(payload):
            if len(post.calls) > 1:                          # zahřátí projde, server selže u prvního měřeného dotazu
                raise urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, None)
        post = FakePost(fail=fail)
        with self.assertRaises(urllib.error.HTTPError) as e:
            hwbench.run_context("m", reps=2, post=post)
        self.assertEqual(e.exception.code, 503)


class Soak(unittest.TestCase):
    DECISIONS = [(0.0, 100.0), (30.0, 110.0), (59.9, 120.0), (60.0, 200.0), (119.0, 220.0), (130.0, 300.0)]
    SAMPLES = [(0.0, 50, 20.0, 1500), (1.0, 52, 40.0, 1500), (2.0, 54, 40.0, 1500), (3.0, 56, 40.0, 1500),
               (4.0, 58, 40.0, 1500), (61.0, 70, 60.0, None)]

    def test_minutes(self):
        rows = hwbench.soak_minutes(self.DECISIONS, self.SAMPLES)
        self.assertEqual([(r["minute"], r["n"], r["p50"], r["p95"]) for r in rows],
                         [(1, 3, 110.0, 120.0), (2, 2, 220.0, 220.0), (3, 1, 300.0, 300.0)])
        self.assertEqual((rows[0]["temp"], rows[0]["power"], rows[0]["clock"]), (54, 36.0, 1500))
        self.assertEqual((rows[1]["temp"], rows[1]["power"]), (70, 60.0))
        self.assertTrue(rows[1]["clock"] != rows[1]["clock"] and rows[2]["temp"] != rows[2]["temp"])     # bez vzorku NaN
        self.assertEqual(hwbench.soak_minutes([], []), [])

    def test_rows_join_the_latest_sample(self):
        rows = hwbench.soak_rows(self.DECISIONS, self.SAMPLES[1:])
        self.assertEqual(rows[0], [0.0, 100.0, None, None, None])
        self.assertEqual(rows[1], [30.0, 110.0, 58, 40.0, 1500])
        self.assertEqual(rows[4], [119.0, 220.0, 70, 60.0, None])

    def test_summary(self):
        s = hwbench.soak_summary(self.DECISIONS, self.SAMPLES[:5])
        self.assertAlmostEqual(s["joules"], 30 + 40 + 40 + 40)
        self.assertAlmostEqual(s["j_per_decision"], 150 / 6)
        self.assertAlmostEqual(s["ratio"], 300 / 110)
        self.assertEqual(s["max_temp"], 58)
        empty = hwbench.soak_summary(self.DECISIONS[:2], [])
        self.assertTrue(empty["j_per_decision"] != empty["j_per_decision"] and empty["ratio"] != empty["ratio"])
        self.assertIsNone(empty["max_temp"])

    def test_run_asks_until_the_time_is_up(self):
        samples = [(float(t), 50, 40.0, 1500) for t in range(0, 81, 5)]       # vzorky pokrývají celý běh
        sampler, post = FakeSampler(samples), FakePost()
        r = hwbench.run_soak("m", 1, post, clock=FakeClock(20.0), sampler=sampler)
        self.assertTrue(sampler.started)
        self.assertEqual([t for t, _ in r["decisions"]], [20.0, 40.0])        # třetí čtení hodin = 60 s → konec
        self.assertEqual(len(post.calls), 3)                                  # zahřátí + 2
        self.assertFalse(r["interrupted"])
        self.assertAlmostEqual(r["joules"], 3200.0)                           # 40 W × 80 s (běh podle hodin skončil v 80. s)
        self.assertAlmostEqual(r["j_per_decision"], 1600.0)

    def test_run_with_samples_ending_early_has_no_energy(self):
        r = hwbench.run_soak("m", 1, FakePost(), clock=FakeClock(20.0), sampler=FakeSampler(self.SAMPLES[:5]))
        self.assertTrue(r["joules"] != r["joules"] and r["j_per_decision"] != r["j_per_decision"])   # vzorky končí ve 4. s, běh v 80. s
        self.assertEqual(r["max_temp"], 58)

    def test_run_without_sampler(self):
        r = hwbench.run_soak("m", 1, FakePost(), clock=FakeClock(20.0))
        self.assertEqual((r["samples"], r["max_temp"]), ([], None))

    def test_ctrl_c_keeps_what_was_measured(self):
        def fail(payload):
            if len(post.calls) == 4:
                raise KeyboardInterrupt
        post = FakePost(fail=fail)
        r = hwbench.run_soak("m", 10, post, clock=FakeClock(1.0), sampler=FakeSampler([]))
        self.assertEqual((len(r["decisions"]), r["interrupted"]), (2, True))


class Sampler(unittest.TestCase):
    def test_collects_samples_until_stopped(self):
        s = hwbench.GpuSampler(read=lambda: {"available": True, "temp_c": 60, "power_w": 30.0, "clock_mhz": 1500}, interval=0.01)
        s.start()
        deadline = time.time() + 5
        while len(s.samples) < 3 and time.time() < deadline:
            time.sleep(0.01)
        samples = s.stop()
        self.assertGreaterEqual(len(samples), 3)
        self.assertEqual(samples[0][1:], (60, 30.0, 1500))
        self.assertTrue(all(a[0] <= b[0] for a, b in zip(samples, samples[1:])))

    def test_no_gpu_gives_up_after_three_reads(self):
        reads = []
        s = hwbench.GpuSampler(read=lambda: reads.append(1) or {"available": False}, interval=0.01)
        s.start()
        s._thread.join(5)
        self.assertEqual((s.stop(), len(reads)), ([], 3))

    def test_slow_first_read_does_not_end_sampling(self):
        answers = [{"available": False}, {"available": True, "temp_c": 60, "power_w": 30.0, "clock_mhz": 1500},
                   {"available": False}, {"available": False}, {"available": False}, {"available": False}]
        reads = []

        def read():
            reads.append(1)
            return answers[min(len(reads), len(answers)) - 1]
        s = hwbench.GpuSampler(read=read, interval=0.01)
        s.start()
        deadline = time.time() + 5
        while len(reads) < 6 and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(s._thread.is_alive())                         # GPU už byla vidět → čte se dál
        self.assertEqual(len(s.stop()), 1)


class Cli(unittest.TestCase):
    def test_parse_args(self):
        a = hwbench.parse_args(["latency", "--n", "200", "tev1", "nimble"])
        self.assertEqual((a.test, a.n, a.minutes, a.csv, a.models), ("latency", 200, 10, None, ["tev1", "nimble"]))
        a = hwbench.parse_args(["soak", "tev1", "--minutes", "3", "--csv", "x.csv"])
        self.assertEqual((a.test, a.minutes, a.csv, a.models), ("soak", 3, "x.csv", ["tev1"]))
        self.assertIsNone(hwbench.parse_args([]).test)

    def test_num_parallel(self):
        self.assertEqual(hwbench.num_parallel({}), "nenastaveno")
        self.assertEqual(hwbench.num_parallel({"OLLAMA_NUM_PARALLEL": "4"}), "4")

    def test_every_table_row_matches_its_header(self):
        for test in hwbench.TESTS:
            with self.subTest(test=test):
                a = hwbench.parse_args([test, "--n", "8", "--minutes", "1", "m"])
                head, rows, csv_head, csv_rows, notes = hwbench.measure(
                    a, "m", FakePost(), FakeSampler(Soak.SAMPLES[:5]), stop=lambda model: None, clock=FakeClock(20.0))
                self.assertTrue(rows and csv_rows)
                self.assertTrue(all(len(r) == len(head) and r[0] == "m" for r in rows))
                self.assertTrue(all(len(r) == len(csv_head) and r[0] == "m" for r in csv_rows))

    def test_hosted_model_skips_cold_and_concurrency_and_gpu(self):
        post = FakePost()
        for test in ("cold", "concurrency"):
            a = hwbench.parse_args([test, "cf:x"])
            head, rows, _, csv_rows, notes = hwbench.measure(a, "cf:x", post, stop=lambda model: self.fail("stop"))
            self.assertEqual((rows, csv_rows, len(notes)), ([], [], 1))
        self.assertEqual(post.calls, [])
        sampler = FakeSampler(Soak.SAMPLES[:5])
        a = hwbench.parse_args(["soak", "--minutes", "1", "--hosted-soak", "cf:x"])
        head, rows, _, _, notes = hwbench.measure(a, "cf:x", post, sampler, clock=FakeClock(20.0))
        self.assertFalse(sampler.started)
        self.assertEqual(rows[0][5:], ["—", "—", "—"])

    def run_main(self, argv, **kw):
        lines = []
        hwbench.main(argv, out=lines.append, **kw)
        return lines

    def test_main_prints_one_table_and_writes_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "out.csv")
            lines = self.run_main(["questions", "--csv", path, "m1", "m2"], post=FakePost())
            self.assertEqual(len(lines), 1 + 2 * 5)                    # záhlaví jednou, 5 stupňů na model
            self.assertTrue(lines[0].startswith("model"))
            with open(path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f, delimiter=";"))
            self.assertEqual(rows[0], ["model", "otázek", "ms na dotaz", "ms na otázku"])
            self.assertEqual([r[0] for r in rows[1:]], ["m1"] * 5 + ["m2"] * 5)

    def test_main_questions_prints_refused_level_and_goes_on_to_the_next_model(self):
        def fail(payload):
            if len(payload["questions"]) > 8:
                raise urllib.error.HTTPError("http://x", 400, "prompt too long", {}, None)
        lines = self.run_main(["questions", "m1", "m2"], post=FakePost(fail=fail))
        self.assertEqual(len(lines), 1 + 2 * 5)                        # záhlaví, 4 změřené stupně + „nevešlo se“ na model
        self.assertEqual([("nevešlo se" in ln, ln.split()[0]) for ln in lines[4:7]], [(False, "m1"), (True, "m1"), (False, "m2")])
        self.assertTrue(lines[5].rstrip().endswith("—"))               # ms na otázku u stupně, který se nevešel

    def test_main_context_refused_level_keeps_cells_apart(self):
        def fail(payload):
            if len(payload["state"].get("background", "").split()) > 1500:
                raise urllib.error.HTTPError("http://x", 400, "context too long", {}, None)
        lines = self.run_main(["context", "m"], post=FakePost(tokens=150, fail=fail))
        self.assertEqual(lines[-1].split(), ["m", "2000", "—", "nevešlo", "se"])     # „nevešlo se“ je širší než sloupec ms

    def test_main_soak_ctrl_c_does_not_start_the_next_model(self):
        sent = []

        def fail(payload):
            if len(inner.calls) == 3:                                # zahřátí + 2. dotaz měřené části
                raise KeyboardInterrupt
        inner = FakePost(fail=fail)

        def post(model, payload):
            sent.append(model)
            return inner(model, payload)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "soak.csv")
            lines = self.run_main(["soak", "--minutes", "1", "--csv", path, "m1", "m2"],
                                  post=post, sampler=FakeSampler([]), clock=FakeClock(1.0))
            with open(path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f, delimiter=";"))
        self.assertEqual(set(sent), {"m1"})                          # druhý model se nezačal měřit
        self.assertTrue(lines[0].startswith("model"))
        self.assertTrue(any("m1" in ln and "přerušeno" in ln for ln in lines))      # souhrn prvního modelu se vypsal
        self.assertEqual([r[0] for r in rows[1:]], ["m1"])           # a jeho řádky se zapsaly do CSV

    def test_main_rejects_bad_arguments_with_usage(self):
        for argv in ([], ["latency"], ["nonsense", "m"], ["latency", "--n", "0", "m"], ["soak", "--minutes", "0", "m"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as e:
                self.run_main(argv, post=FakePost())
            self.assertIn("Použití", str(e.exception.code))

    def test_main_checks_csv_path_before_measuring(self):
        post = FakePost()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit) as e:
            self.run_main(["latency", "--n", "5", "--csv", str(Path(tmp) / "missing" / "x.csv"), "m"], post=post)
        self.assertIn("CSV", str(e.exception.code))
        self.assertEqual(post.calls, [])

    def test_main_cold_needs_ollama_in_path(self):
        post = FakePost()
        with self.assertRaises(SystemExit) as e:
            self.run_main(["cold", "m"], post=post, which=lambda name: None)
        self.assertIn("ollama", str(e.exception.code))
        self.assertEqual(post.calls, [])

    def test_main_reports_unreachable_ollama_without_traceback(self):
        def fail(payload):
            raise urllib.error.URLError("connection refused")
        with self.assertRaises(SystemExit) as e:
            self.run_main(["latency", "--n", "5", "m"], post=FakePost(fail=fail))
        self.assertIn("Ollama nedostupná", str(e.exception.code))

    def test_main_reports_failed_ollama_stop_without_traceback(self):
        def stop(model):
            raise subprocess.CalledProcessError(1, ["ollama", "stop", model])
        with self.assertRaises(SystemExit) as e:
            self.run_main(["cold", "m"], post=FakePost(), stop=stop, which=lambda name: "ollama")
        self.assertIn("ollama stop", str(e.exception.code))

    def test_main_reports_cloudflare_error_without_traceback(self):
        def fail(payload):
            raise providers.ProviderError("cf", 500, '{"errors":[{"message":"internal error"}]}')
        with self.assertRaises(SystemExit) as e:
            self.run_main(["latency", "--n", "5", "cf:x"], post=FakePost(fail=fail))
        self.assertEqual(e.exception.code, 'Cloudflare 500: {"errors":[{"message":"internal error"}]}')

    def test_main_reports_missing_cloudflare_variable_without_traceback(self):
        for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
            def fail(payload, name=name):
                raise KeyError(name)                         # tak končí os.environ[name] v bench.post
            with self.subTest(name=name):
                with self.assertRaises(SystemExit) as e:
                    self.run_main(["latency", "--n", "5", "cf:x"], post=FakePost(fail=fail))
                self.assertIn(name, str(e.exception.code))
                self.assertNotIn("\n", str(e.exception.code))

    def test_main_lets_other_errors_through(self):
        for error in (RuntimeError("boom"), KeyError("answers")):
            def fail(payload, error=error):
                raise error
            with self.subTest(error=error), self.assertRaises(type(error)):
                self.run_main(["latency", "--n", "5", "m"], post=FakePost(fail=fail))

    def test_main_reports_csv_write_error_as_csv_error(self):
        for where in ("write", "flush"):
            with self.subTest(where=where):
                with mock.patch.object(hwbench, "open", lambda *a, **kw: BrokenFile(where), create=True), \
                        self.assertRaises(SystemExit) as e:
                    self.run_main(["latency", "--n", "5", "--csv", "x.csv", "m"], post=FakePost())
                self.assertIn("CSV nejde zapsat", str(e.exception.code))
                self.assertNotIn("Ollama", str(e.exception.code))


class Timeline:
    """Podstrčený čas pro test server: běží jen tím, že někdo spí nebo že dotaz „trvá“ (service sekund).

    spawn pouští práci hned a čeká na ni — jako server s jedním vláknem, kde další dotaz čeká ve frontě."""

    class Done:
        def join(self, timeout=None):
            pass

    def __init__(self, service=0.1, fail_at=None):
        self.t, self.service, self.calls, self.fail_at = 0.0, service, 0, fail_at

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def post(self, model, payload):
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at[0]:
            raise self.fail_at[1]
        self.t += self.service
        return {"answers": {"target": {"choice": "lane_1"}}}, self.service * 1000

    def spawn(self, work, *args):
        work(*args)
        return self.Done()

    def kw(self):
        return {"post": self.post, "clock": self.clock, "sleep": self.sleep, "spawn": self.spawn}


class Server(unittest.TestCase):
    def test_arrivals_are_a_repeatable_poisson_stream(self):
        a = hwbench.arrivals(5.0, 4000)
        self.assertEqual(a, hwbench.arrivals(5.0, 4000, seed=1))
        self.assertNotEqual(a, hwbench.arrivals(5.0, 4000, seed=2))
        self.assertEqual(len(a), 4000)
        self.assertTrue(all(x < y for x, y in zip(a, a[1:])) and a[0] > 0)
        self.assertAlmostEqual(a[-1] / 4000, 1 / 5.0, delta=0.01)                 # průměrná mezera = 1 ÷ tempo
        gaps = [y - x for x, y in zip(a, a[1:])]
        self.assertGreater(max(gaps), 5 * min(gaps))                              # mezery nejsou stejné
        self.assertAlmostEqual(hwbench.arrivals(50.0, 4000)[-1], a[-1] / 10)      # stejné semínko, jiné tempo: jen měřítko

    def test_light_load_measures_the_service_time(self):
        tl = Timeline(service=0.1)
        r = hwbench.run_server_level("m", 0.5, 40, 1000, **tl.kw())
        self.assertEqual((r["rate"], r["sent"], r["done"], r["ok"], r["on_time"]), (0.5, 40, 40, True, 1.0))
        self.assertAlmostEqual(r["p50"], 100.0, delta=1e-6)
        self.assertLess(r["p99"], 1000)
        self.assertAlmostEqual(r["achieved"], 40 / tl.t)

    def test_latency_counts_from_the_planned_arrival_so_the_queue_shows(self):
        tl, n, rate, service = Timeline(service=0.1), 60, 20.0, 0.1               # přichází dvakrát rychleji, než server stíhá
        r = hwbench.run_server_level("m", rate, n, 1000, **tl.kw())
        free, expected = 0.0, []
        for due in hwbench.arrivals(rate, n):                                     # fronta s jedním serverem, spočítaná ručně
            free = max(due, free) + service
            expected.append((free - due) * 1000)
        self.assertAlmostEqual(r["p99"], hwbench.percentile(expected, .99), delta=1e-6)
        self.assertAlmostEqual(r["p50"], hwbench.percentile(expected, .5), delta=1e-6)
        self.assertGreater(r["p99"], 20 * service * 1000)                         # daleko víc než samotná odpověď
        self.assertEqual((r["done"], r["ok"]), (60, False))
        self.assertAlmostEqual(r["on_time"], sum(1 for ms in expected if ms <= 1000) / n)

    def test_overloaded_level_is_cut_at_its_time_budget(self):
        tl = Timeline(service=1.0)
        r = hwbench.run_server_level("m", 10.0, 400, 500, **tl.kw())              # 400 dotazů po sekundě by trvalo 400 s
        self.assertLess(r["sent"], 400)
        self.assertEqual((r["done"], r["ok"]), (r["sent"], False))
        self.assertLess(tl.t, 120)                                                # příchody ~40 s + mez 30 s + jeden dotaz
        self.assertLess(r["on_time"], 0.05)                                       # neodeslané dotazy se počítají jako pozdě

    def test_levels_stop_after_the_first_that_misses_the_limit(self):
        tl = Timeline(service=0.1)
        r = hwbench.run_server("m", 100, 1000, rates=[2, 5, 50, 80], **tl.kw())
        self.assertEqual([row["rate"] for row in r["rows"]], [2, 5, 50])
        self.assertEqual([row["ok"] for row in r["rows"]], [True, True, False])
        self.assertEqual((r["best"], r["interrupted"], r["limit_ms"]), (5, False, 1000))
        self.assertAlmostEqual(r["base_ms"], 100.0)
        self.assertAlmostEqual(r["capacity"], 10.0)

    def test_rates_given_from_the_highest_are_still_measured_from_the_lowest(self):
        tl = Timeline(service=0.1)
        r = hwbench.run_server("m", 100, 1000, rates=[50, 5, 2], **tl.kw())       # zadáno sestupně: bez řazení by měření skončilo po stupni 50
        self.assertEqual([row["rate"] for row in r["rows"]], [2, 5, 50])
        self.assertEqual([row["ok"] for row in r["rows"]], [True, True, False])
        self.assertEqual(r["best"], 5)                                            # nižší stupeň, který limit splní, se najde

    def test_default_levels_follow_the_measured_serial_speed(self):
        tl = Timeline(service=0.2)                                                # 5 dotazů za sekundu po jednom
        r = hwbench.run_server("m", 30, 60000, **tl.kw())
        self.assertEqual([row["rate"] for row in r["rows"]], [round(5.0 * s, 2) for s in hwbench.SERVER_SHARES])
        self.assertEqual(r["best"], 10.0)                                         # s limitem minuta projde všech osm stupňů

    def test_no_level_within_the_limit(self):
        r = hwbench.run_server("m", 20, 50, rates=[1, 2], **Timeline(service=0.1).kw())      # odpověď trvá 100 ms, limit 50
        self.assertEqual((len(r["rows"]), r["best"]), (1, None))

    def test_ctrl_c_keeps_the_levels_already_measured(self):
        tl = Timeline(service=0.1, fail_at=(1 + 20 + 30 + 5, KeyboardInterrupt()))            # zahřátí, 20 sériových, 1. stupeň 30, pátý dotaz 2. stupně
        r = hwbench.run_server("m", 30, 1000, rates=[2, 4, 6], **tl.kw())
        self.assertEqual(([row["rate"] for row in r["rows"]], r["interrupted"], r["best"]), ([2], True, 2))

    def test_failure_of_one_request_fails_the_test(self):
        tl = Timeline(service=0.1, fail_at=(1 + 20 + 3, urllib.error.URLError("refused")))
        with self.assertRaises(urllib.error.URLError):
            hwbench.run_server("m", 30, 1000, rates=[2], **tl.kw())

    def test_error_inside_a_worker_thread_reaches_the_caller(self):
        error = urllib.error.URLError("refused")

        def post(model, payload):                                                 # dotaz selže ve vlákně, ne v hlavním vlákně
            raise error
        with self.assertRaises(urllib.error.URLError) as e:                       # skutečná vlákna: bez zachycení ve vlákně by chyba skončila v excepthooku
            hwbench.run_server_level("m", 400.0, 5, 1000, post)
        self.assertIs(e.exception, error)

    def test_real_threads_and_real_clock(self):
        post = FakePost(ms=[1.0])
        r = hwbench.run_server_level("m", 400.0, 40, 5000, post)
        self.assertEqual((r["sent"], r["done"], r["ok"]), (40, 40, True))
        self.assertEqual(len(post.calls), 40)

    def test_stuck_requests_do_not_hold_the_level_past_its_budget(self):
        gate, lock, calls = threading.Event(), threading.Lock(), []
        self.addCleanup(gate.set)

        def post(model, payload):                                                 # každý druhý dotaz „visí“ (server neodpovídá)
            with lock:
                calls.append(1)
                stuck = len(calls) % 2 == 0
            if stuck:
                gate.wait(30)
            return {"answers": {"target": {"choice": "lane_1"}}}, 1.0
        with mock.patch.multiple(hwbench, SERVER_SLACK=0.3, SERVER_DRAIN=0.2):
            t0 = time.perf_counter()
            r = hwbench.run_server_level("m", 200.0, 10, 50, post)                # limit 50 ms → mez stupně je poslední příchod + 1 s
            took = time.perf_counter() - t0
        self.assertEqual((r["sent"], r["done"]), (10, 5))
        self.assertFalse(r["ok"])                                                 # rychlé odpovědi limit splnily, ale stupeň nedoběhl celý
        self.assertLess(took, 10)                                                 # mez stupně a doběh, ne 30 s visícího dotazu

    def test_table_and_note(self):
        a = hwbench.parse_args(["server", "--n", "30", "--rates", "2,50", "--p99-ms", "1000", "m"])
        self.assertEqual((a.test, a.n, a.rates, a.p99_ms, a.models), ("server", 30, [2.0, 50.0], 1000.0, ["m"]))
        self.assertEqual((hwbench.parse_args(["server", "m"]).p99_ms, hwbench.parse_args(["server", "m"]).rates), (1000.0, None))
        tl = Timeline(service=0.1)
        with mock.patch.multiple(hwbench.time, perf_counter=tl.clock, sleep=tl.sleep), mock.patch.object(hwbench, "start_thread", tl.spawn):
            head, rows, csv_head, csv_rows, notes = hwbench.measure(a, "m", tl.post)
        self.assertEqual(head, ["model", "tempo/s", "dosaženo/s", "p50 ms", "p99 ms", "včas"])
        self.assertEqual((csv_head, csv_rows), (head, rows))
        self.assertEqual([r[:2] for r in rows], [["m", "2.00"], ["m", "50.00"]])
        self.assertEqual((rows[0][3], rows[0][5]), ("100.0", "100 %"))
        self.assertEqual(notes, ["m: sériově medián 100.0 ms (10.0 dotazů/s); nejvyšší udržitelné tempo 2.00 dotazů/s při p99 do 1000 ms"])

    def test_note_when_interrupted_or_nothing_fits(self):
        a = hwbench.parse_args(["server", "--n", "20", "--rates", "1", "--p99-ms", "50", "m"])
        tl = Timeline(service=0.1)
        with mock.patch.multiple(hwbench.time, perf_counter=tl.clock, sleep=tl.sleep), mock.patch.object(hwbench, "start_thread", tl.spawn):
            notes = hwbench.measure(a, "m", tl.post)[4]
        self.assertEqual(notes, ["m: sériově medián 100.0 ms (10.0 dotazů/s); žádný stupeň nesplnil p99 do 50 ms"])
        tl = Timeline(service=0.1, fail_at=(1 + 20 + 5, KeyboardInterrupt()))
        with mock.patch.multiple(hwbench.time, perf_counter=tl.clock, sleep=tl.sleep), mock.patch.object(hwbench, "start_thread", tl.spawn):
            notes = hwbench.measure(a, "m", tl.post)[4]
        self.assertTrue(notes[0].endswith(hwbench.interrupted_mark()))
        self.assertTrue(notes.interrupted)                                   # příznak, podle kterého main další model nezačne; věta je jen pro člověka
        tl = Timeline(service=0.1)
        with mock.patch.multiple(hwbench.time, perf_counter=tl.clock, sleep=tl.sleep), mock.patch.object(hwbench, "start_thread", tl.spawn):
            self.assertFalse(hwbench.measure(a, "m", tl.post)[4].interrupted)

    def test_hosted_model_is_skipped(self):
        post = FakePost()
        head, rows, _, csv_rows, notes = hwbench.measure(hwbench.parse_args(["server", "cf:x"]), "cf:x", post)
        self.assertEqual((rows, csv_rows, post.calls), ([], [], []))
        self.assertEqual(notes, ["cf:x: test server je jen pro lokální modely (souběžné dotazy na hostovaný model jsou mimo rozsah)"])

    def test_main_prints_num_parallel_and_refuses_bad_options(self):
        lines = []
        hwbench.main(["server", "--n", "5", "--rates", "500", "m"], post=FakePost(ms=[1.0]), out=lines.append)
        self.assertTrue(lines[0].startswith("OLLAMA_NUM_PARALLEL (v prostředí tohoto procesu): "))
        self.assertTrue(lines[1].startswith("model"))
        self.assertEqual(len(lines), 4)                                           # hlavička, záhlaví, jeden stupeň, věta
        err = io.StringIO()                                                       # nečitelné --rates hlásí argparse na stderr; ať neteče do výstupu testů
        with contextlib.redirect_stderr(err):
            for argv in (["server", "--p99-ms", "0", "m"], ["server", "--rates", "0,5", "m"], ["server", "--rates", "", "m"]):
                with self.subTest(argv=argv):
                    with self.assertRaises(SystemExit) as e:
                        hwbench.main(argv, post=FakePost(), out=lines.append)
                    self.assertEqual(e.exception.code, hwbench.__doc__)
        self.assertIn("argument --rates", err.getvalue())                         # a uživatel se z toho dozví, která volba je špatně


class Hosted(unittest.TestCase):
    """Hostované modely v hwbench.py. Žádný test nesmí na síť ani na lokální Ollamu; klíče z prostředí se na dobu testu odstraní."""

    def setUp(self):
        for owner, name in ((providers, "https_transport"), (providers, "tls_connect"), (bench.urllib.request, "urlopen")):
            patcher = mock.patch.object(owner, name, side_effect=AssertionError("test sahá na síť"))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"PICKBENCH_TEST_PROVIDER": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("TYPESAFE_API_KEY", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "PICKBENCH_MAX_USD", "PICKBENCH_MAX_HOSTED"):      # i limity vývojáře
            os.environ.pop(name, None)
        self.budget = providers.Budget(env={})
        patcher = mock.patch.object(providers, "BUDGET", self.budget)
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, *argv, **kw):
        """Vrací (řádky výstupu, čím skončil: None nebo argument sys.exit)."""
        lines, code = [], None
        try:
            hwbench.main(list(argv), out=lines.append, **kw)
        except SystemExit as e:
            code = e.code
        return lines, code

    def test_every_hosted_model_skips_cold_concurrency_and_server(self):
        for model in ("jev:jev-1.13.0", "cf:clef", "cf:x", "test:echo"):
            for test, why in (("cold", "ollama stop"), ("concurrency", "souběžné dotazy na hostovaný model jsou mimo rozsah"),
                              ("server", "souběžné dotazy na hostovaný model jsou mimo rozsah")):
                with self.subTest(model=model, test=test):
                    post = FakePost()
                    head, rows, _, csv_rows, notes = hwbench.measure(hwbench.parse_args([test, model]), model, post,
                                                                     stop=lambda m: self.fail("stop"))
                    self.assertEqual((rows, csv_rows, post.calls), ([], [], []))
                    self.assertEqual(notes, [f"{model}: test {test} je jen pro lokální modely ({why})"])

    def test_local_model_with_a_colon_is_still_local(self):
        post = FakePost()
        head, rows, _, _, notes = hwbench.measure(hwbench.parse_args(["concurrency", "--n", "8", "tev1:0.8b"]), "tev1:0.8b", post, clock=FakeClock(2.0))
        self.assertEqual(len(rows), 4)

    def test_soak_of_a_hosted_model_needs_the_switch(self):
        for model in ("jev:jev-1.13.0", "cf:clef"):
            with self.subTest(model=model):
                post, sampler = FakePost(), FakeSampler([])
                a = hwbench.parse_args(["soak", "--minutes", "1", model])
                head, rows, csv_head, csv_rows, notes = hwbench.measure(a, model, post, sampler, clock=FakeClock(20.0))
                self.assertEqual((rows, csv_rows, post.calls, sampler.started), ([], [], [], False))
                self.assertEqual(notes, [f"{model}: test soak u hostovaného modelu stojí peníze po celou dobu běhu — spusťte ho výslovně s --hosted-soak"])
                a = hwbench.parse_args(["soak", "--minutes", "1", "--hosted-soak", model])
                head, rows, _, _, notes = hwbench.measure(a, model, post, sampler, clock=FakeClock(20.0))
                self.assertTrue(rows and post.calls)
                self.assertFalse(sampler.started)                        # GPU tohoto počítače se u hostovaného modelu nevzorkuje
        a = hwbench.parse_args(["soak", "--minutes", "1", "m"])
        self.assertTrue(hwbench.measure(a, "m", FakePost(), FakeSampler([]), clock=FakeClock(20.0))[1])      # lokální model přepínač nepotřebuje

    def test_context_of_a_hosted_model_warns_about_silent_truncation(self):
        head, rows, _, _, notes = hwbench.measure(hwbench.parse_args(["context", "cf:clef"]), "cf:clef", FakePost(tokens=150))
        self.assertEqual(len(rows), 5)
        self.assertEqual(notes, ["cf:clef: hostovaný model dlouhý vstup nemusí odmítnout — Clef ho podle dokumentace tiše ořízne; "
                                 "„nevešlo se“ proto nemusí nastat a směrodatný je sloupec input_tokens"])
        self.assertEqual(hwbench.measure(hwbench.parse_args(["context", "m"]), "m", FakePost(tokens=150))[4], [])

    def test_refusal_is_read_for_every_provider(self):
        for label, pid in (("Jev", "jev"), ("Cloudflare", "cf"), ("Test", "test")):
            wrapped = RuntimeError(f"{label} 429: na větě nezáleží")
            wrapped.__cause__ = providers.ProviderError(pid, 400, "too long")
            self.assertTrue(hwbench.refused(wrapped))
            self.assertFalse(hwbench.refused(RuntimeError(f"{label} 400: too long")))      # jen věta, žádná chyba poskytovatele
            self.assertFalse(hwbench.refused(providers.ProviderError(pid, 429, "slow down")))
        self.assertTrue(hwbench.refused(providers.ProviderError("jev", 400, "x")))
        self.assertFalse(hwbench.refused(providers.Refused("jev", None, "chybí klíč")))
        self.assertFalse(hwbench.refused(providers.BudgetExceeded("jev", None, "strop útraty")))
        self.assertFalse(hwbench.refused(RuntimeError("Ollama 400: x")))
        self.assertFalse(hwbench.refused(ValueError("Jev 400: x")))

    def test_parse_hosted_options(self):
        a = hwbench.parse_args(["soak", "--hosted-soak", "--max-usd", "0.2", "--max-hosted", "300", "jev:jev-1.13.0"])
        self.assertEqual((a.hosted_soak, a.max_usd, a.max_hosted, a.models), (True, 0.2, 300, ["jev:jev-1.13.0"]))
        a = hwbench.parse_args(["latency", "m"])
        self.assertEqual((a.hosted_soak, a.max_usd, a.max_hosted), (False, None, None))

    def test_main_prints_the_cost_and_the_network_under_the_table(self):
        lines, code = self.main("latency", "--n", "5", "test:echo")
        self.assertIsNone(code)
        self.assertTrue(lines[0].startswith("model"))
        self.assertTrue(lines[1].startswith("test:echo "))
        self.assertRegex(lines[2], r"^test:echo: hostovaný model \(Test, zkušební poskytovatel \(nic neodchází\)\): 6 dotazů, \d+ vstupních tokenů, "
                                   r"0\.\d{4} \$ \(\d+\.\d{4} \$ / 1000 rozh\.\), opakování 0; latence včetně sítě$")
        self.assertEqual(lines[3], "test:echo: síť (odhad): spojení TCP + TLS 2 ms")
        self.assertEqual(len(lines), 4)
        self.assertEqual(self.budget.state()["requests"], 6)             # 5 dotazů + zahřívací

    def test_main_stops_at_the_limit_with_a_sentence_and_says_what_was_spent(self):
        lines, code = self.main("latency", "--n", "20", "--max-hosted", "4", "test:echo")
        self.assertRegex(code, r"^Test: strop útraty — odesláno 4 hostovaných dotazů, limit je 4 \(PICKBENCH_MAX_HOSTED\)")
        self.assertTrue(any(line.startswith("test:echo: hostovaný model") and " 4 dotazů" in line for line in lines), lines)
        for bad in (["--max-usd", "-1"], ["--max-usd", "nan"], ["--max-hosted", "-5"]):
            with self.subTest(bad=bad):
                self.assertEqual(self.main("latency", *bad, "test:echo")[1], "--max-usd a --max-hosted musí být nezáporná čísla.")

    def test_main_ends_with_a_sentence_for_a_model_that_cannot_be_called(self):
        for model, part in (("cf:cokoli", "není v seznamu hostovaných modelů"), ("jev:jev-1.13.0", "TYPESAFE_API_KEY")):
            with self.subTest(model=model):
                lines, code = self.main("latency", "--n", "5", model)
                self.assertIn(part, code)
                self.assertNotIn("\n", code)
                self.assertEqual(lines, [])
        self.assertEqual(self.budget.state()["requests"], 0)

    def test_skipped_test_does_not_touch_the_network_estimate(self):
        with mock.patch.object(providers, "net_line", side_effect=AssertionError("odhad sítě u přeskočeného testu")):
            for argv in (["cold", "test:echo"], ["concurrency", "--n", "4", "test:echo"], ["server", "--n", "4", "--rates", "500", "test:echo"],
                         ["soak", "--minutes", "0.01", "test:echo"]):
                with self.subTest(argv=argv):
                    lines, code = self.main(*argv, which=lambda name: "ollama", stop=lambda model: self.fail("ollama stop"), sampler=FakeSampler([]))
                    self.assertIsNone(code)
                    self.assertTrue(lines[-1].startswith("test:echo: test "))
        self.assertEqual(self.budget.state()["requests"], 0)

    def test_fake_post_never_pays_or_measures_the_network(self):         # testy s podstrčeným post nesmějí sahat na počítadlo ani na síť
        with mock.patch.object(providers, "net_line", side_effect=AssertionError("síť")), \
                mock.patch.object(providers, "resolve", side_effect=AssertionError("klíče")):
            lines, code = self.main("latency", "--n", "5", "cf:x", post=FakePost())
        self.assertEqual((code, len(lines)), (None, 2))


def refuse_over(questions):
    """Podstrčená chyba: server odmítne dotaz s víc otázkami (HTTP 400)."""
    def fail(payload):
        if len(payload["questions"]) > questions:
            raise urllib.error.HTTPError("http://x", 400, "prompt too long", {}, None)
    return fail


def refuse_long_context(payload):
    if len(payload["state"].get("background", "").split()) > 1500:
        raise urllib.error.HTTPError("http://x", 400, "context too long", {}, None)


class Language(unittest.TestCase):
    """--lang a PICKBENCH_LANG v hwbench.py: záhlaví a věty anglicky, česky beze změny, jazyk po běhu nezůstane.
    Žádný test nesmí na síť ani na lokální Ollamu; jazyk, klíče a limity z prostředí vývojáře se na dobu testu odstraní."""

    HEADS = {                                        # test → (záhlaví česky, jak bylo před překladem; záhlaví anglicky)
        "latency": (["model", "N", "p50 ms", "p90 ms", "p95 ms", "p99 ms", "max ms", "rozh./s"],
                    ["model", "N", "p50 ms", "p90 ms", "p95 ms", "p99 ms", "max ms", "dec./s"]),
        "cold": (["model", "studený start ms", "teplý medián ms", "studené starty ms"], ["model", "cold start ms", "warm median ms", "cold starts ms"]),
        "concurrency": (["model", "klientů", "N", "rozh./s", "p95 ms"], ["model", "clients", "N", "dec./s", "p95 ms"]),
        "questions": (["model", "otázek", "ms na dotaz", "ms na otázku"], ["model", "questions", "ms per request", "ms per question"]),
        "context": (["model", "cíl tokenů", "input_tokens", "ms"], ["model", "target tokens", "input tokens", "ms"]),
        "soak": (["model", "minuta", "N", "p50 ms", "p95 ms", "°C", "W", "MHz"], ["model", "minute", "N", "p50 ms", "p95 ms", "°C", "W", "MHz"]),
        "server": (["model", "tempo/s", "dosaženo/s", "p50 ms", "p99 ms", "včas"], ["model", "rate/s", "achieved/s", "p50 ms", "p99 ms", "in time"]),
    }
    SOAK_CSV = (["model", "čas s", "ms", "°C", "W", "MHz"], ["model", "time s", "ms", "°C", "W", "MHz"])
    SAMPLES = [(float(t), 50, 40.0, 1500) for t in range(0, 81, 5)]      # vzorky GPU přes celý běh testu soak s hodinami FakeClock(20.0)

    def setUp(self):
        for owner, name in ((providers, "https_transport"), (providers, "tls_connect"), (bench.urllib.request, "urlopen")):
            patcher = mock.patch.object(owner, name, side_effect=AssertionError("test sahá na síť"))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"PICKBENCH_TEST_PROVIDER": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (LANG_ENV, "OLLAMA_NUM_PARALLEL", "TYPESAFE_API_KEY", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "PICKBENCH_MAX_USD", "PICKBENCH_MAX_HOSTED"):
            os.environ.pop(name, None)
        self.budget = providers.Budget(env={})
        patcher = mock.patch.object(providers, "BUDGET", self.budget)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *argv, **kw):
        """Pustí hwbench.main s podstrčeným post (post=None = skutečný, jen pro zkušební hostovaný model).
        Vrací (řádky výstupu, čím skončil: None nebo argument sys.exit)."""
        lines, code = [], None
        try:
            hwbench.main(list(argv), out=lines.append, **{"post": FakePost(), **kw})
        except SystemExit as e:
            code = e.code
        return lines, code

    def both(self, *argv, **kw):
        """Stejný příkaz česky a anglicky (--lang en na konci). kw smí nést továrnu post_of, aby každý běh dostal vlastní podstrčený post."""
        post_of = kw.pop("post_of", FakePost)
        return self.cli(*argv, post=post_of(), **kw), self.cli(*argv, "--lang", "en", post=post_of(), **kw)

    def test_lang_option_may_stand_anywhere(self):
        czech, code = self.cli("questions", "m1")
        self.assertIsNone(code)
        self.assertEqual(czech[0], "model                   otázek  ms na dotaz  ms na otázku")
        english, code = self.cli("--lang", "en", "questions", "m1")
        self.assertIsNone(code)
        self.assertEqual(english[0], "model                 questions  ms per request  ms per question")
        self.assertEqual(CZECH.findall("\n".join(english)), [])
        for argv in (["questions", "--lang", "en", "m1"], ["questions", "m1", "--lang", "en"], ["questions", "m1", "--lang=en"], ["--lang=en", "questions", "m1"]):
            self.assertEqual(self.cli(*argv), (english, None), argv)
        self.assertEqual(self.cli("questions", "--lang", "cs", "m1"), (czech, None))

    def test_unknown_language_ends_with_a_sentence_before_any_request(self):
        for argv in (["latency", "m1", "--lang", "de"], ["--lang=de", "latency", "m1"], ["latency", "m1", "--lang"]):
            with self.subTest(argv=argv):
                post = FakePost()
                lines, code = self.cli(*argv, post=post)
                self.assertEqual((lines, post.calls), ([], []))
                self.assertIsInstance(code, str)                         # věta = nenulový kód ukončení
                self.assertRegex(code, r"cs.*en")
        self.assertEqual(self.cli("latency", "m1", "--lang", "de")[1],   # jazyk ještě není znám: věta v obou
                         'Neznámý jazyk „de“: --lang a PICKBENCH_LANG berou cs nebo en. / Unknown language "de": --lang and PICKBENCH_LANG accept cs or en.')

    def test_environment_variable_sets_the_language_and_the_option_beats_it(self):
        argv = ("latency", "--n", "5", "m1")
        (czech, _), (english, _) = self.both(*argv)
        self.assertNotEqual(english, czech)
        with mock.patch.dict(os.environ, {LANG_ENV: "en"}):
            self.assertEqual(self.cli(*argv), (english, None))
            self.assertEqual(self.cli(*argv, "--lang", "cs"), (czech, None))
            self.assertEqual(self.cli(), ([], hwbench.USAGE_EN))
        with mock.patch.dict(os.environ, {LANG_ENV: "de"}):
            post = FakePost()
            lines, code = self.cli(*argv, post=post)
            self.assertEqual((lines, post.calls), ([], []))
            self.assertIn('"de"', code)
            self.assertEqual(self.cli(*argv, "--lang", "en"), (english, None))      # volba proměnnou přebíjí, i když je v ní nesmysl

    def test_language_does_not_stay_after_the_run(self):
        self.assertEqual(i18n.lang(), "cs")
        for argv in (["latency", "--n", "5", "m1"],                      # doběhl
                     [],                                                 # skončil nápovědou
                     ["latency", "--max-usd", "-1", "m1"]):              # skončil větou
            with self.subTest(argv=argv):
                self.cli(*argv, "--lang", "en")
                self.assertEqual(i18n.lang(), "cs")
        during = []

        def fail(payload):
            during.append(i18n.lang())
            raise ValueError("chyba programu")
        with self.assertRaises(ValueError):                              # výjimka uprostřed měření
            self.cli("latency", "--n", "5", "m1", "--lang", "en", post=FakePost(fail=fail))
        self.assertEqual(during, ["en"])                                 # za běhu platil jazyk volby
        self.assertEqual(i18n.lang(), "cs")

    def test_worker_threads_build_sentences_in_the_language_of_the_run(self):      # jazyk procesu, ne jazyk vlákna
        seen, caller, inner = [], threading.current_thread(), FakePost(ms=[1.0])

        def post(model, payload):
            if threading.current_thread() is not caller:                 # klientská vlákna testu concurrency, vlákna dotazů testu server
                seen.append((i18n.lang(), i18n.t("hw.didNotFit"), hwbench.interrupted_mark(), hwbench.heads("cold")[0][1]))
            return inner(model, payload)
        self.assertEqual(self.cli("concurrency", "--n", "8", "m1", "--lang", "en", post=post)[1], None)
        self.assertEqual(len(seen), 4 * 8)                               # 1, 2, 4 a 8 klientů po 8 dotazech
        self.assertEqual(set(seen), {("en", "did not fit", "(interrupted)", "cold start ms")})
        seen.clear()
        self.assertEqual(self.cli("server", "--n", "5", "--rates", "500", "m1", "--lang", "en", post=post)[1], None)
        self.assertEqual((len(seen), set(seen)), (5, {("en", "did not fit", "(interrupted)", "cold start ms")}))
        seen.clear()
        self.assertEqual(self.cli("server", "--n", "5", "--rates", "500", "m1", post=post)[1], None)
        self.assertEqual(set(seen), {("cs", "nevešlo se", "(přerušeno)", "studený start ms")})
        self.assertEqual(i18n.lang(), "cs")

    def test_no_text_is_composed_when_the_module_is_imported(self):      # konstanta s textem by zůstala v jazyce, ve kterém se modul načetl
        tree = ast.parse(Path(hwbench.__file__).read_text(encoding="utf-8"))
        top = [node for node in tree.body if not isinstance(node, (ast.FunctionDef, ast.ClassDef))]      # co se vykoná při importu: příkazy modulu
        top += [node for cls in tree.body if isinstance(cls, ast.ClassDef) for node in cls.body if not isinstance(node, ast.FunctionDef)]      # a těla tříd
        calls = [ast.unparse(n.func) for node in top for n in ast.walk(node) if isinstance(n, ast.Call)]
        self.assertEqual([name for name in calls if name.startswith("i18n.")], [])
        defaults = [d for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) for d in node.args.defaults + node.args.kw_defaults if d is not None]
        self.assertEqual([ast.unparse(d) for d in defaults if "i18n" in ast.unparse(d)], [])      # ani jako výchozí hodnota parametru
        self.assertFalse(hasattr(hwbench, "INTERRUPTED"))
        self.assertEqual(hwbench.interrupted_mark(), "(přerušeno)")
        with i18n.process("en"):
            self.assertEqual(hwbench.interrupted_mark(), "(interrupted)")
            self.assertEqual(hwbench.num_parallel({}), "not set")
            self.assertEqual(hwbench.num_parallel({"OLLAMA_NUM_PARALLEL": "4"}), "4")

    def test_usage_follows_the_language(self):
        for argv in ([], ["latency"], ["nonsense", "m"], ["latency", "--n", "0", "m"], ["soak", "--minutes", "0", "m"], ["server", "--p99-ms", "0", "m"],
                     ["server", "--rates", "0,5", "m"]):
            with self.subTest(argv=argv):
                post = FakePost()
                self.assertEqual(self.cli(*argv, post=post), ([], hwbench.__doc__))
                self.assertEqual(self.cli(*argv, "--lang", "en", post=post), ([], hwbench.USAGE_EN))
                self.assertEqual(post.calls, [])
        err = io.StringIO()                                              # nečitelnou volbu hlásí argparse na stderr; ať neteče do výstupu testů
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.cli("server", "--rates", "", "m", "--lang", "en"), ([], hwbench.USAGE_EN))
        self.assertIn("argument --rates", err.getvalue())
        self.assertTrue(hwbench.USAGE_EN.startswith("Measures how this machine serves a decision model, "))
        self.assertEqual(CZECH.findall(hwbench.USAGE_EN), [])
        self.assertEqual(len(hwbench.USAGE_EN.splitlines()), len(hwbench.__doc__.splitlines()))
        for text in (hwbench.__doc__, hwbench.USAGE_EN):                 # každý test a každá volba je v obou nápovědách
            for word in (*hwbench.TESTS, "--n", "--minutes", "--p99-ms", "--rates", "--csv", "--hosted-soak", "--max-usd", "--max-hosted", "--lang cs|en", LANG_ENV):
                self.assertIn(word, text)

    def test_sentences_that_end_the_program_follow_the_language(self):
        def http(code):
            def fail(payload):
                raise urllib.error.HTTPError("http://x", code, "Service Unavailable", {}, None)
            return fail

        def down(payload):
            raise urllib.error.URLError("connection refused")

        def no_variable(payload):
            raise KeyError("CLOUDFLARE_API_TOKEN")                       # tak končí os.environ[name] v bench.post

        def ctrl_c(payload):
            raise KeyboardInterrupt

        def stop(model):
            raise subprocess.CalledProcessError(1, ["ollama", "stop", model])
        failed = "Command '['ollama', 'stop', 'm']' returned non-zero exit status 1."
        cases = [(["latency", "--max-usd", "-1", "m"], {}, "--max-usd a --max-hosted musí být nezáporná čísla.", "--max-usd and --max-hosted must be non-negative numbers."),
                 (["latency", "--max-hosted", "-5", "m"], {}, "--max-usd a --max-hosted musí být nezáporná čísla.", "--max-usd and --max-hosted must be non-negative numbers."),
                 (["cold", "m"], {"which": lambda name: None}, "Test cold potřebuje program ollama v PATH (ollama stop <model>).",
                  "The cold test needs the ollama program in PATH (ollama stop <model>)."),
                 (["cold", "m"], {"which": lambda name: "ollama", "stop": stop}, "Příkaz ollama stop selhal: " + failed, "The ollama stop command failed: " + failed),
                 (["latency", "--n", "5", "m"], {"post_of": lambda: FakePost(fail=down)}, "Ollama nedostupná: <urlopen error connection refused>",
                  "Ollama unreachable: <urlopen error connection refused>"),
                 (["latency", "--n", "5", "cf:x"], {"post_of": lambda: FakePost(fail=no_variable)},
                  "Model s předponou cf: potřebuje proměnnou prostředí CLOUDFLARE_API_TOKEN, která není nastavená.",
                  "A model with the cf: prefix needs the environment variable CLOUDFLARE_API_TOKEN, which is not set."),
                 (["latency", "--n", "5", "m"], {"post_of": lambda: FakePost(fail=ctrl_c)}, "Přerušeno.", "Interrupted.")]
        for argv, kw, cs, en in cases:
            with self.subTest(argv=argv, cs=cs):
                (lines_cs, code_cs), (lines_en, code_en) = self.both(*argv, **kw)
                self.assertEqual((code_cs, code_en), (cs, en))
                self.assertEqual(CZECH.findall(en), [])
                self.assertEqual((lines_cs, lines_en), ([], []))
        (_, code_cs), (_, code_en) = self.both("latency", "--n", "5", "m", post_of=lambda: FakePost(fail=http(503)))
        self.assertEqual(code_en, code_cs)                               # stav a tělo odpovědi Ollamy: žádné slovo k překladu
        self.assertTrue(code_cs.startswith("Ollama 503: "), code_cs)

    def test_csv_errors_follow_the_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            (lines_cs, code_cs), (lines_en, code_en) = self.both("latency", "--n", "5", "--csv", str(Path(tmp) / "missing" / "x.csv"), "m")
        self.assertTrue(code_cs.startswith("CSV nejde zapsat: [Errno 2] "), code_cs)
        self.assertTrue(code_en.startswith("hwbench.py cannot write the CSV: [Errno 2] "), code_en)
        self.assertEqual(code_en.split(": ", 1)[1], code_cs.split(": ", 1)[1])      # hláška systému zůstává, jak přišla
        self.assertEqual((lines_cs, lines_en), ([], []))                 # cesta se zkouší před měřením
        for where in ("write", "flush"):
            with self.subTest(where=where), mock.patch.object(hwbench, "open", lambda *a, **kw: BrokenFile(where), create=True):
                (_, code_cs), (_, code_en) = self.both("latency", "--n", "5", "--csv", "x.csv", "m")
                self.assertEqual(code_cs, "CSV nejde zapsat: [Errno 28] No space left on device")
                self.assertEqual(code_en, "hwbench.py cannot write the CSV: [Errno 28] No space left on device")

    def test_headers_of_every_table_in_both_languages(self):
        self.assertEqual(sorted(self.HEADS), sorted(hwbench.TESTS))
        for test, (cs, en) in self.HEADS.items():
            with self.subTest(test=test):
                csv_cs, csv_en = self.SOAK_CSV if test == "soak" else (cs, en)
                self.assertEqual(hwbench.heads(test), (cs, csv_cs))
                with i18n.process("en"):
                    self.assertEqual(hwbench.heads(test), (en, csv_en))
                self.assertEqual((len(en), len(csv_en)), (len(cs), len(csv_cs)))
                self.assertEqual(CZECH.findall(" ".join(en + csv_en)), [])
                for head in (cs, en):                                    # sloupec modelu zleva, každý další končí v řádku pod koncem svého záhlaví
                    ends = lambda text: [m.end() for m in re.finditer(r"\S+(?: \S+)*", text)][1:]
                    cells = ["m"] + ["1.0"] * (len(head) - 1)
                    self.assertEqual(ends(hwbench.line(cells, head)), ends(hwbench.line(head, head)))
                    self.assertEqual(len(ends(hwbench.line(head, head))), len(head) - 1)      # sousední záhlaví se neslepí

    def test_rows_are_the_same_in_both_languages(self):                 # jazyk mění záhlaví a věty, čísla ne
        for test in hwbench.TESTS:
            if test == "server":                                         # skutečná vlákna a hodiny: čísla se liší běh od běhu; má vlastní test níže
                continue
            with self.subTest(test=test):
                got = {}
                for code in ("cs", "en"):
                    a = hwbench.parse_args([test, "--n", "8", "--minutes", "1", "m"])
                    with i18n.process(code):
                        got[code] = hwbench.measure(a, "m", FakePost(), FakeSampler(self.SAMPLES), stop=lambda model: None, clock=FakeClock(20.0))
                (head_cs, rows_cs, csv_head_cs, csv_rows_cs, notes_cs), (head_en, rows_en, csv_head_en, csv_rows_en, notes_en) = got["cs"], got["en"]
                self.assertEqual((head_cs, head_en), self.HEADS[test])
                self.assertTrue(rows_cs and csv_rows_cs)
                self.assertEqual((rows_en, csv_rows_en), (rows_cs, csv_rows_cs))
                self.assertTrue(all(len(row) == len(head_en) for row in rows_en) and all(len(row) == len(csv_head_en) for row in csv_rows_en))
                self.assertEqual(len(notes_en), len(notes_cs))

    def test_main_prints_the_english_table_and_writes_the_english_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            files, printed = {}, {}
            for code, more in (("cs", []), ("en", ["--lang", "en"])):
                path = str(Path(tmp) / f"{code}.csv")
                printed[code], end = self.cli("questions", "--csv", path, "m1", "m2", *more)
                self.assertIsNone(end)
                files[code] = Path(path).read_bytes()
        rows = {code: list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), delimiter=";")) for code, raw in files.items()}
        self.assertEqual(rows["cs"][0], ["model", "otázek", "ms na dotaz", "ms na otázku"])
        self.assertEqual(rows["en"][0], ["model", "questions", "ms per request", "ms per question"])
        self.assertEqual(len(rows["en"][0]), len(rows["cs"][0]))         # stejný počet sloupců
        self.assertEqual(rows["en"][1:], rows["cs"][1:])                 # řádky čísel jsou stejné
        self.assertEqual(len(rows["en"]), 1 + 2 * 5)                     # záhlaví jednou, 5 stupňů na model
        for raw in files.values():                                       # značka BOM a oddělovač zůstávají
            self.assertTrue(raw.startswith(b"\xef\xbb\xbfmodel;"), raw[:20])
        self.assertEqual(len(printed["en"]), len(printed["cs"]))
        self.assertEqual(printed["en"][0].split(), ["model", "questions", "ms", "per", "request", "ms", "per", "question"])
        self.assertEqual([line.split() for line in printed["en"][1:]], [line.split() for line in printed["cs"][1:]])
        self.assertEqual({len(line) for line in printed["en"]}, {len(printed["en"][0])})      # řádky končí pod koncem anglického záhlaví

    def test_soak_csv_has_its_own_header_in_both_languages(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = {}
            for code, more in (("cs", []), ("en", ["--lang", "en"])):
                path = str(Path(tmp) / f"{code}.csv")
                self.cli("soak", "--minutes", "1", "--csv", path, "m", *more, sampler=FakeSampler(self.SAMPLES), clock=FakeClock(20.0))
                with open(path, encoding="utf-8-sig", newline="") as f:
                    rows[code] = list(csv.reader(f, delimiter=";"))
        self.assertEqual((rows["cs"][0], rows["en"][0]), self.SOAK_CSV)
        self.assertEqual(rows["cs"][1:], [["m", "20.0", "10.0", "50", "40.0", "1500"], ["m", "40.0", "10.0", "50", "40.0", "1500"]])
        self.assertEqual(rows["en"][1:], rows["cs"][1:])

    def test_soak_summary_in_both_languages(self):
        (cs, _), (en, _) = self.both("soak", "--minutes", "1", "m", sampler=FakeSampler(self.SAMPLES), clock=FakeClock(20.0))
        self.assertEqual(cs[0].split(), ["model", "minuta", "N", "p50", "ms", "p95", "ms", "°C", "W", "MHz"])
        self.assertEqual(en[0].split(), ["model", "minute", "N", "p50", "ms", "p95", "ms", "°C", "W", "MHz"])
        self.assertEqual(en[1].split(), cs[1].split())
        self.assertEqual(cs[2], "m: 2 rozhodnutí, J na rozhodnutí 1600.00, energie 0.889 Wh, nejvyšší teplota 50 °C, latence poslední ÷ první minuta —")
        self.assertEqual(en[2], "m: 2 decisions, J per decision 1600.00, energy 0.889 Wh, highest temperature 50 °C, latency last ÷ first minute —")
        self.assertEqual((len(cs), len(en)), (3, 3))

        def one_decision():                                              # zahřátí, jedno rozhodnutí, Ctrl+C
            post = FakePost()

            def fail(payload):
                if len(post.calls) == 3:
                    raise KeyboardInterrupt
            post.fail = fail
            return post
        (cs, end_cs), (en, end_en) = self.both("soak", "--minutes", "1", "m1", "m2", post_of=one_decision, sampler=FakeSampler([]), clock=FakeClock(1.0))
        self.assertEqual(cs[2], "m1: 1 rozhodnutí (přerušeno), J na rozhodnutí —, energie — Wh, nejvyšší teplota — °C, latence poslední ÷ první minuta —")
        self.assertEqual(en[2], "m1: 1 decision (interrupted), J per decision —, energy — Wh, highest temperature — °C, latency last ÷ first minute —")
        self.assertEqual((len(cs), len(en), end_cs, end_en), (3, 3, None, None))      # druhý model se po Ctrl+C nezačal měřit, v žádném jazyce

    def test_server_table_and_verdict_in_both_languages(self):
        def measure(code, argv, **timeline):
            tl = Timeline(service=0.1, **timeline)
            with i18n.process(code), mock.patch.multiple(hwbench.time, perf_counter=tl.clock, sleep=tl.sleep), mock.patch.object(hwbench, "start_thread", tl.spawn):
                return hwbench.measure(hwbench.parse_args(argv), "m", tl.post)
        fits, tight = ["server", "--n", "30", "--rates", "2,50", "--p99-ms", "1000", "m"], ["server", "--n", "20", "--rates", "1", "--p99-ms", "50", "m"]
        cs, en = measure("cs", fits), measure("en", fits)
        self.assertEqual((cs[0], en[0]), self.HEADS["server"])
        self.assertEqual((en[2], en[3]), (en[0], en[1]))                 # CSV = tabulka
        self.assertEqual([row[:5] for row in en[1]], [row[:5] for row in cs[1]])
        self.assertEqual(([row[5] for row in cs[1]], [row[5] for row in en[1]]), (["100 %", "40 %"], ["100%", "40%"]))      # procenta česky s mezerou jako dosud
        self.assertEqual(cs[4], ["m: sériově medián 100.0 ms (10.0 dotazů/s); nejvyšší udržitelné tempo 2.00 dotazů/s při p99 do 1000 ms"])
        self.assertEqual(en[4], ["m: serial median 100.0 ms (10.0 requests/s); highest sustainable rate 2.00 requests/s with p99 within 1000 ms"])
        self.assertEqual(measure("en", tight)[4], ["m: serial median 100.0 ms (10.0 requests/s); no rate level kept p99 within 50 ms"])
        stopped = {"fail_at": (1 + 20 + 5, KeyboardInterrupt())}       # zahřátí, 20 sériových, pátý dotaz 1. stupně
        cs, en = measure("cs", tight, **stopped)[4], measure("en", tight, **stopped)[4]
        self.assertEqual(cs, ["m: sériově medián 100.0 ms (10.0 dotazů/s); žádný stupeň nesplnil p99 do 50 ms (přerušeno)"])
        self.assertEqual(en, ["m: serial median 100.0 ms (10.0 requests/s); no rate level kept p99 within 50 ms (interrupted)"])
        self.assertTrue(cs.interrupted and en.interrupted)               # příznak, ne věta: v obou jazycích stejný
        at_once = {"fail_at": (1, KeyboardInterrupt())}                  # Ctrl+C už při zahřívacím dotazu: bez čísel
        self.assertEqual(measure("cs", tight, **at_once)[4], ["m: sériově medián — ms (— dotazů/s); žádný stupeň nesplnil p99 do 50 ms (přerušeno)"])
        self.assertEqual(measure("en", tight, **at_once)[4], ["m: serial median — ms (— requests/s); no rate level kept p99 within 50 ms (interrupted)"])

    def test_interruption_is_never_read_from_the_sentence(self):        # věta je v jazyce uživatele; další model nezačne podle příznaku Notes.interrupted
        for more in ([], ["--lang", "en"]):
            with self.subTest(more=more):
                sent, inner = [], FakePost()

                def post(model, payload):
                    sent.append(model)
                    return inner(model, payload)
                lines, code = self.cli("soak", "--minutes", "1", "m(přerušeno)", "m(interrupted)", *more, post=post,
                                       sampler=FakeSampler(self.SAMPLES), clock=FakeClock(20.0))
                self.assertIsNone(code)
                self.assertTrue(any(line.startswith("m(přerušeno): 2 ") for line in lines), lines)      # značka stojí ve větě souhrnu, měření přitom přerušené nebylo
                self.assertEqual(sorted(set(sent)), ["m(interrupted)", "m(přerušeno)"])               # a druhý model se přesto změřil

    def test_num_parallel_line_in_both_languages(self):
        (cs, _), (en, _) = self.both("server", "--n", "5", "--rates", "500", "m", post_of=lambda: FakePost(ms=[1.0]))
        self.assertEqual(cs[0], "OLLAMA_NUM_PARALLEL (v prostředí tohoto procesu): nenastaveno — bez něj server dotazy řadí za sebe\n")
        self.assertEqual(en[0], "OLLAMA_NUM_PARALLEL (in the environment of this process): not set — without it the server queues the requests one after another\n")
        self.assertEqual((len(cs), len(en)), (4, 4))                     # hlavička, záhlaví, jeden stupeň, věta
        self.assertTrue(en[3].startswith("m: serial median "), en[3])
        with mock.patch.dict(os.environ, {"OLLAMA_NUM_PARALLEL": "4"}):
            (cs, _), (en, _) = self.both("concurrency", "--n", "8", "m", clock=FakeClock(2.0))
        self.assertEqual(cs[0], "OLLAMA_NUM_PARALLEL (v prostředí tohoto procesu): 4 — bez něj server dotazy řadí za sebe\n")
        self.assertEqual(en[0], "OLLAMA_NUM_PARALLEL (in the environment of this process): 4 — without it the server queues the requests one after another\n")
        self.assertEqual(en[1].split(), ["model", "clients", "N", "dec./s", "p95", "ms"])
        self.assertEqual([line.split() for line in en[2:]], [line.split() for line in cs[2:]])
        self.assertEqual(len(en), 2 + 4)

    def test_level_that_did_not_fit_in_both_languages(self):
        (cs, _), (en, _) = self.both("questions", "m1", post_of=lambda: FakePost(fail=refuse_over(8)))
        self.assertEqual(cs[5], "m1                          16   nevešlo se             —")
        self.assertEqual(en[5].split(), ["m1", "16", "did", "not", "fit", "—"])
        self.assertEqual(len(en[5]), len(en[0]))                         # buňka se do sloupce „ms per request“ vejde
        self.assertEqual([line.split() for line in en[1:5]], [line.split() for line in cs[1:5]])
        (cs, _), (en, _) = self.both("context", "m", post_of=lambda: FakePost(tokens=150, fail=refuse_long_context))
        self.assertEqual(cs[-1], "m                           2000             — nevešlo se")
        self.assertEqual(en[-1], "m                              2000             — did not fit")      # širší než sloupec ms: mezera navíc drží buňky od sebe
        self.assertEqual(CZECH.findall("\n".join(en)), [])

    def test_skipped_tests_of_a_hosted_model_in_both_languages(self):
        crowd = ("souběžné dotazy na hostovaný model jsou mimo rozsah", "hwbench.py does not measure concurrent requests to a hosted model")
        cases = [(["cold", "test:echo"], "test:echo: test cold je jen pro lokální modely (ollama stop)", "test:echo: the cold test is only for local models (ollama stop)"),
                 (["concurrency", "--n", "4", "test:echo"], f"test:echo: test concurrency je jen pro lokální modely ({crowd[0]})",
                  f"test:echo: the concurrency test is only for local models ({crowd[1]})"),
                 (["server", "--n", "4", "--rates", "500", "test:echo"], f"test:echo: test server je jen pro lokální modely ({crowd[0]})",
                  f"test:echo: the server test is only for local models ({crowd[1]})"),
                 (["soak", "--minutes", "0.01", "test:echo"], "test:echo: test soak u hostovaného modelu stojí peníze po celou dobu běhu — spusťte ho výslovně s --hosted-soak",
                  "test:echo: the soak test with a hosted model costs money for the whole run duration — run it explicitly with --hosted-soak")]
        with mock.patch.object(providers, "net_line", side_effect=AssertionError("odhad sítě u přeskočeného testu")):
            for argv, cs, en in cases:
                with self.subTest(argv=argv):
                    (lines_cs, end_cs), (lines_en, end_en) = self.both(*argv, post_of=lambda: None, which=lambda name: "ollama",
                                                                      stop=lambda model: self.fail("ollama stop"), sampler=FakeSampler([]))
                    self.assertEqual((lines_cs[-1], lines_en[-1], end_cs, end_en), (cs, en, None, None))
                    self.assertEqual(CZECH.findall("\n".join(lines_en)), [])
                    self.assertEqual(len(lines_en), len(lines_cs))
        self.assertEqual(self.budget.state()["requests"], 0)             # přeskočený test nic neposlal, v žádném jazyce

    def test_context_note_of_a_hosted_model_in_both_languages(self):
        a = hwbench.parse_args(["context", "cf:clef"])
        self.assertEqual(hwbench.measure(a, "cf:clef", FakePost(tokens=150))[4], [
            "cf:clef: hostovaný model dlouhý vstup nemusí odmítnout — Clef ho podle dokumentace tiše ořízne; "
            "„nevešlo se“ proto nemusí nastat a směrodatný je sloupec input_tokens"])
        with i18n.process("en"):
            self.assertEqual(hwbench.measure(a, "cf:clef", FakePost(tokens=150))[4], [
                "cf:clef: a hosted model does not necessarily refuse a long input — according to the documentation, Clef silently truncates it; "
                '"did not fit" may therefore never appear, and the input tokens column is the one to rely on'])
            self.assertEqual(hwbench.measure(hwbench.parse_args(["context", "m"]), "m", FakePost(tokens=150))[4], [])

    def test_hosted_model_gets_the_english_table(self):                  # řádky útraty a sítě staví providers.py; jejich znění tenhle test nepřipíná
        (cs, end_cs), (en, end_en) = self.both("latency", "--n", "5", "test:echo", post_of=lambda: None)
        self.assertEqual((end_cs, end_en), (None, None))
        self.assertEqual(en[0].split(), ["model", "N", "p50", "ms", "p90", "ms", "p95", "ms", "p99", "ms", "max", "ms", "dec./s"])
        self.assertEqual(cs[0].split()[-1], "rozh./s")
        for lines in (cs, en):
            self.assertEqual(len(lines), 4)
            self.assertEqual(lines[1].split()[:2], ["test:echo", "5"])
            self.assertTrue(all(line.startswith("test:echo: ") for line in lines[2:]), lines)
        self.assertEqual(self.budget.state()["requests"], 2 * 6)         # dvakrát 5 dotazů a zahřívací


if __name__ == "__main__":
    unittest.main()
