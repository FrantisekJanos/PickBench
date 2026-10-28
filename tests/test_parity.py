"""Shoda výpočtů, které žijí dvakrát: v Pythonu a v index.html.

Blok mezi značkami „sdíleno s Pythonem“ se z index.html vytáhne a pustí v node nad stejnými daty jako Python.
Před něj jde blok textů (značky „texty“): věty chyb validátoru bere sdílený blok z něj funkcí t(), v jazyce stránky.
Bez programu node se testy přeskočí."""
import itertools
import json
import math
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import i18n
import metrics

BLOCK = re.compile(r"// ==== sdíleno s Pythonem: začátek ====\n(.*?)// ==== sdíleno s Pythonem: konec ====", re.S)
TEXT_BLOCK = re.compile(r"// ==== texty: začátek ====\n(.*?)// ==== texty: konec ====", re.S)      # stejný výraz jako v tests/test_i18n.py
CZECH = re.compile(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„“]")


def shared_js():
    """Blok textů a za ním sdílený blok, jak stojí v index.html."""
    text = (ROOT / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
    parts = []
    for name, block in (("texty", TEXT_BLOCK), ("sdíleno s Pythonem", BLOCK)):
        found = block.findall(text)
        if len(found) != 1:
            raise AssertionError(f"blok „{name}“ má být v index.html právě jednou, je tam {len(found)}×")
        parts.append(found[0])
    return "\n".join(parts)


def run_js(expression, argument, lang="cs"):
    """Spustí blok textů a sdílený blok v node a vrátí hodnotu výrazu; ve výrazu je argument dostupný jako proměnná arg, jazyk stránky je lang."""
    with tempfile.TemporaryDirectory() as tmp:
        script, data = Path(tmp) / "shared.cjs", Path(tmp) / "arg.json"
        script.write_text(shared_js() + f"\nLANG = {json.dumps(lang)};\n"
                          "const arg = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));\n"
                          f"process.stdout.write(JSON.stringify({expression}));\n", encoding="utf-8")
        data.write_text(json.dumps(argument), encoding="utf-8")
        r = subprocess.run(["node", str(script), str(data)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    if r.returncode:
        raise AssertionError("node: " + r.stderr[-800:])
    return json.loads(r.stdout)


def same(test, a, b, where=""):
    """Rekurzivní porovnání; čísla s tolerancí."""
    if isinstance(a, float) or isinstance(b, float):
        test.assertTrue(isinstance(a, (int, float)) and isinstance(b, (int, float)) and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12), f"{where}: {a!r} != {b!r}")
    elif isinstance(a, dict):
        test.assertEqual(sorted(a), sorted(b) if isinstance(b, dict) else b, where)
        for k in a:
            same(test, a[k], b[k], f"{where}.{k}")
    elif isinstance(a, (list, tuple)):
        test.assertEqual(len(a), len(b) if isinstance(b, (list, tuple)) else -1, where)
        for i, (x, y) in enumerate(zip(a, b)):
            same(test, x, y, f"{where}[{i}]")
    else:
        test.assertEqual(a, b, where)


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Metrics(unittest.TestCase):
    def test_python_and_js_agree_on_every_case(self):
        cases = json.loads((ROOT / "tests" / "metrics_cases.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cases), 8)
        got = run_js("arg.map(c => ({ m: classMetrics(c.pairs, c.oos), g: goodput(c.results, c.deadline) }))", cases)
        for case, js in zip(cases, got):
            py = {"m": metrics.classification([tuple(p) for p in case["pairs"]], case["oos"]),
                  "g": metrics.goodput([tuple(r) for r in case["results"]], case["deadline"])}
            same(self, py, js, case["name"])

    def test_cases_cover_what_matters(self):
        cases = json.loads((ROOT / "tests" / "metrics_cases.json").read_text(encoding="utf-8"))
        self.assertTrue(any(not c["pairs"] for c in cases))                                   # prázdný běh
        self.assertTrue(any(c["oos"] for c in cases))                                         # mimo rozsah
        self.assertTrue(any(p[1] is None for c in cases for p in c["pairs"]))                 # neplatná odpověď
        self.assertTrue(any(c["deadline"] is None for c in cases))                            # bez limitu


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class PublicValidator(unittest.TestCase):
    def test_python_and_js_agree_on_every_case(self):
        import bench
        cases = json.loads((ROOT / "tests" / "public_cases.json").read_text(encoding="utf-8"))
        for lang, intro in (("cs", "Neplatná veřejná sada: "), ("en", "Invalid public dataset: ")):
            got = run_js("arg.map(c => { try { return { ok: validatePublic(c.doc) }; } catch (e) { return { error: e.message }; } })",
                         [{"doc": c["doc"]} for c in cases], lang=lang)
            for case, js in zip(cases, got):
                where = f"{lang}: {case['name']}"
                with i18n.process(lang):
                    try:
                        py = {"ok": bench.validate_public(case["doc"])}
                    except ValueError as e:
                        py = {"error": str(e)}
                self.assertEqual("error" in py, case["error"] is not None, where)
                same(self, py, js, where)
                for key in case.get("absent", ()):                                                  # pole, které výsledek nést nesmí: samotná shoda by prošla, i kdyby ho nesly obě strany
                    self.assertNotIn(key, py["ok"], where)
                    self.assertNotIn(key, js["ok"], where)
                if case["error"] is None:
                    continue
                # samotná shoda Pythonu s JS by prošla i bez překladu (obě strany by vracely česky): věta musí být v jazyce, o který šlo
                self.assertTrue(py["error"].startswith(intro), f"{where}: {py['error']}")
                if lang == "cs":
                    self.assertEqual(py["error"], intro + case["error"], where)                     # česky doslova jako před překladem
                else:
                    self.assertEqual(CZECH.findall(py["error"]), [], f"{where}: {py['error']}")     # klíče štítků v případech diakritiku nemají

    def test_shipped_test_sets_pass_in_js_too(self):
        import bench
        for name in ("public-mini.json", "public-options.json"):
            doc = json.loads((ROOT / "tests" / name).read_text(encoding="utf-8"))
            same(self, bench.validate_public(doc), run_js("validatePublic(arg)", doc), name)

    def test_english_description_is_carried_the_same_by_python_and_js(self):   # about_en: volitelný text; výsledek ho nese, jen když ho sada má
        import bench
        base = {"name": "A", "source": "s", "license": "l", "labels": [{"key": "a"}, {"key": "b"}], "items": [{"text": "t", "label": "a"}]}      # se jménem: výchozí název je v jazyce běhu
        docs = [{**base, "about": "česky", "about_en": "in English"}, {**base, "about_en": "in English"}, {**base, "about": "česky", "about_en": ""},
                {**base, "about": "česky", "about_en": None}, {**base, "about": "česky"}, base,
                {**base, "about": "česky", "about_en": "  in English "},       # popis s textem se neořezává
                {**base, "about": "česky", "about_en": "   "}, {**base, "about": "česky", "about_en": "\t\r\n\xa0"}]      # jen bílé znaky = prázdný
        carried = ["in English", "in English", None, None, None, None, "  in English ", None, None]
        for doc, want, js in zip(docs, carried, run_js("arg.map(d => validatePublic(d))", docs)):
            py = bench.validate_public(doc)
            same(self, py, js, repr(doc.get("about_en")))
            self.assertEqual(py.get("about_en"), want)
            self.assertEqual("about_en" in js, want is not None)
            self.assertEqual("about_en" in py, want is not None)
            self.assertEqual(js["about"], doc.get("about") or "")              # český popis se anglickým nepřepisuje
        for doc in (json.loads((ROOT / "tests" / name).read_text(encoding="utf-8")) for name in ("public-mini.json", "public-options.json")):
            self.assertEqual(run_js("validatePublic(arg).about_en", doc), doc["about_en"])      # přibalené zkušební sady anglický popis nesou

    def test_blank_description_means_the_same_characters_in_python_and_js(self):
        # „Jen bílé znaky“ jsou znaky, které ořízne String.prototype.trim. str.strip() bez argumentu bere jinou množinu
        # (navíc U+001C až U+001F a U+0085, bez U+FEFF): sada by pak prošla validátorem ve stránce jinak než v bench.py.
        import bench
        base = {"name": "A", "source": "s", "license": "l", "labels": [{"key": "a"}, {"key": "b"}], "items": [{"text": "t", "label": "a"}]}
        codes = [*range(0x00, 0x21), 0x7F, 0x85, 0xA0, 0x1680, 0x180E, *range(0x2000, 0x2010), 0x2028, 0x2029, 0x202F, 0x205F, 0x2060, 0x3000, 0xFEFF]
        docs = [{**base, "about_en": chr(code)} for code in codes]
        js = run_js("arg.map(d => 'about_en' in validatePublic(d))", docs)
        py = ["about_en" in bench.validate_public(doc) for doc in docs]
        self.assertEqual([f"U+{code:04X}" for code, a, b in zip(codes, py, js) if a != b], [])
        blank = [code for code, kept in zip(codes, js) if not kept]
        self.assertEqual(blank, [0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF])
        self.assertEqual(sorted(map(ord, bench.JS_SPACE)), blank)              # seznam v bench.py je právě tahle množina
        self.assertNotIn("about_en", bench.validate_public({**base, "about_en": bench.JS_SPACE}))


def py_sign(sign):
    """Značka z JSON (seznamy) jako v bench.py (n-tice)."""
    cls, param = sign
    return cls, tuple(param) if isinstance(param, list) else param


def all_signs():
    """Všechny značky, které šablony umí: 4 třídy × 5 typů a 2 třídy × 12 mezí."""
    import bench
    return ([(cls, kind) for cls in ("only", "except_only", "no", "all_except") for kind in bench.VEHICLES]
            + [(cls, (dim, x)) for cls in ("limit_no", "limit_up_to") for dim in bench.LIMITS for x in bench.LIMITS[dim]])


# Generátor stránky s vlastní náhodou (mulberry32 se semínkem): seed, lanes, n → řady signRow.
ROWS_JS = ("(() => { let a = arg.seed >>> 0; const rand = () => { a = (a + 0x6D2B79F5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a);"
           " t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };"
           " return [...Array(arg.n)].map(() => signRow(arg.lanes, rand)); })()")


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Signs(unittest.TestCase):
    def test_constants_are_the_same_as_in_bench(self):
        import bench
        js = run_js("({ vehicles: SIGN_VEHICLES, limits: SIGN_LIMITS, classes: SIGN_CLASSES, partner: SIGN_PARTNER, texts: SIGN_TEXTS })", None)
        same(self, {k: [v[0], list(v[1]), list(v[2])] for k, v in bench.VEHICLES.items()}, js["vehicles"], "vehicles")
        self.assertEqual(list(js["vehicles"]), list(bench.VEHICLES))                          # pořadí typů rozhoduje o losování
        same(self, {k: list(v) for k, v in bench.LIMITS.items()}, js["limits"], "limits")
        self.assertEqual(js["classes"], list(bench.SIGN_CLASSES))
        self.assertEqual(js["partner"], bench.SIGN_PARTNER)
        py_texts = json.loads(json.dumps(bench.SIGN_TEXTS).replace("{x:g}", "{x}"))         # Python {x:g}, stránka {x}
        self.assertEqual(js["texts"], py_texts)

    def test_sign_texts_and_rule_match_bench(self):
        import bench
        signs = all_signs()
        self.assertEqual(run_js("arg.map(signText)", signs), [bench.sign_text(s) for s in signs])
        vehicles = [v for v, _, _, _ in bench.sign_cases(150, 8)] + [
            {"type": "car", "weight_t": 1.5, "height_m": 1.6}, {"type": "tractor", "weight_t": 3.4, "height_m": 2.5},
            {"type": "tractor", "weight_t": 3.6, "height_m": 2.9}, {"type": "truck", "weight_t": 7.5, "height_m": 3.2}]
        got = run_js("arg.vehicles.map(v => arg.signs.map(s => signAllowed(v, s)))", {"vehicles": vehicles, "signs": signs})
        self.assertEqual(got, [[bench.allowed(v, s) for s in signs] for v in vehicles])

    def test_hand_checked_table(self):                                 # tabulka z tests/test_signs.py (Allowed), ověřená ručně
        car = {"type": "car", "weight_t": 1.5, "height_m": 1.6}
        light = {"type": "tractor", "weight_t": 3.4, "height_m": 2.5}
        heavy = {"type": "tractor", "weight_t": 3.6, "height_m": 2.9}
        truck = {"type": "truck", "weight_t": 7.5, "height_m": 3.2}
        table = [
            (truck, ["only", "truck"], True), (truck, ["only", "bus"], False),
            (car, ["except_only", "car"], True), (car, ["except_only", "motorcycle"], False),
            (truck, ["no", "bus"], True), (truck, ["no", "truck"], False),
            (car, ["all_except", "truck"], True), (car, ["all_except", "car"], False),
            (light, ["limit_no", ["weight_t", 3.5]], True), (heavy, ["limit_no", ["weight_t", 3.5]], False),
            (light, ["limit_up_to", ["weight_t", 3.5]], True), (heavy, ["limit_up_to", ["weight_t", 3.5]], False),
            (truck, ["limit_no", ["height_m", 3.5]], True), (truck, ["limit_no", ["height_m", 3]], False),
            (heavy, ["limit_up_to", ["height_m", 3]], True), (truck, ["limit_up_to", ["height_m", 2.8]], False),
            (truck, ["limit_no", ["weight_t", 3.5]], False),
        ]
        self.assertEqual(run_js("arg.map(([v, s]) => signAllowed(v, s))", [[v, s] for v, s, _ in table]), [want for _, _, want in table])

    def test_state_and_question_match_bench(self):
        import bench
        vehicle = {"type": "truck", "weight_t": 7.5, "height_m": 3.2}
        signs = [("only", "bus"), ("all_except", "truck"), ("limit_no", ("weight_t", 3.5)), ("limit_no", ("height_m", 3.5))]
        js = run_js("signState(arg.vehicle, arg.signs)", {"vehicle": vehicle, "signs": signs})
        py = bench.sign_state(vehicle, signs)
        self.assertEqual(json.dumps(js), json.dumps(py))                                    # bajtově stejně, i pořadí klíčů
        for lanes in range(2, 9):
            js = run_js("signQuestion(arg)", lanes)
            self.assertEqual(json.dumps(js), json.dumps(bench.sign_question(lanes)), lanes)

    def test_alternative_wording_matches_bench(self):              # ALT stojí mimo sdílený blok: čte se ze zdrojáku stránky
        import bench
        page = (ROOT / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
        alt = re.search(r"\nconst ALT = \{[^\n]*\n(.*?)\n\};", page, re.S).group(1)
        self.assertEqual(re.findall(r"^  allowed: '([^'\\]*)',$", alt, re.M), [bench.ALT["allowed"]])

    def test_warm_task_has_exactly_one_open_lane(self):
        import bench
        warm = run_js("SIGN_WARM", None)
        vehicle, signs = warm["vehicle"], [py_sign(s) for s in warm["signs"]]
        self.assertEqual(len(signs), 8)
        self.assertEqual(len(set(signs)), 8)
        for lanes in range(2, 9):
            self.assertEqual([i for i, s in enumerate(signs[:lanes]) if bench.allowed(vehicle, s)], [0], lanes)

    def test_generated_rows_keep_the_rules_of_bench(self):
        import bench
        counts = {c: 0 for c in bench.SIGN_CLASSES}
        for lanes in range(2, 9):
            rows = run_js(ROWS_JS, {"seed": lanes, "lanes": lanes, "n": 500})
            for row in rows:
                vehicle, signs = row["vehicle"], [py_sign(s) for s in row["signs"]]
                where = (lanes, vehicle, signs)
                self.assertEqual(list(vehicle), ["type", "weight_t", "height_m"])
                self.assertEqual(len(signs), lanes)
                self.assertEqual(len(set(signs)), lanes, where)                              # žádná značka dvakrát
                self.assertEqual([i for i, s in enumerate(signs) if bench.allowed(vehicle, s)], [row["truth"]], where)      # pravidlem z bench.py
                self.assertEqual(signs[row["truth"]][0], row["cls"])
                _, weight, height = bench.VEHICLES[vehicle["type"]]
                for dim, (lo, hi) in (("weight_t", weight), ("height_m", height)):
                    self.assertTrue(lo <= vehicle[dim] <= hi, where)
                    self.assertEqual(vehicle[dim], round(vehicle[dim], 1), where)
                    self.assertNotIn(vehicle[dim], bench.LIMITS[dim], where)
                if row["cls"].startswith("limit_"):
                    closing = [s for i, s in enumerate(signs) if i != row["truth"] and s[0].startswith("limit_")]
                    self.assertTrue(closing, where)
                    self.assertTrue(bench.stopped_by_a_limit(vehicle), where)
                counts[row["cls"]] += 1
        for cls, n in counts.items():                                                        # 3500 řad, čekáno 583 na třídu
            self.assertTrue(480 <= n <= 690, (cls, n))

    def test_same_seed_same_rows(self):
        first = run_js(ROWS_JS, {"seed": 7, "lanes": 4, "n": 50})
        self.assertEqual(run_js(ROWS_JS, {"seed": 7, "lanes": 4, "n": 50}), first)
        self.assertNotEqual(run_js(ROWS_JS, {"seed": 8, "lanes": 4, "n": 50}), first)

    def test_broken_guarantee_raises_instead_of_fixing(self):
        got = run_js("(() => { signAllowed = () => true; try { signRow(3, Math.random); return null; } catch (e) { return e.message; } })()", None)
        self.assertTrue(got.startswith("signRow: 3 lanes open instead of 1: "), got)


SPEC_HISTORY = ["circle", "square", "star", "triangle", "arrow", "moon"]         # příklad ze specu první iterace paměťové stopy
MEMORY_TASKS = [                                                                  # (historie, N, značky bran): příklad ze specu, N = 1, nejdelší historie s 8 branami
    (SPEC_HISTORY, 3, ["arrow", "triangle", "moon", "star"]),
    (["heart", "cross", "bell"], 1, ["cross", "bell"]),
    (["leaf", "bell", "hexagon", "diamond", "cross", "heart", "moon", "arrow", "triangle", "star", "square", "circle"], 8,
     ["triangle", "heart", "diamond", "moon", "bell", "arrow", "hexagon", "cross"]),
]


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Memory(unittest.TestCase):
    def test_constants_are_the_same_as_in_bench(self):
        import bench
        js = run_js("({ signs: MEMORY_SIGNS, steps: MEMORY_STEPS, length: MEMORY_LENGTH_MAX, share: MEMORY_SPAN_SHARE, labels: [1, 2, 12].map(memoryLabel) })", None)
        self.assertEqual(js["signs"], list(bench.MEMORY_SIGNS))                         # pořadí rozhoduje o losování
        self.assertEqual(js["steps"], list(bench.MEMORY_STEPS))
        self.assertEqual((js["length"], js["share"]), (bench.MEMORY_LENGTH_MAX, bench.MEMORY_SPAN_SHARE))
        self.assertEqual(js["labels"], [bench.memory_label(k) for k in (1, 2, 12)])

    def test_state_and_question_match_bench(self):
        import bench
        got = run_js("arg.map(([h, n, s]) => memoryState(h, n, s))", MEMORY_TASKS)
        for (history, steps, signs), js in zip(MEMORY_TASKS, got):
            self.assertEqual(json.dumps(js), json.dumps(bench.memory_state(history, steps, signs)), (history, steps))      # bajtově stejně, i pořadí klíčů
        got = run_js("arg.map(g => [gateKeys(g), memoryQuestion(g)])", list(range(2, 9)))
        for gates, (keys, question) in zip(range(2, 9), got):
            self.assertEqual(keys, bench.gate_keys(gates))
            self.assertEqual(json.dumps(question), json.dumps(bench.memory_question(gates)), gates)

    def test_alternative_wording_matches_bench(self):              # ALT stojí mimo sdílený blok: čte se ze zdrojáku stránky
        import bench
        page = (ROOT / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
        alt = re.search(r"\nconst ALT = \{[^\n]*\n(.*?)\n\};", page, re.S).group(1)
        self.assertEqual(re.findall(r"^  recall: '([^'\\]*)',$", alt, re.M), [bench.ALT["recall"]])
        self.assertNotIn("recall_count", alt)                      # podoba --count je jen v bench.py

    def test_warm_task_is_fixed_and_has_the_target_at_the_gates(self):
        import bench
        for gates, warm in zip(range(2, 9), run_js("arg.map(g => memoryWarm(g))", list(range(2, 9)))):
            history, steps, signs, truth = warm["history"], warm["steps"], warm["signs"], warm["truth"]
            self.assertEqual(list(warm), ["history", "steps", "signs", "truth"])
            self.assertEqual(history, list(bench.MEMORY_SIGNS))                         # nejdelší historie: horní odhad délky dotazu
            self.assertIn(steps, bench.MEMORY_STEPS)
            self.assertEqual(signs, history[len(history) - gates:])                     # souvislý úsek na konci historie
            self.assertEqual(signs[truth], history[len(history) - steps])
            self.assertEqual(signs.count(signs[truth]), 1)

    def test_generated_rows_keep_the_rules_of_bench(self):
        import bench
        for gates in range(2, 9):
            for row in run_js(MEMORY_ROWS_JS, {"seed": gates, "gates": gates, "n": 500}):
                history, steps, signs, truth = row["history"], row["steps"], row["signs"], row["truth"]
                with self.subTest(gates=gates, history=history, steps=steps, signs=signs):
                    self.assertEqual(list(row), ["history", "steps", "signs", "truth"])
                    self.assertIn(steps, bench.MEMORY_STEPS)
                    self.assertTrue(max(steps, gates) <= len(history) <= bench.MEMORY_LENGTH_MAX)
                    self.assertEqual(len(set(history)), len(history))                         # bez opakování
                    self.assertTrue(set(history) <= set(bench.MEMORY_SIGNS))
                    self.assertEqual(len(signs), gates)
                    self.assertEqual(len(set(signs)), gates)
                    self.assertTrue(set(signs) <= set(history))
                    pos = sorted(history.index(s) for s in signs)
                    self.assertEqual(pos, list(range(pos[0], pos[0] + gates)))                # souvislý úsek historie
                    self.assertEqual(signs[truth], history[len(history) - steps])
                    self.assertEqual(signs.count(signs[truth]), 1)

    def test_steps_are_spread_evenly(self):
        counts = Counter(row["steps"] for row in run_js(MEMORY_ROWS_JS, {"seed": 1, "gates": 4, "n": 800}))
        self.assertEqual(sorted(counts), [1, 2, 3, 4, 5, 6, 7, 8])
        for steps, n in counts.items():
            self.assertTrue(60 <= n <= 140, (steps, n))

    def test_strategies_without_counting_stay_near_chance(self):      # stejné strategie jako tests/test_memory.py, na generátoru stránky
        # Neprojde-li tento test, implementace se zastaví a zapíše to do reportu; generátor se neladí potichu.
        for gates in (3, 4, 8):
            rows, rng = run_js(MEMORY_ROWS_JS, {"seed": 11, "gates": gates, "n": 4000}), random.Random(1)
            cases = [(r["history"], r["signs"], r["truth"]) for r in rows]

            def kth(k):                                                # vždy k-tá nejstarší značka okna
                return lambda h, s: sorted(range(gates), key=lambda i: h.index(s[i]))[k]

            def newest_sign(h, s):                                     # nejnovější značka historie, když je na branách; jinak náhodně
                return s.index(h[-1]) if h[-1] in s else rng.randrange(gates)

            def both_neighbours(h, s):                                 # značka, jejíž oba sousedé z historie jsou na branách; jinak náhodně
                p = {h.index(x) for x in s}
                return rng.choice([i for i, x in enumerate(s) if h.index(x) - 1 in p and h.index(x) + 1 in p] or range(gates))

            strategies = {f"k={k + 1}": kth(k) for k in range(gates)} | {"newest": newest_sign, "neighbours": both_neighbours}
            for name, pick in strategies.items():
                acc = sum(pick(h, s) == top for h, s, top in cases) / len(cases)
                with self.subTest(gates=gates, strategy=name, acc=acc):
                    self.assertLessEqual(acc - 1 / gates, 0.15)

    def test_same_seed_same_rows(self):
        first = run_js(MEMORY_ROWS_JS, {"seed": 7, "gates": 4, "n": 50})
        self.assertEqual(run_js(MEMORY_ROWS_JS, {"seed": 7, "gates": 4, "n": 50}), first)
        self.assertNotEqual(run_js(MEMORY_ROWS_JS, {"seed": 8, "gates": 4, "n": 50}), first)

    def test_broken_guarantee_raises_instead_of_fixing(self):      # stejné vstupy jako tests/test_memory.py; chyba se chytá v JS (run_js bere jen konec výpisu)
        bad = [[["moon", "moon", "star"], 1, 3],                   # opakování v historii
               [["circle", "square"], 1, 3],                       # méně značek než bran
               [SPEC_HISTORY, 1, 0],                               # žádná brána
               [SPEC_HISTORY, 7, 3], [SPEC_HISTORY, 0, 3]]         # N mimo 1…L
        got = run_js("arg.map(([h, n, k]) => { try { memoryGates(Math.random, h, n, k); return null; } catch (e) { return e.message; } })", bad)
        for (history, steps, gates), message in zip(bad, got):
            self.assertTrue(str(message).startswith(f"memoryGates: {steps} steps back, {gates} gates in "), (history, steps, gates, message))

    def test_errors_and_span_match_bench(self):                    # ručně sestavené úlohy a počty z tests/test_memory.py (Errors)
        import bench
        ten = list(bench.MEMORY_SIGNS[:10])                                             # circle … hexagon
        five = (ten, 4, ["heart", "cross", "moon", "hexagon", "circle"], 0)           # heart 4 kroky zpět; cross 3, moon 5, hexagon 1, circle 10
        two = (ten, 2, ["diamond", "hexagon", "cross"], 0)                            # N=2: nejnovější (hexagon) je zároveň soused
        keys = bench.gate_keys(5)
        cases = [five] * 8 + [two]
        choices = ["gate_1", "gate_2", "gate_3", "gate_4", "gate_5", None, "nonsense", ["gate_1"], "gate_2"]
        js_cases = [{"history": h, "steps": n, "signs": s, "truth": top} for h, n, s, top in cases]
        got = run_js("memoryErrors(arg.cases, arg.choices, arg.keys)", {"cases": js_cases, "choices": choices, "keys": keys})
        self.assertEqual(json.dumps(got), json.dumps(bench.memory_errors(cases, choices, keys)))       # i pořadí klíčů
        self.assertEqual(got, {"near": 3, "latest": 1, "other": 1, "invalid": 3})
        full = {n: [10, 10] for n in bench.MEMORY_STEPS}
        tables = [full, {**full, 1: [7, 10]}, {**full, 3: [7, 10]}, {**full, 5: [8, 10]}, {**full, 5: [28, 35]}, {**full, 8: [3, 4]},
                  {**full, 2: [0, 0]}, {n: [0, 0] for n in bench.MEMORY_STEPS}]
        self.assertEqual([bench.memory_span(t) for t in tables], [8, None, 2, 8, 8, 7, 1, None])       # —, propad N=3 při N=4 v pořádku, přesně 80 %, prázdné N
        self.assertEqual(run_js("arg.map(t => memorySpan(t))", tables), [bench.memory_span(t) for t in tables])


BARRIER_NAMES = ["police_officer", "emergency_vehicle", "pedestrian_on_crossing", "light", "sign"]
BARRIER_STATES = [                                                               # příklad ze specu Závory (rozhoduje záchranka), pravidlo 5
    {"police_officer": "none", "emergency_vehicle": "approaching from the left with siren on", "pedestrian_on_crossing": "no",
     "light": "green", "sign": "priority road"},
    {"police_officer": "none", "emergency_vehicle": "parked at the roadside, siren off", "pedestrian_on_crossing": "no",
     "light": "off", "sign": "give way"},
]


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Barrier(unittest.TestCase):
    def test_constants_and_question_are_the_same_as_in_bench(self):
        import bench
        js = run_js("({ fields: BARRIER_FIELDS, share: BARRIER_GO_SHARE, rules: BARRIER_RULES, classes: BARRIER_CLASSES,"
                    " go: BARRIER_GO, stop: BARRIER_STOP, note: INJECT_NOTE_NOUL, question: barrierQuestion() })", None)
        self.assertEqual(json.dumps(js["fields"]), json.dumps(bench.BARRIER_FIELDS))       # i pořadí polí a hodnot: rozhoduje o losování
        self.assertEqual((js["share"], js["rules"], js["classes"]),
                         (bench.BARRIER_GO_SHARE, list(bench.BARRIER_RULES), list(bench.BARRIER_CLASSES)))
        self.assertEqual((js["go"], js["stop"], js["note"]), (bench.BARRIER_GO, bench.BARRIER_STOP, bench.INJECT_NOTE_NOUL))
        self.assertEqual(json.dumps(js["question"]), json.dumps(bench.barrier_question()))

    def test_alternative_wording_matches_bench(self):              # ALT stojí mimo sdílený blok: čte se ze zdrojáku stránky
        import bench
        page = (ROOT / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
        alt = re.search(r"\nconst ALT = \{[^\n]*\n(.*?)\n\};", page, re.S).group(1)
        self.assertEqual(re.findall(r'^  go: "([^"\\]*)",$', alt, re.M), [bench.ALT["go"]])

    def test_decide_matches_bench_on_every_state(self):
        import bench
        states = [dict(zip(BARRIER_NAMES, combo)) for combo in itertools.product(*(list(table) for _, table in bench.BARRIER_FIELDS))]
        self.assertEqual(len(states), 288)
        got = run_js("arg.map(s => [1, 2, 3, 4, 5].map(k => barrierDecide(s, k)))", states)
        self.assertEqual(got, [[list(bench.barrier_decide(s, k)) for k in range(1, 6)] for s in states])
        errors = run_js("[[arg, 6], [{ ...arg, light: 'blue' }, 1]].map(([s, k]) => { try { barrierDecide(s, k); return null; } catch (e) { return e.message; } })",
                        BARRIER_STATES[1])
        self.assertTrue(errors[0].startswith("barrierDecide: no rule from 6 decides "), errors[0])      # Python: RuntimeError
        self.assertTrue(errors[1].startswith("barrierDecide: unknown light "), errors[1])              # Python: KeyError; nic se neopravuje potichu

    def test_note_matches_bench(self):                              # stav s pokynem: bajtově stejně, i pořadí klíčů (note na konci)
        import bench
        calls = [[s, target, inverted] for s in BARRIER_STATES for target in ("go", "stop", None) for inverted in (False, True)]
        got = run_js("arg.map(([s, target, inverted]) => withNoulNote(s, target, inverted))", calls)
        for (state, target, inverted), js in zip(calls, got):
            self.assertEqual(json.dumps(js), json.dumps(bench.with_noul_note(state, target, inverted)), (target, inverted))
        self.assertTrue(run_js("withNoulNote(arg, null) === arg", BARRIER_STATES[0]))          # bez cíle stav beze změny, jako v Pythonu

    def test_warm_task_is_fixed_and_decided_by_the_police(self):
        import bench
        warm = run_js("BARRIER_WARM", None)
        tables = dict(bench.BARRIER_FIELDS)
        self.assertEqual(list(warm), ["state", "go", "rule", "cls"])
        self.assertEqual(list(warm["state"]), BARRIER_NAMES)
        self.assertEqual(bench.barrier_decide(warm["state"]), (True, 1))
        self.assertEqual((warm["go"], warm["rule"], warm["cls"]), (True, 1, "conflict_high"))
        self.assertIs(bench.barrier_decide(warm["state"], 4)[0], False)                        # semafor říká opak: přebitý semafor
        for name in BARRIER_NAMES[1:]:                                                          # pod policistou nejdelší hodnoty: odhad délky dotazu je horní
            self.assertEqual(warm["state"][name], max(tables[name], key=len), name)

    def test_noul_decision_matches_bench(self):
        import bench
        values = [0.5, 0.4999, 1, 0, 1.0, 0.0, 0.73, -0.1, 1.01, None, True, False, "0.7", [0.7], {"noul": 0.7}]
        got = run_js("arg.map(p => [noulDecision(p), noulDecision(p, true)])", values)
        self.assertEqual(got, [[bench.noul_decision(p), bench.noul_decision(p, True)] for p in values])
        self.assertEqual(run_js("[NaN, Infinity, -Infinity, undefined].map(p => [noulDecision(p), noulDecision(p, true)])", None), [[None, None]] * 4)

    def test_generated_cases_keep_the_rules_of_bench(self):
        import bench
        tables = dict(bench.BARRIER_FIELDS)
        cases = run_js(BARRIER_ROWS_JS, {"seed": 11, "n": 4000})
        self.assertTrue(0.70 <= sum(c["go"] for c in cases) / len(cases) <= 0.80)
        for c in cases:
            state, go, rule, cls = c["state"], c["go"], c["rule"], c["cls"]
            with self.subTest(state=state, rule=rule, cls=cls):
                self.assertEqual(list(c), ["state", "go", "rule", "cls"])
                self.assertEqual(list(state), BARRIER_NAMES)                                   # pevné pořadí klíčů stavu
                self.assertTrue(all(state[name] in tables[name] for name in BARRIER_NAMES))
                self.assertEqual(bench.barrier_decide(state), (go, rule))
                self.assertTrue(all(tables[name][state[name]] is None for name in BARRIER_NAMES[:rule - 1]))      # pole nad pravidlem nerozhodují
                if rule == 5:
                    self.assertEqual(cls, "last")
                else:                                                                          # stín: semafor (při off značka), u pravidla 4 značka
                    shadow = bench.barrier_decide(state, 4 if rule < 4 else 5)[0]
                    self.assertEqual(cls, ("conflict_high" if rule < 4 else "conflict_light") if shadow != go else "agree")
        lower = ["agree" if c["cls"] == "agree" else "conflict" for c in cases if c["rule"] < 5]
        for cls in ("agree", "conflict"):
            self.assertTrue(0.40 <= lower.count(cls) / len(lower) <= 0.60, (cls, lower.count(cls), len(lower)))
        self.assertEqual({c["rule"] for c in cases if c["cls"] == "conflict_high"}, {1, 2, 3})
        self.assertEqual({c["rule"] for c in cases if c["cls"] == "conflict_light"}, {4})
        for name, table in bench.BARRIER_FIELDS:                                                # každá hodnota se objeví
            self.assertEqual({c["state"][name] for c in cases}, set(table), name)

        def light_only(state):                                                                 # strategie „jen semafor, při off značka“
            light = tables["light"][state["light"]]
            return tables["sign"][state["sign"]] if light is None else light
        high = [c for c in cases if c["cls"] == "conflict_high"]
        agree_top = [c for c in cases if c["cls"] == "agree" and c["rule"] < 4]
        self.assertEqual(sum(light_only(c["state"]) == c["go"] for c in high), 0)
        self.assertEqual(sum(light_only(c["state"]) == c["go"] for c in agree_top), len(agree_top))

    def test_same_seed_same_cases(self):
        first = run_js(BARRIER_ROWS_JS, {"seed": 7, "n": 50})
        self.assertEqual(run_js(BARRIER_ROWS_JS, {"seed": 7, "n": 50}), first)
        self.assertNotEqual(run_js(BARRIER_ROWS_JS, {"seed": 8, "n": 50}), first)

    def test_endless_retry_raises_instead_of_looping(self):        # jako Stuck v tests/test_barrier.py: jeď, pravidlo 1, konflikt, pole pod ním vždy první hodnota
        got = run_js("(() => { try { barrierCase(() => 0); return null; } catch (e) { return e.message; } })()", None)
        self.assertTrue(str(got).startswith("barrierCase: no conflict state for rule 1 after 1000 tries"), got)

    def test_summary_matches_bench(self):
        import bench
        cases, rng = bench.barrier_cases(400, seed=5), random.Random(3)
        probs = [rng.choice([None, "x", 1.5, True, 0.5, 1, 0, round(rng.random(), 1), round(rng.random(), 3)]) for _ in cases]
        js = run_js("barrierSummary(arg.cases, arg.probs)",
                    {"cases": [{"go": go, "rule": rule, "cls": cls} for _, go, rule, cls in cases], "probs": probs})
        self.assertEqual(json.dumps(js), json.dumps(bench.barrier_summary(cases, probs)))     # i pořadí klíčů
        self.assertGreater(js["invalid"], 0)

    def test_auroc_and_threshold_match_metrics(self):
        sets = [([0.9, 0.8, 0.2, 0.1], [True, True, False, False]), ([0.5, 0.5, 0.5], [True, False, True]),
                ([0.9, None, 0.1, None], [True, True, False, False]), ([0.9, 0.8], [True, True]), ([0.9, None], [True, False]),
                ([0.25, 0.75, 0.625, 0.125], [True, True, False, False]), ([None, 0.9], [True, True]), ([None, None], [True, False]),
                ([0.8] * 4, [True, True, True, False]), ([0.9, 0.3, 0.3, 0.6], [True, True, True, False])]      # z tests/test_barrier.py (Quality)
        rng = random.Random(4)
        for k in range(40):                                      # náhodné sady se shodami hodnot; každá pátá jen úlohy jeď, každá sedmá jen stůj
            n = rng.randint(1, 60)
            truths = [True] * n if k % 5 == 0 else [False] * n if k % 7 == 0 else [rng.random() < 0.75 for _ in range(n)]
            sets.append(([None if rng.random() < 0.1 else round(rng.random(), rng.choice([1, 2, 3])) for _ in range(n)], truths))
        got = run_js("arg.map(([p, t]) => [auroc(p, t), bestThreshold(p, t)])", sets)
        for (probs, truths), (a, th) in zip(sets, got):
            self.assertEqual(a, metrics.auroc(probs, truths), (probs, truths))
            self.assertEqual(th, list(metrics.best_threshold(probs, truths)), (probs, truths))   # přesně, ne s tolerancí
        self.assertIsNone(run_js("bestThreshold([], [])", None))                                # prázdný běh: stránka ukáže „—“

    def test_auroc_and_threshold_match_metrics_on_a_large_set(self):     # stránka počítá v n log n, Python po párech: přesná shoda i u tisíců úloh
        rng = random.Random(11)
        sets = []
        for n, share in ((3000, 0.7), (2001, 0.5)):
            truths = [rng.random() < share for _ in range(n)]
            sets.append(([None if rng.random() < 0.05 else round(rng.random(), rng.choice([1, 2])) for _ in range(n)], truths))   # mnoho shod hodnot
        got = run_js("arg.map(([p, t]) => [auroc(p, t), bestThreshold(p, t)])", sets)
        for (probs, truths), (a, th) in zip(sets, got):
            self.assertEqual(a, metrics.auroc(probs, truths))
            self.assertEqual(th, list(metrics.best_threshold(probs, truths)))


# Generátor paměťové stopy stránky se stejnou náhodou jako ROWS_JS: seed, gates, n → řady memoryRow.
MEMORY_ROWS_JS = ROWS_JS.replace("signRow(arg.lanes, rand)", "memoryRow(arg.gates, rand)")
assert MEMORY_ROWS_JS != ROWS_JS
# Generátor Závory stránky se stejnou náhodou jako ROWS_JS: seed, n → úlohy barrierCase.
BARRIER_ROWS_JS = ROWS_JS.replace("signRow(arg.lanes, rand)", "barrierCase(rand)")
assert BARRIER_ROWS_JS != ROWS_JS
LOOKAHEAD_EXAMPLE = (".#..", "..#.", "###.", "....")             # příklad z rešerše (EXAMPLE v tests/test_lookahead.py): auto v pruhu 2, správně right
LOOKAHEAD_HAND = [[list(LOOKAHEAD_EXAMPLE), 1], [["#.#", "..."], 1], [["..#", "##."], 0], [["..."], 1], [["...", "..."], 0],
                  [["...", "..."], 2], [["..#", "..#"], 2], [[".#.", "..."], 1], [["....", "##..", "##..", "...."], 1],
                  [[".#.", "...", "#..", "..."], 1], [["....",], 0], [[".#...",], 1], [["#.#..",], 1]]      # ručně ověřené mřížky z tests/test_lookahead.py


def lookahead_grids(n, seed):
    """Náhodné mřížky pro paritu prohledávání: K = 3–8, 1–4 řady s hustotou 0,4 (i řady bez volného pole), každý pruh auta."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        lanes = rng.randint(3, 8)
        out.append([["".join("#" if rng.random() < 0.4 else "." for _ in range(lanes)) for _ in range(rng.randint(1, 4))], rng.randrange(lanes)])
    return out


# Generátor Dvou kroků dopředu stránky se stejnou náhodou jako ROWS_JS: seed, lanes, n → řady lookaheadRow.
LOOKAHEAD_ROWS_JS = ROWS_JS.replace("signRow(arg.lanes, rand)", "lookaheadRow(arg.lanes, rand)")
assert LOOKAHEAD_ROWS_JS != ROWS_JS
LOOKAHEAD_PAGE = {}


def lookahead_page(n, lanes, seed=1):
    """Řady generátoru stránky (jednou za běh modulu) a tytéž jako n-tice bench.py (řady, pruh, tah, hloubka, hladový)."""
    if (n, lanes, seed) not in LOOKAHEAD_PAGE:
        raw = run_js(LOOKAHEAD_ROWS_JS, {"seed": seed, "lanes": lanes, "n": n})
        LOOKAHEAD_PAGE[n, lanes, seed] = raw, [(tuple(r["rows"]), r["lane"], r["answer"], r["depth"], r["greedy"]) for r in raw]
    return LOOKAHEAD_PAGE[n, lanes, seed]


def lookahead_rates(cases):
    """Podíl úloh, kde kontrolní strategie trefí správný tah, podle třídy (jako shortcut_rates v tests/test_lookahead.py)."""
    import bench
    out = {}
    for name, pick in bench.LOOKAHEAD_SHORTCUTS.items():
        out[name] = {}
        for cls in bench.LOOKAHEAD_CLASSES:
            sub = [c for c in cases if bench.lookahead_class(c[3]) == cls]
            out[name][cls] = sum(pick(rows, lane) == answer for rows, lane, answer, _, _ in sub) / len(sub)
    return out


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Lookahead(unittest.TestCase):
    def test_constants_state_and_question_match_bench(self):
        import bench
        js = run_js("({ moves: LOOKAHEAD_MOVES, shift: LOOKAHEAD_SHIFT, rows: LOOKAHEAD_ROWS, density: LOOKAHEAD_DENSITY, classes: LOOKAHEAD_CLASSES,"
                    " tries: LOOKAHEAD_TRIES, shortcuts: Object.keys(LOOKAHEAD_SHORTCUTS) })", None)
        self.assertEqual(js["moves"], list(bench.LOOKAHEAD_MOVES))
        self.assertEqual(json.dumps(js["shift"]), json.dumps(bench.LOOKAHEAD_SHIFT))
        self.assertEqual((js["rows"], js["density"], js["tries"]), (bench.LOOKAHEAD_ROWS, bench.LOOKAHEAD_DENSITY, bench.LOOKAHEAD_TRIES))
        self.assertEqual(js["classes"], list(bench.LOOKAHEAD_CLASSES))
        self.assertEqual(js["shortcuts"], list(bench.LOOKAHEAD_SHORTCUTS))                  # pořadí rozhoduje o losování
        tasks = [[list(rows), lane] for lanes in range(3, 9) for rows, lane, *_ in bench.lookahead_cases(4, lanes, seed=lanes)]
        tasks.append([list(LOOKAHEAD_EXAMPLE), 1])
        for (rows, lane), state in zip(tasks, run_js("arg.map(([rows, lane]) => lookaheadState(rows, lane))", tasks)):
            self.assertEqual(json.dumps(state), json.dumps(bench.lookahead_state(tuple(rows), lane, "blocked")), (rows, lane))   # bajtově, i pořadí klíčů
        for lanes, question in zip(range(3, 9), run_js("[3, 4, 5, 6, 7, 8].map(k => lookaheadQuestion(k))", None)):
            self.assertEqual(json.dumps(question), json.dumps(bench.lookahead_question(lanes, "blocked")), lanes)

    def test_alternative_wording_matches_bench(self):              # ALT stojí mimo sdílený blok: čte se ze zdrojáku stránky
        import bench
        page = (ROOT / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
        alt = re.search(r"\nconst ALT = \{[^\n]*\n(.*?)\n\};", page, re.S).group(1)
        self.assertEqual(re.findall(r"^  move_blocked: '([^'\\]*)',$", alt, re.M), [bench.ALT["move_blocked"]])
        self.assertNotIn("\n  move: ", "\n" + alt)                 # podoba --rows je jen v bench.py

    def test_search_matches_bench(self):
        import bench
        grids = lookahead_grids(600, 3) + LOOKAHEAD_HAND
        got = run_js("arg.map(([rows, lane]) => [lookaheadSurvive(rows, lane), lookaheadValues(rows, lane), [1, 2].map(d => lookaheadValues(rows, lane, d)),"
                     " lookaheadGreedy(rows, lane), lookaheadDepth(rows, lane), lookaheadDensity(rows, lane), lookaheadCenter(rows, lane)])", grids)
        for (rows, lane), (survive, values, by_depth, greedy, depth, density, center) in zip(grids, got):
            rows = tuple(rows)
            with self.subTest(rows=rows, lane=lane):
                self.assertEqual(survive, bench.lookahead_survive(rows, lane))
                self.assertEqual(json.dumps(values), json.dumps(bench.lookahead_values(rows, lane)))       # i pořadí tahů
                self.assertEqual(by_depth, [bench.lookahead_values(rows, lane, d) for d in (1, 2)])
                self.assertEqual(greedy, bench.lookahead_greedy(rows, lane))
                self.assertEqual(depth, list(bench.lookahead_depth(rows, lane)))                         # [null, null] = nejlepší tah není jediný
                self.assertEqual(density, bench.lookahead_density(rows, lane))
                self.assertEqual(center, bench.lookahead_center(rows, lane))

    def test_generated_rows_keep_the_guarantees(self):
        import bench
        for lanes, n in ((3, 300), (4, 900), (8, 300)):
            raw, cases = lookahead_page(n, lanes)
            for r, (rows, lane, answer, depth, greedy) in zip(raw, cases):
                with self.subTest(lanes=lanes, rows=rows, lane=lane):
                    self.assertEqual(list(r), ["rows", "lane", "answer", "depth", "greedy", "truth"])
                    self.assertEqual(len(rows), bench.LOOKAHEAD_ROWS)
                    self.assertTrue(all(len(x) == lanes and set(x) <= {".", "#"} and "." in x for x in rows))
                    self.assertTrue(0 <= lane < lanes)
                    values = bench.lookahead_values(rows, lane)
                    self.assertEqual(values[answer], bench.LOOKAHEAD_ROWS)                          # nejlepší tah projede všechny řady
                    self.assertEqual([m for m in bench.LOOKAHEAD_MOVES if values[m] == values[answer]], [answer])     # a je jediný
                    self.assertEqual(bench.lookahead_depth(rows, lane), (answer, depth))
                    self.assertEqual(bench.lookahead_greedy(rows, lane), greedy)
                    self.assertEqual(r["truth"], bench.LOOKAHEAD_MOVES.index(answer))

    def test_classes_and_answers_are_spread(self):
        import bench
        _, cases = lookahead_page(900, 4)
        classes = Counter(bench.lookahead_class(d) for _, _, _, d, _ in cases)
        answers = Counter(a for _, _, a, _, _ in cases)
        for cls in bench.LOOKAHEAD_CLASSES:
            self.assertTrue(0.25 <= classes[cls] / len(cases) <= 0.42, (cls, classes))
        for move in ("left", "right"):
            self.assertGreaterEqual(answers[move] / len(cases), 0.20, (move, answers))
        self.assertGreaterEqual(answers["stay"] / len(cases), 0.17, answers)                       # Rozpory 3: bench.py 0,20 při naměřených 0,211

    def test_control_strategies(self):                             # meze jako tests/test_lookahead.py (Generator.test_control_strategies)
        import bench
        _, cases = lookahead_page(900, 4)

        def acc(pick, cls=None):
            sub = [c for c in cases if cls is None or bench.lookahead_class(c[3]) == cls]
            return sum(pick(rows, lane) == answer for rows, lane, answer, _, _ in sub) / len(sub)

        def lowest_free(rows, lane):                               # tah do volného pole s nejnižším číslem pruhu
            return next((m for m in bench.LOOKAHEAD_MOVES if 0 <= lane + bench.LOOKAHEAD_SHIFT[m] < len(rows[0])
                         and rows[0][lane + bench.LOOKAHEAD_SHIFT[m]] == "."), "stay")
        self.assertEqual(acc(bench.lookahead_greedy, "d1"), 1)
        rates = lookahead_rates(cases)
        for cls in ("d2", "d3+"):
            for name in bench.LOOKAHEAD_SHORTCUTS:
                with self.subTest(cls=cls, strategy=name):
                    self.assertLessEqual(rates[name][cls], 0.45, rates)
            with self.subTest(cls=cls, strategy="lowest_free"):
                self.assertLessEqual(acc(lowest_free, cls), 0.6)
        self.assertLess(acc(lambda rows, lane: "stay"), 0.5)

    def test_control_strategies_on_five_lanes(self):               # při K ≥ 5 se vyvažuje i center; 900 úloh místo 300 (Rozpory 3)
        import bench
        rates = lookahead_rates(lookahead_page(900, 5)[1])
        for cls in ("d2", "d3+"):
            for name in bench.LOOKAHEAD_SHORTCUTS:
                with self.subTest(cls=cls, strategy=name):
                    self.assertLessEqual(rates[name][cls], 0.45, rates)

    def test_same_seed_same_rows(self):
        first = run_js(LOOKAHEAD_ROWS_JS, {"seed": 7, "lanes": 4, "n": 30})
        self.assertEqual(run_js(LOOKAHEAD_ROWS_JS, {"seed": 7, "lanes": 4, "n": 30}), first)
        self.assertNotEqual(run_js(LOOKAHEAD_ROWS_JS, {"seed": 8, "lanes": 4, "n": 30}), first)

    def test_bounded_search_raises(self):                          # jako tests/test_lookahead.py; rand = 0 dá samé překážky, žádná řada neprojde
        got = run_js("[[() => 0, 4, 'd1'], [Math.random, 2, 'd1'], [Math.random, 9, 'd1'], [Math.random, 4, 'd4']]"
                     ".map(([rand, k, cls]) => { try { lookaheadCase(rand, k, cls); return null; } catch (e) { return e.message; } })", None)
        self.assertEqual(got, ["lookaheadCase: no d1 task for 4 lanes after 100000 tries", "lookaheadCase: no d1 task for 2 lanes",
                               "lookaheadCase: no d1 task for 9 lanes", "lookaheadCase: no d4 task for 4 lanes"])

    def test_errors_match_bench(self):
        import bench
        cases = bench.lookahead_cases(60, 4, seed=4) + bench.lookahead_cases(30, 6, seed=6)
        rng = random.Random(2)
        choices = [rng.choice(["left", "stay", "right", "left", "right", None, "LEFT", 1, ["stay"]]) for _ in cases]
        js_cases = [{"rows": list(rows), "lane": lane, "answer": answer, "greedy": greedy} for rows, lane, answer, _, greedy in cases]
        got = run_js("lookaheadErrors(arg.cases, arg.choices)", {"cases": js_cases, "choices": choices})
        self.assertEqual(json.dumps(got), json.dumps(bench.lookahead_errors(cases, choices)))        # i pořadí klíčů
        self.assertTrue(all(got[k] > 0 for k in ("greedy", "crash", "invalid")), got)

    def test_path_follows_the_value_of_the_move(self):             # cesta pro kresbu: délka = hodnota tahu, po volných polích, nejvýš o pruh
        import bench
        cases = bench.lookahead_cases(60, 4, seed=4) + bench.lookahead_cases(30, 7, seed=7)
        grids = [[list(rows), lane] for rows, lane, *_ in cases] + LOOKAHEAD_HAND
        paths = run_js("arg.map(([rows, lane]) => LOOKAHEAD_MOVES.map(m => lookaheadPath(rows, lane, m)))", grids)
        for (rows, lane), by_move in zip(grids, paths):
            values = bench.lookahead_values(tuple(rows), lane)
            for move, path in zip(bench.LOOKAHEAD_MOVES, by_move):
                with self.subTest(rows=rows, lane=lane, move=move):
                    self.assertEqual(len(path), values[move])
                    if path:
                        self.assertEqual(path[0], lane + bench.LOOKAHEAD_SHIFT[move])
                    for i, at in enumerate(path):
                        self.assertEqual(rows[i][at], ".")
                        self.assertTrue(i == 0 or abs(at - path[i - 1]) <= 1)

    def test_outcome_of_a_passed_block(self):                      # příklad z rešerše: left 2 (slepá ulička), stay 0 (náraz), right 4 (správně)
        got = run_js("[['ok', 2], ['bad', 0], ['bad', 1], ['bad', -1], ['late', undefined]].map(([r, c]) => lookaheadOutcome(r, c, arg, 1))"
                     ".concat([lookaheadOutcome('bad', 0, ['...', '...'], 0), lookaheadOutcome('bad', 2, ['...', '...'], 2)])", list(LOOKAHEAD_EXAMPLE))
        self.assertEqual(got, ["ok", "deadEnd", "crash", "invalid", "late", "crash", "crash"])        # mimo silnici je náraz

    def test_warm_task_is_fixed_valid_and_longest(self):
        import bench
        for lanes, warm in zip(range(3, 9), run_js("[3, 4, 5, 6, 7, 8].map(k => lookaheadWarm(k))", None)):
            rows, lane = tuple(warm["rows"]), warm["lane"]
            with self.subTest(lanes=lanes):
                self.assertEqual(list(warm), ["rows", "lane", "answer", "depth", "greedy", "truth"])
                self.assertEqual(len(rows), bench.LOOKAHEAD_ROWS)
                self.assertTrue(all(len(r) == lanes and r.count(".") == 1 for r in rows))           # nejvíc zablokovaných pruhů
                self.assertEqual(bench.lookahead_depth(rows, lane), (warm["answer"], warm["depth"]))
                self.assertEqual(bench.lookahead_values(rows, lane)[warm["answer"]], bench.LOOKAHEAD_ROWS)
                self.assertEqual(warm["greedy"], bench.lookahead_greedy(rows, lane))
                self.assertEqual(warm["truth"], bench.LOOKAHEAD_MOVES.index(warm["answer"]))
                longest = max(len(json.dumps(bench.lookahead_state(r, l, "blocked"))) for r, l, *_ in bench.lookahead_cases(20, lanes, seed=lanes))
                self.assertGreaterEqual(len(json.dumps(bench.lookahead_state(rows, lane, "blocked"))), longest)      # horní odhad délky dotazu


SPEC_RULE_LOG = [["color", True]] * 3 + [["color", False], ["shape", False]]     # příklad z rešerše (SPEC_LOG v tests/test_rule.py): přepnutí na count
C, W = True, False
# Generátor Skrytého pravidla stránky se stejnou náhodou jako ROWS_JS: seed, n → úlohy ruleRow.
RULE_ROWS_JS = ROWS_JS.replace("signRow(arg.lanes, rand)", "ruleRow(rand)")
assert RULE_ROWS_JS != ROWS_JS
RULE_PAGE = {}


def rule_page(n=3000, seed=1):
    """Úlohy generátoru stránky, jednou za běh modulu; deník jako [(znak, správně?)]."""
    if (n, seed) not in RULE_PAGE:
        RULE_PAGE[n, seed] = [{**r, "log": [tuple(e) for e in r["log"]]} for r in run_js(RULE_ROWS_JS, {"seed": seed, "n": n})]
    return RULE_PAGE[n, seed]


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Rule(unittest.TestCase):
    def test_constants_state_and_question_match_bench(self):
        import bench
        js = run_js("({ features: RULE_FEATURES, classes: RULE_CLASSES, streak: RULE_STREAK, log: RULE_LOG, short: RULE_SHORT, lengths: RULE_LENGTHS,"
                    " stick: RULE_STICK, tries: RULE_TRIES, insert: RULE_INSERT, question: ruleQuestion() })", None)
        self.assertEqual((js["features"], js["classes"], js["lengths"]), (list(bench.RULE_FEATURES), list(bench.RULE_CLASSES), list(bench.RULE_LENGTHS)))
        self.assertEqual((js["streak"], js["log"], js["short"], js["stick"], js["tries"], js["insert"]),
                         (bench.RULE_STREAK, list(bench.RULE_LOG), bench.RULE_SHORT, bench.RULE_STICK, bench.RULE_TRIES, bench.RULE_INSERT))
        self.assertEqual(json.dumps(js["question"]), json.dumps(bench.rule_question("labeled")))
        cases = bench.rule_cases(40, seed=3)
        tasks = [[[list(e) for e in log], [bench.rule_shared(sample, g)[0] for g in gates]] for log, sample, gates, *_ in cases]
        for case, state in zip(cases, run_js("arg.map(([log, order]) => ruleState(log, order))", tasks)):
            self.assertEqual(json.dumps(state), json.dumps(bench.rule_state(case, "labeled")))        # bajtově, i pořadí klíčů

    def test_alternative_wording_matches_bench(self):              # ALT stojí mimo sdílený blok: čte se ze zdrojáku stránky
        import bench
        page = (ROOT / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
        alt = re.search(r"\nconst ALT = \{[^\n]*\n(.*?)\n\};", page, re.S).group(1)
        self.assertEqual(re.findall(r"^  gate_labeled: '([^'\\]*)',$", alt, re.M), [bench.ALT["gate_labeled"]])
        self.assertNotIn("\n  gate: ", "\n" + alt)                 # podoba --matched-by je jen v bench.py

    def test_read_and_least_match_bench(self):
        import bench
        rng = random.Random(5)
        logs = [[[rng.choice(bench.RULE_FEATURES), rng.random() < 0.6] for _ in range(rng.randint(0, 10))] for _ in range(800)]
        logs += [SPEC_RULE_LOG, [["color", C]] * 3, [["color", C]] * 3 + [["shape", C]] * 3, [["color", C]] * 3 + [["shape", C]],
                 [["color", C], ["color", C], ["shape", W], ["color", C], ["color", C]], [["color", W], ["shape", W], ["color", W]],
                 [["shape", C]] * 3 + [["shape", W], ["count", W]]]                      # ručně ověřené deníky z tests/test_rule.py (Reading)
        for log, ([candidates, old, cls], least) in zip(logs, run_js("arg.map(log => [ruleRead(log), ruleLeast(log)])", logs)):
            entries = [tuple(e) for e in log]
            py_candidates, py_old, py_cls = bench.rule_read(entries)
            with self.subTest(log=log):
                self.assertEqual(candidates, [f for f in bench.RULE_FEATURES if f in py_candidates])      # pořadí RULE_FEATURES
                self.assertEqual((old, cls), (py_old, py_cls))
                self.assertEqual(least, bench.rule_least(entries))

    def test_changes_follow_the_streak(self):                      # pás deníku: svislá čára za pokusem, po kterém se pravidlo změnilo
        import bench
        hand = [SPEC_RULE_LOG, [["color", C]] * 6 + [["shape", W]], [["color", C], ["color", C], ["shape", W], ["color", C], ["color", C], ["color", C]],
                [["color", C]] * 3 + [["shape", C]], [["color", W]] * 4, []]
        self.assertEqual(run_js("arg.map(ruleChanges)", hand), [[2], [2, 5], [5], [2], [], []])
        rng = random.Random(8)
        logs = [[[rng.choice(bench.RULE_FEATURES), rng.random() < 0.7] for _ in range(rng.randint(1, 10))] for _ in range(300)]
        for log, changes in zip(logs, run_js("arg.map(ruleChanges)", logs)):
            old = bench.rule_read([tuple(e) for e in log])[1]
            self.assertEqual(old, log[changes[-1]][0] if changes else None, log)            # poslední změna je ta, kterou zná rule_read

    def test_generated_rows_keep_the_guarantees(self):
        import bench
        for r in rule_page():
            log, order = r["log"], r["order"]
            with self.subTest(case=r):
                self.assertEqual(list(r), ["log", "order", "truth", "cls", "old"])
                self.assertTrue(bench.RULE_LOG[0] <= len(log) <= bench.RULE_LOG[1])
                self.assertTrue(all(f in bench.RULE_FEATURES and isinstance(ok, bool) for f, ok in log))
                self.assertEqual(sorted(order), sorted(bench.RULE_FEATURES))                       # znaky bran jsou permutace
                candidates, old, cls = bench.rule_read(log)
                self.assertEqual(candidates, {order[r["truth"]]})                                   # odvozené pravidlo je jediné a je to pravidlo úlohy
                self.assertEqual(cls, r["cls"])
                if old is None:
                    self.assertIsNone(r["old"])
                else:
                    self.assertEqual(order[r["old"]], old)
                    self.assertNotEqual(r["old"], r["truth"])

    def test_classes_and_answers_are_spread(self):
        import bench
        c = rule_page()
        classes, tops = Counter(r["cls"] for r in c), Counter(r["truth"] for r in c)
        for cls in bench.RULE_CLASSES:
            self.assertTrue(0.25 <= classes[cls] / len(c) <= 0.42, (cls, classes))
        for top in range(3):
            self.assertTrue(0.25 <= tops[top] / len(c) <= 0.42, (top, tops))
        self.assertTrue(all(r["old"] is not None for r in c if r["cls"] == "switch"))
        self.assertTrue(all(r["old"] is None for r in c if r["cls"] == "eliminate"))
        for cls in bench.RULE_CLASSES:                                                             # délka deníku nezávisí na třídě
            sub = [r for r in c if r["cls"] == cls]
            short = sum(len(r["log"]) <= bench.RULE_SHORT for r in sub) / len(sub)
            self.assertTrue(0.4 <= short <= 0.6, (cls, short))

    def test_control_strategies(self):
        import bench
        c = rule_page()

        def acc(pick, cls=None):
            sub = [r for r in c if cls is None or r["cls"] == cls]
            return sum(pick(r) == r["truth"] for r in sub) / len(sub)

        def solve(r):
            return r["order"].index(next(iter(bench.rule_read(r["log"])[0])))

        def last_correct(r):                                       # zopakuj znak posledního správného pokusu; bez něj brána 1
            hits = [f for f, ok in r["log"] if ok]
            return r["order"].index(hits[-1]) if hits else 0
        self.assertEqual(acc(solve), 1)
        self.assertEqual(acc(last_correct, "repeat"), 1)
        self.assertEqual(acc(last_correct, "switch"), 0)
        self.assertTrue(all(last_correct(r) == r["old"] for r in c if r["cls"] == "switch"))      # v přepnutí vždy brána starého pravidla
        self.assertLessEqual(acc(last_correct, "eliminate"), 0.4)
        self.assertLess(acc(last_correct), 0.5)
        self.assertLess(acc(lambda r: r["order"].index(r["log"][-1][0])), 0.45)                 # zopakuj znak posledního pokusu
        self.assertLess(acc(lambda r: 0), 0.4)

    def test_least_frequent_feature_is_balanced_in_switch(self):
        import bench
        c = rule_page()

        def acc(cls):
            sub = [r for r in c if r["cls"] == cls]
            return sum(r["order"].index(bench.rule_least(r["log"])) == r["truth"] for r in sub) / len(sub)
        self.assertTrue(0.2 <= acc("switch") <= 0.45, acc("switch"))
        self.assertEqual(acc("eliminate"), 1)                     # bez změny je nezkoušený znak totéž co vyloučení

    def test_first_pick_is_not_a_shortcut_in_switch(self):
        import bench
        switch = [r for r in rule_page() if r["cls"] == "switch"]

        def acc(long=None):
            sub = [r for r in switch if long is None or (len(r["log"]) > bench.RULE_SHORT) == long]
            return sum(r["order"].index(r["log"][0][0]) == r["truth"] for r in sub) / len(sub)
        self.assertLessEqual(acc(long=True), 0.45)
        self.assertLessEqual(acc(), 0.6)

    def test_rule_features_are_spread(self):                       # brána jmenuje svůj znak: „vždy count“ nesmí ve třídě vycházet
        import bench
        c = rule_page()

        def shares(sub):
            rules = Counter(r["order"][r["truth"]] for r in sub)
            return {f: rules[f] / len(sub) for f in bench.RULE_FEATURES}
        for cls in ("repeat", "eliminate"):
            for f, share in shares([r for r in c if r["cls"] == cls]).items():
                self.assertTrue(0.25 <= share <= 0.42, (cls, f, share))
        switch = [r for r in c if r["cls"] == "switch"]
        for f, share in shares([r for r in switch if len(r["log"]) > bench.RULE_SHORT]).items():
            self.assertTrue(0.2 <= share <= 0.45, ("switch long", f, share))
        self.assertLessEqual(max(shares(switch).values()), 0.5, shares(switch))

    def test_same_seed_same_rows(self):
        first = run_js(RULE_ROWS_JS, {"seed": 7, "n": 50})
        self.assertEqual(run_js(RULE_ROWS_JS, {"seed": 7, "n": 50}), first)
        self.assertNotEqual(run_js(RULE_ROWS_JS, {"seed": 8, "n": 50}), first)

    def test_bounded_search_raises(self):                          # rand = 0: samé správné pokusy color, deník končí změnou, žádná třída
        got = run_js("[[() => 0, 'eliminate', 'short'], [Math.random, 'guess', 'short'], [Math.random, 'repeat', 'medium']]"
                     ".map(([rand, cls, length]) => { try { ruleCase(rand, cls, length); return null; } catch (e) { return e.message; } })", None)
        self.assertEqual(got, ["ruleCase: no eliminate short log after 10000 tries", "ruleCase: no guess short log", "ruleCase: no repeat medium log"])

    def test_summary_matches_bench(self):                          # i neplatné volby: do perseverace se nepočítají
        import bench
        cases, rng, keys = bench.rule_cases(300, seed=4), random.Random(6), bench.gate_keys(3)
        choices = [rng.choice(keys + keys + [None, "gate_4", 2]) for _ in cases]
        js_cases = [{"log": [list(e) for e in log], "truth": top, "cls": cls, "old": old} for log, _, _, top, cls, old in cases]
        got = run_js("ruleSummary(arg.cases, arg.choices, arg.keys)", {"cases": js_cases, "choices": choices, "keys": keys})
        self.assertEqual(json.dumps(got), json.dumps(bench.rule_summary(cases, choices, keys)))      # i pořadí klíčů
        self.assertEqual(run_js("ruleSummary([], [], arg)", keys)["persev"], [0, 0])                   # prázdná hra: stránka ukáže „—“

    def test_warm_task_is_fixed_valid_and_longest(self):
        import bench
        warm = run_js("ruleWarm()", None)
        log = [tuple(e) for e in warm["log"]]
        self.assertEqual(list(warm), ["log", "order", "truth", "cls", "old"])
        self.assertEqual(len(log), bench.RULE_LOG[1])
        self.assertTrue(all(ok for _, ok in log))                                                   # samé „correct“: nejdelší stav
        self.assertEqual(bench.rule_read(log), ({warm["order"][warm["truth"]]}, warm["order"][warm["old"]], warm["cls"]))
        self.assertEqual(warm["cls"], "repeat")
        state = run_js("ruleState(arg.log, arg.order)", warm)
        longest = max(len(json.dumps(bench.rule_state(c, "labeled"))) for c in bench.rule_cases(200, seed=2))
        self.assertGreaterEqual(len(json.dumps(state)), longest)                                    # horní odhad délky dotazu ve stepEstimate


QUEUE_HAND = [[["car", "car"], ["truck"], ["bus", "van"]], [["car", "van"], ["bus", "car"]], [["motorbike", "tractor"], ["car", "van", "bus"]],
              [["car"], ["van"], ["bus", "car"]], [["bus"], ["truck", "car"], ["van", "van", "van"]], [["bus", "car"], ["tractor"], ["truck", "van"]]]   # ručně ověřené fronty z tests/test_queue.py (Basics)

# Generátor Nejkratší fronty stránky se stejnou náhodou jako ROWS_JS: seed, lanes, weighted, n → řady queueRow.
QUEUE_ROWS_JS = ROWS_JS.replace("signRow(arg.lanes, rand)", "queueRow(arg.lanes, arg.weighted, rand)")
assert QUEUE_ROWS_JS != ROWS_JS
QUEUE_PAGE = {}


def queue_page(n, lanes, weighted, seed=1):
    """Řady generátoru stránky (jednou za běh modulu) a tytéž jako n-tice bench.py (fronty, pruh, třída, nejkratší text, nejméně vozidel)."""
    if (n, lanes, weighted, seed) not in QUEUE_PAGE:
        raw = run_js(QUEUE_ROWS_JS, {"seed": seed, "lanes": lanes, "weighted": weighted, "n": n})
        QUEUE_PAGE[n, lanes, weighted, seed] = raw, [(tuple(tuple(q) for q in r["queues"]), r["answer"], r["cls"], tuple(r["text"]), r["count"]) for r in raw]
    return QUEUE_PAGE[n, lanes, weighted, seed]


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Queue(unittest.TestCase):
    def test_constants_match_bench(self):
        import bench
        js = run_js("({ vehicles: QUEUE_VEHICLES, weights: QUEUE_WEIGHTS, size: QUEUE_SIZE, gaps: QUEUE_GAPS, share: QUEUE_COUNT_SHARE, tries: QUEUE_TRIES,"
                    " heavy: QUEUE_HEAVY, light: QUEUE_LIGHT, shapes: QUEUE_SHAPES, cum: QUEUE_SHAPE_CUM, keys: [2, 8].map(laneKeys),"
                    " textShare: [2, 3, 4, 8].map(queueTextShare) })", None)
        self.assertEqual(js["vehicles"], list(bench.QUEUE_VEHICLES))
        self.assertEqual(json.dumps(js["weights"]), json.dumps(bench.QUEUE_WEIGHTS))                 # i pořadí klíčů
        self.assertEqual((js["size"], js["gaps"]), (list(bench.QUEUE_SIZE), list(bench.QUEUE_GAPS)))
        self.assertEqual((js["share"], js["tries"]), (bench.QUEUE_COUNT_SHARE, bench.QUEUE_TRIES))
        self.assertEqual((js["heavy"], js["light"]), (list(bench.QUEUE_HEAVY), list(bench.QUEUE_LIGHT)))
        self.assertEqual(js["shapes"], [list(s) for s in bench.QUEUE_SHAPES])                       # pořadí rozhoduje o losování
        self.assertEqual(len(js["cum"]), len(bench.QUEUE_SHAPE_CUM))
        for a, b in zip(js["cum"], bench.QUEUE_SHAPE_CUM):
            self.assertTrue(math.isclose(a, b, rel_tol=1e-12), (a, b))
        self.assertEqual(js["keys"], [bench.lane_keys(2), bench.lane_keys(8)])
        self.assertEqual(js["textShare"], [bench.queue_text_share(k) for k in (2, 3, 4, 8)])

    def test_counting_matches_bench(self):                         # délky, nejkratší text, nejméně vozidel a těžkých, shoda = všechny pruhy
        import bench
        cases = bench.queue_cases(60, 4, seed=3) + bench.queue_cases(60, 6, True, seed=4) + bench.queue_cases(30, 2, seed=5)
        grids = [[list(q) for q in queues] for queues, *_ in cases] + QUEUE_HAND
        got = run_js("arg.map(qs => [qs.map(q => queueLength(q, false)), qs.map(q => queueLength(q, true)), queueText(qs), queueCount(qs),"
                     " queueHeavy(qs), queueFewest(qs.map(q => q.length))])", grids)
        for qs, (plain, weighted, text, count, heavy, fewest) in zip(grids, got):
            queues = tuple(tuple(q) for q in qs)
            with self.subTest(queues=queues):
                self.assertEqual(plain, [bench.queue_length(q) for q in queues])
                self.assertEqual(weighted, [bench.queue_length(q, True) for q in queues])
                self.assertEqual(text, list(bench.queue_text(queues)))
                self.assertEqual(count, bench.queue_count(queues))                                   # None = víc pruhů s nejméně vozidly
                self.assertEqual(heavy, list(bench.queue_heavy(queues)))
                self.assertEqual(fewest, list(bench.queue_fewest([len(q) for q in queues])))

    def test_class_matches_bench_and_raises(self):
        import bench
        got = run_js("[1, 2, 3, 4, 11].map(queueClass).concat([(() => { try { queueClass(0); return null; } catch (e) { return e.message; } })()])", None)
        self.assertEqual(got[:5], [bench.queue_class(g) for g in (1, 2, 3, 4, 11)])
        self.assertEqual(got[5], "queueClass: gap must be at least 1, got 0")                        # nic se neopravuje potichu

    def test_state_and_question_match_bench(self):
        import bench
        cases = bench.queue_cases(20, 4, seed=7) + bench.queue_cases(20, 8, True, seed=8)
        grids = [[list(q) for q in queues] for queues, *_ in cases] + QUEUE_HAND
        for qs, state in zip(grids, run_js("arg.map(queueState)", grids)):
            self.assertEqual(json.dumps(state), json.dumps(bench.queue_state(tuple(tuple(q) for q in qs))), qs)     # bajtově, i pořadí klíčů
        asked = run_js("[2, 3, 4, 5, 6, 7, 8].flatMap(k => [queueQuestion(k, false), queueQuestion(k, true)])", None)
        want = [bench.queue_question(k, w) for k in range(2, 9) for w in (False, True)]
        for js, py in zip(asked, want):
            self.assertEqual(json.dumps(js), json.dumps(py), next(iter(py)))

    def test_alternative_wording_and_lane_count_match_bench(self):     # ALT a ALT_LANES stojí mimo sdílený blok: čtou se ze zdrojáku stránky
        import bench
        page = (ROOT / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
        alt = re.search(r"\nconst ALT = \{[^\n]*\n(.*?)\n\};", page, re.S).group(1)
        for key in ("lane", "lane_weighted"):
            self.assertEqual(re.findall(r"^  %s: '([^'\\]*)',$" % key, alt, re.M), [bench.ALT[key]], key)
        self.assertEqual(re.search(r"\nconst ALT_LANES = /(.*?)/;", page).group(1), bench.ALT_LANES.pattern)      # obě větve jako v bench.py

    def test_random_helpers_follow_python(self):                  # cumWeights = accumulate, pickCum = bisect_right na rand · součet, shuffleWith od konce
        import bisect
        rs = [0, 0.1, 1 / 6, 0.5, 0.75, 0.9999]
        got = run_js("({ cum: cumWeights([1, 2, 3.5, 0, 4]), picks: arg.map(r => pickCum(() => r, ['a', 'b', 'c', 'd', 'e'], cumWeights([1, 2, 3.5, 0, 4]))),"
                     " shuffled: shuffleWith((() => { const s = [0.9, 0.1, 0.5, 0.3]; return () => s.shift(); })(), [0, 1, 2, 3, 4]) })", rs)
        cum = list(itertools.accumulate([1, 2, 3.5, 0, 4]))
        self.assertEqual(got["cum"], cum)
        self.assertEqual(got["picks"], ["abcde"[bisect.bisect(cum, r * cum[-1], 0, 4)] for r in rs])     # jako random.choices v CPythonu
        a, seq = [0, 1, 2, 3, 4], [0.9, 0.1, 0.5, 0.3]
        for i in range(4, 0, -1):
            j = math.floor(seq.pop(0) * (i + 1))
            a[i], a[j] = a[j], a[i]
        self.assertEqual(got["shuffled"], a)

    def test_class_tally_matches_bench(self):
        import bench
        rng, keys = random.Random(3), bench.lane_keys(4)
        tops = [[rng.randrange(4), rng.choice(bench.QUEUE_GAPS)] for _ in range(200)]
        choices = [rng.choice(keys + [None, "lane_9", 2]) for _ in tops]
        got = run_js("classTally(QUEUE_GAPS, arg.tops, arg.choices, arg.keys)", {"tops": tops, "choices": choices, "keys": keys})
        self.assertEqual(json.dumps(got), json.dumps(bench.class_tally(bench.QUEUE_GAPS, [tuple(t) for t in tops], choices, keys)))

    def test_generated_rows_keep_the_guarantees(self):            # jako tests/test_queue.py (Generator.test_cases_keep_the_guarantees)
        import bench
        for lanes, n in ((2, 300), (4, 900), (8, 120)):
            for weighted in (False, True):
                raw, cases = queue_page(n, lanes, weighted)
                for r, (queues, answer, cls, text, count) in zip(raw, cases):
                    with self.subTest(lanes=lanes, weighted=weighted, queues=queues):
                        self.assertEqual(list(r), ["queues", "answer", "cls", "text", "count", "truth"])
                        self.assertEqual(r["truth"], answer)
                        self.assertEqual(len(queues), lanes)
                        self.assertTrue(all(1 <= len(q) <= 12 and set(q) <= set(bench.QUEUE_VEHICLES) for q in queues))
                        lengths = [bench.queue_length(q, weighted) for q in queues]
                        rest = sorted(lengths[:answer] + lengths[answer + 1:])
                        self.assertLess(lengths[answer], rest[0])                         # jediná nejkratší fronta
                        self.assertEqual(bench.queue_class(rest[0] - lengths[answer]), cls)
                        self.assertEqual((text, count), (bench.queue_text(queues), bench.queue_count(queues)))
                        self.assertIsNotNone(count)
                        if not weighted:
                            self.assertEqual(count, answer)

    def test_classes_and_answers_are_spread(self):
        for weighted in (False, True):
            _, c = queue_page(900, 4, weighted)
            classes, answers = Counter(x[2] for x in c), Counter(x[1] for x in c)
            for cls in ("gap1", "gap2-3", "gap4+"):
                self.assertTrue(0.25 <= classes[cls] / 900 <= 0.42, (weighted, classes))
            for lane in range(4):
                self.assertGreaterEqual(answers[lane] / 900, 0.15, (weighted, answers))

    def test_shortest_text_is_balanced(self):                     # meze jako tests/test_queue.py (shoda = trefa)
        import bench
        from test_queue import fewest

        def share(c, cls):
            sub = [x for x in c if x[2] == cls]
            return sum(x[1] in fewest([len(json.dumps(list(q))) for q in x[0]]) for x in sub) / len(sub)
        for weighted in (False, True):
            for cls in ("gap1", "gap2-3"):
                with self.subTest(weighted=weighted, cls=cls):
                    self.assertTrue(0.2 <= share(queue_page(900, 4, weighted)[1], cls) <= 0.45)
                    self.assertTrue(0.3 <= share(queue_page(300, 2, weighted)[1], cls) <= 0.7)
        self.assertGreaterEqual(share(queue_page(900, 4, False)[1], "gap4+"), 0.95)
        weighted = queue_page(900, 4, True)[1]
        for cls in ("gap1", "gap2-3"):
            sub = [x for x in weighted if x[2] == cls]
            heavy = sum(x[1] in fewest([sum(v in bench.QUEUE_WEIGHTS for v in q) for q in x[0]]) for x in sub)
            with self.subTest(heavy=cls):
                self.assertTrue(0.2 <= heavy / len(sub) <= 0.45, heavy / len(sub))
        differ = sum(count != answer for _, answer, _, _, count in weighted) / 900
        self.assertTrue(0.4 <= differ <= 0.6, differ)                                          # nejméně vozidel je jinde v polovině úloh

    def test_same_seed_same_rows(self):
        first = run_js(QUEUE_ROWS_JS, {"seed": 7, "lanes": 4, "weighted": True, "n": 30})
        self.assertEqual(run_js(QUEUE_ROWS_JS, {"seed": 7, "lanes": 4, "weighted": True, "n": 30}), first)
        self.assertNotEqual(run_js(QUEUE_ROWS_JS, {"seed": 8, "lanes": 4, "weighted": True, "n": 30}), first)
        self.assertNotEqual(run_js(QUEUE_ROWS_JS, {"seed": 7, "lanes": 4, "weighted": False, "n": 30}), first)

    def test_bounded_search_raises(self):                          # jako tests/test_queue.py; tries = 0 místo mock.patch (Rozhodnutí F2)
        got = run_js("[[4, 'gap1', 0], [1, 'gap1'], [9, 'gap1'], [4, 'gap0']]"
                     ".map(([k, cls, tries]) => { try { queueCase(Math.random, k, cls, true, tries); return null; } catch (e) { return e.message; } })", None)
        self.assertEqual(got, ["queueCase: no gap1 task for 4 lanes after 0 tries", "queueCase: unsupported lanes 1 or class gap1",
                               "queueCase: unsupported lanes 9 or class gap1", "queueCase: unsupported lanes 4 or class gap0"])

    def test_errors_match_bench(self):                             # i neplatné volby; s vahami navíc nejkratší podle počtu
        import bench
        for weighted, lanes in ((False, 4), (True, 4), (True, 6)):
            cases = bench.queue_cases(90, lanes, weighted, seed=6)
            keys, rng = bench.lane_keys(lanes), random.Random(lanes + weighted)
            choices = [rng.choice(keys + keys + [None, "lane_9", 3, ["lane_1"], "LANE_1"]) for _ in cases]
            js_cases = [{"queues": [list(q) for q in qs], "answer": a, "cls": cls, "text": list(text), "count": count} for qs, a, cls, text, count in cases]
            got = run_js("queueErrors(arg.cases, arg.choices, arg.keys, arg.weighted)", {"cases": js_cases, "choices": choices, "keys": keys, "weighted": weighted})
            with self.subTest(weighted=weighted, lanes=lanes):
                self.assertEqual(json.dumps(got), json.dumps(bench.queue_errors(cases, choices, keys, weighted)))      # i pořadí klíčů
                self.assertGreater(got["invalid"], 0)

    def test_warm_task_is_fixed_valid_and_longest(self):
        import bench
        for lanes, warm in zip(range(2, 9), run_js("[2, 3, 4, 5, 6, 7, 8].map(k => queueWarm(k))", None)):
            queues = tuple(tuple(q) for q in warm["queues"])
            with self.subTest(lanes=lanes):
                self.assertEqual(list(warm), ["queues", "answer", "cls", "text", "count", "truth"])
                self.assertEqual(len(queues), lanes)
                for weighted in (False, True):
                    lengths = [bench.queue_length(q, weighted) for q in queues]
                    self.assertEqual([i for i, x in enumerate(lengths) if x == min(lengths)], [warm["answer"]])     # jediná nejkratší, s vahami i bez
                self.assertEqual((warm["truth"], warm["count"], list(warm["text"])), (lanes - 1, bench.queue_count(queues), list(bench.queue_text(queues))))
                longest = max(len(json.dumps(bench.queue_state(c[0]))) for w in (False, True) for c in bench.queue_cases(20, lanes, w, seed=lanes))
                self.assertGreaterEqual(len(json.dumps(bench.queue_state(queues))), longest)          # horní odhad délky dotazu ve stepEstimate


# Generátor Sázky na pruh stránky se stejnou náhodou jako ROWS_JS: seed, lanes (= počet bran), n → řady betRow.
BET_ROWS_JS = ROWS_JS.replace("signRow(arg.lanes, rand)", "betRow(arg.lanes, rand)")
assert BET_ROWS_JS != ROWS_JS
BET_PAGE = {}


def bet_page(n, gates, seed=1):
    """Řady generátoru stránky (jednou za běh modulu) a tytéž jako n-tice bench.bet_case."""
    if (n, gates, seed) not in BET_PAGE:
        raw = run_js(BET_ROWS_JS, {"seed": seed, "lanes": gates, "n": n})
        BET_PAGE[n, gates, seed] = raw, [(tuple(r["rewards"]), tuple(r["chances"]), r["answer"], r["level"], r["topReward"], r["topChance"])
                                         for r in raw]
    return BET_PAGE[n, gates, seed]


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Bet(unittest.TestCase):
    def test_constants_state_and_question_match_bench(self):
        import bench
        js = run_js("({ levels: BET_LEVELS, rewards: BET_REWARDS, same: BET_SAME_CHANCES, chances: BET_CHANCES, margin: BET_MARGIN,"
                    " share: BET_SHORTCUT_SHARE, tries: BET_TRIES,"
                    " top: [[10, 30, 20], [30, 30, 20], [50, 50, 50]].map(betTop), second: [[10, 30, 20], [40, 30, 30]].map(betSecond) })", None)
        self.assertEqual(js["levels"], list(bench.BET_LEVELS))
        self.assertEqual((js["rewards"], js["same"], js["chances"]), (list(bench.BET_REWARDS), list(bench.BET_SAME_CHANCES), list(bench.BET_CHANCES)))
        self.assertEqual((js["margin"], js["share"], js["tries"]), (bench.BET_MARGIN, bench.BET_SHORTCUT_SHARE, bench.BET_TRIES))
        self.assertEqual(js["top"], [bench.bet_top(v) for v in ((10, 30, 20), (30, 30, 20), (50, 50, 50))])
        self.assertEqual(js["second"], [list(bench.bet_second(v)) for v in ((10, 30, 20), (40, 30, 30))])
        tasks = [[list(rewards), list(chances)] for gates in range(3, 9) for rewards, chances, *_ in bench.bet_cases(5, gates, seed=gates)]
        for (rewards, chances), state in zip(tasks, run_js("arg.map(([r, c]) => betState(r, c))", tasks)):
            self.assertEqual(json.dumps(state), json.dumps(bench.bet_state(rewards, chances)))       # bajtově, i pořadí klíčů
        for gates, question in zip(range(3, 9), run_js("[3, 4, 5, 6, 7, 8].map(k => betQuestion(k))", None)):
            self.assertEqual(json.dumps(question), json.dumps(bench.bet_question(gates)), gates)

    def test_alternative_wording_matches_bench(self):              # ALT stojí mimo sdílený blok: čte se ze zdrojáku stránky
        import bench
        page = (ROOT / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
        alt = re.search(r"\nconst ALT = \{[^\n]*\n(.*?)\n\};", page, re.S).group(1)
        self.assertEqual(re.findall(r"^  bet: '([^'\\]*)',$", alt, re.M), [bench.ALT["bet"]])

    def test_generated_rows_keep_the_guarantees(self):             # záruky jako tests/test_bet.py (Generator.test_cases_keep_the_guarantees)
        import bench
        for gates, n in ((3, 900), (4, 900), (8, 300)):
            raw, cases = bet_page(n, gates)
            for r, (rewards, chances, answer, level, top_reward, top_chance) in zip(raw, cases):
                with self.subTest(gates=gates, rewards=rewards, chances=chances):
                    self.assertEqual(list(r), ["rewards", "chances", "answer", "level", "topReward", "topChance", "truth"])
                    self.assertEqual(r["truth"], answer)
                    self.assertEqual((len(rewards), len(chances)), (gates, gates))
                    self.assertTrue(set(rewards) <= set(bench.BET_REWARDS))
                    ev = [x * c for x, c in zip(rewards, chances)]
                    self.assertGreaterEqual(100 * ev[answer], 105 * max(ev[:answer] + ev[answer + 1:]))     # aspoň o 5 % víc než druhá
                    self.assertEqual((top_reward, top_chance), (bench.bet_top(rewards), bench.bet_top(chances)))
                    if level == "same":
                        self.assertEqual(len(set(chances)), 1)
                        self.assertIn(chances[0], bench.BET_SAME_CHANCES)
                        self.assertEqual((top_reward, top_chance), (answer, None))
                    elif level == "round":
                        self.assertEqual(sorted(chances), [50] * (gates - 1) + [100])
                        self.assertEqual(top_reward, answer)
                        self.assertNotEqual(top_chance, answer)
                    else:
                        self.assertEqual(level, "general")
                        self.assertTrue(set(chances) <= set(bench.BET_CHANCES))
                        self.assertNotIn(None, (top_reward, top_chance))

    def test_levels_and_answers_are_spread(self):
        import bench
        for gates, n, low in ((3, 900, 0.2), (8, 300, 0.05)):
            _, cases = bet_page(n, gates)
            levels, answers = Counter(x[3] for x in cases), Counter(x[2] for x in cases)
            for level in bench.BET_LEVELS:
                self.assertTrue(0.22 <= levels[level] / n <= 0.45, (gates, levels))
            for gate in range(gates):
                self.assertGreaterEqual(answers[gate] / n, low, (gates, answers))

    def test_general_shortcuts_are_balanced(self):                 # meze jako tests/test_bet.py: v general při K = 4 žádná zkratka nad 0,45
        general = [x for x in bet_page(1800, 4)[1] if x[3] == "general"]
        self.assertGreaterEqual(len(general), 550)

        def elimination(x):                                        # brána bez nejvyšší odměny i šance; víc kandidátů → vyšší odměna, shoda → dřívější
            rewards, _, _, _, top_reward, top_chance = x
            rest = [i for i in range(len(rewards)) if i not in (top_reward, top_chance)]
            return max(rest, key=lambda i: (rewards[i], -i))
        shortcuts = {"reward": lambda x: x[4] == x[2], "chance": lambda x: x[5] == x[2], "elimination": lambda x: elimination(x) == x[2],
                     "second": lambda x: x[0][x[2]] == sorted(x[0], reverse=True)[1]}
        for name, hit in shortcuts.items():
            with self.subTest(shortcut=name):
                self.assertLessEqual(sum(map(hit, general)) / len(general), 0.45)

    def test_same_seed_same_rows(self):
        first = run_js(BET_ROWS_JS, {"seed": 7, "lanes": 4, "n": 50})
        self.assertEqual(run_js(BET_ROWS_JS, {"seed": 7, "lanes": 4, "n": 50}), first)
        self.assertNotEqual(run_js(BET_ROWS_JS, {"seed": 8, "lanes": 4, "n": 50}), first)

    def test_bounded_search_raises(self):                          # rand = 0: samé odměny 20 a šance 20, marže nikdy nevyjde
        got = run_js("[[() => 0, 3, 'same'], [Math.random, 2, 'same'], [Math.random, 9, 'same'], [Math.random, 3, 'odd']]"
                     ".map(([rand, k, level]) => { try { betCase(rand, k, level); return null; } catch (e) { return e.message; } })", None)
        self.assertEqual(got, ["betCase: no same task for 3 gates after 10000 tries", "betCase: unsupported task for 2 gates and level same",
                               "betCase: unsupported task for 9 gates and level same", "betCase: unsupported task for 3 gates and level odd"])

    def test_summary_matches_bench(self):                          # i neplatné volby: regret 1, do chyb jako neplatné
        import bench
        cases = bench.bet_cases(300, 4, seed=4) + bench.bet_cases(120, 3, seed=3)
        rng = random.Random(6)
        keys4, keys3 = bench.gate_keys(4), bench.gate_keys(3)
        choices = [rng.choice((keys4 if len(c[0]) == 4 else keys3) * 2 + [None, "gate_9", 2, ["gate_1"]]) for c in cases]
        js_cases = [{"rewards": list(r), "chances": list(ch), "answer": a, "level": lv, "topReward": tr, "topChance": tc}
                    for r, ch, a, lv, tr, tc in cases]
        for part, keys in ((slice(0, 300), keys4), (slice(300, None), keys3)):
            got = run_js("betSummary(arg.cases, arg.choices, arg.keys)", {"cases": js_cases[part], "choices": choices[part], "keys": keys})
            py = bench.bet_summary(cases[part], choices[part], keys)
            self.assertEqual(list(got), list(py))                                                   # pořadí klíčů
            self.assertEqual(json.dumps(got["by_level"]), json.dumps(py["by_level"]))
            self.assertEqual(json.dumps(got["errors"]), json.dumps(py["errors"]))
            same(self, py["regret"], got["regret"], "regret")
            self.assertTrue(all(got["errors"][k] > 0 for k in ("reward", "chance", "invalid")), got)
        self.assertIsNone(run_js("betSummary([], [], arg).regret", keys4))                          # NaN → null v JSON: stránka ukáže „—“

    def test_error_kind_follows_the_summary_order(self):           # odměna před šancí, správná volba nikam, neplatná
        case = {"rewards": [300, 200, 100], "chances": [20, 60, 90], "answer": 1, "level": "general", "topReward": 0, "topChance": 2}
        got = run_js("['gate_2', 'gate_1', 'gate_3', null, 'gate_4', 1].map(c => betErrorKind(arg, c, gateKeys(3)))"
                     ".concat([betErrorKind({ ...arg, rewards: [100, 200, 300], topReward: 2, topChance: 2 }, 'gate_3', gateKeys(3)),"
                     " betErrorKind({ ...arg, chances: [50, 60, 50], topChance: 1, answer: 2 }, 'gate_1', gateKeys(3))])", case)
        self.assertEqual(got, [None, "reward", "chance", "invalid", "invalid", "invalid", "reward", "reward"])
        self.assertEqual(run_js("[betEv(120, 70), betEv(390, 95), betEv(30, 15)]", None), [84, 370.5, 4.5])

    def test_warm_task_is_fixed_valid_and_longest(self):
        import bench
        for gates, warm in zip(range(3, 9), run_js("[3, 4, 5, 6, 7, 8].map(k => betWarm(k))", None)):
            rewards, chances = tuple(warm["rewards"]), tuple(warm["chances"])
            with self.subTest(gates=gates):
                self.assertEqual(list(warm), ["rewards", "chances", "answer", "level", "topReward", "topChance", "truth"])
                self.assertEqual((len(rewards), len(chances)), (gates, gates))
                ev = [r * c for r, c in zip(rewards, chances)]
                self.assertGreaterEqual(100 * ev[0], bench.BET_MARGIN * max(ev[1:]))                 # správně brána 1 s marží
                self.assertEqual((warm["answer"], warm["truth"]), (0, 0))
                self.assertEqual((warm["topReward"], warm["topChance"]), (bench.bet_top(rewards), bench.bet_top(chances)))
                longest = max(len(json.dumps(bench.bet_state(r, c))) for r, c, *_ in bench.bet_cases(200, gates, seed=gates))
                self.assertGreaterEqual(len(json.dumps(bench.bet_state(rewards, chances))), longest)      # horní odhad délky dotazu


CRUISE_HAND = [[97, "icy", "none"], [130, "wet", "dense"], [19, "dry", "none"], [300, "dry", "light"], [8, "dry", "none"], [150, "icy", "dense"]]
# příklad ze specu Tempomatu, HAND_A a HAND_C z tests/test_cruise.py, kraje CRUISE_DIST a zahřívací úloha

# Generátor Tempomatu stránky se stejnou náhodou jako ROWS_JS: seed, n → úlohy cruiseRow.
CRUISE_ROWS_JS = ROWS_JS.replace("signRow(arg.lanes, rand)", "cruiseRow(rand)")
assert CRUISE_ROWS_JS != ROWS_JS
CRUISE_PAGE = {}


def cruise_page(n=3000, seed=1):
    """Úlohy generátoru stránky (jednou za běh modulu) a tytéž jako n-tice bench.py (m, road, fog, answer, cls, place)."""
    if (n, seed) not in CRUISE_PAGE:
        raw = run_js(CRUISE_ROWS_JS, {"seed": seed, "n": n})
        CRUISE_PAGE[n, seed] = raw, [(r["m"], r["road"], r["fog"], r["answer"], r["cls"], r["place"]) for r in raw]
    return CRUISE_PAGE[n, seed]


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Cruise(unittest.TestCase):
    def test_constants_and_tables_match_bench(self):
        import bench
        js = run_js("({ top: CRUISE_TOP, bands: CRUISE_BANDS, dist: CRUISE_DIST, edge: CRUISE_EDGE, road: CRUISE_ROAD, fog: CRUISE_FOG,"
                    " classes: CRUISE_CLASSES, places: CRUISE_PLACES, weights: CRUISE_WEIGHTS, ins: CRUISE_INS })", None)
        self.assertEqual((js["top"], js["bands"], js["dist"], js["edge"]),
                         (bench.CRUISE_TOP, list(bench.CRUISE_BANDS), list(bench.CRUISE_DIST), bench.CRUISE_EDGE))
        self.assertEqual(json.dumps(js["road"]), json.dumps(bench.CRUISE_ROAD))           # i pořadí: cruise_case hledá slovo podle srážky
        self.assertEqual(json.dumps(js["fog"]), json.dumps(bench.CRUISE_FOG))
        self.assertEqual((js["classes"], js["places"]), (list(bench.CRUISE_CLASSES), list(bench.CRUISE_PLACES)))
        self.assertEqual(list(js["weights"]), list(bench.CRUISE_WEIGHTS))
        for cls, combos in bench.CRUISE_WEIGHTS.items():
            self.assertEqual(js["weights"][cls], [[list(k), w] for k, w in combos.items()], cls)     # pořadí rozhoduje o losování
        self.assertEqual(js["ins"], bench.CRUISE_INS)
        ms = list(range(0, 401))
        got = run_js("({ band: arg.map(bandLevel), edge: arg.map(nearEdge), neighbor: arg.filter(m => m >= 8 && m <= 300).map(neighborLevel),"
                     " range: [1, 2, 3, 4, 5, 6, 7].map(bandRange) })", ms)
        self.assertEqual(got["band"], [bench.band_level(m) for m in ms])
        self.assertEqual(got["edge"], [bench.near_edge(m) for m in ms])
        self.assertEqual(got["neighbor"], [bench.neighbor_level(m) for m in range(8, 301)])
        self.assertEqual(got["range"], [list(bench.band_range(k)) for k in range(1, 8)])
        combos = [[m, road, fog] for m in range(8, 301, 3) for road in bench.CRUISE_ROAD for fog in bench.CRUISE_FOG]
        self.assertEqual(run_js("arg.map(([m, road, fog]) => cruiseLevel(m, road, fog))", combos),
                         [bench.cruise_level(*c) for c in combos])                               # bez ořezu, i pod 1

    def test_state_question_and_choice_level_match_bench(self):
        import bench
        tasks = CRUISE_HAND + [list(c[:3]) for c in bench.cruise_cases(60, seed=5)]
        for task, state in zip(tasks, run_js("arg.map(([m, road, fog]) => cruiseState(m, road, fog))", tasks)):
            self.assertEqual(json.dumps(state), json.dumps(bench.cruise_state(*task)), task)  # bajtově, i pořadí klíčů
        self.assertEqual(json.dumps(run_js("cruiseQuestion()", None)), json.dumps(bench.cruise_question()))
        self.assertEqual(run_js("levelKeys()", None), bench.level_keys())
        picks = [f"level_{k}" for k in range(0, 9)] + [None, "3", 3, ["level_1"], "Level 3", "level_03", True]
        self.assertEqual(run_js("arg.map(choiceLevel)", picks), [bench.choice_level(p) for p in picks])

    def test_alternative_wording_matches_bench(self):              # ALT stojí mimo sdílený blok: čte se ze zdrojáku stránky
        import bench
        page = (ROOT / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
        alt = re.search(r"\nconst ALT = \{[^\n]*\n(.*?)\n\};", page, re.S).group(1)
        self.assertEqual(re.findall(r"^  speed: '([^'\\]*)',$", alt, re.M), [bench.ALT["speed"]])

    def test_generated_rows_keep_the_guarantees(self):             # jako tests/test_cruise.py (záruky, podmínky třídy, poloha u hranice)
        import bench
        from test_cruise import BANDS, band_level_check
        raw, cases = cruise_page()
        classes = {"table": (False, False), "road": (True, False), "fog": (False, True), "both": (True, True)}
        for r, (m, road, fog, answer, cls, place) in zip(raw, cases):
            with self.subTest(r=r):
                self.assertEqual(list(r), ["m", "road", "fog", "answer", "cls", "place", "truth"])
                self.assertIsInstance(m, int)
                self.assertTrue(8 <= m <= 300)
                self.assertTrue(1 <= answer <= 7)
                self.assertEqual(answer, band_level_check(m) - bench.CRUISE_ROAD[road] - bench.CRUISE_FOG[fog])
                self.assertEqual(r["truth"], answer - 1)
                self.assertEqual((road != "dry", fog != "none"), classes[cls])
                self.assertEqual(any(b - 3 <= m <= b + 2 for b in BANDS), place == "edge")
                self.assertIn((band_level_check(m), bench.CRUISE_ROAD[road], bench.CRUISE_FOG[fog]), bench.CRUISE_WEIGHTS[cls])   # jen kombinace s vahou

    def test_classes_and_places_are_spread(self):                  # meze jako tests/test_cruise.py
        import bench
        _, cases = cruise_page()
        classes = Counter(x[4] for x in cases)
        for cls in bench.CRUISE_CLASSES:
            sub = [x for x in cases if x[4] == cls]
            with self.subTest(cls=cls):
                self.assertTrue(0.22 <= classes[cls] / len(cases) <= 0.28, classes)
                self.assertTrue(0.45 <= sum(x[5] == "edge" for x in sub) / len(sub) <= 0.55)

    def test_cheap_shortcuts_are_balanced(self):                   # stejné zkratky a meze jako tests/test_cruise.py
        import bench
        from test_cruise import band_level_check, cheap_shortcuts
        _, cases = cruise_page()
        for cls in ("road", "fog", "both"):
            sub = [x for x in cases if x[4] == cls]
            for name, guess in cheap_shortcuts().items():
                with self.subTest(cls=cls, shortcut=name):
                    self.assertLessEqual(sum(guess(x) == x[3] for x in sub) / len(sub), 0.35)
        for cls, want in (("table", 1.0), ("road", 0.0), ("fog", 0.0), ("both", 0.0)):
            sub = [x for x in cases if x[4] == cls]
            self.assertEqual(sum(band_level_check(x[0]) == x[3] for x in sub) / len(sub), want, cls)      # jen vzdálenost
        for x in (x for x in cases if x[4] == "both"):                                    # vynechaná mlha / vynechaný povrch nikdy netrefí
            band, p, f = band_level_check(x[0]), bench.CRUISE_ROAD[x[1]], bench.CRUISE_FOG[x[2]]
            self.assertNotIn(x[3], (band - p, band - f), x)

    def test_same_seed_same_rows(self):
        first = run_js(CRUISE_ROWS_JS, {"seed": 7, "n": 50})
        self.assertEqual(run_js(CRUISE_ROWS_JS, {"seed": 7, "n": 50}), first)
        self.assertNotEqual(run_js(CRUISE_ROWS_JS, {"seed": 8, "n": 50}), first)

    def test_bad_arguments_and_a_broken_guarantee_raise(self):     # jako tests/test_cruise.py: nic se neopravuje potichu
        got = run_js("[['floor', 'edge'], ['table', 'near'], ['toString', 'edge']]"
                     ".map(([cls, place]) => { try { cruiseCase(Math.random, cls, place); return null; } catch (e) { return e.message; } })", None)
        self.assertEqual(got, ["cruiseCase: unsupported class floor or place edge", "cruiseCase: unsupported class table or place near",
                               "cruiseCase: unsupported class toString or place edge"])
        broken = run_js("(() => { CRUISE_WEIGHTS.both = [[[1, 1, 1], 1]];"                 # stupeň 1 − 2 je pod 1
                        " try { cruiseCase(Math.random, 'both', 'middle'); return null; } catch (e) { return e.message; } })()", None)
        self.assertEqual(broken, "cruiseCase: generated task breaks its guarantees")

    def test_summary_matches_bench(self):                          # i neplatné odpovědi: v MAE počítají 6, do rozboru jako neplatné
        import bench
        from test_cruise import HAND, HAND_LEVELS
        cases, rng = bench.cruise_cases(300, seed=4), random.Random(6)
        levels = [c[3] if i < 40 else rng.choice([1, 2, 3, 4, 5, 6, 7, None]) for i, c in enumerate(cases)]
        cases, levels = cases + HAND, levels + HAND_LEVELS                                # ruční úlohy s každým druhem chyby
        js_cases = [{"m": m, "road": road, "fog": fog, "answer": a, "cls": cls, "place": place} for m, road, fog, a, cls, place in cases]
        got = run_js("cruiseSummary(arg.cases, arg.levels)", {"cases": js_cases, "levels": levels})
        py = bench.cruise_summary(cases, levels)
        same(self, got, py)
        self.assertEqual([list(got), list(got["scale"]), list(got["errors"])], [list(py), list(py["scale"]), list(py["errors"])])   # pořadí klíčů
        self.assertTrue(all(v > 0 for v in got["errors"].values()), got["errors"])
        empty = run_js("cruiseSummary([], [])", None)
        self.assertEqual(empty["scale"], {"within": None, "mae": None, "fast": 0, "slow": 0})      # NaN: prázdná hra ukáže „—“

    def test_warm_task_is_fixed_valid_and_longest(self):
        import bench
        warm = run_js("cruiseWarm()", None)
        self.assertEqual(list(warm), ["m", "road", "fog", "answer", "cls", "place", "truth"])
        self.assertEqual(warm["answer"], bench.cruise_level(warm["m"], warm["road"], warm["fog"]))
        self.assertEqual((warm["truth"], warm["place"] == "edge"), (warm["answer"] - 1, bench.near_edge(warm["m"])))
        self.assertIn((bench.band_level(warm["m"]), bench.CRUISE_ROAD[warm["road"]], bench.CRUISE_FOG[warm["fog"]]), bench.CRUISE_WEIGHTS[warm["cls"]])
        longest = max(len(json.dumps(bench.cruise_state(*c[:3]))) for c in bench.cruise_cases(300, seed=2))
        self.assertGreaterEqual(len(json.dumps(bench.cruise_state(warm["m"], warm["road"], warm["fog"]))), longest)   # horní odhad délky dotazu


def timetable_js(case):
    """Úloha bench.py (n-tice timetable_case) jako objekt úlohy stránky."""
    rows, order, z, service, exception, answer, size, pos, rule = case
    return {"rows": [list(r) for r in rows], "order": list(order), "zip": z, "service": service,
            "exception": list(exception) if exception else None, "answer": answer, "size": size, "pos": pos, "rule": rule}


# timetableTable stránky se stejnou náhodou jako ROWS_JS: seed, lanes = počet řádků, n tabulek.
TIMETABLE_TABLE_JS = ROWS_JS.replace("signRow(arg.lanes, rand)", "timetableTable(rand, arg.lanes)")
assert TIMETABLE_TABLE_JS != ROWS_JS

# Generátor Jízdního řádu stránky se stejnou náhodou jako ROWS_JS: seed, n → úlohy timetableRow.
TIMETABLE_ROWS_JS = ROWS_JS.replace("signRow(arg.lanes, rand)", "timetableRow(rand)")
assert TIMETABLE_ROWS_JS != ROWS_JS
TIMETABLE_PAGE = {}


def timetable_case_of(r):
    """Úloha stránky jako n-tice timetable_case (řádky, pořadí, PSČ, služba, výjimka, depo, velikost, poloha, výjimka)."""
    return (tuple(tuple(x) for x in r["rows"]), tuple(r["order"]), r["zip"], r["service"], tuple(r["exception"]) if r["exception"] else None,
            r["answer"], r["size"], r["pos"], r["rule"])


def timetable_page(n=900, seed=1):
    """Úlohy generátoru stránky (jednou za běh modulu) a tytéž jako n-tice bench.py."""
    if (n, seed) not in TIMETABLE_PAGE:
        raw = run_js(TIMETABLE_ROWS_JS, {"seed": seed, "n": n})
        TIMETABLE_PAGE[n, seed] = raw, [timetable_case_of(r) for r in raw]
    return TIMETABLE_PAGE[n, seed]


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Timetable(unittest.TestCase):
    def test_constants_and_question_match_bench(self):
        import bench
        js = run_js("({ depots: TIMETABLE_DEPOTS, services: TIMETABLE_SERVICES, exc: TIMETABLE_EXC_SERVICES, zip: TIMETABLE_ZIP, width: TIMETABLE_MIN_WIDTH,"
                    " sizes: TIMETABLE_SIZES, positions: TIMETABLE_POSITIONS, rules: TIMETABLE_RULES, near: TIMETABLE_NEAR, inner: TIMETABLE_INNER,"
                    " positionRules: TIMETABLE_POSITION_RULES, tries: TIMETABLE_TRIES, question: timetableQuestion() })", None)
        self.assertEqual((js["depots"], js["services"], js["exc"]),
                         (list(bench.TIMETABLE_DEPOTS), list(bench.TIMETABLE_SERVICES), list(bench.TIMETABLE_EXC_SERVICES)))
        self.assertEqual((js["zip"], js["width"], js["near"], js["inner"], js["tries"]),
                         (list(bench.TIMETABLE_ZIP), bench.TIMETABLE_MIN_WIDTH, bench.TIMETABLE_NEAR, bench.TIMETABLE_INNER, bench.TIMETABLE_TRIES))
        self.assertEqual(json.dumps(js["sizes"]), json.dumps({k: list(v) for k, v in bench.TIMETABLE_SIZES.items()}))     # i pořadí tříd
        self.assertEqual((js["positions"], js["rules"], js["positionRules"]),
                         (list(bench.TIMETABLE_POSITIONS), list(bench.TIMETABLE_RULES), list(bench.TIMETABLE_POSITION_RULES)))
        self.assertEqual(json.dumps(js["question"]), json.dumps(bench.timetable_question()))          # bajtově, i pořadí dep

    def test_index_and_solver_by_hand(self):                       # jako tests/test_timetable.py (Basics.test_row_and_solve_by_hand)
        import bench
        from test_timetable import ROWS
        spans = [[a, b] for a, b, _ in ROWS]
        got = run_js("[10000, 81333, 81334, 87678, 87679, 99999].map(z => timetableIndex(arg, z))"
                     ".concat([[arg, 9999], [arg, 100000], [[[10000, 50000], [50000, 99999]], 50000]]"
                     ".map(([s, z]) => { try { timetableIndex(s, z); return null; } catch (e) { return e.message; } }))", spans)
        self.assertEqual(got, [0, 0, 1, 1, 2, 2, "timetableIndex: zip 9999 is in 0 rows", "timetableIndex: zip 100000 is in 0 rows",
                               "timetableIndex: zip 50000 is in 2 rows"])
        state = {"parcel": {"zip": 81334, "service": "express"},
                 "routes": [{"zip_from": a, "zip_to": b, "depot": d} for a, b, d in ROWS], "exceptions": []}
        states = [state, {**state, "parcel": {"zip": 81333, "service": "express"}}, {**state, "exceptions": [{"service": "express", "depot": "oak"}]},
                  {**state, "exceptions": [{"service": "cooled", "depot": "oak"}]}, {**state, "routes": state["routes"][::-1]}]
        self.assertEqual(run_js("arg.map(timetableSolve)", states), [bench.timetable_solve(s) for s in states])
        self.assertEqual(run_js("arg.map(timetableSolve)", states), ["maple", "birch", "oak", "maple", "maple"])      # kraje včetně, výjimka přebíjí

    def test_table_keeps_the_guarantees(self):                     # jako tests/test_timetable.py (Basics.test_table_covers_the_range_without_gaps)
        import bench
        for n in range(3, 13):
            for rows in run_js(TIMETABLE_TABLE_JS, {"seed": n, "lanes": n, "n": 30}):
                with self.subTest(n=n, rows=rows):
                    self.assertEqual(len(rows), n)
                    self.assertEqual((rows[0][0], rows[-1][1]), (10000, 99999))
                    self.assertTrue(all(rows[i][1] + 1 == rows[i + 1][0] for i in range(n - 1)))
                    self.assertGreaterEqual(min(b - a + 1 for a, b, _ in rows), 300)
                    self.assertTrue(all(rows[i][2] != rows[i + 1][2] for i in range(n - 1)))
                    self.assertTrue({d for _, _, d in rows} <= set(bench.TIMETABLE_DEPOTS))
        crawl = "let x = 0; const rand = () => (x = (x + 1e-6) % 1);"      # sousední řezy: interval užší než 300 v každém pokusu
        self.assertEqual(run_js("(() => {" + crawl + " try { timetableTable(rand, 5); return null; } catch (e) { return e.message; } })()", None),
                         "timetableTable: no table of 5 rows after 1000 tries")

    def test_state_matches_bench(self):
        import bench
        from test_timetable import APPLIES, DECOY
        cases = bench.timetable_cases(90, seed=3) + [DECOY, APPLIES, DECOY[:4] + (None, "maple", "small", "near", "none")]
        for case, state in zip(cases, run_js("arg.map(timetableState)", [timetable_js(c) for c in cases])):
            self.assertEqual(json.dumps(state), json.dumps(bench.timetable_state(case, "sorted")), case)      # bajtově, i pořadí klíčů

    def test_alternative_wording_matches_bench(self):              # ALT stojí mimo sdílený blok: čte se ze zdrojáku stránky; apostrofy → dvojité uvozovky
        import bench
        page = (ROOT / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
        alt = re.search(r"\nconst ALT = \{[^\n]*\n(.*?)\n\};", page, re.S).group(1)
        self.assertEqual(re.findall(r'^  depot: "([^"\\]*)",$', alt, re.M), [bench.ALT["depot"]])

    def test_generated_rows_keep_the_guarantees(self):             # jako tests/test_timetable.py (Generator.test_cases_keep_the_guarantees)
        import bench
        from test_timetable import position, route_index, solve
        raw, cases = timetable_page()
        for r, case in zip(raw, cases):
            rows, order, z, service, exception, answer, size, pos, rule = case
            with self.subTest(case=case):
                self.assertEqual(list(r), ["rows", "order", "zip", "service", "exception", "answer", "size", "pos", "rule", "truth"])
                self.assertEqual(sorted(order), list(range(len(rows))))
                lo, hi = bench.TIMETABLE_SIZES[size]
                self.assertTrue(lo <= len(rows) <= hi)
                self.assertEqual((rows[0][0], rows[-1][1]), (10000, 99999))
                self.assertTrue(all(rows[k][1] + 1 == rows[k + 1][0] and rows[k][2] != rows[k + 1][2] for k in range(len(rows) - 1)))
                self.assertGreaterEqual(min(b - a + 1 for a, b, _ in rows), 300)
                i = route_index(rows, z)
                self.assertEqual(position(rows, i, z), pos)
                near = {rows[j][2] for j in (i - 1, i + 1) if 0 <= j < len(rows)}
                self.assertIn(service, bench.TIMETABLE_SERVICES)
                if rule == "none":
                    self.assertIsNone(exception)
                else:
                    self.assertIn(exception[0], bench.TIMETABLE_EXC_SERVICES)
                    self.assertNotIn(exception[1], near | {rows[i][2]})
                    if rule == "applies":
                        self.assertEqual(service, exception[0])
                    else:
                        self.assertIn(service, bench.TIMETABLE_EXC_SERVICES)
                        self.assertNotEqual(service, exception[0])
                self.assertEqual(answer, exception[1] if rule == "applies" else rows[i][2])
                self.assertEqual(solve(bench.timetable_state(case, "sorted")), answer)
                self.assertEqual(r["truth"], bench.TIMETABLE_DEPOTS.index(answer))

    def test_classes_answers_and_sides_are_spread(self):           # meze jako tests/test_timetable.py
        import bench
        from test_timetable import route_index
        _, c = timetable_page()
        for k, order in ((6, bench.TIMETABLE_SIZES), (7, bench.TIMETABLE_POSITIONS), (8, bench.TIMETABLE_RULES)):
            counts = Counter(x[k] for x in c)
            for cls in order:
                self.assertTrue(0.25 <= counts[cls] / len(c) <= 0.42, counts)
        answers = Counter(x[5] for x in c)
        for depot in bench.TIMETABLE_DEPOTS:
            self.assertTrue(0.10 <= answers[depot] / len(c) <= 0.25, answers)
        edge = [x for x in c if x[7] != "inner"]
        low = sum(x[2] - x[0][route_index(x[0], x[2])][0] <= bench.TIMETABLE_NEAR for x in edge)      # u zip_from, ne u zip_to
        self.assertTrue(0.4 <= low / len(edge) <= 0.6, low / len(edge))

    def test_table_shortcuts_stay_well_below_the_answer(self):     # první řádek a nejčastější depo nejvýš 0,45 (podoba sorted)
        import bench
        from test_timetable import expected_hit, most_frequent
        _, c = timetable_page()
        for pos in bench.TIMETABLE_POSITIONS:
            sub = [x for x in c if x[7] == pos and x[8] != "applies"]
            first = sum(expected_hit(x[5], {x[0][0][2]}) for x in sub) / len(sub)
            frequent = sum(expected_hit(x[5], most_frequent(x[0])) for x in sub) / len(sub)
            with self.subTest(pos=pos):
                self.assertLessEqual(first, 0.45)
                self.assertLessEqual(frequent, 0.45)

    def test_a_boundary_is_one_of_two_rows(self):                  # u hranice je správně jeden ze dvou sousedních řádků, laťka 0,5
        import bench
        from test_timetable import exclusive, expected_hit, nearest
        _, c = timetable_page()
        near = [x for x in c if x[7] == "near" and x[8] != "applies"]
        for x in near:
            self.assertEqual(expected_hit(x[5], nearest(x[0], x[2], 0)) + expected_hit(x[5], nearest(x[0], x[2], 1)), 1, x)
        for end in (0, 1):
            share = sum(expected_hit(x[5], nearest(x[0], x[2], end)) for x in near) / len(near)
            self.assertTrue(0.35 <= share <= 0.65, (end, share))
        for pos, want in (("inner", 1), ("near", 1), ("bound", 0)):                     # hranice bez rovnosti padá jen na hranici
            sub = [x for x in c if x[7] == pos and x[8] != "applies"]
            self.assertEqual(sum(exclusive(bench.timetable_state(x, "sorted")) == x[5] for x in sub) / len(sub), want, pos)

    def test_exception_is_balanced(self):
        import bench
        from test_timetable import always_exception, never_exception
        with_exception = [x for x in timetable_page()[1] if x[4] is not None]
        for model in (always_exception, never_exception):
            share = sum(model(bench.timetable_state(x, "sorted")) == x[5] for x in with_exception) / len(with_exception)
            self.assertTrue(0.4 <= share <= 0.6, (model.__name__, share))

    def test_same_seed_same_rows(self):
        first = run_js(TIMETABLE_ROWS_JS, {"seed": 7, "n": 40})
        self.assertEqual(run_js(TIMETABLE_ROWS_JS, {"seed": 7, "n": 40}), first)
        self.assertNotEqual(run_js(TIMETABLE_ROWS_JS, {"seed": 8, "n": 40}), first)

    def test_unknown_classes_raise(self):
        got = run_js("[['huge', 'inner', 'none'], ['small', 'edge', 'none'], ['small', 'inner', 'maybe']]"
                     ".map(([s, p, r]) => { try { timetableCase(Math.random, s, p, r); return null; } catch (e) { return e.message; } })", None)
        self.assertEqual(got, ["timetableCase: unknown class huge, inner, none", "timetableCase: unknown class small, edge, none",
                               "timetableCase: unknown class small, inner, maybe"])

    def test_summary_matches_bench(self):                          # i neplatné volby: jiný typ, jiné velikost písmen, nic
        import bench
        cases, rng = bench.timetable_cases(300, seed=4), random.Random(6)
        depots = list(bench.TIMETABLE_DEPOTS)
        choices = [rng.choice(depots + depots + [None, "Oak", "depot_1", 3]) for _ in cases]
        js = [timetable_js(c) for c in cases]
        got = run_js("timetableSummary(arg.cases, arg.choices)", {"cases": js, "choices": choices})
        self.assertEqual(json.dumps(got), json.dumps(bench.timetable_summary(cases, choices)))      # i pořadí klíčů
        kinds = run_js("arg.cases.map((c, i) => timetableErrorKind(c, arg.choices[i]))", {"cases": js, "choices": choices})
        self.assertEqual(Counter(k for k in kinds if k), Counter({k: v for k, v in bench.timetable_errors(cases, choices).items() if v}))
        self.assertEqual(run_js("timetableSummary([], [])", None)["by_rule"]["applies"], [0, 0])     # prázdná hra: stránka ukáže „—“

    def test_warm_task_is_fixed_valid_and_longest(self):
        import bench
        warm = run_js("timetableWarm()", None)
        case = timetable_case_of(warm)
        self.assertEqual(list(warm), ["rows", "order", "zip", "service", "exception", "answer", "size", "pos", "rule", "truth"])
        self.assertEqual(len(case[0]), 12)
        self.assertEqual((case[0][0][0], case[0][-1][1]), (10000, 99999))
        self.assertTrue(all(case[0][k][1] + 1 == case[0][k + 1][0] and case[0][k][2] != case[0][k + 1][2] for k in range(11)))
        self.assertEqual(bench.timetable_solve(bench.timetable_state(case, "sorted")), warm["answer"])
        self.assertEqual((warm["rule"], warm["truth"]), ("decoy", bench.TIMETABLE_DEPOTS.index(warm["answer"])))
        longest = max(len(json.dumps(bench.timetable_state(c, "sorted"))) for c in bench.timetable_cases(300, seed=2))
        self.assertGreaterEqual(len(json.dumps(bench.timetable_state(case, "sorted"))), longest)      # horní odhad délky dotazu ve stepEstimate


# Generátor Rušivé nápovědy stránky se stejnou náhodou jako ROWS_JS: seed, lanes, n trojic → 3n řad hintTrio (set = číslo trojice).
HINT_ROWS_JS = ROWS_JS.replace("[...Array(arg.n)].map(() => signRow(arg.lanes, rand))", "[...Array(arg.n)].map((_, i) => hintTrio(rand, arg.lanes, i)).flat()")
assert HINT_ROWS_JS != ROWS_JS
HINT_PAGE = {}


def hint_page(n, lanes, seed=1):
    """Řady generátoru stránky, jednou za běh modulu."""
    if (n, lanes, seed) not in HINT_PAGE:
        HINT_PAGE[n, lanes, seed] = run_js(HINT_ROWS_JS, {"seed": seed, "lanes": lanes, "n": n})
    return HINT_PAGE[n, lanes, seed]


def py_hint(row):
    """Řada stránky jako úloha hint_cases: (čísla, správná brána, třída, podmínka, nápověda, číslo řady)."""
    return (tuple(row["values"]), row["answer"], row["gap"], row["cond"], row["hint"], row["set"])


def no_nan(x):
    """Výsledek rozboru z Pythonu tak, jak ho vrátí JSON ze stránky: NaN = null, n-tice = seznam."""
    if isinstance(x, float) and x != x:
        return None
    if isinstance(x, dict):
        return {k: no_nan(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [no_nan(v) for v in x]
    return x


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Hint(unittest.TestCase):
    def test_constants_state_and_question_match_bench(self):
        import bench
        js = run_js("({ gaps: NUMBER_GAPS, conds: HINT_CONDS, field: HINT_FIELD, questions: [2, 3, 4, 5, 6, 7, 8].map(hintQuestion) })", None)
        self.assertEqual((js["gaps"], js["conds"], js["field"]), (list(bench.GAPS), list(bench.HINT_CONDS), bench.HINT_FIELD))
        for k, q in zip(range(2, 9), js["questions"]):
            self.assertEqual(json.dumps(q), json.dumps(bench.hint_question(k)), k)          # bajtově, i pořadí klíčů
        cases = bench.hint_cases(40, 4, seed=3)
        states = run_js("arg.map(([values, hint]) => hintState(values, hint))", [[list(c[0]), c[4]] for c in cases])
        for c, state in zip(cases, states):
            self.assertEqual(json.dumps(state), json.dumps(bench.hint_state(c[0], c[4])))     # nápověda poslední klíč, none bez pole

    def test_rows_come_in_trios_like_bench(self):        # záruky jako tests/test_hint.py (Generator) a meze tříd jako number_case
        import bench
        for lanes in (2, 3, 4, 8):
            rows = hint_page(900, lanes, seed=lanes)
            with self.subTest(lanes=lanes):
                self.assertEqual(len(rows), 2700)
                self.assertEqual(Counter(r["cond"] for r in rows), {"none": 900, "congruent": 900, "incongruent": 900})
                trios = {}
                for r in rows:
                    trios.setdefault(r["set"], []).append(r)
                    v, top, second = r["values"], max(r["values"]), sorted(r["values"])[-2]
                    self.assertEqual(len(v), lanes)
                    self.assertEqual((v.count(top), v.index(top), r["truth"]), (1, r["answer"], r["answer"]))
                    self.assertTrue(20 <= second <= 5000, v)
                    lo, hi = {"tight": (1, max(1, second * 5 // 100)), "medium": (second * 5 // 100 + 1, second * 30 // 100),
                              "clear": (second * 30 // 100 + 1, second * 3)}[r["gap"]]
                    self.assertTrue(lo <= top - second <= hi, r)
                    self.assertTrue(all(1 <= x <= second for x in sorted(v)[:-2]), v)
                    if r["cond"] == "none":
                        self.assertIsNone(r["hint"])
                    elif r["cond"] == "congruent":
                        self.assertEqual(r["hint"], r["answer"])
                    else:
                        self.assertTrue(0 <= r["hint"] < lanes and r["hint"] != r["answer"], r)
                self.assertEqual(sorted(trios), list(range(900)))
                for trio in trios.values():
                    self.assertEqual(sorted(r["cond"] for r in trio), sorted(bench.HINT_CONDS))
                    self.assertEqual(len({(tuple(r["values"]), r["answer"], r["gap"]) for r in trio}), 1)     # stejná čísla ve trojici
                self.assertEqual(Counter(trios[s][0]["cond"] for s in trios).keys(), set(bench.HINT_CONDS))     # pořadí ve trojici se losuje

    def test_answers_gaps_wrong_hints_and_strategies_like_bench(self):     # meze tests/test_hint.py (K = 3, 900 řad)
        import bench
        rows = hint_page(900, 3, seed=3)
        none = [r for r in rows if r["cond"] == "none"]
        answers, gaps = Counter(r["answer"] for r in none), Counter(r["gap"] for r in none)
        for gate in range(3):
            self.assertAlmostEqual(answers[gate] / 900, 1 / 3, delta=0.07)
        for gap in bench.GAPS:
            self.assertTrue(0.25 <= gaps[gap] / 900 <= 0.42, gaps)
        inc = [r for r in rows if r["cond"] == "incongruent"]
        self.assertTrue(0.4 <= sum(r["values"][r["hint"]] == sorted(r["values"])[-2] for r in inc) / len(inc) <= 0.6)
        share = lambda pick, cond=None: (lambda sub: sum(pick(r) == r["answer"] for r in sub) / len(sub))(
            [r for r in rows if cond is None or r["cond"] == cond])
        follow = lambda r: 0 if r["hint"] is None else r["hint"]
        anti = lambda r: 1 if r["hint"] == 0 else 0
        self.assertEqual((share(follow, "congruent"), share(follow, "incongruent")), (1, 0))
        self.assertTrue(0.4 <= share(anti, "incongruent") <= 0.6)
        self.assertLess(share(anti), 0.34)
        self.assertLess(share(lambda r: 0), 0.4)

    def test_same_seed_same_rows(self):
        self.assertEqual(hint_page(30, 3, seed=5), run_js(HINT_ROWS_JS, {"seed": 5, "lanes": 3, "n": 30}))
        self.assertNotEqual(hint_page(30, 3, seed=5), run_js(HINT_ROWS_JS, {"seed": 6, "lanes": 3, "n": 30}))

    def test_summary_matches_bench(self):                 # stejné úlohy, stejné volby: stejný rozbor (i neplatné volby, chybějící none, nic)
        import bench
        from test_hint import CASES                       # ručně ověřené úlohy z tests/test_hint.py
        rng, keys = random.Random(7), bench.gate_keys(3)
        rows = hint_page(900, 3, seed=3)
        hand = [{"values": list(c[0]), "answer": c[1], "gap": c[2], "cond": c[3], "hint": c[4], "set": c[5]} for c in CASES]
        bad = [None, "gate_9", 7, "GATE_1", ["gate_1"]]
        choices = [rng.choice(keys + bad) if rng.random() < 0.3 else keys[r["hint"]] if r["hint"] is not None and rng.random() < 0.5
                   else keys[r["answer"]] for r in rows]
        part = [i for i, r in enumerate(rows) if not (r["cond"] == "none" and r["set"] % 3 == 0)]      # trojice bez řady none
        runs = [(rows, choices), ([rows[i] for i in part], [choices[i] for i in part]), ([], []),
                (hand, ["gate_2", "gate_2", "gate_3", "gate_2", "gate_1", "gate_1"]), (hand, [None] * 6)]
        got = run_js("arg.runs.map(([cases, choices]) => hintSummary(cases, choices, arg.keys))", {"runs": runs, "keys": keys})
        for (cases, ch), js in zip(runs, got):
            want = bench.hint_summary([py_hint(c) for c in cases], ch, keys)
            same(self, js, no_nan(want), "hint_summary")                             # celá čísla v JSON stránky jsou 1, v Pythonu 1.0

    def test_points_match_bench(self):                    # pp: znaménko vždy, půlka k sudému jako round v Pythonu, NaN pomlčka
        import bench
        values = [0.125, -0.125, 0.135, -0.135, 0.0, -0.0, 0.5, 1 / 3, -1 / 3, 0.005, -0.005, 0.015, 0.025, 1.0, -1.0]
        values += [k / n - j / n for n in (1, 2, 3, 7, 8, 16, 40, 100, 200) for k in range(n + 1) for j in range(0, n + 1, 3)]
        values += [random.Random(i).uniform(-1, 1) for i in range(300)]
        self.assertEqual(run_js("arg.map(pointsText)", values), [bench.pp(v) for v in values])
        self.assertEqual(run_js("[pointsText(NaN), pointsText(null)]", None), ["—", "—"])

    def test_warm_task_is_valid_and_longest(self):        # zahřívací úloha: jediné maximum v poslední bráně, nejdelší stav ze všech úloh bench.py
        import bench
        for k, w in zip(range(2, 9), run_js("[2, 3, 4, 5, 6, 7, 8].map(hintWarm)", None)):
            v = w["values"]
            self.assertEqual((v.count(max(v)), v.index(max(v)), w["truth"], w["answer"]), (1, k - 1, k - 1, k - 1))
            self.assertTrue(w["hint"] is not None and w["hint"] != w["answer"])
            longest = max(len(json.dumps(bench.hint_state(c[0], c[4]))) for c in bench.hint_cases(300, k))
            self.assertGreaterEqual(len(json.dumps(bench.hint_state(tuple(v), w["hint"]))), longest, k)


# Generátor Co se změnilo stránky se stejnou náhodou jako ROWS_JS: seed, lanes, n → úlohy diffRow.
DIFF_ROWS_JS = ROWS_JS.replace("signRow(arg.lanes, rand)", "diffRow(arg.lanes, rand)")
assert DIFF_ROWS_JS != ROWS_JS
DIFF_PAGE = {}


def diff_page(n, lanes, seed=1):
    """Úlohy generátoru stránky, jednou za běh modulu."""
    if (n, lanes, seed) not in DIFF_PAGE:
        DIFF_PAGE[n, lanes, seed] = run_js(DIFF_ROWS_JS, {"seed": seed, "lanes": lanes, "n": n})
    return DIFF_PAGE[n, lanes, seed]


def py_diff(row):
    """Úloha stránky jako úloha diff_cases: (before, after, answer, cls, mode)."""
    return (tuple(row["before"]), tuple(row["after"]), row["answer"], row["cls"], row["mode"])


@unittest.skipUnless(shutil.which("node"), "program node není v PATH")
class Diff(unittest.TestCase):
    def test_constants_and_relatives_match_bench(self):
        import bench
        js = run_js("({ words: DIFF_WORDS, numbers: DIFF_NUMBERS, classes: DIFF_CLASSES, modes: DIFF_MODES, share: DIFF_REPEAT_SHARE,"
                    " tries: DIFF_TRIES, near: DIFF_NEIGHBOURS, far: DIFF_FAR, pools: [2, 3, 4, 5, 6, 7, 8].map(diffPoolPairs) })", None)
        self.assertEqual((js["words"], js["numbers"]), (list(bench.DIFF_WORDS), list(bench.DIFF_NUMBERS)))
        self.assertEqual((js["classes"], js["modes"]), (list(bench.DIFF_CLASSES), list(bench.DIFF_MODES)))
        self.assertEqual((js["share"], js["tries"]), (bench.DIFF_REPEAT_SHARE, bench.DIFF_TRIES))
        self.assertEqual(json.dumps(js["near"]), json.dumps({w: list(v) for w, v in bench.DIFF_NEIGHBOURS.items()}))      # i pořadí: losuje se z nich
        self.assertEqual(json.dumps(js["far"]), json.dumps({w: list(v) for w, v in bench.DIFF_FAR.items()}))
        self.assertEqual(js["pools"], [bench.diff_pool_pairs(k) for k in range(2, 9)])

    def test_pairs_keep_the_class_relation(self):        # jako tests/test_diff.py (Pairs): 2000× na třídu, vztah platí z obou stran
        import bench
        pairs = run_js(ROWS_JS.replace("[...Array(arg.n)].map(() => signRow(arg.lanes, rand))",
                                       "DIFF_CLASSES.map(cls => [...Array(arg.n)].map(() => diffPair(rand, cls)))"), {"seed": 2, "lanes": 0, "n": 2000})
        for cls, got in zip(bench.DIFF_CLASSES, pairs):
            with self.subTest(cls=cls):
                self.assertTrue(all(bench.diff_related(cls, a, b) for a, b in got))
                self.assertTrue(0.4 <= sum(a < b for a, b in got) / len(got) <= 0.6)            # pořadí ve dvojici 1 : 1
        values = [[2408, 2409], [2409, 2408], [2408, 2418], [1234, 5678], ["wear", "pear"], ["stem", "lane"], ["road", "load"], ["tape", "cape"]]
        got = run_js("arg.map(([a, b]) => [diffDistance(a, b), ...DIFF_CLASSES.map(c => diffRelated(c, a, b))])", values)
        for (a, b), row in zip(values, got):
            want = [bench.diff_distance(a, b)] + [False if c == "num1" and isinstance(a, str) else bench.diff_related(c, a, b) for c in bench.DIFF_CLASSES]
            self.assertEqual(row, want, (a, b))                                               # num1 u slov v Pythonu nejde (rozdíl řetězců)

    def test_state_and_question_match_bench(self):        # ve hře jen podoba lists
        import bench
        cases = bench.diff_cases(60, 5, seed=4)
        states = run_js("arg.map(([b, a]) => diffState(b, a))", [[list(c[0]), list(c[1])] for c in cases])
        for c, state in zip(cases, states):
            self.assertEqual(json.dumps(state), json.dumps(bench.diff_state(c[0], c[1], "lists")))
        for k, q in zip(range(2, 9), run_js("[2, 3, 4, 5, 6, 7, 8].map(diffQuestion)", None)):
            self.assertEqual(json.dumps(q), json.dumps(bench.diff_question(k, "lists")), k)

    def test_alternative_wording_matches_bench(self):      # ALT stojí mimo sdílený blok: čte se ze zdrojáku stránky
        import bench
        page = (ROOT / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
        alt = re.search(r"\nconst ALT = \{[^\n]*\n(.*?)\n\};", page, re.S).group(1)
        self.assertEqual(re.findall(r"^  changed_list: '([^'\\]*)',$", alt, re.M), [bench.ALT["changed_list"]])
        self.assertNotIn("\n  changed: ", "\n" + alt)        # podoba dicts je jen v bench.py

    def test_rows_keep_the_guarantees_like_bench(self):   # záruky a rozložení jako tests/test_diff.py (Generator)
        import bench
        for lanes, n in ((2, 2000), (4, 4000), (8, 2000)):
            rows = diff_page(n, lanes)
            with self.subTest(lanes=lanes):
                for r in rows:
                    before, after, answer, cls = tuple(r["before"]), tuple(r["after"]), r["answer"], r["cls"]
                    self.assertEqual((len(before), len(after), r["truth"]), (lanes, lanes, answer))
                    self.assertEqual([i for i in range(lanes) if before[i] != after[i]], [answer])      # jediná správná odpověď
                    self.assertTrue(bench.diff_related(cls, before[answer], after[answer]), r)
                    if cls in ("num1", "numnew"):
                        self.assertTrue(all(type(v) is int and 1000 <= v <= 9999 for v in before + after), r)
                    else:
                        self.assertTrue(all(v in bench.DIFF_WORDS for v in before + after), r)
                    if r["mode"] == "plain":
                        self.assertEqual((len(set(before)), len(set(after))), (lanes, lanes))
                classes, modes = Counter(r["cls"] for r in rows), Counter(r["mode"] for r in rows)
                for cls in bench.DIFF_CLASSES:
                    self.assertTrue(0.2 <= classes[cls] / n <= 0.3, classes)
                self.assertLessEqual(abs(modes["repeat"] / n - bench.DIFF_REPEAT_SHARE), 0.05, modes)
        answers = Counter(r["answer"] for r in diff_page(4000, 4))
        self.assertTrue(all(abs(answers[i] / 4000 - 0.25) <= 0.05 for i in range(4)), answers)

    def test_twins_and_shortcuts_like_bench(self):        # dvojčata jako diff_twins; zkratky z jednoho snímku u náhody (meze tests/test_diff.py)
        import bench
        from test_diff import NUMBERS_CASE, WORDS_CASE
        rows = diff_page(4000, 4)
        hand = [{"before": list(c[0]), "after": list(c[1]), "answer": c[2], "cls": c[3], "mode": c[4]} for c in (NUMBERS_CASE, WORDS_CASE)]
        twins = run_js("arg.map(diffTwins)", rows + hand)
        self.assertEqual(twins, [sorted(bench.diff_twins(py_diff(r))) for r in rows + hand])
        repeat = [t for t, r in zip(twins, rows) if r["mode"] == "repeat"]
        self.assertTrue(0.6 <= sum(map(bool, repeat)) / len(repeat) <= 0.8)
        self.assertFalse(any(t for t, r in zip(twins, rows) if r["mode"] == "plain"))

        def repeated(row):
            count = Counter(row)
            return {i for i, v in enumerate(row) if count[v] > 1}
        shortcuts = {"first": lambda b, a: {0}, "last": lambda b, a: {len(a) - 1}, "repeated_after": lambda b, a: repeated(a),
                     "repeated_before": lambda b, a: repeated(b), "unique_after": lambda b, a: set(range(len(a))) - repeated(a),
                     "max_after": lambda b, a: {a.index(max(a))} if type(a[0]) is int else set(),
                     "first_text_after": lambda b, a: {i for i, v in enumerate(a) if str(v) == min(map(str, a))}}

        def hit(r, pick):                                  # očekávaná trefa: z kandidátů náhodně, prázdná množina = hádání
            found = pick(r["before"], r["after"])
            return 1 / len(r["before"]) if not found else (1 / len(found) if r["answer"] in found else 0)
        for cls in bench.DIFF_CLASSES:
            sub = [r for r in rows if r["cls"] == cls]
            for name, pick in shortcuts.items():
                with self.subTest(cls=cls, shortcut=name):
                    self.assertLessEqual(sum(hit(r, pick) for r in sub) / len(sub), 0.25 + 0.08)
        new_value = lambda b, a: {i for i in range(len(a)) if a[i] not in b}     # srovnání množin: plain ho pustí, repeat láme
        for mode, low, high in (("plain", 1, 1), ("repeat", 0, 0.8)):
            sub = [r for r in rows if r["mode"] == mode]
            self.assertTrue(low <= sum(hit(r, new_value) for r in sub) / len(sub) <= high, mode)

    def test_same_seed_same_rows_and_bounds(self):
        self.assertEqual(diff_page(30, 6, seed=3), run_js(DIFF_ROWS_JS, {"seed": 3, "lanes": 6, "n": 30}))
        self.assertNotEqual(diff_page(30, 6, seed=3), run_js(DIFF_ROWS_JS, {"seed": 4, "lanes": 6, "n": 30}))
        calls = ["diffCase(Math.random, 4, 'num1', 'plain', 0)", "diffCase(Math.random, 1, 'num1', 'plain')", "diffCase(Math.random, 9, 'num1', 'plain')",
                 "diffCase(Math.random, 4, 'odd', 'plain')", "diffCase(Math.random, 4, 'num1', 'odd')", "diffPair(Math.random, 'odd')"]
        got = run_js("arg.map(c => { try { eval(c); return null; } catch (e) { return e.message; } })", calls)
        self.assertEqual(got, ["diffCase: no num1 plain task for 4 lanes after 0 tries", "diffCase: unsupported 1 lanes, class num1, mode plain",
                               "diffCase: unsupported 9 lanes, class num1, mode plain", "diffCase: unsupported 4 lanes, class odd, mode plain",
                               "diffCase: unsupported 4 lanes, class num1, mode odd", "diffPair: unknown class odd"])

    def test_summary_matches_bench(self):                 # stejné úlohy, stejné volby: stejný rozbor (i neplatné volby a úlohy bench.py)
        import bench
        from test_diff import NUMBERS_CASE, WORDS_CASE
        rng, keys = random.Random(9), bench.lane_keys(4)
        rows = diff_page(4000, 4) + [{"before": list(c[0]), "after": list(c[1]), "answer": c[2], "cls": c[3], "mode": c[4]}
                                     for c in bench.diff_cases(200, 4, seed=8) + [NUMBERS_CASE, WORDS_CASE]]
        bad = [None, "lane_9", 3, ["lane_1"], "LANE_1"]
        choices = [rng.choice(keys + bad) if rng.random() < 0.3 else keys[r["answer"]] if rng.random() < 0.4 else rng.choice(keys) for r in rows]
        runs = [(rows, choices), (rows[:1], choices[:1]), ([], []), (rows[-2:], ["lane_2", "lane_2"])]     # dvojče před sousedem
        got = run_js("arg.runs.map(([cases, choices]) => diffSummary(cases, choices, arg.keys))", {"runs": runs, "keys": keys})
        for (cases, ch), js in zip(runs, got):
            self.assertEqual(json.dumps(js, sort_keys=True), json.dumps(bench.diff_summary([py_diff(c) for c in cases], ch, keys), sort_keys=True))
        self.assertEqual(got[3]["errors"], {"twin": 1, "neighbor": 1, "other": 0, "invalid": 0})

    def test_warm_task_is_valid_and_longest(self):        # zahřívací úloha: plain, word1 v posledním pruhu, nejdelší stav ze všech úloh bench.py
        import bench
        for k, w in zip(range(2, 9), run_js("[2, 3, 4, 5, 6, 7, 8].map(diffWarm)", None)):
            before, after = tuple(w["before"]), tuple(w["after"])
            self.assertEqual([i for i in range(k) if before[i] != after[i]], [k - 1])
            self.assertEqual((w["answer"], w["truth"], w["cls"], w["mode"]), (k - 1, k - 1, "word1", "plain"))
            self.assertTrue(bench.diff_related("word1", before[-1], after[-1]) and all(v in bench.DIFF_WORDS for v in before + after))
            self.assertEqual((len(set(before)), len(set(after))), (k, k))
            longest = max(len(json.dumps(bench.diff_state(c[0], c[1], "lists"))) for c in bench.diff_cases(300, k))
            self.assertGreaterEqual(len(json.dumps(bench.diff_state(before, after, "lists"))), longest, k)


if __name__ == "__main__":
    unittest.main()
