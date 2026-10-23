"""Shoda výpočtů, které žijí dvakrát: v Pythonu a v index.html.

Blok mezi značkami „sdíleno s Pythonem“ se z index.html vytáhne a pustí v node nad stejnými daty jako Python.
Před něj jde blok textů (značky „texty“): věty chyb validátoru bere sdílený blok z něj funkcí t(), v jazyce stránky.
Bez programu node se testy přeskočí."""
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


# Generátor paměťové stopy stránky se stejnou náhodou jako ROWS_JS: seed, gates, n → řady memoryRow.
MEMORY_ROWS_JS = ROWS_JS.replace("signRow(arg.lanes, rand)", "memoryRow(arg.gates, rand)")
assert MEMORY_ROWS_JS != ROWS_JS


if __name__ == "__main__":
    unittest.main()
