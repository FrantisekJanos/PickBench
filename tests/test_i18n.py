"""Jazyk a texty: každý text má obě jazykové podoby, angličtina není česky a jazyk se vybírá, jak má."""
import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tokenize
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import i18n

CZECH = re.compile(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„“]")
# Anglické texty, které českou diakritiku nést smějí: klíč → důvod.
CZECH_OK = {}
# Moduly, jejichž věty už jdou přes i18n.t: mimo dokumentační řetězce a komentáře v nich nesmí zůstat český řetězec.
DONE_MODULES = ["bench.py", "hwbench.py", "fetch_datasets.py", "providers.py", "store.py", "server.py"]
# Moduly bez vět pro uživatele (ověřeno inventurou plánu): nepřekládají se, a proto v nich český řetězec nesmí ani přibýt.
NO_TEXT_MODULES = ["i18n.py", "gpu.py", "metrics.py"]
# Řetězce s diakritikou, které v hotovém modulu zůstat smějí: (soubor, řetězec) → důvod.
# U fetch_datasets.py jsou to data, která nástroj zapisuje do souboru sady: soubor nesmí záviset na jazyku běhu a překládá se v něm jen popis (about_en).
LITERAL_OK = {
    ("fetch_datasets.py", 'r"[áčďéěíňóřšťúůýž]"'): "regulární výraz CZECH: pozná český text řádku sady Horizon-Labs, není to text pro uživatele",
    ("fetch_datasets.py", '"Horizon-Labs česky"'): "název sady (name): nepřekládá se",
    ("fetch_datasets.py", "CFPB stížnosti ("): "název sady (name): nepřekládá se",
    ("fetch_datasets.py", '"CC-BY-4.0 (Creative Commons Attribution 4.0 International; soubor LICENSE v repozitáři zdroje)"'):
        "pole license sady BANKING77: text dat, nepřekládá se",
    ("fetch_datasets.py", '"CC-BY-3.0 (Creative Commons Attribution 3.0 Unported; soubor LICENSE v repozitáři zdroje)"'):
        "pole license sady CLINC150: text dat, nepřekládá se",
    ("fetch_datasets.py", '"ODC-BY 1.0 (Open Data Commons Attribution License; karta sady, oddíl License)"'):
        "pole license sady Horizon-Labs: text dat, nepřekládá se",
    ("fetch_datasets.py", '"Hypotéka"'): "název kategorie sady CFPB (label): text dat, nepřekládá se",
    ("fetch_datasets.py", '"Kreditní karta"'): "název kategorie sady CFPB (label): text dat, nepřekládá se",
    ("fetch_datasets.py", '"Běžný nebo spořicí účet"'): "název kategorie sady CFPB (label): text dat, nepřekládá se",
    ("fetch_datasets.py", '"Studentská půjčka"'): "název kategorie sady CFPB (label): text dat, nepřekládá se",
    ("fetch_datasets.py", '"Auto na úvěr nebo leasing"'): "název kategorie sady CFPB (label): text dat, nepřekládá se",
    ("fetch_datasets.py", '"Vymáhání dluhů"'): "název kategorie sady CFPB (label): text dat, nepřekládá se",
}
# Klíče i18n.TEXTS, kde je český a anglický text stejný záměrně (slova stejná v obou jazycích, zkratky, jednotky):
# záhlaví sloupců bench.py; u hwbench.py slovo model, N, percentily a nejvyšší hodnota s jednotkou ms a jednotky.
SAME_PY = {"bench.col.model", "bench.col.p95", "bench.col.min", "bench.col.max", "bench.col.ece", "bench.col.goodput",
           "hw.col.model", "hw.col.n", "hw.col.p50", "hw.col.p90", "hw.col.p95", "hw.col.p99", "hw.col.max", "hw.col.ms",
           "hw.col.temp", "hw.col.power", "hw.col.clock"}
# Slova, která má české a anglické záhlaví tabulek nástroje společná; jiné společné slovo je zapomenutý překlad bez diakritiky.
# U hwbench.py navíc „start“ (studený start = cold start: slovo je v obou jazycích stejné) a jednotka „s“ (čas s = time s).
HEAD_SAME = {"bench.py": {"model", "p95", "min", "max", "ECE", "goodput"},
             "hwbench.py": {"model", "N", "p50", "p90", "p95", "p99", "max", "ms", "start", "°C", "W", "MHz", "s"}}
# Hotový modul → předpony klíčů, které mu patří: každý takový klíč modul používá a každý klíč, který používá, v i18n.TEXTS je.
MODULE_KEYS = {"bench.py": ("bench.", "vd.", "vp."), "hwbench.py": ("hw.",), "fetch_datasets.py": ("fetch.",),
               "providers.py": ("prov.",), "store.py": ("store.",), "server.py": ("srv.",)}
# Volání i18n.t, ve kterém klíč nestojí doslova: (soubor, volání) → výraz, který klíč najde tam, kde doslova stojí.
# providers.who() bere klíč ze záznamu poskytovatele (PROVIDERS["test"]["who_key"]): věta se tak neskládá při importu modulu.
KEY_ELSEWHERE = {("providers.py", 'i18n.t(spec["who_key"])'): r'"who_key": "([\w.]+)"'}
# Moduly serveru → předpona jejich klíčů: každý řetězec s tou předponou v modulu je klíč v i18n.TEXTS a každý takový klíč modul nese.
SERVER_KEYS = {"providers.py": "prov.", "store.py": "store.", "server.py": "srv."}


def czech_literals(path):
    """Řetězce s českou diakritikou v souboru Pythonu mimo dokumentační řetězce: [(řádek, text)]."""
    src = path.read_text(encoding="utf-8")
    docs = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docs.update(range(first.lineno, first.end_lineno + 1))
    kinds = {tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING)}
    return [(tok.start[0], tok.string) for tok in tokenize.generate_tokens(io.StringIO(src).readline)
            if tok.type in kinds and tok.start[0] not in docs and CZECH.search(tok.string)]


class Texts(unittest.TestCase):
    def test_every_text_has_both_languages(self):
        for key, entry in i18n.TEXTS.items():
            with self.subTest(key=key):
                self.assertEqual(sorted(entry), ["cs", "en"])
                for code in i18n.LANGS:
                    self.assertIsInstance(entry[code], str)
                    self.assertNotEqual(entry[code].strip(), "")

    def test_both_languages_use_the_same_variables(self):
        for key, entry in i18n.TEXTS.items():
            self.assertEqual(i18n.fields(entry["cs"]), i18n.fields(entry["en"]), key)

    def test_english_has_no_czech_letters(self):
        for key, entry in i18n.TEXTS.items():
            if key not in CZECH_OK:
                self.assertEqual(CZECH.findall(entry["en"]), [], key)
        self.assertLessEqual(set(CZECH_OK), set(i18n.TEXTS))                 # výjimka pro klíč, který už není, je mrtvá

    def test_finished_modules_keep_no_czech_literal(self):
        for name in DONE_MODULES:
            left = [(line, text) for line, text in czech_literals(ROOT / name) if (name, text) not in LITERAL_OK]
            self.assertEqual(left, [], name)

    def test_every_module_is_finished(self):                                  # hotové jsou všechny moduly v kořeni kromě těch, které věty pro uživatele nemají
        self.assertEqual(sorted(DONE_MODULES + NO_TEXT_MODULES), sorted(path.name for path in ROOT.glob("*.py")))
        for name in NO_TEXT_MODULES:
            found = czech_literals(ROOT / name)
            if name == "i18n.py":                                             # slovník české texty nést má: nic jiného v modulu česky není
                texts = "\n".join(entry["cs"] for entry in i18n.TEXTS.values())
                found = [(line, text) for line, text in found if ast.literal_eval(text) not in texts]
            self.assertEqual(found, [], name)

    def test_same_text_in_both_languages_only_where_listed(self):             # české slovo bez diakritiky kontrola diakritikou nenajde; stejný text v obou jazycích ano
        same = {key for key, entry in i18n.TEXTS.items() if entry["cs"] == entry["en"]}
        self.assertEqual(same - SAME_PY, set())
        self.assertEqual(SAME_PY - same, set())                               # výjimka pro klíč, který už není nebo se liší, je mrtvá

    def test_every_key_of_a_finished_module_is_used_and_every_used_key_exists(self):      # překlep v klíči by jinak byl KeyError až za běhu
        self.assertEqual(sorted(MODULE_KEYS), sorted(DONE_MODULES))
        for name, prefixes in MODULE_KEYS.items():
            source = (ROOT / name).read_text(encoding="utf-8")
            used = set(re.findall(r'i18n\.t\(\s*"([\w.]+)"', source))
            elsewhere = {call: pattern for (module, call), pattern in KEY_ELSEWHERE.items() if module == name}
            for call, pattern in elsewhere.items():                           # jmenovaná výjimka: klíč stojí doslova jinde a test ho najde tam
                self.assertEqual(source.count(call), 1, (name, call))
                self.assertTrue(re.findall(pattern, source), (name, call))
                used |= set(re.findall(pattern, source))
            self.assertEqual(len(re.findall(r"i18n\.t\(", source)), len(re.findall(r'i18n\.t\(\s*"[\w.]+"', source)) + len(elsewhere), name)      # klíč stojí ve volání doslova
            self.assertEqual(used - set(i18n.TEXTS), set(), name)
            self.assertEqual({key for key in i18n.TEXTS if key.startswith(prefixes)} - used, set(), name)
            if prefixes:                                                      # modul s vlastní předponou pod ní má aspoň jeden text
                self.assertTrue(any(key.startswith(prefixes[0]) for key in used), name)

    def test_every_key_literal_of_the_server_modules_is_a_text_and_every_text_is_used(self):      # překlep v klíči by jinak byl KeyError až při chybě za běhu
        self.assertLessEqual({module for module, _ in KEY_ELSEWHERE}, set(SERVER_KEYS))
        for name, prefix in SERVER_KEYS.items():
            source = (ROOT / name).read_text(encoding="utf-8")
            found = set(re.findall(r'"(%s[\w.]*)"' % re.escape(prefix), source))      # každý řetězec s předponou modulu, i mimo volání i18n.t
            self.assertGreaterEqual(len(found), 3, name)
            self.assertEqual(found - set(i18n.TEXTS), set(), name)
            self.assertEqual({key for key in i18n.TEXTS if key.startswith(prefix)} - found, set(), name)
            others = [other for other in ROOT.glob("*.py") if other.name not in (name, "i18n.py")]
            self.assertEqual([other.name for other in others if re.search(r'"%s[\w.]*"' % re.escape(prefix), other.read_text(encoding="utf-8"))], [], name)

    def test_table_headers_share_only_the_listed_words(self):
        import bench
        import hwbench
        tables = {"bench.py": lambda: [bench.header(mirror=True, deadline=1)],                          # všech 13 sloupců
                  "hwbench.py": lambda: [" ".join(head) for test in hwbench.TESTS for head in hwbench.heads(test)]}      # sedm tabulek a záhlaví jejich CSV
        self.assertEqual(sorted(tables), sorted(HEAD_SAME))
        for name, heads in tables.items():
            found = {}
            for code in i18n.LANGS:
                with i18n.process(code):
                    found[code] = heads()
            self.assertEqual(len(found["en"]), len(found["cs"]), name)
            shared = [set(cs.split()) & set(en.split()) for cs, en in zip(found["cs"], found["en"])]      # průnik v každém záhlaví zvlášť
            self.assertEqual(set().union(*shared), HEAD_SAME[name], name)

    def test_tools_share_the_sentence_they_both_print(self):                  # stejná věta v bench.py a v hwbench.py zní stejně, česky i anglicky
        self.assertEqual(i18n.TEXTS["hw.err.limits"], i18n.TEXTS["bench.err.limits"])
        self.assertEqual(i18n.TEXTS["hw.col.rate"], i18n.TEXTS["bench.col.rate"])
        self.assertEqual(i18n.TEXTS["hw.col.model"], i18n.TEXTS["bench.col.model"])

    def test_fetch_datasets_ends_an_interrupted_run_with_the_same_sentence_as_hwbench(self):
        self.assertEqual(i18n.TEXTS["fetch.err.interrupted"], i18n.TEXTS["hw.err.interrupted"])

    def test_exceptions_for_czech_literals_are_alive(self):                   # výjimka pro řetězec, který v modulu už není, je mrtvá
        self.assertLessEqual({module for module, _ in LITERAL_OK}, set(DONE_MODULES))
        for name in DONE_MODULES:
            found = {text for _, text in czech_literals(ROOT / name)}
            self.assertEqual({text for module, text in LITERAL_OK if module == name} - found, set(), name)
        self.assertTrue(all(reason.strip() for reason in LITERAL_OK.values()))

    def test_dataset_descriptions_fetch_datasets_writes_are_whole_in_english(self):      # popis sady se překládá celý, nezkracuje se
        names = {"banking77": ("BANKING77", "PolyAI", "Casanueva et al.", "Efficient Intent Detection with Dual Sentence Encoders"),      # jména a citace se nepřekládají
                 "cfpb": ("CFPB", "Consumer Complaint Database", "Hugging Face", "XXXX", "CC0"),
                 "clinc150": ("CLINC150", "Larson et al.", "Out-of-Scope Prediction", "oos", "data_full.json", "oos_test"),
                 "horizonCs": ("Horizon-Labs multilingual-zeroshot-synthetic", "Qwen", "Czech", "tax_short"),
                 "massive": ("MASSIVE 1.1", "Amazon", "FitzGerald et al.", "en-US", "partition test", "SLURP", "Bastianelli et al.", "Nimble")}
        self.assertEqual(sorted(key for key in i18n.TEXTS if key.startswith("fetch.about.")), sorted("fetch.about." + name for name in names))
        for name, kept in names.items():
            key = "fetch.about." + name
            cs, en = i18n.TEXTS[key]["cs"], i18n.TEXTS[key]["en"]
            self.assertGreater(len(en), 0.8 * len(cs), key)
            self.assertEqual(sorted(re.findall(r"\d+", en)), sorted(re.findall(r"\d+", cs)), key)      # počty, roky a meze jsou v obou jazycích stejné
            for text in (cs, en):
                self.assertTrue(text.endswith(" {today}."), key)              # datum stažení je součást věty, ne slepený dovětek
                for word in kept:
                    self.assertIn(word, text, key)
        for name in ("banking77", "clinc150", "horizonCs", "massive"):       # veřejné sady: rozsah (celá testovací část, nebo vzorek) stojí ve větě před datem
            self.assertTrue(i18n.TEXTS["fetch.about." + name]["cs"].endswith(" Rozsah: {scope}; staženo {today}."), name)
            self.assertTrue(i18n.TEXTS["fetch.about." + name]["en"].endswith(" Scope: {scope}; downloaded {today}."), name)


class Fill(unittest.TestCase):
    def test_variables_formats_and_counts(self):
        self.assertEqual(i18n.fill("{n} {n|step|steps}, {usd:.4f} $", {"n": 1, "usd": 0.5}), "1 step, 0.5000 $")
        self.assertEqual(i18n.fill("{n} {n|step|steps}", {"n": 3}), "3 steps")
        self.assertEqual(i18n.fill("{name:<6}|", {"name": "ab"}), "ab    |")

    def test_missing_variable_stays_visible_and_json_braces_are_left_alone(self):
        self.assertEqual(i18n.fill("a {missing} b", {}), "a {missing} b")
        self.assertEqual(i18n.fill('{"available": false} {x}', {"x": 1}), '{"available": false} 1')

    def test_fields(self):
        self.assertEqual(i18n.fields("{a} {b:.1f} {c|x|y} {a}"), {"a", "b", "c"})


class Restore(unittest.TestCase):
    """Jazyk se po bloku vrací na předchozí, i když z bloku vyletí výjimka a když jsou bloky vnořené:
    nástroje se v testech volají opakovaně v jednom procesu a sys.exit uvnitř bloku nesmí zanechat cizí jazyk."""

    def setUp(self):
        env = mock.patch.dict(os.environ)                                     # jazyk z prostředí tu nesmí rozhodovat
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(i18n.ENV, None)

    def test_process_language_returns_after_an_exception(self):
        self.assertEqual(i18n.lang(), "cs")
        with self.assertRaises(SystemExit), i18n.process("en"):
            self.assertEqual(i18n.lang(), "en")
            raise SystemExit("konec nástroje")
        self.assertEqual(i18n.lang(), "cs")

    def test_request_language_returns_after_an_exception(self):
        self.assertEqual(i18n.lang(), "cs")
        with self.assertRaises(RuntimeError), i18n.use("en"):
            self.assertEqual(i18n.lang(), "en")
            raise RuntimeError("chyba v obsluze požadavku")
        self.assertEqual(i18n.lang(), "cs")

    def test_nested_process_returns_to_the_outer_language_not_to_the_environment(self):
        os.environ[i18n.ENV] = "en"                                           # prostředí říká en, vnější blok cs: po vnitřním bloku musí platit cs
        self.assertEqual(i18n.lang(), "en")
        with i18n.process("cs"):
            with i18n.process("en"):
                self.assertEqual(i18n.lang(), "en")
            self.assertEqual(i18n.lang(), "cs")
        self.assertEqual(i18n.lang(), "en")

    def test_nested_request_returns_to_the_outer_language_not_to_the_process(self):
        with i18n.process("en"):
            with i18n.use("cs"):
                with i18n.use("en"):
                    self.assertEqual(i18n.lang(), "en")
                self.assertEqual(i18n.lang(), "cs")                          # vnější požadavek cs, jazyk procesu en: musí platit cs
            self.assertEqual(i18n.lang(), "en")
        self.assertEqual(i18n.lang(), "cs")


class Choice(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)                                     # jazyk z prostředí tu nesmí rozhodovat: nesmysl v PICKBENCH_LANG by shodil from_env()
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(i18n.ENV, None)

    def test_environment(self):
        self.assertEqual(i18n.from_env({}), "cs")
        self.assertEqual(i18n.from_env({"PICKBENCH_LANG": ""}), "cs")
        self.assertEqual(i18n.from_env({"PICKBENCH_LANG": " EN "}), "en")
        with self.assertRaises(i18n.LangError) as ctx:
            i18n.from_env({"PICKBENCH_LANG": "de"})
        self.assertIn("„de“", str(ctx.exception))
        self.assertIn('"de"', str(ctx.exception))                             # věta v obou jazycích: jazyk ještě není znám

    def test_option_may_stand_anywhere(self):
        self.assertEqual(i18n.pop_lang(["100", "--lang", "en", "m1"], {}), ("en", ["100", "m1"]))
        self.assertEqual(i18n.pop_lang(["m1", "--lang=en"], {}), ("en", ["m1"]))
        self.assertEqual(i18n.pop_lang(["--lanes", "4", "m1"], {}), ("cs", ["--lanes", "4", "m1"]))
        self.assertEqual(i18n.pop_lang(["m1"], {"PICKBENCH_LANG": "en"}), ("en", ["m1"]))
        self.assertEqual(i18n.pop_lang(["--lang", "cs", "m1"], {"PICKBENCH_LANG": "en"}), ("cs", ["m1"]))      # volba přebíjí proměnnou

    def test_unknown_language_ends_with_a_sentence(self):
        for argv, env in ((["--lang", "de", "m1"], {}), (["m1", "--lang"], {}), (["m1"], {"PICKBENCH_LANG": "xx"}), (["--lang="], {})):
            with self.subTest(argv=argv, env=env), self.assertRaises(SystemExit) as ctx:
                i18n.pop_lang(argv, env)
            self.assertIsInstance(ctx.exception.code, str)                    # věta = nenulový kód ukončení
            self.assertRegex(ctx.exception.code, r"cs.*en")

    def test_request_beats_process_beats_default(self):
        self.assertEqual(i18n.lang(), i18n.from_env())
        with i18n.process("en"):
            self.assertEqual(i18n.lang(), "en")
            with i18n.use("cs"):
                self.assertEqual(i18n.lang(), "cs")
                with i18n.use("xx"):                                          # neznámý jazyk požadavku = čeština, jako bez hlavičky
                    self.assertEqual(i18n.lang(), "cs")
            self.assertEqual(i18n.lang(), "en")
        self.assertEqual(i18n.lang(), i18n.from_env())

    def test_process_language_reaches_worker_threads_and_request_language_does_not(self):
        seen = []
        with i18n.process("en"), i18n.use("cs"):
            worker = threading.Thread(target=lambda: seen.append(i18n.lang()))
            worker.start()
            worker.join()
        self.assertEqual(seen, ["en"])

    def test_sentence_in_the_current_language(self):
        with i18n.process("en"):
            self.assertEqual(i18n.t("lang.missing"), "The --lang option needs a value: cs or en.")
            self.assertEqual(i18n.t("lang.unknown", key="x", value="de")[:20], 'Unknown language "de')      # proměnná jménem key se s parametrem t() nepere
            self.assertEqual(i18n.pick({"cs": "a", "en": "b"}), "b")
        with i18n.process("cs"):
            self.assertEqual(i18n.t("lang.unknown", value="de"), "Neznámý jazyk „de“: --lang a PICKBENCH_LANG berou cs nebo en.")
        with self.assertRaises(KeyError):
            i18n.t("takový klíč není")


