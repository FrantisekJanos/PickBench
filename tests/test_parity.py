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


if __name__ == "__main__":
    unittest.main()
