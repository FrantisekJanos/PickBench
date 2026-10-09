import functools
import http.client
import json
import os
import re
import socket
import ssl
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
import i18n
import providers

JEV_KEY = "tajny-klic-JEV-7f3a9c"                    # poznatelné hodnoty: testy je hledají ve všem, co modul vrátí nebo vyhodí
CF_ACCOUNT = "0123456789abcdef0123456789abcdef"
CF_TOKEN = "tajny-token-CF-55aa77"
ENV = {"TYPESAFE_API_KEY": JEV_KEY, "CLOUDFLARE_ACCOUNT_ID": CF_ACCOUNT, "CLOUDFLARE_API_TOKEN": CF_TOKEN}
TEST_ENV = {"PICKBENCH_TEST_PROVIDER": "1"}
PAYLOAD = {"state": {"current_lane": "clear"}, "questions": {"move": {"type": "choice", "instructions": "Which lane?",
                                                                         "criteria": {"left": "move left", "stay": None}}}}
ANSWER = {"model": "x", "answers": {"move": {"choice": "left", "confidence": 0.9}}, "usage": {"input_tokens": 120, "output_tokens": 1}}


def setUpModule():                                  # post() bez budget= jde přes počítadlo procesu, které při importu postavily limity z prostředí vývojáře: pro testy modulu čisté
    patcher = mock.patch.object(providers, "BUDGET", providers.Budget(env={}))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    env = mock.patch.dict(os.environ)                # testy českých vět volají modul přímo a ten čte jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní a po něm vrátí
    env.start()
    unittest.addModuleCleanup(env.stop)
    os.environ.pop(i18n.ENV, None)


def both_languages(test):
    """Test úniku klíče pustí česky i anglicky: jeho tvrzení na jazyce nezávisí, věty, které prohledává, ano."""
    @functools.wraps(test)
    def run(self):
        for code in i18n.LANGS:
            with self.subTest(lang=code), i18n.process(code):
                test(self)
    return run


def reply(status=200, body=ANSWER, headers=None):
    return status, {k.lower(): v for k, v in (headers or {}).items()}, body if isinstance(body, bytes) else json.dumps(body).encode()


class Script:
    """Podstrčený transport: vrací připravené odpovědi po řadě (výjimku vyhodí) a pamatuje si, co dostal."""

    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    def __call__(self, host, path, headers, body, timeout):
        self.calls.append({"host": host, "path": path, "headers": dict(headers), "body": json.loads(body), "timeout": timeout})
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, BaseException):
            raise step
        return step


class Sleeps:
    def __init__(self):
        self.waits = []

    def __call__(self, seconds):
        self.waits.append(seconds)