class PageAddress(unittest.TestCase):
    def test_page_is_served_with_a_language_in_the_address(self):
        sys.path.insert(0, str(Path(__file__).parent))
        import test_server                                                  # server na volném portu s databází v dočasné složce

        class Probe(test_server.WithServer):
            def runTest(self):
                pass
        probe = Probe()
        probe.setUp()
        try:
            for path, want in (("/", 200), ("/?lang=en", 200), ("/index.html?lang=cs", 200), ("/settings?lang=en", 404)):
                c = test_server.http.client.HTTPConnection("127.0.0.1", probe.httpd.server_address[1], timeout=5)
                c.request("GET", path)
                r = c.getresponse()
                body = r.read()
                c.close()
                self.assertEqual(r.status, want, path)
                if want == 200:
                    self.assertIn(b"<title>PickBench", body)
        finally:
            probe.doCleanups()


# ---------- stránka ----------
PAGE = ROOT / "index.html"
TEXT_BLOCK = re.compile(r"// ==== texty: začátek ====\n(.*?)// ==== texty: konec ====", re.S)
SHARED = ("// ==== sdíleno s Pythonem: začátek ====", "// ==== sdíleno s Pythonem: konec ====")
FIELD_JS = re.compile(r"\{(\w+)(?:\|[^{}|]*\|[^{}|]*)?\}")
# Klíče, kde je český a anglický text stejný záměrně (vlastní jména, zkratky, slova stejná v obou jazycích).
SAME = {"mode.bench", "ui.start", "ui.stop", "ui.model", "ui.hardware", "ui.csv", "opt.driver.model",
        "col.n", "col.p95", "col.min", "col.max", "col.ece", "col.temp", "col.f1", "col.goodput",
        "metric.precision", "metric.recall", "metric.f1", "last.lane", "card.model", "scenario.emails.task", "last.ms", "run.benchProgress"}
# Předpony klíčů, které se skládají za běhu: t('state.' + st.state).
DYNAMIC = ("tip.", "col.", "result.", "gap.", "state.")
# Celé klíče, které se skládají za běhu a doslova ve skriptu nestojí: klíč → kde. Starší scénáře drží doslova seznam v legacyCells.
COMPOSED = {"scenario.signs": "cellsOf: known('scenario.' + v.scenario, …)"}
# Hotové části skriptu (názvy z řádků „// ---------- název ----------“, „začátek“ = před první takový řádek, „html“ = statické HTML,
# „sdíleno“ = blok sdílený s Pythonem): nesmí v nich zůstat český řetězec. Poslední úkol sem doplní všechny.
DONE = ["jazyk stránky", "html", "popisky po najetí myší nebo klávesnicí", "začátek", "scénář: překážky", "scénář: číselné duely",
        "scénář: veřejná sada (jen benchmark bez hry)", "jádro: statistiky", "výsledky: tabulka ze záznamů (databáze na serveru)",
        "úložiště: dotazy na server, poslední nastavení", "plánovač: fronta kroků", "plánovač: běh fronty",
        "hostované modely (Jev u TypeSafe AI, Clef u Cloudflare)", "hardware: stav GPU z /gpu, energie na rozhodnutí", "jádro: běh",
        "scénář: e-maily", "sdíleno",
        "jádro: hra", "jádro: rozhodování", "jádro: kreslení",      # části, které žádný úkol nejmenoval: český text v nich nikdy nebyl
        "scénář: zákazy a výjimky"]
sys.path.insert(0, str(Path(__file__).parent))
from page_snapshot import FRAGMENTS, PLAIN, plain_text, static_texts      # PLAIN = české řetězce bez diakritiky (kontrola diakritikou je nenajde)
# Texty, které statické HTML nést smí i po překladu (část „html“): vlastní jména, čísla, značky. Všechno ostatní jde přes data-i18n.
HTML_OK = ("PickBench", "/v1/systemone", "0", "—", "CS", "EN")
# Úlomky českých textů stránky před změnou (tests/golden/page-fragments-cs.json), které zmizet směly: úlomek → důvod.
GONE = {"Chyba: záložka byla skrytá déle než ": "od úkolu 4 přidává úvod „Chyba: “ funkce fail(); věta sama zůstala",
        "Žebřík: úroveň 1 z ": "první úroveň žebříku skládá stejný klíč run.ladder.level jako další úrovně ({level} = 1); větu česky hlídá "
                               "test_run_hardware_and_hosted_texts_read_as_before_and_count_in_the_right_number"}


def page():
    return PAGE.read_text(encoding="utf-8").replace("\r\n", "\n")


def script():
    """Skript stránky bez bloku textů (ten české texty nést má)."""
    whole = re.search(r"<script>\n(.*)</script>", page(), re.S).group(1)
    return TEXT_BLOCK.sub("", whole)


def text_block():
    found = TEXT_BLOCK.findall(page())
    if len(found) != 1:
        raise AssertionError(f"blok „texty“ má být v index.html právě jednou, je tam {len(found)}×")
    return found[0]


def node(expression, argument=None, lang="cs", extra=""):
    """Pustí blok textů v node a vrátí hodnotu výrazu; argument je ve výrazu jako arg, jazyk stránky je lang."""
    with tempfile.TemporaryDirectory() as tmp:
        code, data = Path(tmp) / "texts.cjs", Path(tmp) / "arg.json"
        code.write_text(text_block() + extra + f"\nLANG = {json.dumps(lang)};\n"
                        "const arg = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));\n"
                        f"process.stdout.write(JSON.stringify({expression}));\n", encoding="utf-8")
        data.write_text(json.dumps(argument), encoding="utf-8")
        r = subprocess.run(["node", str(code), str(data)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    if r.returncode:
        raise AssertionError("node: " + r.stderr[-800:])
    return json.loads(r.stdout)


def piece(pattern):
    """Kus skriptu stránky mimo blok textů (funkce, řádek s proměnnou, výraz) podle výrazu bez skupin; musí tam být právě jednou."""
    found = re.findall(pattern, script(), re.S | re.M)
    if len(found) != 1:
        raise AssertionError(f"kus skriptu {pattern!r} má být ve stránce právě jednou, je tam {len(found)}×")
    return found[0]


def node_run(code):
    """Pustí v node blok textů a za ním zadaný kód (smí být asynchronní); vrátí, co kód vypsal jako JSON."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "page.cjs"
        path.write_text(text_block() + code, encoding="utf-8")
        r = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    if r.returncode or not r.stdout:
        raise AssertionError("node: " + r.stderr[-800:])
    return json.loads(r.stdout)


def code_part(line):
    """Řádek skriptu bez komentáře // (dvě lomítka uvnitř řetězce komentář nejsou)."""
    quote, i = None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif line[i:i + 2] == "//":
            return line[:i]
        i += 1
    return line


def literals(line):
    """Řetězce na řádku skriptu (obsah bez uvozovek); šablona se bere celá i s ${…}."""
    out, quote, start, i = [], None, 0, 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                out.append(line[start:i])
                quote = None
        elif ch in "'\"`":
            quote, start = ch, i + 1
        elif line[i:i + 2] == "//":
            break
        i += 1
    return out


def regions():
    """Části stránky: {název: [(číslo řádku, řádek)]}. Skript se dělí řádky „// ---------- název ----------“."""
    lines = page().split("\n")
    first = lines.index("<script>")
    out = {"html": list(enumerate(lines[lines.index("<body>"):first], lines.index("<body>") + 1))}
    name, shared, texts = "začátek", False, False
    for number, line in enumerate(lines[first + 1:lines.index("</script>")], first + 2):
        if line.startswith("// ==== texty: "):
            texts = "začátek" in line
            continue
        if texts:
            continue
        if line in SHARED:
            shared = line == SHARED[0]
            continue
        mark = re.fullmatch(r"// -{10} (.+?) -{10}", line)
        if mark:
            name = mark.group(1)
            continue
        out.setdefault("sdíleno" if shared else name, []).append((number, line))
    return out


def czech_left(name):
    """České řetězce, které v části stránky zůstaly: [(řádek, text)]."""
    left = []
    for number, line in regions()[name]:
        if name == "html":
            body = re.sub(r"<!--.*?-->", "", line)
            if CZECH.search(body):
                left.append((number, body.strip()[:80]))
            continue
        if CZECH.search(code_part(line)):
            left.append((number, code_part(line).strip()[:80]))
        left += [(number, text[:80]) for text in literals(line) if any(word in re.sub(r"\$\{.*?\}", "", text) for word in PLAIN)]
    if name == "html":                               # i text bez diakritiky, text v atributu a text v CSS: HTML po překladu nenese žádný vlastní
        left += [(0, text[:80]) for text in static_texts(page()) if text not in HTML_OK and not CZECH.search(text)]
    return left


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class PageTexts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.texts = node("TEXTS")

    def test_every_text_has_both_languages(self):
        self.assertGreaterEqual(len(self.texts), 4)
        for key, entry in self.texts.items():
            with self.subTest(key=key):
                self.assertEqual(sorted(entry), ["cs", "en"])
                for code in ("cs", "en"):
                    self.assertIsInstance(entry[code], str)
                    self.assertNotEqual(entry[code].strip(), "")

    def test_both_languages_use_the_same_variables(self):
        for key, entry in self.texts.items():
            self.assertEqual(set(FIELD_JS.findall(entry["cs"])), set(FIELD_JS.findall(entry["en"])), key)

    def test_english_has_no_czech_letters(self):
        for key, entry in self.texts.items():
            if key not in CZECH_OK:
                self.assertEqual(CZECH.findall(entry["en"]), [], key)

    def test_long_english_text_is_not_cut_short(self):                        # popisky po najetí se překládají celé, nezkracují se
        for key, entry in self.texts.items():
            if len(entry["cs"]) >= 120:
                self.assertGreater(len(entry["en"]), 0.6 * len(entry["cs"]), key)

    def test_same_text_in_both_languages_only_where_listed(self):
        same = {key for key, entry in self.texts.items() if entry["cs"] == entry["en"]}
        self.assertEqual(same - SAME, set())
        self.assertEqual(SAME - set(self.texts), set())                       # výjimka pro klíč, který už není, je mrtvá

    def test_every_key_is_used_and_every_used_key_exists(self):
        rest = TEXT_BLOCK.sub("", page())
        used = set(re.findall(r"\bt\('([\w.]+)'[,)]", rest)) | set(re.findall(r'data-i18n(?:-placeholder|-empty)?="([\w.]+)"', rest))
        self.assertEqual(used - set(self.texts), set())
        idle = {key for key in self.texts if key not in used and f"'{key}'" not in rest and not key.startswith(DYNAMIC) and key not in COMPOSED
                and f"'{key}'" not in re.sub(r"(?m)^  '[\w.]+': \{.*$", "", text_block())}      # klíč smí použít i funkce uvnitř bloku
        self.assertEqual(idle, set())

    def test_tips_and_columns_have_their_texts(self):                         # tip.<id> a col.<klíč> se skládají za běhu: chybějící text by shodil stránku při načtení
        code, ids = script(), {}
        for name in ("TIPS", "COLS"):
            found = re.search(r"\nconst %s = \[(.*?)\];" % name, code, re.S)
            self.assertIsNotNone(found, name)                                 # seznam klíčů, ne objekt s texty
            ids[name] = re.findall(r"'(\w+)'", found.group(1))
        self.assertGreaterEqual(len(ids["TIPS"]), 40)
        self.assertGreaterEqual(len(ids["COLS"]), 29)
        self.assertEqual(sorted("tip." + i for i in ids["TIPS"]), sorted(k for k in self.texts if k.startswith("tip.")))
        self.assertEqual(sorted(k for c in ids["COLS"] for k in ("col." + c, "col." + c + ".tip")),
                         sorted(k for k in self.texts if k.startswith("col.")))

    def test_row_results_and_number_gaps_have_their_texts(self):              # result.<stav> a gap.<třída> se skládají za běhu: chybějící text by shodil běh
        code = script()
        gaps, states = re.search(r"\nconst GAPS = \[(.*?)\];", code), re.search(r"\nconst RESULT = \{(.*?)\};", code)
        self.assertIsNotNone(gaps)
        self.assertIsNotNone(states)
        self.assertEqual(sorted("gap." + k for k in re.findall(r"'(\w+)'", gaps.group(1))), sorted(k for k in self.texts if k.startswith("gap.")))
        self.assertEqual(sorted("result." + k for k in re.findall(r"(\w+): \{", states.group(1))), sorted(k for k in self.texts if k.startswith("result.")))
        self.assertEqual(len([k for k in self.texts if k.startswith(("gap.", "result."))]), 6)

    def test_last_decision_lines_keep_their_columns(self):                    # „Poslední rozhodnutí“: popisky jednoho výpisu končí ve stejném sloupci, v obou jazycích
        blocks = [key for key, entry in self.texts.items() if key.startswith("last.") and "\n" in entry["cs"]]
        self.assertEqual(len(blocks), 4)                                      # e-maily, číselné duely, veřejná sada, zákazy a výjimky
        for key in blocks:
            for code in ("cs", "en"):
                lines = self.texts[key][code].split("\n")
                self.assertEqual(len(lines), len(self.texts[key]["cs"].split("\n")), key)
                self.assertEqual(len({len(re.match(r"[^:]+:\s+", line).group(0)) for line in lines}), 1, (key, code))
        values = {"text": "x", "pick": "a", "truth": "b"}
        self.assertEqual(node("t('last.public', arg)", {**values, "n": 1}).split("\n")[2], "nabídka: 1 štítků")      # česky beze změny
        self.assertEqual([node("t('last.public', arg)", {**values, "n": n}, lang="en").split("\n")[2] for n in (1, 77)], ["list:    1 label", "list:    77 labels"])

    def test_number_gap_classes_carry_one_name_everywhere(self):              # třídu rozdílu čísel jmenuje popis scénáře i popisek tabulky stejně jako tabulka sama
        for key in ("gap.tight", "gap.medium", "gap.clear"):
            for code in ("cs", "en"):
                name, limits = re.fullmatch(r"(\S+) \((.+)\)", self.texts[key][code]).groups()
                for where in ("scenario.numbers.about", "scenario.numbers.extraTip"):
                    self.assertIn(f"{name} {limits}", self.texts[where][code], (key, where, code))
        self.assertEqual([self.texts["gap." + k]["en"].split()[0] for k in ("tight", "medium", "clear")], ["narrow", "medium", "clear"])

    def test_sign_classes_carry_one_name_everywhere(self):                    # jméno třídy značky: tabulka stránky, popisek tabulky a bench.py stejně
        names = dict(re.findall(r"(\w+): '(sign\.\w+)'", piece(r"^const SIGN_NAME = \{.*?\};$")))
        self.assertEqual(list(names), ["only", "except_only", "no", "all_except", "limit_no", "limit_up_to"])      # pořadí SIGN_CLASSES
        for cls, key in names.items():
            for code in ("cs", "en"):
                self.assertEqual(self.texts[key][code], i18n.TEXTS["bench." + key][code], (key, code))
                self.assertIn(self.texts[key][code] + " (", self.texts["scenario.signs.extraTip"][code], (key, code))
        for code in ("cs", "en"):                                             # popisek voliče Scénář jmenuje volbu
            self.assertIn(self.texts["opt.scenario.signs"][code] + ": ", self.texts["tip.scenario"][code], code)
            self.assertIn(self.texts["scenario.signs.extraTitle"][code], self.texts["scenario.signs.about"][code], code)
        self.assertEqual(COMPOSED, {"scenario.signs": COMPOSED["scenario.signs"]})
        for key in ("tip.lanes", "tip.mN", "col.errors.tip", "col.late.tip", "col.goodput.tip", "row.tip.bad"):     # popisky, které výčtem jmenují scénáře s řadami
            self.assertIn("výjim", self.texts[key]["cs"], key)
            self.assertIn("bans and exceptions", self.texts[key]["en"], key)

    def test_statistics_and_storage_texts_count_in_the_right_number(self):    # anglicky jednotné i množné číslo, česky beze změny
        calls = [["tile.trojan.label", {"n": 1}], ["tile.trojan.label", {"n": 12}], ["chart.head", {"n": 1, "max": "40"}], ["chart.head", {"n": 150, "max": "40"}],
                 ["cal.head", {"ece": "X", "n": 1}], ["cal.head", {"ece": "X", "n": 30}], ["db.unsaved", {"n": 1}], ["db.unsaved", {"n": 2}]]
        text = "arg.map(([key, vars]) => t(key, vars))"
        cs, en = node(text, calls), node(text, calls, lang="en")
        self.assertEqual(cs[:4], ["trojský kůň: odolal / podlehl (1 úloh s pokynem)", "trojský kůň: odolal / podlehl (12 úloh s pokynem)",
                                  "posledních 1 rozhodnutí, max 40 ms", "posledních 150 rozhodnutí, max 40 ms"])
        self.assertEqual(cs[4], "ECE X  ·  1 odpovědí s jistotou  ·  nad osou kolikrát správně, pod osou kolikrát špatně (šlo zvolit líp)")
        self.assertEqual(cs[6], "Výsledků, které se nepodařilo uložit a jsou jen na stránce: 1 — vyexportujte je do CSV, po obnovení stránky zmizí.")
        self.assertEqual(en[:4], ["Trojan horse: resisted / fooled (1 task with the instruction)", "Trojan horse: resisted / fooled (12 tasks with the instruction)",
                                  "last 1 decision, max 40 ms", "last 150 decisions, max 40 ms"])
        self.assertEqual([line.split("  ·  ")[1] for line in en[4:6]], ["1 answer with a confidence", "30 answers with a confidence"])
        self.assertEqual([line.split(" — ")[1] for line in en[6:]], ["export it to CSV; it disappears after a page reload.", "export them to CSV; they disappear after a page reload."])

    def test_texts_quoted_in_tooltips_read_as_the_page_shows_them(self):      # popisek po najetí cituje text stránky: citace a text se nesmějí rozejít
        for code, (left, right) in (("cs", "„“"), ("en", '""')):
            unpriced = self.texts["stat.unpriced"][code].replace("{n}", "N").strip()
            self.assertIn(left + unpriced + right, self.texts["tip.mSpend"][code], code)                # „(+N bez ceny)“ za útratou
            resisted = re.search(left + r"(\w+)" + right, self.texts["tile.clean"][code]).group(1)      # hodnota „odolal“ na dlaždici trojského koně
            self.assertIn(f": {resisted} / ", self.texts["tile.trojan.label"][code], code)
            self.assertTrue(self.texts["last.trojanResisted"][code].endswith(" — " + resisted), code)

    def test_step_states_have_their_texts(self):                              # state.<stav> se skládá za běhu: chybějící text by shodil vykreslení fronty
        found = re.search(r"\nconst STATES = \[(.*?)\];", script())
        self.assertIsNotNone(found)                                           # seznam platných stavů, ne objekt s texty
        states = re.findall(r"'(\w+)'", found.group(1))
        self.assertEqual(sorted("state." + s for s in states), sorted(k for k in self.texts if k.startswith("state.")))
        order = ("wait", "run", "done", "error", "skip", "abort")
        self.assertEqual([self.texts["state." + s]["cs"] for s in order], ["čeká", "běží", "hotovo", "chyba", "přeskočeno", "přerušeno"])
        self.assertEqual([self.texts["state." + s]["en"] for s in order], ["waiting", "running", "done", "error", "skipped", "interrupted"])

    def test_scheduler_texts_quote_states_and_buttons_as_the_page_shows_them(self):      # věta plánovače cituje stav kroku, tlačítko nebo volbu: citace a text se nesmějí rozejít
        for code, (left, right) in (("cs", "„“"), ("en", '""')):
            text = lambda key: self.texts[key][code]
            quoted = lambda key: left + text(key) + right
            for where in ("plan.end.stopped", "tip.sqReset", "tip.sqStop"):
                self.assertIn(quoted("state.wait"), text(where), (code, where))
            for key in ("step.again", "ui.sqReset"):                          # „znovu“ u kroku a tlačítko Vrátit stavy
                self.assertIn(quoted(key), text("plan.run.nothing"), (code, key))
            for where in ("plan.note.visible", "plan.add.publicBench"):       # volba druhu kroku
                self.assertIn(quoted("opt.kind.bench"), text(where), (code, where))
            for state in ("wait", "abort", "error"):                          # popisek tlačítka Spustit frontu jmenuje stavy, které fronta spouští
                self.assertIn(text("state." + state), text("tip.sqRun"), (code, state))
        for state in ("done", "skip"):                                        # česky jsou skloněné („hotové a přeskočené“), anglicky doslova
            self.assertIn(self.texts["state." + state]["en"], self.texts["tip.sqRun"]["en"], state)
        for code, field in (("cs", "Délka průjezdu"), ("en", "Run length")):  # pole citované v hlášce nese text svého popisku
            self.assertIn(field, self.texts["plan.add.needsEnd"][code], code)
            self.assertTrue(self.texts["ui.runlen"][code].startswith(field), code)

    def test_scheduler_texts_count_in_the_right_number(self):                 # anglicky jednotné i množné číslo, česky beze změny
        calls = [["step.sum.lanes", {"n": 1}], ["step.sum.lanes", {"n": 3}], ["step.sum.bench", {"n": 1}], ["step.sum.bench", {"n": 30}],
                 ["step.sum.benchLimit", {"n": 1, "ms": 50}], ["step.sum.benchLimit", {"n": 30, "ms": 50}],
                 ["step.sum.turn", {"len": 1}], ["step.sum.turn", {"len": 12}],
                 ["step.sum.realtime", {"speed": 2, "len": 1}], ["step.sum.realtime", {"speed": 2, "len": 8}]]
        text = "arg.map(([key, vars]) => t(key, vars))"
        self.assertEqual(node(text, calls), ["1 pruhů", "3 pruhů", "benchmark 1 úloh", "benchmark 30 úloh", "benchmark 1 úloh, limit 50 ms",
                                             "benchmark 30 úloh, limit 50 ms", "po tazích, 1 polí", "po tazích, 12 polí",
                                             "reálný čas 2 polí/s, 1 polí", "reálný čas 2 polí/s, 8 polí"])
        self.assertEqual(node(text, calls, lang="en"), ["1 lane", "3 lanes", "benchmark of 1 task", "benchmark of 30 tasks",
                                                        "benchmark of 1 task, deadline 50 ms", "benchmark of 30 tasks, deadline 50 ms",
                                                        "turn-based, 1 cell", "turn-based, 12 cells",
                                                        "real time at 2 cells/s, 1 cell", "real time at 2 cells/s, 8 cells"])

    def test_run_hardware_and_hosted_texts_read_as_before_and_count_in_the_right_number(self):      # česky doslova jako před překladem, anglicky jednotné i množné číslo
        calls = [["run.ladder.level", {"level": 1, "total": 12, "speed": 1}], ["run.ladder.level", {"level": 3, "total": 12, "speed": 1.6}],
                 ["run.ladder.best", {"best": 2.4}], ["run.ladder.none", {}], ["run.ladder.interrupted", {"best": 2.4}], ["run.ladder.interruptedNone", {}],
                 ["run.limitDone", {"n": 1}], ["run.limitDone", {"n": 8}], ["run.benchProgress", {"i": 3, "n": 30}], ["run.speed", {"speed": "2.0"}],
                 ["run.didNotFit", {"labels": 77, "error": "Ollama 400: x"}], ["run.didNotFit", {"labels": "různě", "error": "x"}],
                 ["tile.hwJoule.label", {"wh": "0.012"}], ["hosted.option", {"name": "jev:jev-1.13.0", "who": "TypeSafe AI", "price": 0.1}],
                 ["opt.scenario.emails", {"name": "X"}], ["opt.scenario.public", {"name": "X"}], ["run.serverDown", {"error": "x"}], ["run.initFailed", {"error": "x"}]]
        text = "arg.map(([key, vars]) => t(key, vars))"
        self.assertEqual(node(text, calls), [
            "Žebřík: úroveň 1 z 12, 1 polí/s", "Žebřík: úroveň 3 z 12, 1.6 polí/s", "Žebřík: nejvyšší zvládnutá rychlost 2.4 polí/s.",
            "Žebřík: model nezvládl ani první úroveň.", "Žebřík přerušen: nejvyšší zvládnutá rychlost 2.4 polí/s.", "Žebřík přerušen: žádná zvládnutá úroveň.",
            "Průjezd dokončen: 1 polí.", "Průjezd dokončen: 8 polí.", "Benchmark: 3 / 30", "2.0 polí/s",
            "nevešlo se — server odmítl dotaz s nabídkou 77 štítků (Ollama 400: x)", "nevešlo se — server odmítl dotaz s nabídkou různě štítků (x)",
            "J na rozhodnutí (běh celkem 0.012 Wh)", "jev:jev-1.13.0 — TypeSafe AI, 0.1 $ za milion tokenů",
            "E-maily: X", "Veřejná sada: X", "Server nedostupný: x", "načtení stránky skončilo chybou (x)"])
        en = node(text, calls, lang="en")
        self.assertEqual(en[:2], ["Ladder: level 1 of 12, 1 cells/s", "Ladder: level 3 of 12, 1.6 cells/s"])
        self.assertEqual(en[6:10], ["Run finished: 1 cell.", "Run finished: 8 cells.", "Benchmark: 3 / 30", "2.0 cells/s"])
        self.assertEqual(en[13:16], ["jev:jev-1.13.0 — TypeSafe AI, $0.1 per million tokens", "E-mails: X", "Public dataset: X"])
        for line, labels in zip(en[10:12], ("77", "různě")):                  # počet štítků stojí za dvojtečkou: věta sedí s číslem i se slovem „různě“ / "varies"
            self.assertTrue(line.startswith("did not fit — "), line)
            self.assertIn(f": {labels} (", line)
        warn = {"model": "test:echo", "who": "zkušební poskytovatel", "price": 0.1}
        cs = node("[t('hosted.warning', arg), t('hosted.warningFallback', arg)]", warn)
        self.assertEqual(cs[0], "Model test:echo běží u poskytovatele zkušební poskytovatel. K poskytovateli odejde všechno, co je ve zvolené sadě nebo scénáři — "
                                "stav hry i celé texty e-mailů a vět, ať je sada přibalená, veřejná, nebo vaše vlastní — a každý dotaz stojí peníze: "
                                "0.1 $ za milion vstupních tokenů. Útratu hlídá strop na serveru. Potvrzení platí pro všechny modely tohoto poskytovatele a pamatuje se.")
        self.assertEqual(cs[1], "Uložený model není v nabídce, proto stránka předvolila tento hostovaný model; nikdo si ho nevybral, a než ho potvrdíte "
                                "nebo vyberete jiný, žádný dotaz neodejde. " + cs[0])
        self.assertIn("$0.1 per million input tokens", node("t('hosted.warning', arg)", warn, lang="en"))

    def test_hosted_and_dataset_texts_read_as_the_tooltips_and_panels_name_them(self):      # věta cituje skupinu voliče, volbu sady, panel nebo dlaždici: citace a text se nesmějí rozejít
        for code, (left, right) in (("cs", "„“"), ("en", '""')):
            text = lambda key: self.texts[key][code]
            group = text("hosted.group").split(" — ")[0]                     # „Hostované“ / "Hosted"
            self.assertIn(left + group + right, text("tip.model"), code)
            for key in ("opt.scenario.emails", "opt.scenario.public"):        # popisek voliče Scénář jmenuje volby „E-maily:“ a „Veřejná sada:“
                self.assertTrue(text(key).endswith(": {name}"), (code, key))
                self.assertIn(text(key).replace("{name}", "").strip(), text("tip.scenario"), (code, key))
            for key in ("opt.scenario.noEmails", "opt.scenario.listFailed"):  # vypnutá volba začíná stejným slovem jako volba sady e-mailů
                self.assertTrue(text(key).startswith(text("opt.scenario.emails").split(":")[0] + " ("), (code, key))
            for key in ("ui.speedPanel", "ui.tile.usd"):                      # panel Hardware u hostovaného modelu odkazuje na panel a dlaždici ceny
                self.assertIn(text(key), text("hosted.hw"), (code, key))
            self.assertIn(text("ui.model"), text("hosted.consentNeeded"), code)                         # volič Model nese text svého popisku
            self.assertTrue(text("hosted.warningFallback").endswith(". " + text("hosted.warning")), code)      # náhradní model: celé varování, před ním úvod
        self.assertEqual(self.texts["hosted.group"]["en"].split()[0], "Hosted")
        self.assertEqual([self.texts[key]["en"] for key in ("opt.scenario.emails", "opt.scenario.public")], ["E-mails: {name}", "Public dataset: {name}"])
        for key, word in (("tile.hwPower.label", "power draw"), ("tile.hwAvgPower.label", "power draw"), ("tile.hwClock.label", "clock"),
                          ("hw.throttled", "throttled"), ("run.ladder.interrupted", "interrupted"), ("run.benchDone", "done")):      # slovníček
            self.assertIn(word, self.texts[key]["en"], key)

    def test_csv_file_name_follows_the_page_language(self):                   # klíč je v obou jazycích jiný, proto není v SAME
        self.assertEqual(self.texts["csv.file"], {"cs": "pickbench-vysledky.csv", "en": "pickbench-results.csv"})
        self.assertNotIn("csv.file", SAME)

    def test_csv_cell_keeps_negative_numbers_and_percents_and_guards_formulas(self):      # procenta jsou česky „-3 %“ a anglicky „-3%“; obojí je číslo, ne vzorec
        numbers = ["-5", "-5.25", "-5,25", "-3 %", "-3%", "  -3%  ", "-12.5%", "-0"]
        formulas = ["=1+1", "+x", "-x", "@a", " =SUM(A1)", "-3 %x", "-%", "--5", "-5%%", "-3 % %"]
        plain = ["", "abc", "85%", "85 %", "5-3", "a=b"]
        got = node("arg.map(csvCell)", numbers + formulas + plain)
        self.assertEqual(got[:len(numbers)], numbers)
        self.assertEqual(got[len(numbers):len(numbers) + len(formulas)], ["'" + cell for cell in formulas])
        self.assertEqual(got[len(numbers) + len(formulas):], plain)

    def test_viewer_selects_and_model_chooses(self):                          # volba diváka v ovladači je anglicky select / selected; chosen, chose a picks patří odpovědi modelu nebo řidiče
        en = lambda key: self.texts[key]["en"]
        self.assertIn("selected dataset", en("tip.scenario"))                 # „ze zvolené sady“ stejně jako varování poskytovatele („ve zvolené sadě nebo scénáři“)
        self.assertNotIn("chosen dataset", en("tip.scenario"))
        for key in ("hosted.warning", "hosted.warningFallback"):
            self.assertIn("selected dataset", en(key), key)
        self.assertIn("you select it here", en("tip.lanes"))                  # počet pruhů „se volí tady“
        self.assertIn("selected speed", en("tip.mode"))                       # „zvolenou rychlostí“
        for key in self.texts:                                                # co divák volí v ovladači, není nikde chosen
            self.assertEqual(re.findall(r"\bchosen (?:dataset|scenario|model|driver|speed|mode|lane count|number of lanes)\b", en(key)), [], key)
        for key in ("last.emails", "last.numbers", "last.public", "scenario.emails.confHead", "scenario.public.col.confusions"):      # odpověď modelu zůstává chosen
            self.assertIn("chosen", en(key), key)

    def test_dataset_validator_texts_are_the_same_in_python_and_in_the_page(self):      # věty validátorů sad žijí dvakrát: stejný klíč = stejný text, znak po znaku
        for prefix in ("vd.", "vp."):
            keys = {key for key in self.texts if key.startswith(prefix)}
            self.assertEqual(keys, {key for key in i18n.TEXTS if key.startswith(prefix)}, prefix)
            self.assertGreaterEqual(len(keys), 14, prefix)                    # vd: 13 vět a výchozí název sady; vp: 12 vět, „úloha {n}“ a výchozí název
            for key in sorted(keys):
                self.assertEqual(i18n.TEXTS[key], self.texts[key], key)       # česky i anglicky
                for code in ("cs", "en"):                                     # a obě strany v nich vidí stejné proměnné ({n:.1f} by znal jen Python)
                    self.assertEqual(i18n.fields(self.texts[key][code]), set(FIELD_JS.findall(self.texts[key][code])), (key, code))

    def test_command_line_says_the_same_words_as_the_page(self):              # bench.py tiskne tytéž věci jako stránka: stejné slovo, v záhlaví nejvýš zkrácené
        columns = {"avg": "col.avg", "p50": "col.p50", "p95": "col.p95", "min": "col.min", "max": "col.max", "rate": "col.rate", "acc": "col.acc",
                   "conf": "tile.conf.label", "ece": "col.ece", "stab": "col.stab", "goodput": "col.goodput", "tokens": "last.tokens"}
        for code in ("cs", "en"):
            page_text, tool = (lambda key: self.texts[key][code]), (lambda key: i18n.TEXTS[key][code])
            for column, key in columns.items():
                words = re.split(r"[ /:,]+", page_text(key))
                for part in tool("bench.col." + column).split("/"):          # „rozh./s“, "dec./s", "conf.": zkratka s tečkou je začátek slova ze stránky
                    found = any(word.startswith(part[:-1]) if part.endswith(".") else word == part for word in words)
                    self.assertTrue(found, (code, column, part, words))
            for gap in ("tight", "medium", "clear"):                         # třídy rozdílu čísel: jméno bez mezí
                self.assertEqual(tool("bench.gap." + gap), page_text("gap." + gap).split()[0], (code, gap))
            fit = page_text("run.didNotFit").split("({error})")[0]           # „nevešlo se“: stejná stavba věty až po závorku s důvodem
            for key in ("bench.didNotFit", "bench.didNotFitReason"):
                self.assertTrue(tool(key).startswith(fit + "(HTTP 400"), (code, key))
            self.assertIn(page_text("scenario.public.tile.invalid"), tool("bench.quality"), code)
            self.assertIn(page_text("scenario.public.tile.invalid"), tool("bench.qualityRefused"), code)
            self.assertTrue(tool("bench.confusions").startswith(page_text("scenario.public.col.confusions").split(":")[0] + ": "), code)
            self.assertTrue(tool("bench.oos").startswith(page_text("scenario.public.tile.oos").split(" / ")[0] + " "), code)
            for name in page_text("tile.variants.label").split(": ")[1].split(" / "):      # původní / obrácené pořadí / jiná formulace
                self.assertIn(f" {name} {{", tool("bench.variants"), (code, name))
            trojan = tool("bench.trojan")
            self.assertTrue(trojan.startswith(page_text("tile.trojan.label").split(": ")[0] + ": "), code)
            self.assertIn(re.search(r"\((.+)\)", page_text("tile.trojan.label")).group(1), trojan, code)       # „{n} úloh s pokynem“
            for word in (*page_text("tile.trojan.label").split(": ")[1].split(" (")[0].split(" / "), page_text("last.trojanOther").split(" — ")[1]):
                self.assertIn(f" {word} {{", trojan, (code, word))           # odolal, podlehl, jiná chyba

    def test_hwbench_says_the_same_words_as_the_page(self):                   # hwbench.py tiskne tytéž věci jako panel Hardware a plánovač: stejné slovo
        for code, (left, right) in (("cs", "„“"), ("en", '""')):
            page_text, tool = (lambda key: self.texts[key][code]), (lambda key: i18n.TEXTS[key][code])
            self.assertEqual(tool("hw.didNotFit"), page_text("run.didNotFit").split(" — ")[0], code)        # nevešlo se
            self.assertEqual(tool("hw.interrupted"), "(" + page_text("state.abort") + ")", code)            # (přerušeno)
            self.assertIn(left + tool("hw.didNotFit") + right, tool("hw.note.truncation"), code)            # věta cituje buňku tabulky
            self.assertIn(" " + tool("hw.col.inputTokens"), tool("hw.note.truncation"), code)               # a jmenuje sloupec
            summary = tool("hw.soak.summary")
            self.assertIn(", " + page_text("tile.hwJoule.label").split(" (")[0] + " {j}", summary, code)    # J na rozhodnutí
            self.assertIn(", " + " ".join(page_text("tile.hwMaxTemp.label").split()[:2]) + " {temp} °C", summary, code)      # nejvyšší teplota
            self.assertTrue(page_text("tile.goodput.labelGame").endswith(" " + tool("hw.col.onTime")), code)                 # včas
            scope = page_text("scenario.public.tile.oos").split(":")[0]                                     # mimo rozsah
            for key in ("hw.skip.concurrency", "hw.skip.server"):            # česky „mimo rozsah“ v běžném smyslu jako dosud; anglicky je out of scope jen štítek veřejné sady
                self.assertEqual(tool(key).endswith(" " + scope + ")"), code == "cs", (code, key))
                self.assertEqual(scope in tool(key), code == "cs", (code, key))
            self.assertEqual(tool("hw.skip.concurrency").split(" (")[1], tool("hw.skip.server").split(" (")[1], code)      # oba testy přeskakují ze stejného důvodu
            self.assertEqual([i18n.fill(tool("hw.pct"), {"value": share * 100}) for share in (0.4, 1)],     # procenta jako ve stránce: česky s mezerou, anglicky bez
                             node("[pctOf(0.4), pctOf(1)]", lang=code), code)
        en = lambda key: i18n.TEXTS[key]["en"]
        hosted = self.texts["plan.pause.hosted"]["en"].split(" — ")[1].split(":")[0]                        # hosted model
        for key in ("hw.skip.concurrency", "hw.skip.server", "hw.skip.soak", "hw.note.truncation"):
            self.assertRegex(en(key), r" " + re.escape(hosted) + r"\b", key)
        self.assertEqual(en("hw.err.ollamaDown"), self.texts["run.serverDown"]["en"].replace("Server", "Ollama"))      # nedostupná = unreachable

    def test_fetch_datasets_says_the_same_words_as_the_page(self):            # popisy sad a hlášky stahování jmenují tytéž věci jako stránka: stejné slovo
        for code, (left, right) in (("cs", "„“"), ("en", '""')):
            page_text, tool = (lambda key: self.texts[key][code]), (lambda key: i18n.TEXTS[key][code])
            self.assertTrue(tool("fetch.scope.sample").endswith(", " + page_text("step.sum.seed")), code)       # semínko {seed}
            whole = {"cs": "celá testovací část", "en": "the whole test split"}[code]
            self.assertIn(whole, page_text("scenario.public.tip.acc"), code)
            self.assertTrue(tool("fetch.scope.full").startswith(whole + ", "), code)
            for name in ("banking77", "clinc150", "massive"):                 # „Testovací část sady“ nese stejné slovo
                self.assertIn({"cs": "Testovací část sady", "en": "The test split of the dataset"}[code], tool("fetch.about." + name), (code, name))
            scope = page_text("scenario.public.tile.oos").split(":")[0]      # mimo rozsah
            self.assertIn(left + scope + right, tool("fetch.about.clinc150"), code)
            own = re.search(r"\((.+?)\)", page_text("scenario.public.aboutOwn")).group(1).removesuffix({"cs": " štítků", "en": " of labels"}[code])
            self.assertIn("; " + own + ".", tool("fetch.about.horizonCs"), code)                               # každá úloha má vlastní nabídku
        en = lambda key: i18n.TEXTS[key]["en"]
        for key in ("fetch.saved", "fetch.scope.full", "fetch.horizon.ended", "fetch.horizon.fallback", "fetch.horizon.progress"):      # úloha = task, i v jednotném čísle
            self.assertIn("{n|task|tasks}", en(key), key)
        for key in (k for k in i18n.TEXTS if k.startswith("fetch.") and "štítk" in i18n.TEXTS[k]["cs"]):      # štítek = label
            self.assertIn("label", en(key), key)
        for key in (k for k in i18n.TEXTS if k.startswith("fetch.") and "záměr" in i18n.TEXTS[k]["cs"]):      # záměr = intent
            self.assertIn("intent", en(key), key)

    def test_providers_and_store_say_the_same_words_as_the_page_and_the_tools(self):      # věty poskytovatelů a úložiště jmenují tytéž věci jako stránka: stejné slovo
        import bench
        for code in ("cs", "en"):
            page_text, tool = (lambda key: self.texts[key][code]), (lambda key: i18n.TEXTS[key][code])
            usage = {"cs": bench.__doc__, "en": bench.USAGE_EN}[code]
            self.assertIn("{per} " + page_text("col.usd") + ")", tool("prov.spent"), code)                  # útrata na 1000 rozhodnutí: jednotka jako sloupec stránky
            self.assertIn(", " + page_text("col.usd") + ", ", usage, code)                                  # a jak ji jmenuje nápověda bench.py
            self.assertTrue(tool("prov.spent").endswith("; " + page_text("note.network")), code)           # latence včetně sítě
            self.assertTrue(tool("prov.spentUnpriced").startswith(tool("prov.spent") + "; "), code)        # dovětek o odpovědích bez tokenů stojí za celou větou
            self.assertTrue(tool("prov.spent").startswith(page_text("plan.pause.hosted").split(" — ")[1].split(":")[0] + " ("), code)      # hostovaný model
            for word in re.split(r"[ :(),]+", page_text("ui.tile.net")):                                    # síť, spojení, odhad
                if word != "ms":
                    self.assertIn(word, tool("prov.net"), (code, word))
            self.assertEqual(tool("prov.netFailed").split(":")[0], tool("prov.net").split(":")[0], code)
            cut = {"cs": 1, "en": 2}[code]                                                                  # věta bez podmětu: „neodpověděl do {s} s“
            self.assertEqual(tool("prov.timeout").split(" ", cut)[cut], page_text("db.timeout").split(" ", cut)[cut].replace(" ({path})", ""), code)
            self.assertTrue(tool("hw.err.cfEnv").rstrip(".").endswith(tool("prov.keyMissing").split("{model} ")[1]), code)      # proměnná prostředí, která není nastavená
        en = lambda key: i18n.TEXTS[key]["en"]
        for key in ("prov.cap.requests", "prov.cap.usd", "prov.cap.usdHeld"):                               # strop útraty = spend cap; věta končí tím, že další dotaz neodešel
            self.assertTrue(i18n.TEXTS[key]["cs"].startswith("strop útraty — ") and en(key).startswith("spend cap — "), key)
            self.assertTrue(en(key).endswith("; " + en("prov.cap.requests").split("; ")[1]), key)
        for text in (en("prov.spentUnpriced"), self.texts["tip.mSpend"]["en"]):                             # spodní odhad = lower bound, jako v popisku útraty
            self.assertIn("without a token count", text)
            self.assertIn(" is a lower bound", text)
        self.assertEqual(i18n.TEXTS["srv.ollamaDown"], i18n.TEXTS["hw.err.ollamaDown"])                     # stejná věta v server.py a v hwbench.py zní stejně
        self.assertIn(self.texts["ui.tile.spend"]["en"].split(" / ")[1].split()[0], en("srv.start.hosted"))  # strop = cap, jako na dlaždici útraty
        words = (("dotaz", "request"), ("poskytovatel", "provider"), ("odpově", "answer"), ("hostovan", "hosted"), ("spojení", "connection"),
                 ("opakov", "retr"), ("pokus", "attempt"), ("databáze", "database"), ("výsled", "result"), ("front", "queue"), ("drží odhad", "estimate"),
                 ("požadav", "http request"), ("strop útraty", "spend cap"), ("odhad sítě", "network estimate"), ("jméno modelu", "model name"))
        for key in (k for k in i18n.TEXTS if k.startswith(tuple(SERVER_KEYS.values()))):                    # slovníček: jedno české slovo, jeden anglický protějšek
            for cs, word in words:
                if cs in i18n.TEXTS[key]["cs"].lower():
                    self.assertIn(word, en(key).lower(), (key, cs))

    def test_category_name_and_letter_follow_the_page_language(self):         # anglická stránka bere label_en a letter_en, když je kategorie má; jinak label a letter
        cats = [{"label": "Faktura", "letter": "F", "label_en": "Invoice", "letter_en": "I"}, {"label": "Podpora", "letter": "P"}]
        self.assertEqual(node("arg.map(c => [labelOf(c), letterOf(c)])", cats), [["Faktura", "F"], ["Podpora", "P"]])
        self.assertEqual(node("arg.map(c => [labelOf(c), letterOf(c)])", cats, lang="en"), [["Invoice", "I"], ["Podpora", "P"]])
        code = script()                                                       # název a písmeno kategorie čte stránka pro diváka jen přes labelOf a letterOf
        self.assertEqual((code.count("labelOf("), code.count("letterOf(")), (7, 2))
        for direct in ("cat.label", "cat.letter", "].label", "dot, c.label", "th(c.label"):
            self.assertEqual(code.count(direct), 0, direct)

    def test_dataset_description_follows_the_page_language(self):             # anglická stránka bere about_en, když ho sada má a není prázdný; jinak about
        sets = [{"about": "česky", "about_en": "in English"}, {"about": "česky"}, {"about": "česky", "about_en": ""},
                {"about": "česky", "about_en": None}, {"about": "", "about_en": "in English"}, {"about": ""}]
        self.assertEqual(node("arg.map(aboutOf)", sets), ["česky", "česky", "česky", "česky", "", ""])       # česká stránka vždy about
        self.assertEqual(node("arg.map(aboutOf)", sets, lang="en"), ["in English", "česky", "česky", "česky", "in English", ""])
        blank = [{"about": "česky", "about_en": text} for text in ("   ", "\t\n", "\xa0 \r\n")]         # jen bílé znaky = prázdný: anglická stránka nesmí ukázat prázdný popis
        self.assertEqual(node("arg.map(aboutOf)", blank, lang="en"), ["česky"] * 3)
        self.assertEqual(node("arg.map(aboutOf)", blank), ["česky"] * 3)
        self.assertEqual(node("aboutOf(arg)", {"about": "česky", "about_en": "  in English "}, lang="en"), "  in English ")      # popis s textem se neořezává

    def test_about_line_shows_the_english_description_of_the_selected_dataset(self):      # řádek pod ovládáním: sada e-mailů i veřejná sada, po přepnutí jazyka znovu
        parts = [piece(r"^function aboutText\(\) [^\n]*$"), piece(r"^const publicSet = \{.*?^\};$")]
        got = node_run("""
let DS = null, PS = null, SC = null;
%s
const read = () => ['cs', 'en', 'cs'].map(code => { LANG = code; return aboutText(); });      // přepnutí jazyka: renderLang volá aboutText
const out = {};
SC = { about: 'Scénář.' };
DS = { about: 'Česky.', about_en: 'In English.' }; out.emails = read();
DS = { about: 'Česky.' }; out.emailsCzechOnly = read();              // vlastní sada uživatele s českým popisem
DS = { about: '' }; out.emailsNone = read();
DS = null; SC = publicSet;
PS = { about: 'Česky.', about_en: 'In English.', source: 'zdroj', license: 'licence', labels: [{}, {}], oos: null }; out.public = read();
PS = { ...PS, labels: null }; out.publicOwn = read();
PS = { about: 'Česky.', source: 'zdroj', license: 'licence', labels: [{}, {}], oos: 'oos' }; out.publicCzechOnly = read();
process.stdout.write(JSON.stringify(out));
""" % "\n".join(parts))
        both = lambda cs, en: [cs, en, cs]
        self.assertEqual(got["emails"], both("Scénář. Česky.", "Scénář. In English."))
        self.assertEqual(got["emailsCzechOnly"], both("Scénář. Česky.", "Scénář. Česky."))
        self.assertEqual(got["emailsNone"], both("Scénář.", "Scénář."))
        fill = lambda key, code, rest: self.texts[key][code].replace("{n}", "2").replace("{n|label|labels}", "labels").replace("{oos}", "oos").replace("{rest}", rest)
        tail = {"cs": " Zdroj: zdroj. Licence: licence.", "en": " Source: zdroj. License: licence."}      # zdroj a licence jsou text sady: nepřekládají se
        self.assertEqual(got["public"], both(fill("scenario.public.about", "cs", "Česky." + tail["cs"]), fill("scenario.public.about", "en", "In English." + tail["en"])))
        self.assertEqual(got["publicOwn"], both(fill("scenario.public.aboutOwn", "cs", "Česky." + tail["cs"]), fill("scenario.public.aboutOwn", "en", "In English." + tail["en"])))
        self.assertEqual(got["publicCzechOnly"], both(fill("scenario.public.aboutOos", "cs", "Česky." + tail["cs"]), fill("scenario.public.aboutOos", "en", "Česky." + tail["en"])))
        self.assertIn(" Česky. Zdroj: zdroj.", got["public"][0])
        self.assertIn(" In English. Source: zdroj.", got["public"][1])

    def test_email_validator_takes_about_en_the_same_in_python_and_in_the_page(self):      # about_en je volitelný text jako about; jiný typ má vlastní větu
        import bench
        cat = lambda k: {"key": k, "label": k.upper(), "description": "d " + k}
        good = {"categories": [cat("a"), cat("b")], "emails": [{"subject": "s", "body": "b", "category": "a"}]}
        sets = [{**good, "about_en": 5}, {**good, "about_en": ["x"]}, {**good, "about_en": {"en": "x"}}, {**good, "about_en": True},
                {**good, "about": 5, "about_en": 5},                          # obě špatně: první je věta o about, jako dosud
                {**good, "about": "česky", "about_en": "in English"}, {**good, "about_en": "in English"}, {**good, "about": "česky", "about_en": None},
                {**good, "about": "česky", "about_en": ""}, {**good, "about": "česky"},
                {**good, "about": "česky", "about_en": "   "}, {**good, "about": "česky", "about_en": "\t\r\n\xa0"},      # jen bílé znaky = prázdný
                {**good, "about": "česky", "about_en": "  in English "}]                                                   # popis s textem se neořezává
        source = re.search(r"\nfunction validateDataset\(d\) \{\n.*?\n\}\n", page(), re.S).group(0)
        shape = lambda ds: {"about": ds["about"], "about_en": ds.get("about_en"), "keys": sorted(ds)}
        for lang, wrong, about in (("cs", "Neplatná sada: about_en musí být text", "Neplatná sada: about musí být text"),
                                   ("en", "Invalid dataset: about_en must be text", "Invalid dataset: about must be text")):
            js = node("arg.map(d => { try { const ds = validateDataset(d); return { about: ds.about, about_en: ds.about_en ?? null, keys: Object.keys(ds).sort() }; } "
                      "catch (e) { return e.message; } })", sets, lang=lang, extra="\nconst PALETTE = [];" + source)
            py = []
            with i18n.process(lang):
                for d in sets:
                    try:
                        py.append(shape(bench.validate_dataset(d)))
                    except ValueError as e:
                        py.append(str(e))
            self.assertEqual(js, py, lang)
            self.assertEqual(py[:5], [wrong] * 4 + [about], lang)
            base = ["about", "categories", "emails", "index", "instructions", "name"]
            self.assertEqual(py[5:], [{"about": "česky", "about_en": "in English", "keys": sorted(base + ["about_en"])},
                                      {"about": "", "about_en": "in English", "keys": sorted(base + ["about_en"])},
                                      {"about": "česky", "about_en": None, "keys": base}, {"about": "česky", "about_en": None, "keys": base},
                                      {"about": "česky", "about_en": None, "keys": base},             # bez anglického popisu výsledek pole nenese
                                      {"about": "česky", "about_en": None, "keys": base}, {"about": "česky", "about_en": None, "keys": base},
                                      {"about": "česky", "about_en": "  in English ", "keys": sorted(base + ["about_en"])}], lang)
        edge = [{**good, "about_en": chr(code)} for code in (0xFEFF, 0x1F, 0x85, 0x200B)]      # hranice množiny: bílé znaky jsou ty, které ořízne String.prototype.trim
        js = node("arg.map(d => 'about_en' in validateDataset(d))", edge, extra="\nconst PALETTE = [];" + source)
        self.assertEqual(js, [False, True, True, True])
        self.assertEqual(["about_en" in bench.validate_dataset(d) for d in edge], js)      # bench.py bere touž množinu (str.strip() bez argumentu by se lišil)

    def test_email_validator_says_the_same_in_python_and_in_the_page(self):
        import bench
        cat = lambda k, **more: {"key": k, "label": k.upper(), "description": "d " + k, **more}
        mail = lambda c: {"subject": "s", "body": "b", "category": c}
        good = {"categories": [cat("a"), cat("b")], "emails": [mail("a")]}
        BAD_SETS = [[], {}, {**good, "name": 5}, {**good, "about": 5}, {**good, "categories": [cat("a")]}, {**good, "emails": []},
                    {**good, "categories": [cat("a"), 7]}, {**good, "categories": [cat("a"), {"key": "b"}]}, {**good, "categories": [cat("a"), cat("a")]},
                    {**good, "categories": [cat("a"), cat("b", letter=5)]}, {**good, "categories": [cat("a"), cat("b", color="red")]},
                    {**good, "emails": [7]}, {**good, "emails": [{"subject": "s"}]}, {**good, "emails": [mail("zzz")]}, good]
        source = re.search(r"\nfunction validateDataset\(d\) \{\n.*?\n\}\n", page(), re.S).group(0)
        for lang in ("cs", "en"):
            js = node("arg.map(d => { try { validateDataset(d); return null; } catch (e) { return e.message; } })", BAD_SETS, lang=lang,
                      extra="\nconst PALETTE = [];" + source)
            py = []
            with i18n.process(lang):
                for d in BAD_SETS:
                    try:
                        bench.validate_dataset(d)
                        py.append(None)
                    except ValueError as e:
                        py.append(str(e))
            self.assertEqual(js, py, lang)
            self.assertEqual(len(set(js)), 14, lang)                          # 13 různých vět a jedna platná sada
            self.assertTrue(all(m is None or m.startswith({"cs": "Neplatná sada: ", "en": "Invalid dataset: "}[lang]) for m in js), lang)

    def test_czech_texts_of_the_page_are_kept(self):                           # česky beze změny i tam, kam prohlídka nedohlédne
        golden = json.loads(FRAGMENTS.read_text(encoding="utf-8"))
        self.assertGreater(len(golden), 300)
        rest = TEXT_BLOCK.sub("", page())
        cs = "\n".join(entry["cs"] for entry in self.texts.values())
        lost = [f for f in golden if f not in GONE and f not in rest and plain_text(f) not in cs]
        self.assertEqual(lost, [])                                            # úlomek českého textu před změnou není ve skriptu ani v češtině TEXTS
        self.assertEqual([f for f in GONE if f not in golden], [])            # výjimka pro úlomek, který ve zlatém souboru není, je mrtvá

    def test_sentence_in_the_page_language(self):
        self.assertEqual(node("t('page.title')"), "PickBench — demonstrátor rozhodovacích modelů")
        self.assertEqual(node("t('page.title')", lang="en"), self.texts["page.title"]["en"])
        self.assertEqual(node("fill('{n} {n|step|steps} of {total}', arg)", {"n": 1, "total": 3}), "1 step of 3")
        self.assertEqual(node("fill('{n} {n|step|steps}', arg)", {"n": 2}), "2 steps")
        self.assertEqual(node("fill('a {missing} b', {})"), "a {missing} b")

    def test_missing_english_is_loud_and_unknown_key_is_an_error(self):
        self.assertEqual(node("(TEXTS.x = { cs: 'česky {n}', en: '' }, t('x', { n: 2 }))", lang="en"), "[[x]] česky 2")
        self.assertEqual(node("(() => { try { return t('takový klíč není'); } catch (e) { return 'chyba'; } })()"), "chyba")

    def test_first_language(self):
        cases = [(["en", "cs", "cs-CZ"], {"lang": "en", "save": True}),       # ?lang= má přednost a uloží se
                 (["de", "en", "cs-CZ"], {"lang": "en", "save": False}),      # nesmysl v adrese: platí uložená volba
                 ([None, None, "cs-CZ"], {"lang": "cs", "save": False}),
                 ([None, None, "sk"], {"lang": "cs", "save": False}),
                 ([None, None, "en-US"], {"lang": "en", "save": False}),
                 ([None, "xx", "pl"], {"lang": "en", "save": False}),
                 ([None, None, None], {"lang": "en", "save": False}),
                 ([None, None, "csb"], {"lang": "en", "save": False}),
                 (["EN", "cs", "cs-CZ"], {"lang": "en", "save": True}),       # parametr adresy jako na serveru (i18n.known): velká písmena a mezery kolem nevadí
                 ([" en ", None, "cs-CZ"], {"lang": "en", "save": True}),     # /?lang=en%20
                 (["Cs", "en", "en-US"], {"lang": "cs", "save": True}),
                 (["xx", "cs", "en-US"], {"lang": "cs", "save": False}),      # nesmysl dál neplatí
                 (["", "cs", "en-US"], {"lang": "cs", "save": False}),
                 ([" ", None, "en-US"], {"lang": "en", "save": False}),
                 ([None, "EN", "cs-CZ"], {"lang": "cs", "save": False}),      # uložená volba a jazyk prohlížeče se berou, jak jsou
                 ([None, " en ", "cs-CZ"], {"lang": "cs", "save": False})]
        self.assertEqual(node("arg.map(a => chooseLang(...a))", [args for args, _ in cases]), [want for _, want in cases])
        asked = ["cs", "en", "EN", " en ", "Cs", "\tcs\n", "xx", "", " ", "en-US", "cs,en", "e n"]
        page = node("arg.map(q => chooseLang(q, null, null))", asked)         # stránka bere z adresy právě to, co server z hlavičky jazyka
        self.assertEqual([got["lang"] if got["save"] else None for got in page], [i18n.known(q) for q in asked])

    def test_numbers_and_time(self):
        expr = "[f(1.25, 1), f(NaN), pctOf(0.5), pctOf(0.1234, 1), pctOf(NaN), pctOf(null), fmtTime('2026-10-06T21:14:03'), fmtTime('x')]"
        self.assertEqual(node(expr), ["1.3", "—", "50 %", "12.3 %", "—", "—", "6. 10. 21:14:03", "—"])
        self.assertEqual(node(expr, lang="en"), ["1.3", "—", "50%", "12.3%", "—", "—", "Oct 6 21:14:03", "—"])


VALS = {"driver": "model", "model": "tev1", "extra": True, "mirror": True, "scenario": "obstacles", "set": None, "lanes": 3,
        "mode": "ladder", "aborted": True, "speed": None, "best": None, "n": 12, "avg": 41.26, "p50": 40.0, "p95": 55.5, "min": 30.0,
        "max": 60.04, "rate": 9.87, "acc": 0.75, "ece": 0.0312, "stab": None, "errors": 2, "late": None, "dist": 59.7, "joule": 3.456,
        "temp": 61.0, "fooled": None, "runlen": None, "seed": "", "f1": None, "goodput": None, "usd": None, "net": None,
        "note": [["hot", {"temp": 71, "target": 60}], ["stopped"], ["text", "volná věta"]]}
OLD = {"driver": "tev1 +2. otázka +zrcadla", "scenario": "číselné duely", "lanes": "různě", "mode": "žebřík (přerušen), max — polí/s", "n": 5,
       "avg": "12.3", "acc": "85 %", "ece": "3.1 %", "stab": "—", "f1": "71.4 %", "goodput": "—", "fooled": "25 %",
       "note": "start při 71 °C (cíl pod 60 °C); zastaven před délkou průjezdu; cena neznámá u 2 z 5 dotazů; něco vlastního"}


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Results(unittest.TestCase):
    def test_new_row_is_composed_in_the_page_language(self):
        data = {"vals": VALS, "batch": 3, "step": 2}
        cs = node("cellsOf(arg)", data)
        self.assertEqual(cs, {"driver": "tev1 +2. otázka +zrcadla", "scenario": "překážky", "lanes": 3, "mode": "žebřík (přerušen), max — polí/s",
                              "n": 12, "avg": "41.3", "p50": "40.0", "p95": "55.5", "min": "30.0", "max": "60.0", "rate": "9.9", "acc": "75 %",
                              "ece": "3.1 %", "stab": "—", "errors": 2, "late": "—", "dist": "60", "joule": "3.46", "temp": "61", "fooled": "—",
                              "runlen": "—", "seed": "—", "f1": "—", "goodput": "—", "usd": "—", "net": "—", "batch": "3/2",
                              "note": "start při 71 °C (cíl pod 60 °C); zastaven před délkou průjezdu; volná věta"})
        en = node("cellsOf(arg)", data, lang="en")
        self.assertEqual({k: v for k, v in en.items() if v != cs[k]},
                         {"driver": "tev1 +2nd question +mirrors", "scenario": "obstacles", "mode": "ladder (interrupted), max — cells/s",
                          "acc": "75%", "ece": "3.1%", "note": "started at 71 °C (target below 60 °C); stopped before reaching the run length; volná věta"})

    def test_every_mode_driver_and_scenario(self):
        rows = [{"vals": {**VALS, **change}} for change in (
            {"mode": "bench", "aborted": False}, {"mode": "bench", "aborted": True}, {"mode": "ladder", "aborted": False, "best": 2.4},
            {"mode": "realtime", "speed": "2.4"}, {"mode": "turn"}, {"driver": "oracle", "model": None, "extra": False, "mirror": False},
            {"driver": "random"}, {"scenario": "numbers"}, {"scenario": "emails", "set": "Firemní e-maily"}, {"scenario": "public", "set": "MASSIVE", "lanes": None},
            {"driver": "neznámý"})]
        pick = "arg.map(d => cellsOf(d)).map(c => [c.mode, c.driver, c.scenario, c.lanes])"
        cs, en = node(pick, rows), node(pick, rows, lang="en")
        self.assertEqual([c[0] for c in cs[:5]], ["benchmark", "benchmark (přerušen)", "žebřík, max 2.4 polí/s", "reálný čas, 2.4 polí/s", "po tazích"])
        self.assertEqual([c[0] for c in en[:5]], ["benchmark", "benchmark (interrupted)", "ladder, max 2.4 cells/s", "real time, 2.4 cells/s", "turn-based"])
        self.assertEqual([c[1] for c in cs[5:7]], ["Pravidla (ideální řidič)", "Náhodně"])
        self.assertEqual([c[1] for c in en[5:7]], ["Rules (ideal driver)", "Random"])
        self.assertEqual([c[2] for c in cs[7:10]], ["číselné duely", "Firemní e-maily", "MASSIVE"])
        self.assertEqual([c[2] for c in en[7:10]], ["number duels", "Firemní e-maily", "MASSIVE"])      # název sady se nepřekládá
        self.assertEqual((cs[9][3], en[9][3]), ("různě", "varies"))
        self.assertEqual((cs[10][1], en[10][1]), ("neznámý", "neznámý"))                                # neznámý klíč stránku neshodí

    def test_signs_row_reads_in_both_languages(self):                         # nový scénář nemá řádky z doby před data.vals, jen klíč scenario.signs
        rows = [{"vals": {**VALS, "scenario": "signs", "lanes": 5}}]
        pick = "arg.map(d => cellsOf(d)).map(c => [c.scenario, c.lanes])"
        self.assertEqual(node(pick, rows), [["zákazy a výjimky", 5]])
        self.assertEqual(node(pick, rows, lang="en"), [["bans and exceptions", 5]])

    def test_row_saved_before_the_change_reads_in_both_languages(self):
        self.assertEqual(node("cellsOf(arg)", {"cells": OLD}), OLD)                                     # česky doslova tak, jak je uložená
        en = node("cellsOf(arg)", {"cells": OLD}, lang="en")
        self.assertEqual(en, {**OLD, "driver": "tev1 +2nd question +mirrors", "scenario": "number duels", "lanes": "varies",
                              "mode": "ladder (interrupted), max — cells/s", "acc": "85%", "ece": "3.1%", "f1": "71.4%", "fooled": "25%",
                              "note": "started at 71 °C (target below 60 °C); stopped before reaching the run length; price unknown for 2 of 5 requests; něco vlastního"})
        more = [{"cells": {"driver": d, "scenario": "Vlastní sada", "mode": m}} for d, m in (
            ("Pravidla (ideální řidič)", "reálný čas, 2 polí/s"), ("Náhodně", "po tazích"), ("tev1:0.8b", "benchmark (přerušen)"), ("tev1 +zrcadla", "benchmark"))]
        self.assertEqual(node("arg.map(d => cellsOf(d)).map(c => [c.driver, c.scenario, c.mode])", more, lang="en"),
                         [["Rules (ideal driver)", "Vlastní sada", "real time, 2 cells/s"], ["Random", "Vlastní sada", "turn-based"],
                          ["tev1:0.8b", "Vlastní sada", "benchmark (interrupted)"], ["tev1 +mirrors", "Vlastní sada", "benchmark"]])
        self.assertEqual(node("[cellsOf(null), cellsOf({}), cellsOf({ cells: 5 })]", lang="en"), [{}, {}, {}])      # poškozený řádek

    def test_broken_row_from_the_database_does_not_take_the_table_down(self):
        # Stránka takový řádek nezapíše, ale databáze ho nést může (ruční zásah, jiná verze). Jediná výjimka z cellsOf shodí renderResults
        # a s ním načtení stránky: tabulka zůstane prázdná, fronta se nenačte a nový výsledek se neuloží. Proto cokoli, co projde JSON.
        notes = [(None, ""), (5, ""), ("volná věta", "volná věta"), ([None], ""), ([[5, {}]], "5"),
                 ([["hot", None]], "start při {temp} °C (cíl pod {target} °C)"),          # proměnné, které nejsou objekt, se berou jako prázdné
                 ([["unpriced", "x"]], "cena neznámá u {missing} z {n} dotazů"), ([["retries", 7]], "opakování po odmítnutí poskytovatelem: {n}"),
                 ([["hot", [1]]], "start při {temp} °C (cíl pod {target} °C)"), ([["hot"]], "start při {temp} °C (cíl pod {target} °C)"),
                 ([["takový klíč není", {}]], "takový klíč není"), ([[True, 1]], "true"),   # neznámý nebo neřetězcový klíč: text klíče
                 ([[]], "—"), ([[None]], "—"), ([[{"a": 1}, {}]], "—"), ([[["hot"], {}]], "—"),
                 ([["text"]], "—"), ([["text", None]], "—"), ([["text", {"a": 1}]], "—"), ([["text", 5]], "5"),
                 ([["stopped"], None, [5], ["error", "x"]], "zastaven před délkou průjezdu; 5; skončil chybou")]
        rows = [{"vals": {**VALS, "note": note}} for note, _ in notes]
        who = [(5, "5"), (None, "—"), ({"a": 1}, "—"), ([1], "—"), (True, "true"), ("", "")]
        rows += [{"vals": {**VALS, "driver": bad}} for bad, _ in who] + [{"vals": {**VALS, "scenario": bad}} for bad, _ in who]
        rows += [{"vals": {**VALS, "model": bad}} for bad in (None, {"a": 1}, 5)]
        bare = [{"vals": {}}, {"vals": []}, {"vals": {"driver": "model"}}, {"vals": {"mode": "realtime"}}, {"vals": {"mode": "ladder"}},
                {"vals": {}, "batch": 3}, {"vals": {"note": [["unpriced"]]}, "batch": 3, "step": None}]
        legacy = [{"vals": None}, {"vals": "x"}, {"vals": 5}, {"vals": True}, {"vals": None, "cells": 5}, {"cells": "text"}, {"cells": [1, {"a": 1}]},
                  {"cells": {"driver": 5, "scenario": None, "mode": {"a": 1}, "note": [1], "acc": 0.5, "lanes": []}}, [], {"vals": 5, "cells": None}]
        rows += bare + legacy
        pick = "arg.map(d => { try { return cellsOf(d); } catch (e) { return 'VÝJIMKA ' + e.message; } })"
        cs, en = node(pick, rows), node(pick, rows, lang="en")
        for lang, got in (("cs", cs), ("en", en)):
            self.assertEqual([(i, cells) for i, cells in enumerate(got) if isinstance(cells, str)], [], lang)      # žádný řádek nevyhodil výjimku
            texts = [(i, key, text) for i, cells in enumerate(got) if isinstance(cells, dict) for key, text in cells.items() if isinstance(text, str)]
            self.assertEqual([hit for hit in texts if "undefined" in hit[2] or "[[" in hit[2]], [], lang)
        a, b, c = len(notes), len(notes) + len(who), len(notes) + 2 * len(who)
        self.assertEqual([cells["note"] for cells in cs[:a]], [want for _, want in notes])
        self.assertEqual(en[5]["note"], "started at {temp} °C (target below {target} °C)")
        self.assertEqual(en[6]["note"], "price unknown for {missing} of {n} {n|request|requests}")
        self.assertEqual([cells["driver"] for cells in cs[a:b]], [want for _, want in who])
        self.assertEqual([cells["scenario"] for cells in cs[b:c]], [want for _, want in who])
        self.assertEqual([cells["driver"] for cells in cs[c:c + 3]], ["— +2. otázka +zrcadla", "— +2. otázka +zrcadla", "5 +2. otázka +zrcadla"])
        empty = {"driver": "—", "scenario": "—", "lanes": "různě", "mode": "po tazích", "avg": "—", "acc": "—", "errors": "—", "seed": "—", "batch": "—", "note": ""}
        for cells in cs[c + 3:c + 5]:                                           # vals bez jediného pole, jako objekt i jako pole
            self.assertEqual({key: cells.get(key) for key in empty}, empty)
            self.assertNotIn("n", cells)                                        # pole bez hodnoty tabulka ukáže jako „—“ (renderResults)
        self.assertEqual([cells["driver"] for cells in cs[c + 5:c + 6]], ["—"])                         # řidič Model bez jména modelu
        self.assertEqual([cells["mode"] for cells in cs[c + 6:c + 8]], ["reálný čas, — polí/s", "žebřík, max — polí/s"])
        self.assertEqual([cells["batch"] for cells in cs[c + 8:c + 10]], ["3/—", "3/—"])               # dávka bez čísla kroku
        self.assertEqual(en[c + 9]["note"], "price unknown for {missing} of {n} {n|request|requests}")
        old = c + 3 + len(bare)
        self.assertEqual(cs[old:old + 7], [{}, {}, {}, {}, {}, {}, [1, {"a": 1}]])                    # vals, které nejsou objekt: řádek ve starém tvaru
        self.assertEqual(en[old:old + 7], [{}, {}, {}, {}, {}, {}, {"0": 1, "1": {"a": 1}}])
        self.assertEqual(en[old + 7], {"driver": 5, "scenario": None, "mode": {"a": 1}, "note": [1], "acc": 0.5, "lanes": []})      # co není text, zůstává; tabulka z toho udělá „—“
        self.assertEqual((cs[old + 8:], en[old + 8:]), ([{}, {}], [{}, {}]))

    def test_unpriced_note_counts_requests_in_the_right_number(self):
        def note(missing, n):
            return {"vals": {**VALS, "note": [["unpriced", {"missing": missing, "n": n}]]}}
        rows = [note(1, 1), note(3, 3), note(2, 5)]
        pick = "arg.map(d => cellsOf(d).note)"
        self.assertEqual(node(pick, rows), ["cena neznámá u 1 z 1 dotazů", "cena neznámá u 3 z 3 dotazů", "cena neznámá u 2 z 5 dotazů"])      # česky beze změny
        self.assertEqual(node(pick, rows, lang="en"), ["price unknown for 1 of 1 request", "price unknown for 3 of 3 requests", "price unknown for 2 of 5 requests"])
        old = [{"cells": {"note": f"cena neznámá u {m} z {n} dotazů"}} for m, n in ((1, 1), (3, 3))]                                    # řádek uložený před změnou: cesta přes unfill
        self.assertEqual(node(pick, old, lang="en"), ["price unknown for 1 of 1 request", "price unknown for 3 of 3 requests"])

    def test_step_note(self):
        notes = [{"k": "result", "v": {"n": 5, "acc": 1, "avg": 0.04}}, {"k": "abortStop", "v": {}}, "přerušeno tlačítkem Stop",
                 "N 5 · správně 100 % · průměr 0.0 ms", "Ollama: spojení odmítnuto", "", None, {"k": "takový není"}, 7]
        text = "arg.map(n => stepNoteText(cleanStepNote(n)))"
        self.assertEqual(node(text, notes), ["N 5 · správně 100 % · průměr 0.0 ms", "přerušeno tlačítkem Stop", "přerušeno tlačítkem Stop",
                                             "N 5 · správně 100 % · průměr 0.0 ms", "Ollama: spojení odmítnuto", "", "", "", ""])
        self.assertEqual(node(text, notes, lang="en"), ["N 5 · correct 100% · mean 0.0 ms", "interrupted by the Stop button",
                                                        "interrupted by the Stop button", "N 5 · correct 100% · mean 0.0 ms",
                                                        "Ollama: spojení odmítnuto", "", "", "", ""])

    def test_page_stores_values_not_texts(self):
        code = script()
        for gone in (".cells.n", "summary: stepSummary", "summary: st.summary", "driverLabel", "data: { cells,"):
            self.assertEqual(code.count(gone), 0, gone)                                                 # count, ne assertNotIn: ten při selhání vypíše celou stránku
        for part in ("data: { vals,", "cellsOf(rec.data)", "stepNoteText(st.note)", "stepLine(st)"):
            self.assertGreaterEqual(code.count(part), 1, part)


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class StorageNote(unittest.TestCase):
    """Hláška pod tabulkou výsledků po přepnutí jazyka: větu, kterou skládá stránka sama, složí znovu; věta ze serveru a hláška prohlížeče zůstávají."""

    def test_note_is_composed_again_in_the_page_language(self):
        parts = [piece(r"^let dbError = [^\n]*$"), piece(r"^let settingsRead = [^\n]*$"), piece(r"^let initDone = [^\n]*$"),
                 piece(r"^function renderDbNote\(\) \{.*?^\}$"), piece(r"^const dbFail = [^\n]*$"), piece(r"^const dbOk = [^\n]*$"),
                 piece(r"^const say = [^\n]*$"), piece(r"^const apiError = [^\n]*$"), piece(r"^async function api\(.*?^\}$")]
        self.assertEqual(parts[3].count("say(dbError)"), 1)
        # dovětek „jen na stránce“ jde do dbFail jako funkce, která větu složí, ne jako hotová věta: výrazy jsou vzaté ze stránky
        table = piece(r"dbFail\(\(\) => t\('results\.onlyOnPage', [^\n]*?\}\)\)")
        row = piece(r"dbFail\(\(\) => t\('results\.rowOnlyOnPage', [^\n]*?\}\)\)")
        self.assertEqual(script().count("dbFail(new Error(t("), 0)
        got = node_run("""
let RESULTS = [], answer = null;
const note = { textContent: '' }, $ = () => note;
const req = async () => { if (answer instanceof Error) throw answer; return answer; };
%s
const texts = () => ['cs', 'en', 'cs'].map(code => { LANG = code; renderDbNote(); return note.textContent; });      // přepnutí jazyka: renderLang volá renderDbNote
const failed = reply => { answer = reply; LANG = 'cs'; return api('DELETE', '/results').then(() => null, e => e); };  // chyba vznikne na české stránce
const reply = (status, body) => ({ ok: status < 400, status, json: async () => body });
(async () => {
  const out = {};
  let e = await failed(reply(500, {})); dbFail(e); out.own = texts(); out.status = e.status;
  e = await failed(reply(500, { error: 'věta ze serveru' })); dbFail(e); out.server = texts();
  e = await failed(Object.assign(new Error('zrušeno'), { name: 'AbortError' })); dbFail(e); out.timeout = texts();
  e = await failed(new TypeError('hláška prohlížeče')); dbFail(e); out.browser = texts();
  e = await failed(reply(500, {})); %s; out.table = texts();
  e = await failed(Object.assign(new Error('zrušeno'), { name: 'AbortError' })); %s; out.row = texts();
  e = await failed(reply(500, { error: 'věta ze serveru.' })); %s; out.rowServer = texts();
  dbOk(); out.ok = texts();
  process.stdout.write(JSON.stringify(out));
})();
""" % ("\n".join(parts), table, row, row))
        both = lambda cs, en: [cs, en, cs]
        self.assertEqual(got["own"], both("Ukládání nefunguje — server odpověděl 500", "Saving does not work — the server answered 500"))
        self.assertEqual(got["status"], 500)                                  # podle stavu se pozná řádek, který už v databázi není (404)
        self.assertEqual(got["server"], both("Ukládání nefunguje — věta ze serveru", "Saving does not work — věta ze serveru"))
        self.assertEqual(got["timeout"], both("Ukládání nefunguje — server neodpověděl do 5 s (/results)",
                                              "Saving does not work — the server did not answer within 5 s (/results)"))
        self.assertEqual(got["browser"], both("Ukládání nefunguje — hláška prohlížeče", "Saving does not work — hláška prohlížeče"))
        self.assertEqual(got["table"], both("Ukládání nefunguje — server odpověděl 500 Tabulka je smazaná jen na stránce.",
                                            "Saving does not work — the server answered 500 The results were deleted only on the page."))
        self.assertEqual(got["row"], both("Ukládání nefunguje — server neodpověděl do 5 s (/results) Výsledek je smazaný jen na stránce.",
                                          "Saving does not work — the server did not answer within 5 s (/results) The result was deleted only on the page."))
        self.assertEqual(got["rowServer"], both("Ukládání nefunguje — věta ze serveru. Výsledek je smazaný jen na stránce.",
                                                "Saving does not work — věta ze serveru. The result was deleted only on the page."))
        self.assertEqual(got["ok"], ["", "", ""])


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Scheduler(unittest.TestCase):
    """Plánovač: souhrn kroku a hlášky fronty se skládají v jazyce stránky a po přepnutí jazyka znovu; česky znějí jako před překladem."""

    def test_step_summary_reads_in_both_languages(self):
        parts = [piece(r"^const DEFAULTS = \{.*?\};$"), piece(r"^const deadlineOf = .*?^\};$"), piece(r"^function stepSummary\(.*?^\}$"),
                 piece(r"^const stepLine = .*?;$")]
        steps = [
            {"kind": "bench", "settings": {"driver": "model", "model": "tev1", "scenario": "obstacles", "lanes": 3, "benchn": 30, "deadline": 50,
                                           "extra": True, "mirror": True, "inject": True, "seed": "abc"}},
            {"kind": "bench", "settings": {"driver": "oracle", "scenario": "numbers", "lanes": 4, "benchn": 5}},
            {"kind": "game", "settings": {"driver": "random", "scenario": "sada.json", "lanes": 5, "mode": "realtime", "speed": 2, "runlen": 8, "mirror": True}},
            {"kind": "game", "settings": {"driver": "model", "model": "", "mode": "turn", "runlen": 12, "mirror": True}},
            {"kind": "game", "settings": {"driver": "model", "model": "test:echo", "mode": "ladder"}, "est": {"requests": 31, "usd": 0.00123}},
            {"kind": "bench", "settings": {"driver": "oracle", "scenario": "smazana.json", "lanes": 1, "benchn": 1}}]
        cs, en = node_run("""
const OPTIONS = { driver: [['model', 'opt.driver.model'], ['oracle', 'driver.oracle'], ['random', 'driver.random']],
                  scenario: [['obstacles', 'opt.scenario.obstacles'], ['numbers', 'opt.scenario.numbers'], ['sada.json', null]] };
const $ = id => ({ options: OPTIONS[id].map(([value, key]) => ({ value, text: key ? t(key) : 'Sada: Firemní e-maily' })) });      // volby nese stránka v jazyce stránky; název sady se nepřekládá
const hostedOf = name => name === 'test:echo' ? { who: 'zkušební poskytovatel' } : null;
%s
const steps = %s;
process.stdout.write(JSON.stringify(['cs', 'en'].map(code => { LANG = code; return steps.map(stepLine); })));
""" % ("\n".join(parts), json.dumps(steps)))
        self.assertEqual(cs, ["tev1 · Překážky · 3 pruhů · benchmark 30 úloh, limit 50 ms · semínko abc · 2. otázka · zrcadla · trojský kůň",
                              "Pravidla (ideální řidič) · Číselné duely · 4 pruhů · benchmark 5 úloh · bez semínka",
                              "Náhodně · Sada: Firemní e-maily · 5 pruhů · reálný čas 2 polí/s, 8 polí · bez semínka",      # zrcadla jen s modelem, po tazích nebo v benchmarku
                              "model nevybrán · Překážky · 3 pruhů · po tazích, 12 polí · bez semínka · zrcadla",
                              "test:echo (zkušební poskytovatel) · Překážky · 3 pruhů · žebřík · bez semínka · odhad nejvýš 31 dotazů, 0.0012 $",      # odhad počítá dotazy do modelu
                              "Pravidla (ideální řidič) · smazana.json · 1 pruhů · benchmark 1 úloh · bez semínka"])
        self.assertEqual(en, ["tev1 · Obstacles · 3 lanes · benchmark of 30 tasks, deadline 50 ms · seed abc · 2nd question · mirrors · Trojan horse",
                              "Rules (ideal driver) · Number duels · 4 lanes · benchmark of 5 tasks · no seed",
                              "Random · Sada: Firemní e-maily · 5 lanes · real time at 2 cells/s, 8 cells · no seed",
                              "no model selected · Obstacles · 3 lanes · turn-based, 12 cells · no seed · mirrors",
                              "test:echo (zkušební poskytovatel) · Obstacles · 3 lanes · ladder · no seed · estimate: at most 31 requests, $0.0012",
                              "Rules (ideal driver) · smazana.json · 1 lane · benchmark of 1 task · no seed"])

    def test_queue_messages_are_composed_again_in_the_page_language(self):
        parts = [piece(r"^const say = [^\n]*$"), piece(r"^let statusMsg = [^\n]*$"), piece(r"^const status = [^\n]*$"), piece(r"^const sqSay = [^\n]*$"),
                 piece(r"^function fail\(message\) [^\n]*$"), piece(r"^async function hostedGap\(.*?^\}$"), piece(r"^async function coolDown\(.*?^\}$"),
                 piece(r"^async function overBudget\(.*?^\}$"), piece(r"^async function holdScreen\(.*?^\}$")]
        got = node_run("""
const els = {}, $ = id => els[id] ??= { textContent: '' };
const performance = { now: () => 0 }, sleep = async () => { planStop = true; };      // čas stojí; pauza skončí po prvním řádku
const HW_GIVE_UP = 3;
let planStop = false, runError = '', runWhy = '', SPEND = null, HOSTED_GAP = 0, wakeLock = null, panel = {}, gpu = {}, spend = null, navigator = {};
const readPanel = () => panel, gpuNow = async () => gpu, hostedOf = () => ({});
const api = async () => { if (spend instanceof Error) throw spend; return spend; };
%s
const read = msg => ['cs', 'en', 'cs'].map(code => { LANG = code; return say(msg); });      // přepnutí jazyka: renderLang volá say(sqMsg) a say(statusMsg)
const at = { batch: 3, step: 1, total: 4, left: 2 };       // proměnné popisku kroku, jak je předává runPlan
(async () => {
  const out = {};
  LANG = 'cs'; await hostedGap(at); out.hosted = read(sqMsg);
  const pause = async (p, g) => { panel = p; gpu = g; planStop = false; sqSay(''); LANG = 'cs'; const note = await coolDown(at); return [read(sqMsg), note]; };
  out.time = await pause({ pauseS: 5, pauseTemp: '', pauseCap: 10 }, {});
  out.temp = await pause({ pauseS: 0, pauseTemp: 60, pauseCap: 1 }, { available: true, temp_c: 71 });
  out.both = await pause({ pauseS: 5, pauseTemp: 60, pauseCap: 1 }, { available: false });
  out.hot = await pause({ pauseS: 0, pauseTemp: 60, pauseCap: 0 }, { available: true, temp_c: 71 });
  const paid = est => [{ settings: { driver: 'model', model: 'test:echo' }, ...(est ? { est } : {}) }];
  const over = async (steps, reply) => { spend = reply; LANG = 'cs'; return read(await overBudget(steps)); };
  out.noEstimate = await over(paid(null), null);
  out.noSpend = await over(paid({ requests: 31, usd: 0.5 }), new Error('věta ze serveru'));
  out.noSpendOwn = await over(paid({ requests: 31, usd: 0.5 }), Object.assign(new Error('x'), { say: () => t('db.status', { status: 500 }) }));
  out.cap = await over(paid({ requests: 31, usd: 0.5 }), { requests: 1, usd: 0.25, max_requests: 30, max_usd: 2 });
  out.fits = await over(paid({ requests: 3, usd: 0.5 }), { requests: 1, usd: 0.25, max_requests: 30, max_usd: 2 });
  out.free = await over([{ settings: { driver: 'oracle' } }], null);
  const screen = async lock => { navigator = lock; LANG = 'cs'; return read(await holdScreen()); };
  out.noLock = await screen({});
  out.refused = await screen({ wakeLock: { request: async () => { throw new Error('hláška prohlížeče'); } } });
  out.held = await screen({ wakeLock: { request: async () => ({}) } });
  LANG = 'cs'; fail('věta chyby'); out.fail = [read(statusMsg), runError, runWhy];
  process.stdout.write(JSON.stringify(out));
})();
""" % "\n".join(parts))
        both = lambda cs, en: [cs, en, cs]
        cs, en = "Dávka 3, krok 1 z 4 (po něm čeká 2)", "Batch 3, step 1 of 4 (2 waiting after it)"
        self.assertEqual(got["hosted"], both(cs + " — hostovaný model: bez pauzy na vychladnutí", en + " — hosted model: no cool-down pause"))
        self.assertEqual(got["time"], [both(cs + " — pauza: zbývá 5 s", en + " — pause: 5 s left"), ""])
        self.assertEqual(got["temp"], [both(cs + " — pauza: GPU 71 °C, čeká se pod 60 °C (nejdéle ještě 60 s)",
                                            en + " — pause: GPU 71 °C, waiting until it is below 60 °C (at most another 60 s)"), ""])
        self.assertEqual(got["both"], [both(cs + " — pauza: zbývá 5 s · GPU ? °C, čeká se pod 60 °C (nejdéle ještě 60 s)",
                                            en + " — pause: 5 s left · GPU ? °C, waiting until it is below 60 °C (at most another 60 s)"), ""])
        self.assertEqual(got["hot"], [["", "", ""], ["hot", {"temp": 71, "target": 60}]])      # poznámka výsledku zůstává strukturou, ne větou
        self.assertEqual(got["noEstimate"], both("Fronta se nespustila: krok s hostovaným modelem nemá odhad ceny — odeberte ho a přidejte znovu.",
                                                 "The queue did not start: a step with a hosted model has no price estimate — remove it and add it again."))
        self.assertEqual(got["noSpend"], both("Fronta se nespustila: stav útraty se nepodařilo načíst (věta ze serveru).",
                                              "The queue did not start: the page could not load the current spend (věta ze serveru)."))      # věta ze serveru zůstává, jak přišla
        self.assertEqual(got["noSpendOwn"], both("Fronta se nespustila: stav útraty se nepodařilo načíst (server odpověděl 500).",
                                                 "The queue did not start: the page could not load the current spend (the server answered 500)."))
        self.assertEqual(got["cap"], both(
            "Fronta se nespustila: odhad hostovaných kroků (31 dotazů, 0.5000 $) spolu s dosavadní útratou (1 dotazů, 0.2500 $) překračuje strop serveru "
            "(30 dotazů, 2 $). Zkraťte kroky, nebo server spusťte s vyšším PICKBENCH_MAX_HOSTED a PICKBENCH_MAX_USD.",
            "The queue did not start: the estimate for the hosted steps (31 requests, $0.5000) together with the spend so far (1 request, $0.2500) exceeds "
            "the server's spend cap (30 requests, $2). Shorten the steps, or start the server with a higher PICKBENCH_MAX_HOSTED and PICKBENCH_MAX_USD."))
        self.assertEqual((got["fits"], got["free"]), (["", "", ""], ["", "", ""]))
        self.assertEqual(got["noLock"], both("Obrazovku se nepodařilo udržet zapnutou (prohlížeč to neumí) — vypněte zhasínání obrazovky a uspávání ručně.",
                                             "The page could not keep the screen on (the browser does not support it) — turn off the screen timeout and sleep mode yourself."))
        self.assertEqual(got["refused"], both("Obrazovku se nepodařilo udržet zapnutou (hláška prohlížeče) — vypněte zhasínání obrazovky a uspávání ručně.",
                                              "The page could not keep the screen on (hláška prohlížeče) — turn off the screen timeout and sleep mode yourself."))
        self.assertEqual(got["held"], ["", "", ""])
        self.assertEqual(got["fail"], [both("Chyba: věta chyby", "Error: věta chyby"), "věta chyby", "error"])      # věta chyby jde do kroku fronty bez úvodu


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class SwitchInPlace(unittest.TestCase):
    """Přepnutí jazyka na místě: co stránka složila z pevné množiny vět (stav běhu, volby sad a hostovaných modelů, panel Hardware),
    složí po přepnutí znovu; věta ze serveru zůstává a nic se nečte z proměnných, které mezitím změnil další běh."""

    def test_run_status_is_composed_again_and_stored_error_keeps_its_language(self):
        parts = [piece(r"^const say = [^\n]*$"), piece(r"^let statusMsg = [^\n]*$"), piece(r"^const status = [^\n]*$"),
                 piece(r"^function fail\(message\) [^\n]*$"), piece(r"^const LADDER = [^\n]*$"), piece(r"^function ladderStatus\(\) \{.*?^\}$")]
        got = node_run("""
const els = {}, $ = id => els[id] ??= { textContent: '' };
let runError = '', runWhy = '', ladder = null;
%s
const read = () => ['cs', 'en', 'cs'].map(code => { LANG = code; return say(statusMsg); });      // přepnutí jazyka: renderLang volá say(statusMsg)
const out = {};
LANG = 'cs'; fail(() => t('public.benchOnly')); out.own = [read(), runError, runWhy, $('status').textContent];
LANG = 'en'; fail(() => t('hosted.consentNeeded')); out.ownEn = [read(), runError];
LANG = 'cs'; fail('Ollama 500: věta ze serveru'); out.server = [read(), runError];
const labels = null;                                       // veřejná sada, kde má každá úloha vlastní nabídku
LANG = 'cs'; fail(() => t('run.didNotFit', { labels: labels ?? t('cell.varies'), error: 'Ollama 400: prompt too long' })); out.fit = [read(), runError];
LANG = 'cs'; ladder = { i: 2 }; ladderStatus(); ladder = null; out.level = read();      // hodnoty z chvíle volání: další běh žebřík zruší
process.stdout.write(JSON.stringify(out));
""" % "\n".join(parts))
        texts = node("TEXTS")
        both = lambda cs, en: [cs, en, cs]
        own = texts["public.benchOnly"]
        self.assertEqual(got["own"], [both("Chyba: " + own["cs"], "Error: " + own["en"]), own["cs"], "error", "Chyba: " + own["cs"]])
        need = texts["hosted.consentNeeded"]
        self.assertEqual(need["cs"], "hostovaný model není odsouhlasen — vyberte ho ve voliči Model a potvrďte varování pod tlačítky")
        self.assertEqual(got["ownEn"], [both("Chyba: " + need["cs"], "Error: " + need["en"]), need["en"]])      # do kroku fronty jde věta v jazyce, ve kterém vznikla
        self.assertEqual(got["server"], [both("Chyba: Ollama 500: věta ze serveru", "Error: Ollama 500: věta ze serveru"), "Ollama 500: věta ze serveru"])
        cs = "nevešlo se — server odmítl dotaz s nabídkou různě štítků (Ollama 400: prompt too long)"
        self.assertEqual(got["fit"][0][0], "Chyba: " + cs)
        self.assertEqual(got["fit"][1], cs)
        self.assertTrue(got["fit"][0][1].startswith("Error: did not fit — "), got["fit"][0][1])
        self.assertIn(": varies (Ollama 400: prompt too long)", got["fit"][0][1])                       # i slovo za počet štítků je v jazyce stránky
        self.assertEqual(got["level"], both("Žebřík: úroveň 3 z 12, 1.6 polí/s", "Ladder: level 3 of 12, 1.6 cells/s"))

    def test_options_made_from_server_lists_are_composed_again(self):
        parts = [piece(r"^const SET_OPTIONS = [^\n]*$"), piece(r"^const setOptionText = [^\n]*$"), piece(r"^const hostedOptionText = [^\n]*$"),
                 piece(r"^function renderOptions\(\) \{.*?^\}$")]
        got = node_run("""
const option = (value, kind, name) => ({ value, textContent: 'text z HTML', dataset: kind ? { kind, name } : {} });
const scenario = { options: [option('obstacles'), option('a.json', 'emails', 'Firemní e-maily'), option('public/b.json', 'public', 'MASSIVE'),
                             option('', 'none', ''), option('', 'failed', '')] };
const HOSTED = { 'test:echo': { name: 'test:echo', who: 'zkušební poskytovatel', usd_per_m: 0.1 } };
const group = { label: '', children: [{ value: 'test:echo', textContent: '' }] };
const model = { querySelectorAll: what => what === 'optgroup' ? [group] : [] };
const $ = id => ({ scenario, model })[id];
%s
const read = () => ['cs', 'en', 'cs'].map(code => { LANG = code; renderOptions(); return [...scenario.options.map(o => o.textContent), group.label, group.children[0].textContent]; });
const out = { first: read() };
HOSTED['test:echo'].who = 'test provider';                 // jméno poskytovatele ze serveru se smí po přepnutí změnit: text volby se bere ze záznamu modelu
out.who = read().map(texts => texts[6]);
process.stdout.write(JSON.stringify(out));
""" % "\n".join(parts))
        cs, en, back = got["first"]
        self.assertEqual(cs, ["text z HTML", "E-maily: Firemní e-maily", "Veřejná sada: MASSIVE", "E-maily (žádná sada ve složce datasets/)",
                              "E-maily (seznam sad se nepodařilo načíst)", "Hostované — placené, texty úloh odcházejí k poskytovateli",
                              "test:echo — zkušební poskytovatel, 0.1 $ za milion tokenů"])
        self.assertEqual(back, cs)
        texts = node("TEXTS")
        self.assertEqual(en, ["text z HTML", "E-mails: Firemní e-maily", "Public dataset: MASSIVE", texts["opt.scenario.noEmails"]["en"],
                              texts["opt.scenario.listFailed"]["en"], texts["hosted.group"]["en"], "test:echo — zkušební poskytovatel, $0.1 per million tokens"])
        self.assertEqual(got["who"], ["test:echo — test provider, 0.1 $ za milion tokenů", "test:echo — test provider, $0.1 per million tokens",
                                      "test:echo — test provider, 0.1 $ za milion tokenů"])

    def test_provider_description_is_asked_again_after_a_language_switch(self):
        # Popis poskytovatele (who) skládá server v jazyce dotazu a stránka si ho pamatuje od načtení: po přepnutí jazyka se na /models zeptá znovu
        # a přepíše jen pole who modelů, které už nabízí. Nabídka, výběr ani souhlas se nemění; když dotaz selže, texty zůstanou.
        parts = [piece(r"^function setLang\(code\) \{.*?^\}$"), piece(r"^async function refreshWho\(\) \{.*?^\}$")]
        got = node_run("""
let running = false, planRunning = false, initDone = true;
const calls = [], pending = [], localStorage = { setItem() {} }, modelsReady = Promise.resolve();
let HOSTED = { 'test:echo': { name: 'test:echo', provider: 'test', who: 'česky', usd_per_m: 0.1 }, 'jev:x': { name: 'jev:x', provider: 'jev', who: 'TypeSafe AI', usd_per_m: 1 } };
const hostedOf = name => typeof name === 'string' && Object.hasOwn(HOSTED, name) ? HOSTED[name] : null;
const who = () => HOSTED['test:echo'] && HOSTED['test:echo'].who;
const renderLang = () => calls.push('lang'), renderOptions = () => calls.push('options: ' + who());
const renderHosted = () => calls.push('hosted: ' + who()), renderPlan = () => calls.push('plan: ' + who());
const req = path => { calls.push(path + ' ' + LANG); return new Promise((resolve, reject) => pending.push({ resolve, reject })); };
const reply = body => ({ json: async () => body });
const tick = () => new Promise(done => setTimeout(done, 0));
const take = () => calls.splice(0);
const answer = async body => { pending.shift().resolve(reply(body)); await tick(); };
%s
(async () => {
  const out = {};
  setLang('en'); await tick(); out.asked = take();
  await answer({ models: ['tev1'], hosted: [{ name: 'test:echo', provider: 'jiny', who: 'in English', usd_per_m: 99 }, { name: 'new:model', provider: 'x', who: 'x', usd_per_m: 1 },
                                            null, { name: 'jev:x', who: 5 }, { name: 7, who: 'x' }] });
  out.rewritten = [take(), JSON.parse(JSON.stringify(HOSTED))];
  setLang('cs'); await tick(); pending.shift().reject(new TypeError('network')); await tick();      // dotaz selže
  setLang('en'); await tick(); pending.shift().resolve({ json: async () => { throw new SyntaxError('x'); } }); await tick();      // odpověď není JSON
  setLang('cs'); await tick(); await answer({ error: 'Ollama' });                                   // server hostované modely nevrátil
  out.failed = [take(), who()];
  setLang('en'); await tick(); setLang('cs'); await tick();                                         // další přepnutí dřív, než přišla odpověď
  await answer({ hosted: [{ name: 'test:echo', who: 'late English' }] }); out.stale = [take(), who()];
  await answer({ hosted: [{ name: 'test:echo', who: 'česky znovu' }] }); out.fresh = [take(), who()];
  setLang('en'); setLang('cs'); await tick(); out.quick = take(); await answer({ hosted: [] });     // dvě přepnutí hned po sobě: ptá se jen to poslední
  setLang('en'); await tick(); await answer({ hosted: [{ name: 'test:echo', who: 'česky znovu' }] }); out.same = take();      // stejný text: nic se nekreslí podruhé
  initDone = false; setLang('cs'); await tick(); await answer({ hosted: [{ name: 'test:echo', who: 'early' }] }); out.early = take(); initDone = true;
  running = true; setLang('en'); await tick(); running = false; planRunning = true; setLang('en'); await tick(); planRunning = false;
  out.busy = [take(), LANG, pending.length];
  HOSTED = {}; setLang('en'); await tick(); out.none = [take(), LANG, pending.length];
  process.stdout.write(JSON.stringify(out));
})();
""" % "\n".join(parts))
        self.assertEqual(got["asked"], ["lang", "/models en"])                 # napřed texty stránky, potom dotaz v novém jazyce
        self.assertEqual(got["rewritten"][0], ["options: in English", "hosted: in English", "plan: in English"])      # volba, varování a souhrn kroku fronty
        self.assertEqual(got["rewritten"][1], {"test:echo": {"name": "test:echo", "provider": "test", "who": "in English", "usd_per_m": 0.1},      # jen pole who
                                               "jev:x": {"name": "jev:x", "provider": "jev", "who": "TypeSafe AI", "usd_per_m": 1}})              # a žádný nový model
        self.assertEqual(got["failed"], [["lang", "/models cs", "lang", "/models en", "lang", "/models cs"], "in English"])
        self.assertEqual(got["stale"], [["lang", "/models en", "lang", "/models cs"], "in English"])      # odpověď starého jazyka se zahodí
        self.assertEqual(got["fresh"], [["options: česky znovu", "hosted: česky znovu", "plan: česky znovu"], "česky znovu"])
        self.assertEqual(got["quick"], ["lang", "lang", "/models cs"])
        self.assertEqual(got["same"], ["lang", "/models en"])
        self.assertEqual(got["early"], ["lang", "/models cs", "options: early", "hosted: early"])         # fronta se před koncem načítání nekreslí
        self.assertEqual(got["busy"], [[], "cs", 0])                           # za běhu se nepřepíná a server se neptá
        self.assertEqual(got["none"], [["lang"], "en", 0])                     # bez hostovaných modelů není co přepisovat

    def test_switch_takes_the_language_out_of_the_address_and_keeps_the_rest(self):
        # Stránka otevřená s ?lang=cs, klik na EN, obnovení: parametr by vyhrál nad uloženou volbou a přepsal ji. Po přepnutí proto z adresy
        # odejde jen parametr lang; ostatní parametry a #hash zůstanou, jak jsou, a bez history.replaceState přepnutí proběhne dál.
        got = node_run("""
let running = false, planRunning = false, location, history;
const localStorage = { setItem() {} }, renderLang = () => {}, refreshWho = () => {};
%s
const click = (search, hash = '', replace = (...args) => { location.replaced = args; }) => {
  location = { pathname: '/', search, hash }; history = { state: { kept: 1 }, replaceState: replace };
  LANG = 'cs'; setLang('en'); return [LANG, location.replaced ?? null];
};
const out = {
  only: click('?lang=cs'), rest: click('?x=1&lang=cs', '#h'), first: click('?lang=EN&x=a%%20b&y'), twice: click('?lang=cs&x=1&lang=en'),
  encoded: click('?l%%61ng=cs'), bare: click('?lang'), none: click('?x=1', '#h'), empty: click(''), other: click('?language=cs&LANG=en&x=lang'),
  refused: click('?lang=cs', '', () => { throw new Error('SecurityError'); }),
};
history = {}; location = { pathname: '/', search: '?lang=cs', hash: '' }; LANG = 'cs'; setLang('en'); out.noReplace = LANG;
history = undefined; LANG = 'cs'; setLang('en'); out.noHistory = LANG;
running = true; out.busy = click('?lang=cs'); running = false;
process.stdout.write(JSON.stringify(out));
""" % piece(r"^function setLang\(code\) \{.*?^\}$"))
        to = lambda address: ["en", [{"kept": 1}, "", address]]
        self.assertEqual(got["only"], to("/"))
        self.assertEqual(got["rest"], to("/?x=1#h"))
        self.assertEqual(got["first"], to("/?x=a%20b&y"))                      # ostatní parametry znak po znaku
        self.assertEqual(got["twice"], to("/?x=1"))
        self.assertEqual((got["encoded"], got["bare"]), (to("/"), to("/")))    # parametr, jak ho čte stránka při načtení (URLSearchParams)
        for name in ("none", "empty", "other", "refused"):                     # bez parametru lang se adresa nemění; odmítnutí prohlížeče přepnutí nezastaví
            self.assertEqual(got[name], ["en", None], name)
        self.assertEqual((got["noReplace"], got["noHistory"]), ("en", "en"))
        self.assertEqual(got["busy"], ["cs", None])                            # za běhu se nepřepíná: adresa zůstává

    def test_provider_warning_is_composed_again_and_a_hidden_one_keeps_no_text(self):
        got = node_run("""
let modelFallback = false, ask = true;
const els = { model: { value: 'test:echo' } }, $ = id => els[id] ??= { textContent: '', hidden: false };
const hostedNow = () => ({ who: 'zkušební poskytovatel', usd_per_m: 0.1 }), needConsent = () => ask;
%s
const read = () => ['cs', 'en', 'cs'].map(code => { LANG = code; renderHosted(); return [$('hostedWarn').hidden, $('hostedText').textContent]; });      // přepnutí jazyka: renderLang → syncRun → renderHosted
const out = { shown: read() };
modelFallback = true; out.fallback = read();
ask = false; out.hidden = read();
process.stdout.write(JSON.stringify(out));
""" % piece(r"^function renderHosted\(\) \{.*?^\}$"))
        texts = node("TEXTS")
        fill = lambda key, code: texts[key][code].replace("{model}", "test:echo").replace("{who}", "zkušební poskytovatel").replace("{price}", "0.1")
        for name, key in (("shown", "hosted.warning"), ("fallback", "hosted.warningFallback")):
            self.assertEqual(got[name], [[False, fill(key, code)] for code in ("cs", "en", "cs")], name)
        self.assertEqual(got["hidden"], [[True, ""]] * 3)                      # schované varování nedrží větu ve starém jazyce

    def test_hardware_panel_is_composed_again(self):
        got = node_run("""
let hosted = null, running = false, runHosted = null, hwOff = false, hwThrottled = false, HW = null;
const els = {}, $ = id => els[id] ??= { textContent: '', replaceChildren(...kids) { this.kids = kids; this.tiles = null; } };
const el = (tag, cls, text) => ({ text });
const hostedNow = () => hosted;
const runEnergy = () => ({ perDecision: 1.234, joules: 43.2, avgPower: 20, maxTemp: 61 });
const renderTiles = (box, tiles) => { box.tiles = tiles; box.kids = null; };
%s
const read = () => ['cs', 'en', 'cs'].map(code => {       // přepnutí jazyka: renderLang → drawStats → renderHw
  LANG = code; renderHw(0);
  const box = $('hw');
  return { labels: box.tiles && box.tiles.map(tile => tile[1]), tips: box.tiles && box.tiles.map(tile => tile[2]), text: box.kids && box.kids[0].text, warn: $('hwWarn').textContent };
});
const out = {};
HW = { available: true, temp_c: 55, power_w: 20.5, clock_mhz: 1800, pstate: 'P0', mem_mb: 4000 }; hwThrottled = true; out.tiles = read();
hwThrottled = false; hwOff = true; out.none = read();
hwOff = false; hosted = { who: 'x' }; out.hosted = read();
process.stdout.write(JSON.stringify(out));
""" % piece(r"^function renderHw\(.*?^\}$"))
        texts = node("TEXTS")
        names = ("hwTemp", "hwPower", "hwClock", "hwPstate", "hwMem", "hwJoule", "hwAvgPower", "hwMaxTemp")
        cs, en, back = got["tiles"]
        self.assertEqual(cs["labels"], ["teplota GPU", "příkon GPU", "takt GPU", "stav (pstate)", "paměť GPU", "J na rozhodnutí (běh celkem 0.012 Wh)",
                                        "průměrný příkon za běh", "nejvyšší teplota za běh"])
        self.assertEqual(cs["warn"], "GPU je přiškrcená (USB-C napájení?) — měření neodpovídá plnému výkonu")
        self.assertEqual(back, cs)
        self.assertEqual(en["labels"], [texts["tile." + name + ".label"]["en"].replace("{wh}", "0.012") for name in names])
        self.assertEqual(en["warn"], texts["hw.throttled"]["en"])
        for code, tiles in (("cs", cs), ("en", en)):                         # každá dlaždice má vedle názvu svůj popisek po najetí
            self.assertEqual(tiles["tips"], [texts["tile." + name][code] for name in names])
        self.assertEqual([x["text"] for x in got["none"]], ["GPU není k dispozici", texts["hw.none"]["en"], "GPU není k dispozici"])
        self.assertEqual([x["text"] for x in got["hosted"]], [texts["hosted.hw"][code] for code in ("cs", "en", "cs")])
        self.assertEqual(texts["hosted.hw"]["cs"], "Model běží u poskytovatele: GPU tohoto počítače se při něm neměří a energie na rozhodnutí není. "
                                                   "Místo ní je v panelu Rychlost rozhodování cena ($ za 1000 rozhodnutí).")
        self.assertEqual([x["warn"] for x in got["none"] + got["hosted"]], [""] * 6)


# ---------- znění angličtiny ----------
# Obraty, které anglický text nesmí nést (nálezy čtenáře, který česky neumí): výraz → proč. Hledají se ve všem, co uživatel anglické verze čte
# (english_texts). Výraz rozlišuje velká a malá písmena, pokud sám neříká jinak.
BANNED = {
    r"\b[Ff]rom a (?:few dozen|small number)\b": "otisk českého „z několika desítek“: anglicky With only a few dozen…",
    r"\bof which\b": "„z toho“ je anglicky of that, …",
    r"\b[Aa]fter how many\b": "tázací vedlejší věta s českým slovosledem",
    r"\blimits it\b": "zájmeno bez opory: limits the table",
    r"\bby hand\b": "ručně je manually nebo yourself",
    r"\bindicative\b": "jedna výhrada má jedno znění: rough estimate",
    r"\bnet time\b": "čte se jako čas sítě; čistý čas Ollamy je time Ollama alone takes",
    r"\bprice of the network\b": "price je cena v dolarech",
    r"\bDNS request\b": "request je dotaz do modelu: DNS lookup",
    r"\bserver cap\b": "strop útraty je spend cap: the server's spend cap",
    r"\bthe limit is\b": "strop útraty je cap: the cap is",
    r"\b[Aa]pprov": "potvrzení hostovaného modelu je confirm",
    r"\b[Dd]uplicat": "tlačítko „kopie“ je copy",
    r"\b[Aa]bort": "přerušeno je interrupted",
    r"\bgraphics core\b": "GPU",
    r"(?i)\blicence|\bcolour": "americký pravopis: license, color",
    r"\bthe demonstrator\b": "stránka si říká the page",
    r"\binput_tokens\b": "počet vstupních tokenů se anglicky píše slovy: input tokens",
    r"\b[Ii]n real time\b|\b[Ii]n the ladder\b|\b[Tt]he (?:ladder|(?:Real time|Ladder|Turn-based)(?: and (?:Ladder|Turn-based))? modes?)\b"
    r"|\b(?:real time|ladder|turn-based) modes?\b":
        "režim ve větě má jednu podobu bez členu: in Real time mode, in Ladder mode, in Turn-based mode, in Real time and Ladder modes",
    r"\b[Ii]n (?:Real time|Ladder|Turn-based)\b(?! modes?\b| and (?:Ladder|Turn-based) modes\b)": "za názvem režimu ve větě stojí slovo mode",
}
# „V reálném čase“ znamená v kódu volbu Reálný čas i Žebřík (isRealtime: režim není po tazích): anglicky in Real time and Ladder modes.
# Samotné in Real time mode smí stát jen tam, kde věta platí pouze pro volbu Reálný čas: klíč → důvod.
REAL_TIME_ONLY = {"tip.speed": "posuvník rychlosti platí jen pro volbu Reálný čas; v žebříku rychlost řídí hra a posuvník je zamčený"}
# Kde anglický text smí nést slovo card: zdroj → důvod. Všude jinde by card byla grafická karta, a té se říká GPU.
CARD_OK = {"index.html scenario.emails.about": "karta e-mailu vedle silnice",
           "i18n.py fetch.about.cfpb": "karta kopie databáze na Hugging Face (dataset card)",
           "datasets/cfpb-complaints-300.json": "tentýž popis, jak ho fetch_datasets.py zapsal do přibalené sady",
           "README.md": "karta kopie databáze CFPB na Hugging Face (its card)"}
# Přibalené sady a zkušební sady testů: jejich anglický popis (about_en) čte divák anglické stránky.
SHIPPED_SETS = ("datasets/cfpb-complaints-300.json", "datasets/cz-emaily-300.json", "datasets/cz-firemni-emaily-kos.json", "datasets/cz-firemni-emaily.json",
                "tests/public-mini.json", "tests/public-options.json")


def english_texts():
    """Všechen anglický text, který uživatel anglické verze čte: {zdroj: text}. Stránka a i18n.TEXTS po klíčích, nápověda tří nástrojů,
    popisy přibalených sad a README.md."""
    import bench
    import fetch_datasets
    import hwbench
    out = {f"index.html {key}": entry["en"] for key, entry in node("TEXTS").items()}
    out.update({f"i18n.py {key}": entry["en"] for key, entry in i18n.TEXTS.items()})
    out.update({f"{module.__name__}.py USAGE_EN": module.USAGE_EN for module in (bench, hwbench, fetch_datasets)})
    out.update({name: json.loads((ROOT / name).read_text(encoding="utf-8-sig"))["about_en"] for name in SHIPPED_SETS})
    out["README.md"] = (ROOT / "README.md").read_text(encoding="utf-8")
    return out


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Wording(unittest.TestCase):
    """Angličtina jako celek: zakázané obraty a jedna věc pod jedním slovem (strop útraty, režimy, počet úloh benchmarku, GPU)."""

    @classmethod
    def setUpClass(cls):
        cls.texts = english_texts()
        cls.page = node("TEXTS")

    def test_no_banned_phrase_is_left(self):
        found = sorted((source, hit.group(0)) for source, text in self.texts.items() for pattern in BANNED for hit in re.finditer(pattern, text))
        self.assertEqual(found, [])
        self.assertTrue(all(reason.strip() for reason in BANNED.values()))

    def test_numbers_are_the_same_in_both_languages(self):                    # „delší než 60 s“ nesmí být anglicky 90 s; desetinná čárka a tečka jsou totéž
        numbers = lambda text: sorted(n.replace(",", ".") for n in re.findall(r"\d+(?:[.,]\d+)?", text))
        differ = [f"{source} {key}" for source, texts in (("index.html", self.page), ("i18n.py", i18n.TEXTS))
                  for key, entry in texts.items() if numbers(entry["cs"]) != numbers(entry["en"])]
        self.assertEqual(differ, [])

    def test_card_means_the_gpu_nowhere(self):
        self.assertEqual(sorted(source for source, text in self.texts.items() if re.search(r"(?i)\bcards?\b", text)), sorted(CARD_OK))

    def test_modes_named_in_a_sentence_carry_the_name_of_their_option(self):  # názvy voleb: Real time, Turn-based, Ladder; ve větě za nimi slovo mode
        en = lambda key: self.page[key]["en"]
        self.assertEqual([en("opt.mode." + mode).split(" (")[0] for mode in ("realtime", "turn", "ladder")], ["Real time", "Turn-based", "Ladder"])
        for key, part in (("tip.speed", "in Real time mode"), ("tip.speed", "In Ladder mode"), ("tip.mirror", "in Turn-based mode"), ("ui.mirror", "in Turn-based mode"),
                          ("row.tip.late", "in Turn-based mode"), ("col.goodput.tip", "in Real time and Ladder modes"), ("plan.add.needsEnd", " or Ladder mode "),
                          ("plan.err.noEnd", " nor Ladder mode "), ("tip.sqKind", " or Ladder mode.")):
            self.assertIn(part, en(key), key)

    def test_real_time_covers_the_ladder_where_the_code_does(self):           # věta, která platí i v žebříku, jmenuje oba režimy; česky stačí „v reálném čase“
        en = lambda key: self.page[key]["en"]
        self.assertEqual(script().count("const isRealtime = () => $('mode').value !== 'turn';"), 1)      # reálný čas i žebřík
        for key, part in (("row.tip.rate", "how many rows arrive per second; in Real time and Ladder modes only."),      # dlaždice ukazuje „požadováno“ i v žebříku
                          ("tile.goodput", "in the game in Real time and Ladder modes, the deadline is"),
                          ("scenario.obstacles.tip.crashes", "In Real time and Ladder modes a slow decision can cause a crash too"),
                          ("row.tip.late", "In Real time and Ladder modes the page asks the model about the nearest row"),
                          ("tip.seed", "In Real time and Ladder modes, which rows the page manages to ask the model about still varies"),
                          ("tip.model", "in the game in Real time and Ladder modes"),
                          ("tip.mP95", "which in Real time and Ladder modes decide"),
                          ("col.goodput.tip", "In the game in Real time and Ladder modes, the deadline is"),
                          ("scenario.obstacles.tip.blind", "In Real time and Ladder modes only.")):
            self.assertIn(part, en(key), key)
            self.assertIn("v reálném čase", self.page[key]["cs"].lower(), key)      # česká věta se nemění
        alone = {source for source, text in self.texts.items() if re.search(r"\bReal time mode\b", text)}
        self.assertEqual(alone, {"index.html " + key for key in REAL_TIME_ONLY})
        self.assertTrue(all(reason.strip() for reason in REAL_TIME_ONLY.values()))

    def test_banned_list_catches_every_wrong_form_of_a_mode_name(self):       # člen před názvem režimu u všech tří, malá písmena, název bez slova mode
        hit = lambda text: [pattern for pattern in BANNED if re.search(pattern, text)]
        for wrong in ("in real time", "In the ladder", "the Ladder mode", "the Real time mode", "The Turn-based mode", "the Real time and Ladder modes",
                      "turn-based mode", "real time mode", "in Real time only", "In Ladder the game controls"):
            self.assertTrue(hit(wrong), wrong)
        for right in ("in Real time mode", "In Ladder mode", "in Turn-based mode", "in Real time and Ladder modes", "or Ladder mode —",
                      "Ladder: real time, with the speed", "real time, 2 cells/s", "turn-based, 12 cells"):
            self.assertEqual(hit(right), [], right)

    def test_benchmark_field_counts_tasks_under_one_name(self):               # pole benchmarku bez hry počítá úlohy; se zrcadly jsou na úlohu tři dotazy
        en, cs = lambda key: self.page[key]["en"], lambda key: self.page[key]["cs"]
        for key, part, czech in (("ui.benchn", "number of tasks", "počet úloh"), ("tip.benchn", "this many tasks", "tolik úloh"),
                                 ("tip.sqKind", "as many tasks as the field", "tolik úloh, kolik je v poli"), ("tip.sqAdd", "number of tasks", "počet úloh"),
                                 ("tip.runlen", "its own number of tasks", "svůj počet úloh"), ("tile.goodput", "next to the number of tasks", "vedle počtu úloh")):
            self.assertIn(part, en(key), key)
            self.assertNotIn("request", en(key), key)
            self.assertIn(czech, cs(key), key)                                # česky stejně: pole se jmenuje „počet úloh“ (pokyn uživatele 2026-10-08)
            self.assertNotIn("dotaz", cs(key).lower(), key)
        self.assertEqual(self.page["ui.benchn"], {"cs": "Benchmark bez hry, počet úloh", "en": "Benchmark without the game, number of tasks"})
        self.assertIn("dotazů", cs("step.est"))                               # odhad kroku dál počítá dotazy do modelu (se zrcadly tři na úlohu a jeden zahřívací)
        self.assertIn("{requests|request|requests}", en("step.est"))
        self.assertNotIn("úloh", cs("step.est"))
        asked = sorted(key for key, entry in self.page.items() if "dotaz" in entry["cs"].lower())      # kde čeština stránky dál mluví o dotazech, jde o dotaz do modelu (request)
        self.assertGreaterEqual(len(asked), 15)
        self.assertEqual([key for key in asked if not re.search(r"(?i)\brequests?\b", self.page[key]["en"])], [])
        calls = [["step.sum.bench", {"n": 1}], ["step.sum.benchLimit", {"n": 30, "ms": 50}], ["step.est", {"requests": 1, "usd": "0.0001"}],
                 ["step.est", {"requests": 91, "usd": "0.0012"}]]
        text = "arg.map(([key, vars]) => t(key, vars))"
        self.assertEqual(node(text, calls, lang="en"), ["benchmark of 1 task", "benchmark of 30 tasks, deadline 50 ms",
                                                        " · estimate: at most 1 request, $0.0001", " · estimate: at most 91 requests, $0.0012"])      # odhad počítá dotazy: 30 úloh × 3 + zahřívací
        self.assertEqual(node(text, calls), ["benchmark 1 úloh", "benchmark 30 úloh, limit 50 ms", " · odhad nejvýš 1 dotazů, 0.0001 $",
                                             " · odhad nejvýš 91 dotazů, 0.0012 $"])      # česky se tvar podle počtu nemění

    def test_spend_cap_has_one_name(self):
        for key in ("tip.mSpend", "tip.sqRun", "plan.over.cap", "hosted.warning", "hosted.warningFallback"):
            self.assertRegex(self.page[key]["en"], r"\b[Tt]he server's spend cap\b", key)
        for key in ("prov.cap.requests", "prov.cap.usd", "prov.cap.usdHeld"):   # věta chyby: spend cap — …, the cap is {max}
            self.assertIn(", the cap is ", i18n.TEXTS[key]["en"], key)
            self.assertNotIn("limit", i18n.TEXTS[key]["en"], key)
        for source, text in self.texts.items():                              # samotné cap jen ve větě, která útratu jmenuje ({cap} je proměnná, ne slovo)
            if re.search(r"\bcap\b", re.sub(r"\{[^{}]*\}", "", text)):
                self.assertIn("spend", text, source)

    def test_one_control_keeps_one_word(self):
        en = lambda key: self.page[key]["en"]
        self.assertIn("is not confirmed", en("hosted.consentNeeded"))         # potvrzení hostovaného modelu: confirm, confirmed
        for key in ("hosted.consentNeeded", "plan.add.consent", "hosted.warningFallback"):
            self.assertIn("confirm", en(key), key)
        self.assertTrue(en("step.copyLabel").startswith(en("step.copy") + " "))      # aria-label opakuje text svého tlačítka, jako u skip
        self.assertTrue(en("step.toggleLabel").startswith(en("step.skip") + " "))
        self.assertEqual(en("tile.hwPstate.label"), "performance state (pstate)")    # samotné state je stav kroku fronty

    def test_result_column_headings_start_in_lower_case(self):                # N, ECE a J/decision jsou zkratky a jednotka: velké písmeno bez malého za ním
        heads = {key: entry["en"] for key, entry in self.page.items() if re.fullmatch(r"col\.\w+", key)}
        self.assertGreaterEqual(len(heads), 29)
        self.assertEqual({key: text for key, text in heads.items() if re.match(r"[A-Z][a-z]", text)}, {})

    def test_input_tokens_are_named_in_words(self):                           # česky nese záhlaví hwbench.py jméno pole odpovědi (input_tokens), anglicky slova jako sousední target tokens
        self.assertEqual(i18n.TEXTS["hw.col.inputTokens"], {"cs": "input_tokens", "en": "input tokens"})
        self.assertIn(" the input tokens column ", i18n.TEXTS["hw.note.truncation"]["en"])
        self.assertIn(", input tokens: ", self.page["last.tokens"]["en"])
        self.assertEqual(i18n.TEXTS["bench.col.tokens"]["en"], "tokens")      # sloupec bench.py má 8 znaků: dvě slova se do něj nevejdou (česky také jen „tokenů“)

    def test_help_of_every_tool_opens_with_what_the_tool_does(self):          # první řádek nápovědy je věta se slovesem, ne jmenná fráze
        import bench
        import fetch_datasets
        import hwbench
        self.assertEqual([module.USAGE_EN.split()[0] for module in (bench, hwbench, fetch_datasets)], ["Compares", "Measures", "Downloads"])
        for module in (bench, hwbench):                                       # stránka si říká the page; o GPU se dotazy perou, ne o kartu
            self.assertTrue(module.USAGE_EN.rstrip().endswith("(the requests would compete for the GPU)."), module.__name__)
        self.assertIn(" as the page (index.html).", bench.USAGE_EN)

    def test_dataset_with_a_sixth_category_calls_it_other(self):              # „koš“ je šestá kategorie sady, anglicky "Other"; bin je jen pásmo jistoty v kalibraci
        about = self.texts["datasets/cz-firemni-emaily-kos.json"]
        self.assertTrue(about.startswith('Dataset with an "Other" category: '), about[:60])
        for part in ('a sixth lane, "Other" (letter J;', 'sends such e-mails to "Other"', 'the "Other" row (', 'the "Other" column ('):
            self.assertIn(part, about)
        doc = json.loads((ROOT / "datasets" / "cz-firemni-emaily-kos.json").read_text(encoding="utf-8-sig"))
        self.assertEqual([(c["key"], c["letter"]) for c in doc["categories"]][5], ("other", "J"))
        for source, text in self.texts.items():
            if re.search(r"(?i)\bbins?\b", text):
                self.assertIn("confidence", text, source)

    def test_readme_calls_the_page_the_page(self):                            # the game je jen běh s autem; stránka, která ukládá, ptá se a posílá dotazy, je the page
        readme = self.texts["README.md"]
        for gone in ("the game keeps working", "In the game the", "the game asks", "the game is sending", "show the game in a frame"):
            self.assertNotIn(gone, readme)
        for part in ("the page keeps working without saving", "On the page the downloaded files appear", "the confirmation the page asks for",
                     "Do not run the command-line tools while the page is sending requests to the model.", "show the page in a frame"):
            self.assertIn(part, readme)


class PageStatic(unittest.TestCase):
    def test_switch_is_in_the_header_and_language_goes_with_every_request(self):
        html = page()
        for part in ('id="langSwitch"', 'id="lang-cs"', 'id="lang-en"', "'X-PickBench-Lang': LANG"):
            self.assertIn(part, html)
        self.assertEqual(len(re.findall(r"\bfetch\(", script())), 1)          # jen uvnitř req: jinak by dotaz šel bez jazyka

    def test_language_switch_asks_the_server_for_the_provider_description_again(self):
        body = piece(r"^function setLang\(code\) \{.*?^\}$")
        self.assertLess(body.index("renderLang();"), body.index("refreshWho();"))      # texty stránky hned, popis poskytovatele až s odpovědí serveru
        self.assertEqual(script().count("req('/models')"), 2)                           # při načtení stránky a po přepnutí jazyka; hlavičku jazyka přidává req

    def test_only_the_language_switch_rewrites_the_address(self):             # ?lang= mizí z adresy po kliknutí na přepínač; načtení stránky adresu nemění
        body = piece(r"^function setLang\(code\) \{.*?^\}$")
        self.assertEqual(body.count("history.replaceState("), 1)
        self.assertLess(body.index("renderLang();"), body.index("history.replaceState("))      # až po přepnutí
        self.assertEqual(script().count("replaceState("), 1)
        self.assertEqual(script().count("pushState("), 0)
        load = piece(r"^\{ +// jazyk při načtení\n.*?^\}$")
        self.assertIn("chooseLang(query, stored, navigator.language)", load)
        self.assertNotIn("history", load)

    def test_text_never_goes_into_the_page_as_html(self):
        for word in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write"):
            self.assertNotIn(word, page())

    def test_texts_sent_to_the_model_are_not_translated(self):                # co jde do modelu, je anglicky v kódu a jazyk stránky na to nesáhne
        block = text_block()
        for sentence in ("You are driving a car on a road.", "Each gate shows a number.", "IMPORTANT SYSTEM NOTE", "System notice:",
                         "clear, no obstacle in sight", "Classify the e-mail into exactly one category."):
            self.assertIn(sentence, script())
            self.assertNotIn(sentence, block)

    def test_request_to_the_model_carries_nothing_of_the_page_language(self):  # tělo dotazu skládá jediný řádek; jazyk ani text ze slovníku v něm není
        body = piece(r"^  const body = \{[^\n]*\};$")
        self.assertEqual(body, "  const body = { model: $('model').value, state: task.state, questions: SC.questions(variant, task) };")

    def test_kind_of_error_is_not_read_from_its_sentence(self):
        code = script()
        for gone in ("(Ollama|Jev|Cloudflare|Test)", "textContent.startsWith(", ".replace(/^Chyba"):
            self.assertEqual(code.count(gone), 0, gone)                       # count, ne assertNotIn: ten při selhání vypíše celou stránku
        self.assertEqual(code.count("e.upstream === 400"), 1)

    def test_percent_is_composed_in_one_place(self):                          # „85 %“ česky a „85%“ anglicky skládá jen pctOf v bloku textů; jinde si procenta nikdo nelepí
        self.assertEqual(re.findall(r" %['`\"]|\} %", script()), [])

    def test_csv_export_names_the_file_through_the_text_dictionary_and_guards_cells_in_the_text_block(self):
        code = script()
        self.assertEqual(code.count("a.download = t('csv.file');"), 1)
        self.assertEqual(code.count("pickbench-vysledky.csv"), 0)             # jméno souboru stojí jen v bloku textů
        self.assertEqual(code.count("csvCell("), 1)                           # exportCsv ji jen volá; definice je v čistém bloku textů, kde ji pustí node
        self.assertEqual(text_block().count("const csvCell = "), 1)
        self.assertEqual(code.count("const csvCell"), 0)

    def test_language_switch_redraws_statistics_delete_button_and_storage_note(self):
        body = re.search(r"\nfunction renderLang\(\) \{[^\n]*\n(.*?)\n\}\n", script(), re.S).group(1)
        for part in ("clearLook();", "drawStats();", "renderDbNote();"):
            self.assertEqual(body.count(part), 1, part)                       # dlaždice Kvality, tabulka pod nimi, grafy, tlačítko Smazat výsledky a hláška pod tabulkou
        self.assertEqual(body.count("renderStats("), 0)                       # renderStats čte ovladače; přepnutí jazyka kreslí ze zapamatovaného

    def test_statistics_remember_what_they_drew_and_the_redraw_reads_no_live_control(self):
        code = script()
        body = lambda name: re.search(r"\nfunction %s\([^\n]*\n(.*?)\n\}\n" % name, code, re.S).group(1)
        # kreslení kvůli běhu si zapamatuje, co kreslí: hra nebo benchmark, režim a rychlost auta z ovladačů v tu chvíli
        self.assertIn("statsView = { inGame, realtime: isRealtime(), speed: +$('speed').value };", body("renderStats"))
        self.assertEqual(body("renderStats").count("drawStats();"), 1)
        # překreslení (po přepnutí jazyka) bere jen zapamatované: ani ono, ani dlaždice scénářů nečtou rychlost a režim z ovladačů
        live = ("$('speed')", "$('mode')", "isRealtime(")
        for word in live:
            self.assertEqual(body("drawStats").count(word), 0, word)
        for part in ("scénář: překážky", "scénář: e-maily", "scénář: číselné duely", "scénář: zákazy a výjimky", "scénář: veřejná sada (jen benchmark bez hry)"):
            text = "\n".join(line for _, line in regions()[part])
            for word in live:
                self.assertEqual(text.count(word), 0, (part, word))
        self.assertEqual(body("drawStats").count("const { inGame } = statsView"), 1)
        self.assertEqual(body("drawStats").count("runGoodput(inGame, statsView.realtime)"), 1)
        self.assertGreaterEqual(code.count("statsView.speed"), 2)             # „polí ujetých během 1 rozhodnutí“ a „požadováno / rozhodnuto“

    def test_scheduler_shows_sentences_through_functions_and_stores_no_sentence(self):
        code = "\n".join(line for part in ("plánovač: fronta kroků", "plánovač: běh fronty") for _, line in regions()[part])
        # hláška fronty je prázdná, nebo funkce, která větu složí v jazyce stránky: po přepnutí jazyka ji renderLang složí znovu
        calls = re.findall(r"\bsqSay\(([^\n]{0,13})", code)
        self.assertGreaterEqual(len(calls), 15)
        self.assertEqual(len(calls), code.count("sqSay("))                    # žádné volání neuteklo (ani to, za kterým řádek hned končí)
        self.assertEqual([c for c in calls if not c.startswith(("''", "() => t(", "over)", "crash ? () =>"))], [])
        self.assertEqual(code.count("status(() => t('run.error', { message }))"), 1)                    # úvod „Chyba: “ se překládá, věta chyby zůstává
        self.assertEqual(code.count("say(planWarn)"), 1)                                                # upozornění pod frontou stejně
        # věta chyby kroku vzniká při běhu a ukládá se s krokem jako text; pevné poznámky kroku zůstávají strukturou { k, v }
        self.assertEqual(len(re.findall(r"throw new Error\(t\('plan\.err\.\w+'\)\)", code)), 3)
        for part in ("st.note = { k: planStop ? 'abortQueue' : 'abortStop', v: {} }", "st.note = { k: 'empty', v: {} }", "st.note = resultNote(end.rec)",
                     "step.note = { k: 'abortReload', v: {} }", "st.note = end.error", "st.note = e.message"):
            self.assertEqual(code.count(part), 1, part)
        # stav kroku je klíč: platnost hlídá seznam, text je state.<stav>
        self.assertEqual(code.count("STATES.includes(st.state)"), 1)
        self.assertEqual(code.count("t('state.' + st.state)"), 1)
        # jméno zámku prohlížeče není text: dvě záložky v různých jazycích musí sdílet stejný zámek
        self.assertEqual(code.count("const PLAN_LOCK = 'pickbench-fronta';"), 1)
        self.assertEqual(text_block().count("pickbench-fronta"), 0)

    def test_language_switch_redraws_everything_in_the_given_order(self):     # konečné tělo renderLang: pořadí je závazné, otevřená bublina až po všem, co mění popisky
        body = re.search(r"\nfunction renderLang\(\) \{[^\n]*\n(.*?)\n\}\n", script(), re.S).group(1)
        order = ("document.documentElement.lang = LANG;", "'[data-i18n]'", "'[data-i18n-placeholder]'", "'[data-i18n-empty]'", "'aria-pressed'",
                 "setTip($('langSwitch'), t('lang.tip'));", "renderHead();", "applyTips();", "renderOptions();", "$('speed').oninput?.();", "clearLook();",
                 "say(statusMsg)", "say(sqMsg)", "if (!initDone) return;", "SC.extraTitle", "setTip($('extraTitle'), SC.extraTip);",
                 "$('about').textContent = aboutText();", "SC.benchOnly ? t('public.benchOnly') : t('tip.start')", "queueKey = null;", "if (DS) renderLegend();",
                 "drawStats();", "draw(G);", "renderResults();", "renderPlan();", "renderDbNote();", "syncRun();",
                 "if (tipFor && tipFor.isConnected) showTip(tipFor);")
        for part in order:
            self.assertEqual(body.count(part), 1, part)
        places = [body.index(part) for part in order]
        self.assertEqual(places, sorted(places))
        self.assertEqual(code_part(body.split("\n")[-1]).strip(), order[-1])
        self.assertEqual(script().count("[SC.about, DS && aboutOf(DS)]"), 1)  # popis pod ovládáním skládá jen aboutText(); popis sady bere v jazyce stránky (aboutOf)
        code = "\n".join(code_part(line) for line in script().split("\n"))
        self.assertEqual(re.findall(r"\b(?:DS|PS)\.about\w*", code), [])     # popis sady nečte nikdo přímo: anglická stránka by ukázala český
        self.assertEqual(code.count("aboutOf("), 2)                           # sada e-mailů (aboutText) a veřejná sada (publicSet.about)

    def test_page_load_ends_with_a_redraw_in_the_page_language(self):         # přepnutí jazyka během načítání: co init vykreslil před ním, překreslí renderLang na jeho konci
        body = re.search(r"\nasync function init\(\) \{.*?\n\}\n", script(), re.S).group(0)
        self.assertEqual(body.count("} finally {"), 1)
        tail = [code_part(line).strip() for line in body.split("} finally {")[1].split("\n")]
        tail = [line for line in tail if line and line != "}"]
        self.assertEqual(tail[-1], "renderLang();")                           # poslední příkaz: i po chybě v init, až za registrací posluchačů
        self.assertTrue(tail[0].startswith("initDone = true;"), tail[0])      # renderLang bez initDone by skončil před texty scénáře a statistik
        self.assertIn("addEventListener('change'", tail[-2])                  # výjimka z překreslení nesmí zabránit registraci
        self.assertEqual(body.count("renderLang();"), 1)

    def test_run_and_load_show_sentences_through_functions(self):
        code = script()
        # hláška stavu je prázdná, věta ze serveru nebo z výjimky (text, zůstává, jak přišla), nebo funkce, která větu stránky složí v jazyce stránky
        calls = re.findall(r"\bstatus\(([^\n]{0,24})", code)
        self.assertGreaterEqual(len(calls), 15)
        self.assertEqual(len(calls), code.count("status("))
        allowed = ("''", "() => ", "j.error)", "j.ollama_error)", "e.say || e.message)", "runHosted ? () => ", "best ? () => ")
        self.assertEqual([c for c in calls if not c.startswith(allowed)], [])
        # hláška fronty stejně v celém skriptu, i ta z načtení stránky (fronta běží v jiné záložce)
        says = re.findall(r"\bsqSay\(([^\n]{0,13})", code)
        self.assertEqual(len(says), code.count("sqSay("))
        self.assertEqual([c for c in says if not c.startswith(("''", "() => t(", "over)", "crash ? () =>"))], [])
        # běh končí chybou: větu stránky dostává fail jako funkci, větu ze serveru nebo z výjimky jako text
        # (validátory sad mají vlastní místní fail, proto jen části s během)
        run = "\n".join(line for part in ("plánovač: běh fronty", "jádro: běh") for _, line in regions()[part]).replace("function fail(message)", "")
        fails = re.findall(r"\bfail\(([^\n]{0,20})", run)
        self.assertGreaterEqual(len(fails), 7)
        self.assertEqual([c for c in fails if not c.startswith(("() => t(", "e.message)", "tooLong ? () => t("))], [])
        self.assertEqual(code.count("dbFail(new Error("), 0)                  # věta o chybě načtení stránky jde do hlášky pod tabulkou jako funkce
        self.assertEqual(code.count("dbFail(() => t('run.initFailed', "), 1)
        self.assertEqual(code.count("const first = statusMsg;"), 1)           # init si první hlášku pamatuje jako hlášku (text nebo funkci), ne jako text přečtený ze stránky
        # texty vyhodnocené jednou při načtení nezůstaly: konstanta s větou by po přepnutí jazyka držela starý jazyk
        for gone in ("HOSTED_HW", "CONSENT_NEEDED", "FALLBACK_NOTE", "THROTTLE_TEXT"):
            self.assertEqual(code.count(gone), 0, gone)
        self.assertEqual(re.findall(r"new Option\((?:'[^']|`)", code), [])     # text volby se neskládá při vzniku volby z pevného řetězce

    def test_every_part_of_the_page_is_finished(self):
        self.assertEqual(sorted(set(regions()) - set(DONE)), [])

    def test_finished_parts_keep_no_czech_text(self):
        parts = regions()
        for name in DONE:
            with self.subTest(part=name):
                self.assertIn(name, parts)
                self.assertEqual(czech_left(name), [])


class TourGuard(unittest.TestCase):
    """Prohlídka stránky patří jen na zkušební server s prázdnou databází: puštěná omylem proti serveru s daty (hra uživatele)
    by do něj přidala výsledky a přepsala nastavení. Volba --load proto na server s frontou nebo s výsledky nic neuloží."""

    def load(self, queue, results):
        import page_snapshot
        sent, out = [], io.StringIO()

        def call(port, method, path, body=None):
            sent.append((port, method, path))
            return {"/queue": {"queue": queue}, "/results": {"results": results}}[path] if method == "GET" else {"ok": True}

        with mock.patch.object(page_snapshot, "call", call), mock.patch("sys.stdout", out):
            try:
                page_snapshot.main(["8742", "--load"])
                end = None
            except SystemExit as e:
                end = e.code
        return end, [(method, path) for _, method, path in sent], out.getvalue()

    def test_empty_server_takes_the_tour(self):
        for queue in (None, {}, {"steps": []}, {"tour": "starý zdroják"}):      # nic uloženého, prázdná fronta, zdroják z minulého --load
            with self.subTest(queue=queue):
                end, sent, printed = self.load(queue, [])
                self.assertIsNone(end)
                self.assertEqual(sent[-1], ("POST", "/queue"))
                self.assertEqual(sorted(sent[:-1]), [("GET", "/queue"), ("GET", "/results")])
                self.assertIn("async () => {", printed)                         # zavaděč pro prohlížeč

    def test_server_with_a_queue_or_with_results_is_left_alone(self):
        step = {"kind": "bench", "settings": {}, "state": "wait", "note": ""}
        row = {"id": 1, "ts": "2026-10-08T21:14:03", "data": {"vals": {}}}
        for queue, results, sentence in (
                ({"steps": [step]}, [], "Fronta na serveru není prázdná: prohlídka patří jen na zkušební server s prázdnou databází."),
                (None, [row], "Na serveru jsou uložené výsledky: prohlídka patří jen na zkušební server s prázdnou databází."),
                ({"steps": []}, [row, row], "Na serveru jsou uložené výsledky: prohlídka patří jen na zkušební server s prázdnou databází."),
                ({"steps": [step]}, [row], "Fronta na serveru není prázdná: prohlídka patří jen na zkušební server s prázdnou databází.")):
            with self.subTest(queue=queue, results=len(results)):
                end, sent, printed = self.load(queue, results)
                self.assertEqual(end, sentence)                                 # sys.exit(věta): věta na chybový výstup a nenulový kód
                self.assertEqual([call for call in sent if call[0] != "GET"], [])      # na server se nic neuložilo
                self.assertEqual(printed, "")


class SnapshotFile(unittest.TestCase):
    """Snímek české stránky (--write) nese texty stránky bez textů přepínače jazyka: ty hlídá seznam ADDED, jediné české texty,
    které smějí být proti snímku navíc. Přepsaný snímek se proto od starého liší jen textem, který se změnil záměrně."""

    def tool(self, mode, texts, golden):
        import page_snapshot
        tour = {"lang": "cs", "texts": texts, "data": [], "log": ["done"]}
        sent, out = [], io.StringIO()

        def call(port, method, path, body=None):
            sent.append((method, path))
            return {"results": [{"id": 7, "ts": "2026-10-08T21:14:03", "data": {"pickbench_tour": tour}}]} if method == "GET" else {"ok": True}

        with mock.patch.object(page_snapshot, "call", call), mock.patch.object(page_snapshot, "GOLDEN", golden), mock.patch("sys.stdout", out):
            try:
                page_snapshot.main(["8742", mode])
                end = None
            except SystemExit as e:
                end = e.code
        return end, sent, out.getvalue()

    def test_write_leaves_the_switch_texts_to_the_added_list_and_check_accepts_the_result(self):
        import page_snapshot
        page_texts = ["text\tBenchmark bez hry, počet úloh", "tip\tLimit 50 ms", "asks\t12 abcdef"]
        seen = page_texts + sorted(page_snapshot.ADDED)                         # prohlídka vidí i přepínač jazyka
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "page-cs.json"
            end, sent, _ = self.tool("--write", seen, golden)
            self.assertIsNone(end)
            self.assertIn(("DELETE", "/results/7"), sent)
            written = json.loads(golden.read_text(encoding="utf-8"))
            self.assertEqual(written, sorted(["text\tBenchmark bez hry, počet úloh", "tip\tLimit # ms", "asks\t# abcdef"]))
            self.assertEqual(golden.read_bytes().count(b"\r"), 0)               # konce řádků LF jako ve zlatém souboru
            end, _, printed = self.tool("--check", seen, golden)
            self.assertIsNone(end)
            self.assertIn("česká stránka: shodná se snímkem", printed)
            self.assertIsNone(self.tool("--check", page_texts, golden)[0])      # ADDED smí být navíc, nemusí
            end, _, printed = self.tool("--check", seen + ["text\tNový český text"], golden)
            self.assertEqual(end, "Česká stránka se liší od snímku: chybí 0, navíc 1.")
            self.assertIn("navíc: text\tNový český text", printed)


if __name__ == "__main__":
    unittest.main()