class Clock:
    """Hodiny, které při každém čtení poskočí o 0,1 s: každý pokus tak „trvá“ 100 ms."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.1
        return self.t


def post(model, steps, env=ENV, **kw):
    script, sleeps = steps if isinstance(steps, Script) else Script(*steps), Sleeps()
    kw.setdefault("budget", providers.Budget(env={}))
    out = providers.post(model, PAYLOAD, transport=script, env=env, sleep=sleeps, clock=Clock(), **kw)
    return out, script, sleeps


def failure(model, steps, env=ENV, budget=None):
    """Chyba, kterou dotaz skončí, plus transport a čekání."""
    script, sleeps = Script(*steps), Sleeps()
    try:
        providers.post(model, PAYLOAD, transport=script, env=env, sleep=sleeps, clock=Clock(), budget=budget or providers.Budget(env={}))
    except providers.ProviderError as e:
        return e, script, sleeps
    raise AssertionError("dotaz měl skončit chybou")


class Models(unittest.TestCase):
    def test_prefix_names_the_provider(self):
        for model, want in (("jev:jev-1.13.0", "jev"), ("cf:clef", "cf"), ("cf:cokoli", "cf"), ("tev1", None), ("tev1:0.8b", None),
                            ("nimble:9b-q4_K_M", None), ("test:echo", None), ("", None), (None, None), (5, None), ("jev", None)):
            with self.subTest(model=model):
                self.assertEqual(providers.provider_of(model, {}), want)
                self.assertEqual(providers.hosted(model, {}), want is not None)

    def test_test_provider_exists_only_with_the_variable(self):
        self.assertEqual(providers.provider_of("test:echo", TEST_ENV), "test")
        for value in ("0", "", "true", "yes", " 1"):
            with self.subTest(value=value):
                self.assertIsNone(providers.provider_of("test:echo", {"PICKBENCH_TEST_PROVIDER": value}))

    def test_offered_models_need_their_keys(self):
        names = lambda env: [m["name"] for m in providers.available(env)]
        self.assertEqual(names({}), [])
        self.assertEqual(names({"TYPESAFE_API_KEY": JEV_KEY}), ["jev:jev-1.13.0"])
        self.assertEqual(names({"CLOUDFLARE_API_TOKEN": CF_TOKEN}), [])                       # bez ID účtu Cloudflare nejde
        self.assertEqual(names({"CLOUDFLARE_API_TOKEN": CF_TOKEN, "CLOUDFLARE_ACCOUNT_ID": CF_ACCOUNT}), ["cf:clef-flash", "cf:clef"])
        self.assertEqual(names({"TYPESAFE_API_KEY": "   "}), [])                               # prázdná hodnota = nenastaveno
        self.assertEqual(names(ENV), ["jev:jev-1.13.0", "cf:clef-flash", "cf:clef"])
        self.assertEqual(names(TEST_ENV), ["test:echo", "test:drahy"])
        self.assertEqual(providers.available({"TYPESAFE_API_KEY": JEV_KEY})[0],
                         {"name": "jev:jev-1.13.0", "provider": "jev", "label": "Jev", "who": "TypeSafe AI", "usd_per_m": 0.042})

    @both_languages
    def test_offer_never_carries_a_key(self):
        text = json.dumps(providers.available({**ENV, **TEST_ENV}))
        for secret in (JEV_KEY, CF_ACCOUNT, CF_TOKEN):
            self.assertNotIn(secret, text)

    def test_price_list_has_a_date_and_a_source(self):
        self.assertRegex(providers.PRICES_AS_OF, r"^\d{4}-\d\d-\d\d$")
        self.assertEqual({m: providers.MODELS[m]["usd_per_m"] for m in ("jev:jev-1.13.0", "cf:clef-flash", "cf:clef")},
                         {"jev:jev-1.13.0": 0.042, "cf:clef-flash": 0.09, "cf:clef": 0.24})
        for pid in ("jev", "cf"):
            self.assertTrue(providers.PROVIDERS[pid]["prices"].startswith("https://"))


class Requests(unittest.TestCase):
    def test_jev_request(self):
        (out, ms, meta), script, sleeps = post("jev:jev-1.13.0", [reply()])
        call = script.calls[0]
        self.assertEqual((call["host"], call["path"], call["timeout"]), ("api.typesafe.ai", "/v1/systemone", 30))
        self.assertEqual(call["headers"], {"Content-Type": "application/json", "Authorization": "Bearer " + JEV_KEY})
        self.assertEqual(call["body"], {"model": "jev-1.13.0", **PAYLOAD})
        self.assertEqual(out, ANSWER)
        self.assertAlmostEqual(ms, 100.0)
        self.assertEqual(meta, {"provider": "jev", "attempts": 1, "retries": 0, "waited_ms": 0.0, "tokens": 120, "model_ms": None,
                                "usd": 120 * 0.042 / 1_000_000})
        self.assertEqual(sleeps.waits, [])

    def test_cloudflare_request_and_envelope(self):
        wrapped = {"result": ANSWER, "success": True, "errors": [], "messages": []}
        (out, ms, meta), script, _ = post("cf:clef-flash", [reply(body=wrapped)])
        call = script.calls[0]
        self.assertEqual((call["host"], call["path"]),
                         ("api.cloudflare.com", f"/client/v4/accounts/{CF_ACCOUNT}/ai/run/@cf/cloudflare/clef-flash"))
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + CF_TOKEN)
        self.assertEqual(call["body"]["model"], "clef-flash")
        self.assertEqual((out, meta["provider"], meta["tokens"]), (ANSWER, "cf", 120))

    def test_cloudflare_answer_without_envelope_passes_too(self):
        self.assertEqual(post("cf:clef", [reply()])[0][0], ANSWER)

    def test_only_state_and_questions_leave(self):                      # stránka nesmí k poskytovateli propašovat jiné pole
        script = Script(reply())
        providers.post("jev:jev-1.13.0", {**PAYLOAD, "model": "jiny", "stream": True, "scenario": "obstacles", "url": "https://evil.example"},
                       transport=script, env=ENV)
        self.assertEqual(script.calls[0]["body"], {"model": "jev-1.13.0", **PAYLOAD})

    def test_model_outside_the_list_is_refused_without_a_request(self):
        for model in ("cf:cokoli", "jev:jev-latest", "cf:../../user", "cf:clef-flash/../x", "jev:", "test:echo"):
            with self.subTest(model=model):
                e, script, _ = failure(model, [reply()])
                self.assertIsInstance(e, providers.Refused)
                self.assertEqual(script.calls, [])
                self.assertIn("není v seznamu hostovaných modelů", str(e))

    def test_missing_key_is_refused_and_names_the_variable(self):
        for model, env, name in (("jev:jev-1.13.0", {}, "TYPESAFE_API_KEY"),
                                 ("cf:clef", {"CLOUDFLARE_API_TOKEN": CF_TOKEN}, "CLOUDFLARE_ACCOUNT_ID"),
                                 ("cf:clef", {"CLOUDFLARE_ACCOUNT_ID": CF_ACCOUNT}, "CLOUDFLARE_API_TOKEN"),
                                 ("cf:clef", {"CLOUDFLARE_ACCOUNT_ID": CF_ACCOUNT, "CLOUDFLARE_API_TOKEN": " "}, "CLOUDFLARE_API_TOKEN")):
            with self.subTest(model=model, name=name):
                e, script, _ = failure(model, [reply()], env=env)
                self.assertIsInstance(e, providers.Refused)
                self.assertEqual(script.calls, [])
                self.assertIn(name, str(e))
                self.assertNotIn("\n", str(e))

    @both_languages
    def test_key_that_cannot_go_into_a_header_is_refused_without_showing_it(self):
        for name, value in (("TYPESAFE_API_KEY", "tajny-klic\nX-Evil: 1"), ("TYPESAFE_API_KEY", "tajny klic s mezerou"),
                            ("TYPESAFE_API_KEY", "tajny-klíč-s-háčkem")):
            with self.subTest(value=value):
                e, script, _ = failure("jev:jev-1.13.0", [reply()], env={name: value})
                self.assertIsInstance(e, providers.Refused)
                self.assertEqual(script.calls, [])
                self.assertIn(name, str(e))
                self.assertNotIn("tajny", str(e))
        e, script, _ = failure("cf:clef", [reply()], env={"CLOUDFLARE_ACCOUNT_ID": "abc/../../user", "CLOUDFLARE_API_TOKEN": CF_TOKEN})
        self.assertIsInstance(e, providers.Refused)                      # ID účtu jde do adresy: jen písmena a číslice
        self.assertEqual(script.calls, [])
        self.assertNotIn("abc/", str(e))

    def test_body_over_the_limit_is_not_sent(self):
        big = {"state": {"text": "x" * providers.MAX_BODY}, "questions": PAYLOAD["questions"]}
        script = Script(reply())
        with self.assertRaises(providers.Refused) as e:
            providers.post("jev:jev-1.13.0", big, transport=script, env=ENV)
        self.assertEqual(script.calls, [])
        self.assertIn(str(providers.MAX_BODY), str(e.exception))

    def test_payload_that_is_not_an_object_is_refused(self):
        for bad in (None, [1], "x", 5):
            with self.subTest(bad=bad), self.assertRaises(providers.Refused):
                providers.post("jev:jev-1.13.0", bad, transport=Script(reply()), env=ENV)


class Errors(unittest.TestCase):
    def test_statuses_that_are_not_retried(self):
        for status in (400, 404, 422):
            with self.subTest(status=status):
                e, script, sleeps = failure("jev:jev-1.13.0", [reply(status, {"error": "bad input"})])
                self.assertEqual((e.status, e.provider, len(script.calls), sleeps.waits), (status, "jev", 1, []))
                self.assertEqual(str(e), f'Jev {status}: {{"error": "bad input"}}')
                self.assertNotIsInstance(e, providers.Refused)

    def test_bad_key_ends_at_once_without_the_providers_words(self):
        for model, label, names in (("jev:jev-1.13.0", "Jev", "TYPESAFE_API_KEY"), ("cf:clef", "Cloudflare", "CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN")):
            for status in (401, 403):
                with self.subTest(model=model, status=status):
                    echo = {"error": f"Invalid API key: {JEV_KEY[:12]}… / {CF_TOKEN}"}           # poskytovatel klíč v chybě zopakuje
                    e, script, sleeps = failure(model, [reply(status, echo)])
                    self.assertEqual((e.status, len(script.calls), sleeps.waits), (status, 1, []))
                    self.assertEqual(str(e), f"{label} {status}: klíč neplatí nebo nemá oprávnění (proměnné prostředí: {names})")

    @both_languages
    def test_no_error_text_carries_a_key(self):
        leaky = f"denied for {JEV_KEY} on account {CF_ACCOUNT} with {CF_TOKEN}"
        cases = [[reply(400, leaky.encode())], [reply(500, leaky.encode())], [reply(429, leaky.encode())], [reply(200, leaky.encode())],
                 [ConnectionResetError(leaky)], [http.client.RemoteDisconnected(leaky)], [TimeoutError(leaky)], [OSError(leaky)],
                 [reply(401, leaky.encode())]]
        for model in ("jev:jev-1.13.0", "cf:clef"):
            own = [[reply(200, {"success": False, "errors": [{"message": leaky}]})]] if model == "cf:clef" else []      # chyba v obálce Cloudflare
            for steps in cases + own:
                with self.subTest(model=model, steps=str(steps)[:60]):
                    e, _, _ = failure(model, steps)
                    chain = [e, e.__cause__, e.__context__]
                    for secret in (JEV_KEY, CF_ACCOUNT, CF_TOKEN):
                        self.assertNotIn(secret, str(e))
                        self.assertNotIn(secret, repr(e))
                    self.assertEqual(chain[1:], [None, None])               # ani původní výjimka se s chybou nenese

    @both_languages
    def test_answer_that_repeats_a_key_is_cleaned(self):                # i odpověď 200: server ji posílá stránce celou
        body = {"answers": {"move": {"choice": "left", "note": f"key {JEV_KEY}"}}, "debug": {"account": CF_ACCOUNT, "auth": "Bearer " + CF_TOKEN}}
        for model in ("jev:jev-1.13.0", "cf:clef"):
            with self.subTest(model=model):
                (out, _, _), _, _ = post(model, [reply(body=body)])
                self.assertEqual(out, {"answers": {"move": {"choice": "left", "note": "key ***"}}, "debug": {"account": "***", "auth": "Bearer ***"}})

    @both_languages
    def test_key_spelled_with_json_escapes_never_reaches_the_answer_or_an_error(self):
        def spellings(key):                                              # platné zápisy téhož textu v řetězci JSON
            short = lambda c: json.dumps(c)[1:-1]                        # " a \ krátce, ostatní doslova
            return [short(key), json.dumps(key)[1:-1].replace("/", "\\/"),
                    "".join(f"\\u{ord(c):04x}" for c in key), "".join(f"\\u{ord(c):04X}" for c in key),
                    "".join(f"\\u{ord(c):04x}" if i % 2 else short(c) for i, c in enumerate(key)),
                    "".join(f"\\u{ord(c):04X}" if c in '+/&=' else short(c) for c in key)]
        for key in ("abCD+ef/gh&ij==", 'x"y\\z/Q+w9'):
            for model, env, label in (("jev:jev-1.13.0", {"TYPESAFE_API_KEY": key}, "Jev"),
                                      ("cf:clef", {"CLOUDFLARE_ACCOUNT_ID": CF_ACCOUNT, "CLOUDFLARE_API_TOKEN": key}, "Cloudflare")):
                for spelled in spellings(key):
                    with self.subTest(model=model, key=key, spelled=spelled):
                        ok = ('{"answers": {"move": {"choice": "left", "confidence": 0.9, "note": "key ' + spelled + '"}}, "usage": {"input_tokens": 120}}').encode()
                        (out, _, _), _, _ = post(model, [reply(200, ok)], env=env)
                        for blob in (json.dumps(out), json.dumps(out, ensure_ascii=False), repr(out)):
                            self.assertNotIn(key, blob)
                            self.assertNotIn(json.dumps(key)[1:-1], blob)
                        self.assertEqual(out["answers"]["move"]["note"], "key ***")      # vrácené (už dekódované) texty klíč nenesou
                        e, _, _ = failure(model, [reply(400, ('{"error": "bad ' + spelled + '"}').encode())], env=env)
                        self.assertEqual(str(e), label + ' 400: {"error": "bad ***"}')
                        for text in (str(e), repr(e)):
                            self.assertNotIn(key, text)
                            self.assertNotIn(spelled, text)

    @both_languages
    def test_scrub(self):
        self.assertEqual(providers.scrub(f"a {JEV_KEY} b {CF_TOKEN} c {CF_ACCOUNT}", ENV), "a *** b *** c ***")
        self.assertEqual(providers.scrub("nic tajného", ENV), "nic tajného")
        self.assertEqual(providers.scrub("a b c", {"TYPESAFE_API_KEY": "a"}), "a b c")       # krátká hodnota (do 3 znaků) by rozbila každý text
        odd = {"TYPESAFE_API_KEY": 'ab/cd+"ef\\gh=='}                    # klíč se znaky, které JSON zapisuje jinak: " jako \", \ jako \\, / někdy jako \/
        self.assertEqual(providers.scrub("bad key " + odd["TYPESAFE_API_KEY"], odd), "bad key ***")
        self.assertEqual(providers.scrub(json.dumps({"error": "bad key " + odd["TYPESAFE_API_KEY"]}), odd), '{"error": "bad key ***"}')
        self.assertEqual(providers.scrub('{"error": "bad key ab\\/cd+\\"ef\\\\gh=="}', odd), '{"error": "bad key ***"}')
        self.assertEqual(sorted(providers.secrets(ENV)), sorted([JEV_KEY, CF_ACCOUNT, CF_TOKEN]))
        self.assertEqual(providers.secrets({}), [])

    @both_languages
    def test_scrub_finds_a_key_in_every_spelling_json_allows(self):         # JSON smí každý znak zapsat i jako \uXXXX (a–f malými i velkými) a " \ / ještě krátce; v jednom výskytu se zápisy míchají
        odd = {"TYPESAFE_API_KEY": 'ab/cd+"ef\\gh=='}
        for spelled in ('ab\\u002fcd\\u002B\\"ef\\u005cgh\\u003d\\u003D',     # \u malými i velkými písmeny, krátké \"
                        'ab\\/cd\\u002b"ef\\\\gh==',                             # krátké \/ a \\, \u u plusu, doslovná uvozovka
                        '\\u0061\\u0062/cd+\\u0022ef\\u005Cgh\\u003d=',          # písmena, uvozovka a zpětné lomítko jako \u
                        'ab/cd+"ef\\u005cgh=='):                                # doslovný zbytek s jediným \u
            with self.subTest(spelled=spelled):
                self.assertEqual(providers.scrub('{"error": "bad key ' + spelled + '"}', odd), '{"error": "bad key ***"}')
        self.assertEqual(providers.scrub("a \\u002f b \\u0041 c \\u002 d ab\\u002fcd+\\u002", odd), "a \\u002f b \\u0041 c \\u002 d ab\\u002fcd+\\u002")      # cizí \u a neúplný zápis se nemění
        non_ascii = {"TYPESAFE_API_KEY": "tajny-klíč-\U0001f600-x"}      # i znak mimo ASCII (json.dumps ho píše \uXXXX, nad U+FFFF dvojicí): dosavadní pokrytí zůstává
        for spelled in ("tajny-kl\\u00ed\\u010d-\\ud83d\\ude00-x", "tajny-kl\\u00ED\\u010D-\\uD83D\\uDE00-x", "tajny-klí\\u010d-\U0001f600-x"):
            with self.subTest(spelled=spelled):
                self.assertEqual(providers.scrub("v " + spelled + " v", non_ascii), "v *** v")

    def test_answer_that_is_not_a_json_object(self):
        for body in (b"<html>gateway</html>", b"[1, 2]", b'"text"', b""):
            with self.subTest(body=body):
                e, script, _ = failure("jev:jev-1.13.0", [reply(200, body)])
                self.assertEqual((e.status, len(script.calls)), (200, 1))
                self.assertIn("odpověď není objekt JSON", str(e))

    def test_cloudflare_success_false_is_an_error(self):
        e, _, _ = failure("cf:clef", [reply(200, {"success": False, "errors": [{"code": 5007, "message": "No such model"}], "result": None})])
        self.assertEqual(e.status, 200)
        self.assertIn("No such model", str(e))

    def test_long_error_body_is_cut(self):
        e, _, _ = failure("jev:jev-1.13.0", [reply(400, b"x" * 5000)])
        self.assertLessEqual(len(str(e)), 420)

    def test_status_and_body_can_be_read_back_from_the_text(self):
        e, _, _ = failure("cf:clef", [reply(400, {"errors": [{"message": "bad input"}]})])
        wrapped = RuntimeError(f"{e} (úloha: x)")
        wrapped.__cause__ = e
        for err in (e, wrapped):                                             # stav a tělo nese chyba sama, ne její věta
            with self.subTest(err=str(err)[:30]):
                self.assertTrue(providers.is_provider_error(err))
                found = providers.find_error(err)
                self.assertEqual((found.status, found.text, found.code), (400, '{"errors": [{"message": "bad input"}]}', "provider"))
        self.assertEqual((providers.Refused("jev", None, "x").code, providers.BudgetExceeded("jev", None, "x").code), ("refused", "budget"))
        self.assertFalse(hasattr(providers, "STATUS_RE") or hasattr(providers, "BODY_RE"))
        for other in (RuntimeError("boom"), ValueError("Cloudflare 400: x"), KeyError("x"), RuntimeError("Ollama 400: x"),
                      RuntimeError("Cloudflare 400: jen věta"), RuntimeError("Jev 422: x")):      # věta, která jen vypadá jako chyba poskytovatele
            with self.subTest(other=other):
                self.assertFalse(providers.is_provider_error(other))
        self.assertTrue(providers.is_provider_error(providers.Refused("jev", None, "chybí klíč")))   # i bez stavu v textu


class Retries(unittest.TestCase):
    def test_429_then_200(self):
        (out, ms, meta), script, sleeps = post("jev:jev-1.13.0", [reply(429, {"error": "slow down"}), reply()])
        self.assertEqual((out, len(script.calls), sleeps.waits), (ANSWER, 2, [1]))
        self.assertAlmostEqual(ms, 100.0)                                # doba posledního pokusu, bez čekání
        self.assertEqual((meta["attempts"], meta["retries"]), (2, 1))
        self.assertAlmostEqual(meta["waited_ms"], 1100.0)                # 1 s čekání + 100 ms neúspěšného pokusu

    def test_statuses_that_are_retried_with_growing_waits(self):
        for status in (429, 500, 502, 503, 529, 599):
            with self.subTest(status=status):
                e, script, sleeps = failure("cf:clef", [reply(status, {"errors": [{"message": "busy"}]})])
                self.assertEqual((e.status, len(script.calls), sleeps.waits), (status, 4, [1, 2, 4]))
                self.assertTrue(str(e).startswith(f"Cloudflare {status}: "))
                self.assertIn("po 4 pokusech", str(e))

    def test_retry_after_replaces_the_wait(self):
        steps = [reply(429, b"", {"Retry-After": "3"}), reply(529, b"", {"Retry-After": "0.5"}), reply(503, b"", {"Retry-After": "nesmysl"}), reply()]
        (out, ms, meta), script, sleeps = post("jev:jev-1.13.0", steps)
        self.assertEqual((sleeps.waits, meta["attempts"]), ([3.0, 0.5, 4], 4))

    def test_retry_after_longer_than_a_minute_ends_the_run(self):
        e, script, sleeps = failure("jev:jev-1.13.0", [reply(429, b"", {"Retry-After": "3600"}), reply()])
        self.assertEqual((e.status, len(script.calls), sleeps.waits), (429, 1, []))
        self.assertIn("3600", str(e))

    def test_negative_or_non_finite_retry_after_is_ignored(self):
        for value in ("-5", "nan", "inf", ""):
            with self.subTest(value=value):
                _, _, sleeps = post("jev:jev-1.13.0", [reply(429, b"", {"Retry-After": value}), reply()])
                self.assertEqual(sleeps.waits, [1])

    def test_dropped_connection_is_tried_again_at_once_then_with_waits(self):
        (out, _, meta), script, sleeps = post("jev:jev-1.13.0", [ConnectionResetError("reset"), reply()])
        self.assertEqual((out, len(script.calls), sleeps.waits, meta["retries"]), (ANSWER, 2, [], 1))
        e, script, sleeps = failure("jev:jev-1.13.0", [http.client.RemoteDisconnected("closed")])
        self.assertEqual((e.status, len(script.calls), sleeps.waits), (None, 4, [2, 4]))
        self.assertEqual(str(e), "Jev: spojení se nepodařilo po 4 pokusech (RemoteDisconnected: closed)")

    def test_timeout_is_not_retried(self):
        e, script, sleeps = failure("jev:jev-1.13.0", [TimeoutError("timed out"), reply()])
        self.assertEqual((e.status, len(script.calls), sleeps.waits), (None, 1, []))
        self.assertEqual(str(e), "Jev: poskytovatel neodpověděl do 30 s")

    def test_mixed_failures_share_the_four_attempts(self):
        e, script, sleeps = failure("jev:jev-1.13.0", [reply(500, b"a"), ConnectionResetError("reset"), reply(429, b"b"), reply(503, b"c"), reply()])
        self.assertEqual((e.status, len(script.calls)), (503, 4))

    def test_model_time_from_server_timing(self):
        for header, want in (("model;dur=41.5, server;dur=2, total;dur=44", 41.5), ("total;dur=44, model;desc=x;dur=7", 7.0),
                             ("server;dur=2", None), ("model;dur=abc", None), ("", None)):
            with self.subTest(header=header):
                (_, _, meta), _, _ = post("jev:jev-1.13.0", [reply(headers={"Server-Timing": header})])
                self.assertEqual(meta["model_ms"], want)

    def test_tokens_that_are_not_a_count_are_none(self):
        for usage in (None, {}, {"input_tokens": "12"}, {"input_tokens": -1}, {"input_tokens": 0}, {"input_tokens": True}, {"input_tokens": 1.5}, "x"):     # i nula: dotaz s tělem nemůže stát nula vstupních tokenů
            with self.subTest(usage=usage):
                (_, _, meta), _, _ = post("jev:jev-1.13.0", [reply(body={"answers": {}, "usage": usage})])
                self.assertIsNone(meta["tokens"])

    def test_default_sleep_is_the_modules_own(self):                    # server volá post() bez sleep: testy podstrkují providers._sleep
        waits = []
        with mock.patch.object(providers, "_sleep", waits.append):
            providers.post("jev:jev-1.13.0", PAYLOAD, transport=Script(reply(500, b""), reply()), env=ENV)
        self.assertEqual(waits, [1])


class TestProvider(unittest.TestCase):
    def test_echo_answers_without_any_network(self):
        with mock.patch.object(providers, "https_transport", side_effect=AssertionError("síť")):
            out, ms, meta = providers.post("test:echo", PAYLOAD, env=TEST_ENV)
        self.assertEqual(out["answers"], {"move": {"choice": "left", "confidence": 0.9}})
        self.assertEqual((meta["provider"], meta["attempts"], meta["model_ms"]), ("test", 1, 1.5))
        self.assertEqual(out["usage"]["input_tokens"], meta["tokens"])
        self.assertGreater(meta["tokens"], 10)

    def test_question_without_choices_gets_a_noul(self):
        out, _, _ = providers.post("test:echo", {"state": {}, "questions": {"danger": {"type": "noul", "instructions": "?"}}}, env=TEST_ENV)
        self.assertEqual(out["answers"], {"danger": {"noul": 0.5}})

    def test_without_the_variable_it_is_not_a_hosted_model(self):
        self.assertFalse(providers.hosted("test:echo", {}))
        with self.assertRaises(providers.Refused):
            providers.post("test:echo", PAYLOAD, env={})

    def test_real_models_never_use_the_echo(self):
        script = Script(reply())
        providers.post("jev:jev-1.13.0", PAYLOAD, transport=script, env={**ENV, **TEST_ENV})
        self.assertEqual(script.calls[0]["host"], "api.typesafe.ai")


class Cost(unittest.TestCase):
    def test_cost_is_input_tokens_times_the_price(self):
        self.assertAlmostEqual(providers.cost_usd("jev:jev-1.13.0", 1_000_000), 0.042)
        self.assertAlmostEqual(providers.cost_usd("cf:clef-flash", 500), 0.000045)
        self.assertAlmostEqual(providers.cost_usd("cf:clef", 2000), 0.00048)
        self.assertEqual(providers.cost_usd("cf:clef", 0), 0.0)
        for model, tokens in (("cf:clef", None), ("cf:cokoli", 100), ("tev1", 100)):
            with self.subTest(model=model, tokens=tokens):
                self.assertIsNone(providers.cost_usd(model, tokens))

    def test_meta_and_budget_carry_the_cost(self):
        budget = providers.Budget(env={})
        (_, _, meta), _, _ = post("cf:clef", [reply()], budget=budget)
        self.assertAlmostEqual(meta["usd"], 120 * 0.24 / 1_000_000)
        state = budget.state()
        self.assertEqual((state["requests"], state["answered"], state["tokens"], state["retries"], state["unpriced"]), (1, 1, 120, 0, 0))
        self.assertAlmostEqual(state["usd"], 120 * 0.24 / 1_000_000)

    def test_answer_without_tokens_has_no_cost_and_is_counted_apart(self):
        budget = providers.Budget(env={})
        (_, _, meta), _, _ = post("cf:clef", [reply(body={"answers": {}})], budget=budget)
        self.assertIsNone(meta["usd"])
        state = budget.state()
        self.assertEqual((state["requests"], state["answered"], state["unpriced"], state["usd"]), (1, 1, 1, 0.0))


class Spending(unittest.TestCase):
    def test_defaults_and_variables(self):
        state = providers.Budget(env={}).state()
        self.assertEqual((state["max_usd"], state["max_requests"]), (1.0, 5000))
        self.assertEqual(state, {"requests": 0, "answered": 0, "tokens": 0, "usd": 0.0, "retries": 0, "unpriced": 0, "max_requests": 5000, "max_usd": 1.0})
        state = providers.Budget(env={"PICKBENCH_MAX_USD": "0.25", "PICKBENCH_MAX_HOSTED": "40"}).state()
        self.assertEqual((state["max_usd"], state["max_requests"]), (0.25, 40))
        state = providers.Budget(2, 10, env={"PICKBENCH_MAX_USD": "0.25", "PICKBENCH_MAX_HOSTED": "40"}).state()
        self.assertEqual((state["max_usd"], state["max_requests"]), (2.0, 10))               # výslovná hodnota má přednost před proměnnou
        state = providers.Budget(env={"PICKBENCH_MAX_USD": " 0,25 ", "PICKBENCH_MAX_HOSTED": "40"}).state()
        self.assertEqual((state["max_usd"], state["max_requests"]), (0.25, 40))              # desetinná čárka: „0,25“ nesmí tiše znamenat výchozí 1 $

    def test_nonsense_in_a_variable_is_the_default_not_no_limit(self):
        for value in ("", "abc", "-1", "nan", "inf", "-inf", "1e999", "bez limitu"):
            with self.subTest(value=value):
                state = providers.Budget(env={"PICKBENCH_MAX_USD": value, "PICKBENCH_MAX_HOSTED": value}).state()
                self.assertEqual((state["max_usd"], state["max_requests"]), (1.0, 5000))

    def test_request_limit_stops_before_sending(self):
        budget, script = providers.Budget(max_requests=3, env={}), Script(reply())
        for _ in range(3):
            providers.post("jev:jev-1.13.0", PAYLOAD, transport=script, env=ENV, budget=budget)
        with self.assertRaises(providers.BudgetExceeded) as e:
            providers.post("jev:jev-1.13.0", PAYLOAD, transport=script, env=ENV, budget=budget)
        self.assertEqual(len(script.calls), 3)                           # čtvrtý dotaz neodešel
        self.assertEqual(str(e.exception), "Jev: strop útraty — odesláno 3 hostovaných dotazů, limit je 3 (PICKBENCH_MAX_HOSTED); další dotaz se neposlal")
        self.assertEqual(budget.state()["requests"], 3)

    def test_dollar_limit_stops_before_sending(self):
        budget, script = providers.Budget(max_usd=0.0003, env={}), Script(reply())          # test:drahy: 120 tokenů = 0,12 $… zde Clef: 120 × 0,24 $/M
        sent = 0
        with self.assertRaises(providers.BudgetExceeded) as e:
            for _ in range(100):
                providers.post("cf:clef", PAYLOAD, transport=script, env=ENV, budget=budget)
                sent += 1
        self.assertEqual((sent, len(script.calls)), (11, 11))            # 10 × 0,0000288 $ je pod limitem, jedenáctý dotaz ho překročí, dvanáctý neodejde
        self.assertIn("limit je 0.0003 $ (PICKBENCH_MAX_USD)", str(e.exception))
        self.assertIn("utraceno 0.0003", str(e.exception))

    def test_zero_means_no_hosted_request_at_all(self):
        for budget in (providers.Budget(max_requests=0, env={}), providers.Budget(max_usd=0, env={})):
            script = Script(reply())
            with self.assertRaises(providers.BudgetExceeded):
                providers.post("jev:jev-1.13.0", PAYLOAD, transport=script, env=ENV, budget=budget)
            self.assertEqual(script.calls, [])

    def test_every_attempt_counts_and_the_limit_can_stop_a_retry(self):
        budget = providers.Budget(max_requests=2, env={})
        e, script, sleeps = failure("jev:jev-1.13.0", [reply(500, b"")], budget=budget)
        self.assertIsInstance(e, providers.BudgetExceeded)
        self.assertEqual((len(script.calls), sleeps.waits), (2, [1, 2]))
        state = budget.state()
        self.assertEqual((state["requests"], state["answered"], state["retries"]), (2, 0, 2))

    def test_refused_request_is_not_counted(self):
        budget = providers.Budget(env={})
        failure("cf:cokoli", [reply()], budget=budget)
        failure("jev:jev-1.13.0", [reply()], env={}, budget=budget)
        self.assertEqual(budget.state()["requests"], 0)

    def test_the_process_budget_is_used_when_none_is_given(self):
        with mock.patch.object(providers, "BUDGET", providers.Budget(max_requests=1, env={})):
            providers.post("jev:jev-1.13.0", PAYLOAD, transport=Script(reply()), env=ENV)
            with self.assertRaises(providers.BudgetExceeded):
                providers.post("jev:jev-1.13.0", PAYLOAD, transport=Script(reply()), env=ENV)

    def test_counting_is_safe_across_threads(self):
        budget, script = providers.Budget(max_requests=200, env={}), Script(reply())
        errors = []

        def work():
            for _ in range(50):
                try:
                    providers.post("jev:jev-1.13.0", PAYLOAD, transport=script, env=ENV, budget=budget)
                except providers.BudgetExceeded as e:
                    errors.append(e)
        threads = [threading.Thread(target=work) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((budget.state()["requests"], budget.state()["answered"], len(errors)), (200, 200, 100))

    SIZE = len(json.dumps({"model": "clef", **PAYLOAD}).encode())        # bajty těla dotazu, jak ho post() pošle Clef

    def test_requests_in_flight_hold_their_estimate_so_parallel_ones_cannot_overshoot(self):
        est = providers.estimate_usd("cf:clef", self.SIZE)
        self.assertAlmostEqual(est, -(-self.SIZE // 3) * 0.24 / 1_000_000)
        budget, gate, seen, sent, refused = providers.Budget(max_usd=2.5 * est, env={}), threading.Event(), threading.Semaphore(0), [], []

        def slow(host, path, headers, body, timeout):
            sent.append(len(body))
            seen.release()
            gate.wait(5)
            return reply()

        def work():
            try:
                providers.post("cf:clef", PAYLOAD, transport=slow, env=ENV, budget=budget)
            except providers.BudgetExceeded as e:
                refused.append(str(e))
                seen.release()
        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        waited = [seen.acquire(timeout=5) for _ in threads]              # každé z osmi vláken buď čeká na odpověď, nebo bylo odmítnuto
        gate.set()
        for t in threads:
            t.join()
        self.assertEqual((all(waited), sent, len(refused)), (True, [self.SIZE] * 3, 5))        # dva odhady na cestě jsou pod stropem, třetí ho naplní, čtvrtý dotaz už neodejde
        self.assertIn("drží odhad", refused[0])

    def test_attempt_whose_price_is_never_learned_keeps_its_estimate_in_the_limit(self):
        limit = 2.5 * providers.estimate_usd("cf:clef", self.SIZE)
        for step in (reply(body={"answers": {}}), TimeoutError("timed out"), reply(200, b"<html>")):      # bez tokenů, bez odpovědi do limitu, nečitelná odpověď
            with self.subTest(step=str(step)[:40]):
                budget, script, ends = providers.Budget(max_usd=limit, env={}), Script(step), []
                for _ in range(5):
                    try:
                        providers.post("cf:clef", PAYLOAD, transport=script, env=ENV, budget=budget)
                        ends.append("odpověď")
                    except providers.BudgetExceeded as e:
                        ends.append("strop")
                        self.assertIn("drží odhad", str(e))
                    except providers.ProviderError:
                        ends.append("chyba")
                self.assertEqual((len(script.calls), ends[3:], budget.state()["usd"]), (3, ["strop", "strop"], 0.0))

    def test_answer_reporting_zero_input_tokens_is_unpriced_and_keeps_its_estimate(self):
        est = providers.estimate_usd("cf:clef", self.SIZE)
        budget, script = providers.Budget(max_usd=0.5 * est, env={}), Script(reply(body={"answers": {}, "usage": {"input_tokens": 0}}))
        (_, _, meta), script, _ = post("cf:clef", script, budget=budget)
        self.assertEqual((meta["tokens"], meta["usd"]), (None, None))    # nula tokenů u neprázdného dotazu není cena nula: cena je neznámá
        state = budget.state()
        self.assertEqual((state["requests"], state["answered"], state["unpriced"], state["tokens"], state["usd"]), (1, 1, 1, 0, 0.0))
        with self.assertRaises(providers.BudgetExceeded) as e:           # odhad zůstal ve stropu, takže další dotaz neodejde
            providers.post("cf:clef", PAYLOAD, transport=script, env=ENV, budget=budget)
        self.assertIn("drží odhad", str(e.exception))
        self.assertEqual(len(script.calls), 1)
        limit = 1.5 * est                                                # pod dvěma odhady: druhý dotaz projde, třetí už ne
        budget, ends = providers.Budget(max_usd=limit, env={}), []
        for _ in range(4):
            try:
                providers.post("cf:clef", PAYLOAD, transport=script, env=ENV, budget=budget)
                ends.append("odpověď")
            except providers.BudgetExceeded:
                ends.append("strop")
        self.assertEqual((ends, budget.state()["unpriced"]), (["odpověď", "odpověď", "strop", "strop"], 2))

    def test_failed_attempt_that_is_retried_gives_its_estimate_back(self):
        budget = providers.Budget(max_usd=0.5 * providers.estimate_usd("cf:clef", self.SIZE), env={})      # strop pod jedním odhadem: kdyby kterýkoli pokus svůj odhad podržel, další dotaz by neodešel
        e, script, _ = failure("cf:clef", [reply(500, b""), reply(429, b""), reply(400, b"x")], budget=budget)
        self.assertEqual((e.status, len(script.calls)), (400, 3))        # 500 a 429 svůj odhad uvolnily: jinak by další pokus zastavil strop
        (out, _, _), script, _ = post("cf:clef", [ConnectionResetError("reset"), reply()], budget=budget)    # konečné 400 odhad uvolnilo, jinak by už tento dotaz zastavil strop
        self.assertEqual((out, len(script.calls)), (ANSWER, 2))          # spadlé spojení také: jinak by nový pokus zastavil strop


class Datasets(unittest.TestCase):
    def test_no_rule_says_which_sets_may_go_to_a_hosted_model(self):    # rozhodnutí uživatele 2026-10-07: smí kterákoli sada; ať se pravidlo nevrátí přes starý import
        for name in ("HOSTED_DATASETS", "SYNTHETIC", "SCENARIO_RE", "scenario_refusal", "dataset_key"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(providers, name))


class Network(unittest.TestCase):
    def test_connect_time_goes_to_the_providers_own_server(self):
        seen = []
        for model, host in (("jev:jev-1.13.0", "api.typesafe.ai"), ("cf:clef", "api.cloudflare.com")):
            ms = providers.connect_ms(model, ENV, connect=lambda h, timeout: seen.append((h, timeout)), clock=Clock())
            self.assertAlmostEqual(ms, 100.0)
            self.assertEqual(seen[-1], (host, 10))

    def test_test_provider_has_a_fixed_time_and_opens_nothing(self):
        def boom(host, timeout):
            raise AssertionError("síť")
        self.assertEqual(providers.connect_ms("test:echo", TEST_ENV, connect=boom), 1.5)

    def test_failed_connection_is_a_sentence_without_a_key(self):
        def refuse(host, timeout):
            raise OSError(f"cannot reach {host} with {JEV_KEY}")
        with self.assertRaises(providers.ProviderError) as e:
            providers.connect_ms("jev:jev-1.13.0", ENV, connect=refuse)
        self.assertEqual(str(e.exception), "Jev: spojení se serverem api.typesafe.ai se nepodařilo (OSError)")
        self.assertIsNone(e.exception.__context__)

    def test_model_that_cannot_be_called_is_refused(self):
        for model, env in (("cf:cokoli", ENV), ("jev:jev-1.13.0", {}), ("tev1", ENV)):
            with self.subTest(model=model), self.assertRaises(providers.Refused):
                providers.connect_ms(model, env, connect=lambda h, timeout: self.fail("síť"))

    def test_tls_context_is_built_outside_the_measured_interval(self):
        now, built, opened = [0.0], [], []                               # hodiny a „procesor“ jen z čísel; žádný skutečný socket

        def open_socket(address, timeout):
            opened.append((address, timeout))
            now[0] += 0.03                                               # TCP: 30 ms
            return mock.MagicMock()

        def wrap_socket(sock, server_hostname):
            now[0] += 0.02                                               # TLS: 20 ms
            return mock.MagicMock()

        def create_context():
            built.append(1)
            now[0] += 0.05                                               # kontext TLS (načtení certifikátů): 50 ms procesoru, se sítí nemá nic společného
            return mock.Mock(wrap_socket=wrap_socket)
        with mock.patch.object(providers, "_tls_context", None, create=True), mock.patch.object(ssl, "create_default_context", create_context), \
                mock.patch.object(socket, "create_connection", open_socket):
            first = providers.connect_ms("jev:jev-1.13.0", ENV, clock=lambda: now[0])
            second = providers.connect_ms("jev:jev-1.13.0", ENV, clock=lambda: now[0])
        self.assertAlmostEqual(first, 50.0)                              # jen TCP + TLS
        self.assertAlmostEqual(second, 50.0)
        self.assertEqual((len(built), opened), (1, [(("api.typesafe.ai", 443), 10)] * 2))        # kontext se staví jednou a znovu se používá

    def test_tls_context_that_cannot_be_built_is_a_connection_failure_not_a_raw_exception(self):
        def broken():
            raise ssl.SSLError(f"nelze načíst certifikáty pro {JEV_KEY}")
        sentence = "Jev: spojení se serverem api.typesafe.ai se nepodařilo (SSLError)"        # stejná věta jako u jiného selhání spojení
        cached = providers._tls_context                                      # mezipaměť kontextu je stav modulu: před testem ji vyprázdníme a po něm vrátíme
        with mock.patch.object(providers, "_tls_context", None), mock.patch.object(ssl, "create_default_context", broken), \
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("síť")):
            with self.assertRaises(providers.ProviderError) as e:
                providers.connect_ms("jev:jev-1.13.0", ENV)
            self.assertEqual(str(e.exception), sentence)
            self.assertIsNone(e.exception.__context__)                       # původní výjimka (s cizím textem) se s chybou nenese
            self.assertIsNone(e.exception.__cause__)
            self.assertEqual(providers.net_line("jev:jev-1.13.0", ENV), f"síť (odhad): nezměřeno ({sentence})")
            self.assertIsNone(providers._tls_context)                        # nic se do mezipaměti nezapsalo: další pokus kontext postaví znovu
        self.assertIs(providers._tls_context, cached)

    def test_default_connector_is_the_modules_own(self):                # server volá connect_ms() bez connect: testy podstrkují providers.tls_connect
        seen = []
        with mock.patch.object(providers, "tls_connect", lambda host, timeout: seen.append(host)):
            providers.connect_ms("cf:clef", ENV)
        self.assertEqual(seen, ["api.cloudflare.com"])


class Lines(unittest.TestCase):
    def test_net_line(self):
        self.assertEqual(providers.net_line("test:echo", TEST_ENV), "síť (odhad): spojení TCP + TLS 2 ms")

        def refuse(host, timeout):
            raise OSError("x")
        self.assertEqual(providers.net_line("jev:jev-1.13.0", ENV, connect=refuse),
                         "síť (odhad): nezměřeno (Jev: spojení se serverem api.typesafe.ai se nepodařilo (OSError))")

    def test_spent_line(self):
        budget = providers.Budget(env={})
        before = budget.state()
        self.assertIsNone(providers.spent_line("cf:clef", before, budget.state()))
        post("cf:clef", [reply(500, b""), reply()], budget=budget)
        post("cf:clef", [reply(body={"answers": {}})], budget=budget)
        self.assertEqual(providers.spent_line("cf:clef", before, budget.state()),
                         "hostovaný model (Cloudflare, Cloudflare Workers AI): 3 dotazů, 120 vstupních tokenů, 0.0000 $ (0.0144 $ / 1000 rozh.), "
                         "opakování 1; latence včetně sítě; 1 odpovědí bez údaje o tokenech — cena je spodní odhad")

    def test_spent_line_when_nothing_was_answered(self):
        budget = providers.Budget(env={})
        before = budget.state()
        failure("jev:jev-1.13.0", [reply(400, b"x")], budget=budget)
        self.assertEqual(providers.spent_line("jev:jev-1.13.0", before, budget.state()),
                         "hostovaný model (Jev, TypeSafe AI): 1 dotazů, 0 vstupních tokenů, 0.0000 $ (— $ / 1000 rozh.), opakování 0; latence včetně sítě")


class FakeConnection:
    """Náhrada http.client.HTTPSConnection: žádná síť."""
    made = []

    def __init__(self, host, timeout=None):
        self.host, self.timeout, self.requests, self.closed, self.fail = host, timeout, [], False, None
        FakeConnection.made.append(self)

    def request(self, method, path, body=None, headers=None):
        if self.fail:
            raise self.fail
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return mock.Mock(status=200, read=lambda: b"{}", getheaders=lambda: [("Server-Timing", "model;dur=1"), ("X-Other", "y")])

    def close(self):
        self.closed = True


class Transport(unittest.TestCase):
    def setUp(self):
        FakeConnection.made = []
        patcher = mock.patch.object(http.client, "HTTPSConnection", FakeConnection)
        patcher.start()
        self.addCleanup(patcher.stop)
        providers._pool.clear()
        self.addCleanup(providers._pool.clear)

    def test_connection_is_kept_open_and_outlives_the_thread_that_opened_it(self):
        for _ in range(3):
            status, headers, raw = providers.https_transport("api.typesafe.ai", "/v1/systemone", {"A": "b"}, b"{}", 30)
        self.assertEqual((status, headers, raw), (200, {"server-timing": "model;dur=1", "x-other": "y"}, b"{}"))
        self.assertEqual(len(FakeConnection.made), 1)
        self.assertEqual((FakeConnection.made[0].host, FakeConnection.made[0].timeout, len(FakeConnection.made[0].requests)), ("api.typesafe.ai", 30, 3))
        for _ in range(2):                                               # server hry obsluhuje každý požadavek v novém vlákně: spojení se kvůli tomu nesmí otevírat znovu
            other = threading.Thread(target=providers.https_transport, args=("api.typesafe.ai", "/v1/systemone", {}, b"{}", 30))
            other.start()
            other.join()
        self.assertEqual((len(FakeConnection.made), len(FakeConnection.made[0].requests)), (1, 5))
        providers.https_transport("api.cloudflare.com", "/x", {}, b"{}", 30)
        self.assertEqual(len(FakeConnection.made), 2)                    # jiný server má vlastní spojení

    def test_two_threads_at_once_never_share_a_connection(self):
        providers.https_transport("api.typesafe.ai", "/v1/systemone", {}, b"{}", 30)       # v zásobě teď čeká jedno volné spojení
        gate, inside, answer = threading.Event(), threading.Semaphore(0), FakeConnection.getresponse

        def slow(conn):
            inside.release()
            gate.wait(5)
            return answer(conn)
        with mock.patch.object(FakeConnection, "getresponse", slow):
            threads = [threading.Thread(target=providers.https_transport, args=("api.typesafe.ai", "/v1/systemone", {}, b"{}", 30)) for _ in range(2)]
            for t in threads:
                t.start()
            both = [inside.acquire(timeout=5) for _ in threads]          # obě vlákna čekají na odpověď zároveň
            gate.set()
            for t in threads:
                t.join()
        self.assertEqual((both, sorted(len(c.requests) for c in FakeConnection.made)), ([True, True], [1, 2]))   # jedno vlákno si vzalo spojení ze zásoby, druhé muselo otevřít vlastní
        providers.https_transport("api.typesafe.ai", "/v1/systemone", {}, b"{}", 30)
        self.assertEqual(len(FakeConnection.made), 2)                    # po odpovědi se obě spojení vrátila do zásoby

    def test_broken_connection_is_closed_and_replaced(self):
        providers.https_transport("api.typesafe.ai", "/v1/systemone", {}, b"{}", 30)
        first = FakeConnection.made[0]
        first.fail = ConnectionResetError("reset")
        with self.assertRaises(ConnectionResetError):
            providers.https_transport("api.typesafe.ai", "/v1/systemone", {}, b"{}", 30)
        self.assertTrue(first.closed)
        providers.https_transport("api.typesafe.ai", "/v1/systemone", {}, b"{}", 30)
        self.assertEqual(len(FakeConnection.made), 2)

    def test_a_dead_pooled_connection_takes_the_other_idle_ones_of_its_server_with_it(self):
        host, other_host = "api.typesafe.ai", "api.cloudflare.com"          # server mezitím zavřel všechna volná spojení, ne jen jedno
        dead = [FakeConnection(host, 30) for _ in range(4)]
        for conn in dead:
            conn.fail = ConnectionResetError("reset")
        elsewhere = FakeConnection(other_host, 30)
        providers._pool[host], providers._pool[other_host] = list(dead), [elsewhere]
        budget, sleeps = providers.Budget(env={}), Sleeps()
        out, _, meta = providers.post("jev:jev-1.13.0", PAYLOAD, env=ENV, sleep=sleeps, clock=Clock(), budget=budget)
        self.assertEqual((out, meta["attempts"], sleeps.waits, budget.state()["requests"]), ({}, 2, [], 2))     # hned druhý pokus uspěl, bez čekání a bez dalších mrtvých spojení
        opened = FakeConnection.made[len(dead) + 1:]
        self.assertEqual(len(opened), 1)                                 # jediné nové spojení
        self.assertTrue(all(conn.closed for conn in dead))
        self.assertEqual(providers._pool[host], opened)                  # v zásobě je jen živé spojení, žádné mrtvé
        self.assertEqual((opened[0].fail, opened[0].closed, len(opened[0].requests)), (None, False, 1))
        self.assertEqual(providers._pool[other_host], [elsewhere])       # jiný server zůstal, jak byl
        self.assertFalse(elsewhere.closed)

    def test_failure_on_a_fresh_connection_discards_nothing_else(self):
        host, other_host = "api.typesafe.ai", "api.cloudflare.com"
        healthy, elsewhere = FakeConnection(host, 30), FakeConnection(other_host, 30)
        providers._pool[other_host] = [elsewhere]

        def request(conn, method, path, body=None, headers=None):
            providers._pool[host].append(healthy)                        # zatímco se čekalo, jiné vlákno vrátilo do zásoby živé spojení
            raise ConnectionResetError("reset")
        with mock.patch.object(FakeConnection, "request", request), self.assertRaises(ConnectionResetError):
            providers.https_transport(host, "/v1/systemone", {}, b"{}", 30)
        fresh = FakeConnection.made[2]                                   # healthy, elsewhere, a teprve pak nové spojení, které selhalo
        self.assertTrue(fresh.closed)
        self.assertEqual((healthy.closed, providers._pool[host]), (False, [healthy]))
        self.assertEqual((elsewhere.closed, providers._pool[other_host]), (False, [elsewhere]))


CZECH = re.compile(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„“]")
OFFERED = "jev:jev-1.13.0, cf:clef-flash, cf:clef"                      # nabídka s klíči z ENV
BIG = {"state": {"text": "x" * providers.MAX_BODY}, "questions": PAYLOAD["questions"]}
BIG_SIZE = len(json.dumps({"model": "jev-1.13.0", **BIG}).encode())
TEST_WHO = {"cs": "zkušební poskytovatel (nic neodchází)", "en": "test provider (nothing leaves this computer)"}


def caught(call):
    """Chyba hostovaného modelu, kterou volání skončí."""
    try:
        call()
    except providers.ProviderError as e:
        return e
    raise AssertionError("volání mělo skončit chybou")


def no_route(host, timeout):
    raise OSError("no route")


def held():
    """Počítadlo, kterému dotaz na cestě drží odhad nad stropem."""
    budget = providers.Budget(max_usd=0.001, env={})
    budget.reserve("cf", 0.002)
    return budget


# Věty chyb modulu: klíč textu → (jak chybu vyvolat, česky doslova jako před překladem, anglicky). Podstrčené chyby nenesou český text.
SENTENCES = {
    "prov.label": (lambda: caught(lambda: providers.connect_ms("tev1", ENV, connect=no_route)),
                   f"Hostovaný model: model „tev1“ není v seznamu hostovaných modelů ({OFFERED})",
                   f'Hosted model: the model "tev1" is not in the list of hosted models ({OFFERED})'),
    "prov.notOffered": (lambda: failure("cf:cokoli", [reply()])[0],
                        f"Cloudflare: model „cf:cokoli“ není v seznamu hostovaných modelů ({OFFERED})",
                        f'Cloudflare: the model "cf:cokoli" is not in the list of hosted models ({OFFERED})'),
    "prov.keyMissing": (lambda: failure("jev:jev-1.13.0", [reply()], env={})[0],
                        "Jev: model jev:jev-1.13.0 potřebuje proměnnou prostředí TYPESAFE_API_KEY, která není nastavená",
                        "Jev: the model jev:jev-1.13.0 needs the environment variable TYPESAFE_API_KEY, which is not set"),
    "prov.keyChars": (lambda: failure("jev:jev-1.13.0", [reply()], env={"TYPESAFE_API_KEY": "a b"})[0],
                      "Jev: proměnná prostředí TYPESAFE_API_KEY obsahuje znaky, které v ní být nesmí (mezera, konec řádku, znak mimo ASCII)",
                      "Jev: the environment variable TYPESAFE_API_KEY contains characters that it must not contain "
                      "(a space, a line break, a non-ASCII character)"),
    "prov.payload": (lambda: caught(lambda: providers.post("jev:jev-1.13.0", [1], transport=Script(reply()), env=ENV)),
                     "Jev: dotaz musí být objekt se state a questions", "Jev: the request must be an object with state and questions"),
    "prov.tooBig": (lambda: caught(lambda: providers.post("jev:jev-1.13.0", BIG, transport=Script(reply()), env=ENV)),
                    f"Jev: dotaz má {BIG_SIZE} bajtů; hostovanému modelu se posílá nejvýš 64000",
                    f"Jev: the request is {BIG_SIZE} bytes long; PickBench sends at most 64000 to a hosted model"),
    "prov.timeout": (lambda: failure("jev:jev-1.13.0", [TimeoutError("timed out")])[0],
                     "Jev: poskytovatel neodpověděl do 30 s", "Jev: the provider did not answer within 30 s"),
    "prov.badKey": (lambda: failure("cf:clef", [reply(401, b"")])[0],
                    "Cloudflare 401: klíč neplatí nebo nemá oprávnění (proměnné prostředí: CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN)",
                    "Cloudflare 401: the key is not valid or lacks permission (environment variables: CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN)"),
    "prov.dropped": (lambda: failure("jev:jev-1.13.0", [http.client.RemoteDisconnected("closed")])[0],
                     "Jev: spojení se nepodařilo po 4 pokusech (RemoteDisconnected: closed)",
                     "Jev: the connection failed after 4 attempts (RemoteDisconnected: closed)"),
    "prov.afterAttempts": (lambda: failure("jev:jev-1.13.0", [reply(529, b"overloaded")])[0],
                           "Jev 529: overloaded (po 4 pokusech)", "Jev 529: overloaded (after 4 attempts)"),
    "prov.retryAfter": (lambda: failure("jev:jev-1.13.0", [reply(429, b"", {"Retry-After": "3600"})])[0],
                        "Jev 429: poskytovatel žádá počkat 3600 s (Retry-After), víc než 60 s; dotaz se neopakuje",
                        "Jev 429: the provider asks to wait 3600 s (Retry-After), more than 60 s; PickBench does not retry the request"),
    "prov.cfError": (lambda: failure("cf:clef", [reply(200, {"success": False, "errors": [{"message": "No such model"}]})])[0],
                     'Cloudflare 200: poskytovatel hlásí chybu: [{"message": "No such model"}]',
                     'Cloudflare 200: the provider reports an error: [{"message": "No such model"}]'),
    "prov.notObject": (lambda: failure("jev:jev-1.13.0", [reply(200, b"<html>gateway</html>")])[0],
                       "Jev 200: odpověď není objekt JSON: <html>gateway</html>", "Jev 200: the answer is not a JSON object: <html>gateway</html>"),
    "prov.cap.requests": (lambda: failure("jev:jev-1.13.0", [reply()], budget=providers.Budget(max_requests=0, env={}))[0],
                          "Jev: strop útraty — odesláno 0 hostovaných dotazů, limit je 0 (PICKBENCH_MAX_HOSTED); další dotaz se neposlal",
                          "Jev: spend cap — 0 hosted requests sent, the cap is 0 (PICKBENCH_MAX_HOSTED); PickBench did not send the next request"),
    "prov.cap.usd": (lambda: failure("jev:jev-1.13.0", [reply()], budget=providers.Budget(max_usd=0, env={}))[0],
                     "Jev: strop útraty — utraceno 0.0000 $, limit je 0 $ (PICKBENCH_MAX_USD); další dotaz se neposlal",
                     "Jev: spend cap — $0.0000 spent, the cap is $0 (PICKBENCH_MAX_USD); PickBench did not send the next request"),
    "prov.cap.usdHeld": (lambda: failure("cf:clef", [reply()], budget=held())[0],
                         "Cloudflare: strop útraty — utraceno 0.0000 $ a 0.0020 $ drží odhad dotazů na cestě nebo bez známé ceny, limit je 0.001 $ "
                         "(PICKBENCH_MAX_USD); další dotaz se neposlal",
                         "Cloudflare: spend cap — $0.0000 spent and the cap counts $0.0020 as an estimate for requests in progress or without a known price, "
                         "the cap is $0.001 (PICKBENCH_MAX_USD); PickBench did not send the next request"),
    "prov.connectFailed": (lambda: caught(lambda: providers.connect_ms("jev:jev-1.13.0", ENV, connect=no_route)),
                           "Jev: spojení se serverem api.typesafe.ai se nepodařilo (OSError)",
                           "Jev: the connection to the server api.typesafe.ai failed (OSError)"),
}
OTHER_TEXTS = {"prov.testWho", "prov.net", "prov.netFailed", "prov.spent", "prov.spentUnpriced"}      # věty, které nejsou chybou: testy who a řádků níž


class English(unittest.TestCase):
    """Věty modulu v obou jazycích: česky doslova jako před překladem, anglicky celé; druh chyby (stav, kód) na jazyce nezávisí."""

    def test_every_error_sentence_reads_in_czech_as_before_and_in_english(self):
        self.assertEqual(set(SENTENCES) | OTHER_TEXTS, {key for key in i18n.TEXTS if key.startswith("prov.")})      # žádná věta modulu tu nechybí
        for key, (call, cs, en) in SENTENCES.items():
            with self.subTest(key=key):
                with i18n.process("cs"):
                    czech = call()
                with i18n.process("en"):
                    english = call()
                self.assertEqual((str(czech), str(english)), (cs, en))
                self.assertEqual(CZECH.findall(str(english)), [])
                self.assertNotEqual(str(english), str(czech))                # ani český úlomek bez diakritiky, který kontrola diakritikou nenajde
                self.assertEqual((type(english), english.provider, english.status, english.code),
                                 (type(czech), czech.provider, czech.status, czech.code))
                for e in (czech, english):                                   # text chyby nese větu v jazyce, ve kterém vznikla
                    self.assertTrue(str(e).endswith(": " + e.text), str(e))

    def test_the_providers_own_words_are_the_same_in_both_languages(self):      # tělo odpovědi poskytovatele je cizí text
        got = []
        for code in i18n.LANGS:
            with i18n.process(code):
                e = failure("jev:jev-1.13.0", [reply(400, {"error": "bad input"})])[0]
                got.append((str(e), e.text, e.status, e.code))
        self.assertEqual(got, [('Jev 400: {"error": "bad input"}', '{"error": "bad input"}', 400, "provider")] * 2)

    def test_lines_under_a_hosted_model(self):
        budget = providers.Budget(env={})
        zero = budget.state()
        post("cf:clef", [reply(500, b""), reply()], budget=budget)
        post("cf:clef", [reply(body={"answers": {}})], budget=budget)
        refused = providers.Budget(env={})
        failure("jev:jev-1.13.0", [reply(400, b"x")], budget=refused)
        two = {**zero, "requests": 2, "answered": 2, "tokens": 90, "usd": 0.000009}
        with i18n.process("en"):
            self.assertEqual(providers.spent_line("cf:clef", zero, budget.state()),
                             "hosted model (Cloudflare, Cloudflare Workers AI): 3 requests, 120 input tokens, $0.0000 (0.0144 $ / 1000 decisions), "
                             "retries 1; latency includes the network; 1 answer without a token count — the price is a lower bound")
            self.assertEqual(providers.spent_line("jev:jev-1.13.0", zero, refused.state()),
                             "hosted model (Jev, TypeSafe AI): 1 request, 0 input tokens, $0.0000 (— $ / 1000 decisions), retries 0; latency includes the network")
            self.assertEqual(providers.spent_line("test:echo", zero, two),
                             "hosted model (Test, test provider (nothing leaves this computer)): 2 requests, 90 input tokens, $0.0000 "
                             "(0.0045 $ / 1000 decisions), retries 0; latency includes the network")
            self.assertEqual(providers.spent_line("cf:neznamy", zero, two)[:22], "hosted model (?, ?): 2")
            self.assertIsNone(providers.spent_line("cf:clef", zero, zero))
            self.assertEqual(providers.net_line("test:echo", TEST_ENV), "network (estimate): TCP + TLS connection 2 ms")
            self.assertEqual(providers.net_line("jev:jev-1.13.0", ENV, connect=no_route),
                             "network (estimate): not measured (Jev: the connection to the server api.typesafe.ai failed (OSError))")
        with i18n.process("cs"):                                             # česky beze změny i u zkušebního poskytovatele a u modelu mimo ceník
            self.assertEqual(providers.spent_line("test:echo", zero, two),
                             "hostovaný model (Test, zkušební poskytovatel (nic neodchází)): 2 dotazů, 90 vstupních tokenů, 0.0000 $ (0.0045 $ / 1000 rozh.), "
                             "opakování 0; latence včetně sítě")
            self.assertEqual(providers.spent_line("cf:neznamy", zero, two)[:27], "hostovaný model (?, ?): 2 d")

    def test_test_provider_is_described_when_asked_not_when_the_module_loads(self):
        self.assertEqual(providers.PROVIDERS["test"].get("who_key"), "prov.testWho")
        self.assertNotIn("who", providers.PROVIDERS["test"])                 # žádná věta ve stavu modulu: skládá se až při použití
        seen = {}
        for code in i18n.LANGS:
            with i18n.process(code):
                seen[code] = (providers.who("test"), providers.who("jev"), providers.who("cf"), [m["who"] for m in providers.available(TEST_ENV)])
        for code in i18n.LANGS:                                              # Jev a Cloudflare jsou vlastní jména
            self.assertEqual(seen[code], (TEST_WHO[code], "TypeSafe AI", "Cloudflare Workers AI", [TEST_WHO[code]] * 2))
        got = []

        def request():
            with i18n.use("en"):                                             # jazyk požadavku platí jen ve vlákně požadavku
                got.append(providers.who("test"))
        thread = threading.Thread(target=request)
        thread.start()
        thread.join()
        self.assertEqual(got + [providers.who("test")], [TEST_WHO["en"], TEST_WHO["cs"]])

    def test_language_follows_the_environment_when_no_tool_or_request_sets_it(self):
        self.assertNotIn(i18n.ENV, os.environ)                               # setUpModule: testy českých vět na prostředí vývojáře nezávisí
        sentence = lambda: str(failure("jev:jev-1.13.0", [TimeoutError("timed out")])[0])
        self.assertEqual(sentence(), "Jev: poskytovatel neodpověděl do 30 s")
        with mock.patch.dict(os.environ, {i18n.ENV: "en"}):
            self.assertEqual(sentence(), "Jev: the provider did not answer within 30 s")
        self.assertEqual(sentence(), "Jev: poskytovatel neodpověděl do 30 s")

    def test_two_requests_at_once_each_get_the_sentence_in_their_own_language(self):
        barrier, got = threading.Barrier(2, timeout=10), {"cs": [], "en": []}

        def together(host, path, headers, body, timeout):
            barrier.wait()                                                   # oba dotazy jsou v obsluze naráz; věta chyby se skládá až potom
            return reply(401, b"")

        def work(code):
            for _ in range(20):
                with i18n.use(code):
                    got[code].append(str(caught(lambda: providers.post("jev:jev-1.13.0", PAYLOAD, transport=together, env=ENV,
                                                                       budget=providers.Budget(env={})))))
        threads = [threading.Thread(target=work, args=(code,)) for code in got]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(got, {"cs": ["Jev 401: klíč neplatí nebo nemá oprávnění (proměnné prostředí: TYPESAFE_API_KEY)"] * 20,
                               "en": ["Jev 401: the key is not valid or lacks permission (environment variables: TYPESAFE_API_KEY)"] * 20})

    # ---------- klíče ----------
    @both_languages
    def test_foreign_text_is_cleaned_before_it_is_cut(self):                 # řez uprostřed klíče by nechal jeho začátek, který už scrub nepozná
        start = JEV_KEY[:4]
        for where, limit, steps in (("tělo odpovědi 400", 400, lambda text: [reply(400, text.encode())]),
                                    ("tělo po pokusech", 300, lambda text: [reply(529, text.encode())]),
                                    ("spadlé spojení", 300 - len("ConnectionResetError: "), lambda text: [ConnectionResetError(text)]),
                                    ("odpověď 200, která není JSON", 200, lambda text: [reply(200, text.encode())])):
            for pad in range(limit - len(JEV_KEY) - 1, limit + 2):
                with self.subTest(where=where, pad=pad):
                    e, _, _ = failure("jev:jev-1.13.0", steps("x" * pad + JEV_KEY + " end"))
                    self.assertNotIn(start, str(e))
                    self.assertNotIn(start, e.text)
        for pad in range(300 - len(CF_TOKEN) - 20, 300 + 2):                 # chyba v obálce Cloudflare: řez platí pro text JSON
            with self.subTest(where="obálka Cloudflare", pad=pad):
                e, _, _ = failure("cf:clef", [reply(200, {"success": False, "errors": [{"message": "x" * pad + CF_TOKEN}]})])
                self.assertNotIn(CF_TOKEN[:4], str(e))

    def test_braces_in_foreign_text_stay_as_they_came(self):                 # věta se doplňuje jedním průchodem: cizí text se podruhé nečte
        odd = "{attempts} {status} {body} {"
        cases = (("jev:jev-1.13.0", [reply(529, odd.encode())], f"Jev 529: {odd} (po 4 pokusech)", f"Jev 529: {odd} (after 4 attempts)"),
                 ("jev:jev-1.13.0", [ConnectionResetError("{attempts} {fault}")],
                  "Jev: spojení se nepodařilo po 4 pokusech (ConnectionResetError: {attempts} {fault})",
                  "Jev: the connection failed after 4 attempts (ConnectionResetError: {attempts} {fault})"),
                 ("jev:jev-1.13.0", [reply(200, odd.encode())], f"Jev 200: odpověď není objekt JSON: {odd}", f"Jev 200: the answer is not a JSON object: {odd}"),
                 ("cf:clef", [reply(200, {"success": False, "errors": "{errors} {status}"})],
                  'Cloudflare 200: poskytovatel hlásí chybu: "{errors} {status}"', 'Cloudflare 200: the provider reports an error: "{errors} {status}"'),
                 ("cf:{offered} {model}", [reply()], f"Cloudflare: model „cf:{{offered}} {{model}}“ není v seznamu hostovaných modelů ({OFFERED})",
                  f'Cloudflare: the model "cf:{{offered}} {{model}}" is not in the list of hosted models ({OFFERED})'),
                 ("jev:jev-1.13.0", [reply(400, odd.encode())], f"Jev 400: {odd}", f"Jev 400: {odd}"))
        for model, steps, cs, en in cases:
            with self.subTest(cs=cs):
                got = []
                for code in i18n.LANGS:
                    with i18n.process(code):
                        got.append(str(failure(model, steps)[0]))
                self.assertEqual(got, [cs, en])

    def test_bad_key_ends_with_an_english_sentence_without_the_providers_words(self):      # anglické dvojče test_bad_key_ends_at_once_without_the_providers_words
        for model, label, names in (("jev:jev-1.13.0", "Jev", "TYPESAFE_API_KEY"), ("cf:clef", "Cloudflare", "CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN")):
            for status in (401, 403):
                with self.subTest(model=model, status=status), i18n.process("en"):
                    echo = {"error": f"Invalid API key: {JEV_KEY[:12]}… / {CF_TOKEN}"}
                    e, script, sleeps = failure(model, [reply(status, echo)])
                    self.assertEqual((e.status, len(script.calls), sleeps.waits), (status, 1, []))
                    self.assertEqual(str(e), f"{label} {status}: the key is not valid or lacks permission (environment variables: {names})")

    def test_failed_connection_is_an_english_sentence_without_a_key(self):   # anglická dvojčata testů třídy Network
        def refuse(host, timeout):
            raise OSError(f"cannot reach {host} with {JEV_KEY}")
        sentence = "Jev: the connection to the server api.typesafe.ai failed (OSError)"
        with i18n.process("en"):
            e = caught(lambda: providers.connect_ms("jev:jev-1.13.0", ENV, connect=refuse))
            self.assertEqual((str(e), e.__context__, e.__cause__), (sentence, None, None))
            self.assertEqual(providers.net_line("jev:jev-1.13.0", ENV, connect=refuse), f"network (estimate): not measured ({sentence})")

        def broken():
            raise ssl.SSLError(f"cannot load the certificates for {JEV_KEY}")
        sentence = "Jev: the connection to the server api.typesafe.ai failed (SSLError)"
        with i18n.process("en"), mock.patch.object(providers, "_tls_context", None), mock.patch.object(ssl, "create_default_context", broken), \
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("síť")):
            e = caught(lambda: providers.connect_ms("jev:jev-1.13.0", ENV))
            self.assertEqual((str(e), e.__context__, e.__cause__), (sentence, None, None))
            self.assertEqual(providers.net_line("jev:jev-1.13.0", ENV), f"network (estimate): not measured ({sentence})")


if __name__ == "__main__":
    unittest.main()
