"""Výstup bench.py a hwbench.py pro lokální modely se nesmí změnit ani o znak.

Zlaté soubory v tests/golden/ vznikly z kódu před napojením hostovaných modelů (master na 19b9449).
Příkazy běží s podstrčenou Ollamou (urllib.request.urlopen) a s podstrčenými hodinami (time.perf_counter),
takže výstup je pokaždé stejný. Síť, Ollama ani GPU se nepoužijí.

Anglický výstup (--lang en) má vlastní zlaté soubory v tests/golden/en/ pod stejnými jmény.

Nové zlaté soubory:  python tests/test_golden.py --write        (jen když se český výstup mění záměrně)
                     python tests/test_golden.py --write-usage  (jen česká nápověda)
                     python tests/test_golden.py --write-en     (jen anglické: výstupy i nápověda)
"""
import contextlib
import io
import json
import os
import re
import runpy
import sys
import time
import unittest
import urllib.request
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
GOLDEN = Path(__file__).parent / "golden"
sys.path.insert(0, str(ROOT))

BENCH = {                                            # jméno zlatého souboru → argumenty bench.py
    "bench-lanes": ["12", "m1", "m2"],
    "bench-lanes-volby": ["12", "--lanes", "4", "--mirror", "--inject", "--deadline-ms", "9", "m1"],
    "bench-numbers": ["10", "--numbers", "m1"],
    "bench-emails": ["20", "--emails", str(ROOT / "datasets" / "cz-firemni-emaily.json"), "m1"],
    "bench-public": ["--public", str(ROOT / "tests" / "public-mini.json"), "m1"],
    "bench-signs": ["12", "--signs", "--lanes", "4", "--mirror", "--inject", "m1"],
    "bench-signs-pairs": ["12", "--signs", "--pairs", "--deadline-ms", "9", "m1", "m2"],
    "bench-memory": ["16", "--memory", "--mirror", "--inject", "--deadline-ms", "9", "m1", "m2"],       # bez --lanes: drží i výchozí 4 brány
    "bench-memory-count": ["12", "--memory", "--count", "--lanes", "3", "m1"],
    "bench-barrier": ["24", "--barrier", "--mirror", "--inject", "--deadline-ms", "9", "m1", "m2"],     # 24 úloh: objeví se všech pět pravidel
    "bench-barrier-plain": ["24", "--barrier", "m1"],
    "bench-lookahead": ["12", "--lookahead", "--mirror", "--inject", "--deadline-ms", "9", "m1", "m2"],     # bez --lanes a bez podoby: výchozí
    "bench-lookahead-rows": ["12", "--lookahead", "--rows", "--lanes", "3", "m1"],
    "bench-lookahead-blocked": ["12", "--lookahead", "--blocked", "--lanes", "6", "m1"],
    "bench-rule": ["24", "--rule", "--mirror", "--inject", "--deadline-ms", "9", "m1", "m2"],              # bez podoby: výchozí
    "bench-rule-matched-by": ["12", "--rule", "--matched-by", "m1"],
    "bench-rule-labeled": ["12", "--rule", "--labeled", "m1"],
    "bench-queue": ["12", "--queue", "--mirror", "--inject", "--deadline-ms", "9", "m1", "m2"],          # bez --lanes: výchozí 4 pruhy
    "bench-queue-weighted": ["12", "--queue", "--weighted", "--lanes", "6", "m1"],
    "bench-bet": ["12", "--bet", "--mirror", "--inject", "--deadline-ms", "9", "m1", "m2"],              # bez --lanes: výchozí 4 brány
    "bench-bet-lanes": ["12", "--bet", "--lanes", "5", "m1"],
    "bench-hint": ["12", "--hint", "--deadline-ms", "9", "m1", "m2"],                                  # 12 řad = 36 dotazů na model, bez --lanes: 3 brány
    "bench-hint-mirror": ["12", "--hint", "--mirror", "--lanes", "4", "m1"],
    "bench-cruise": ["12", "--cruise", "--as-choice", "m1"],
    "bench-cruise-mirror": ["12", "--cruise", "--as-choice", "--mirror", "--inject", "--deadline-ms", "9", "m1", "m2"],
    "bench-cruise-score": ["12", "--cruise", "--as-score", "--mirror", "--inject", "--deadline-ms", "9", "m1"],
    "bench-timetable": ["18", "--timetable", "--mirror", "--inject", "--deadline-ms", "9", "m1", "m2"],      # bez podoby: výchozí
    "bench-timetable-shuffled": ["12", "--timetable", "--shuffled", "m1"],
    "bench-diff": ["12", "--diff", "--mirror", "--inject", "--deadline-ms", "9", "m1", "m2"],            # bez --lanes a bez podoby: výchozí 8 pruhů, seznamy
    "bench-diff-lists": ["12", "--diff", "--lists", "--lanes", "6", "m1"],
    "bench-pattern": ["12", "--pattern", "--deadline-ms", "9", "m1", "m2"],      # bez podoby: výchozí
    "bench-pattern-counter": ["12", "--pattern", "--counter", "m1"],
    "bench-pattern-mirror": ["12", "--pattern", "--mirror", "--inject", "m1"],
}
HWBENCH = {                                          # jméno zlatého souboru → argumenty hwbench.py
    "hwbench-latency": ["latency", "--n", "20", "m1"],
    "hwbench-questions": ["questions", "m1", "m2"],
    "hwbench-context": ["context", "m1"],
    "hwbench-cache": ["cache", "--n", "3", "m1", "m2"],
    "hwbench-repeat": ["repeat", "--n", "2", "m1", "m2"],
}
USAGE = ("bench", "hwbench", "fetch_datasets")       # nástroje, jejichž nápověda (spuštění bez argumentů) má zlatý soubor <jméno>-usage.txt
ENGLISH = [*BENCH, *HWBENCH]                         # příkazy, které už mají anglický zlatý soubor v tests/golden/en/
USAGE_EN = ("bench", "hwbench", "fetch_datasets")    # nástroje, které už mají anglickou nápovědu
LANG_ENV = "PICKBENCH_LANG"
CZECH = re.compile(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„“]")
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
OPTION = re.compile(r"--[a-z][a-z0-9-]*")


def lang_args(lang):
    """Volba jazyka na konec argumentů (tím se zároveň ověřuje, že smí stát kdekoli); česky žádná, jako dosud."""
    return [] if lang == "cs" else ["--lang", lang]


def usage(tool, lang="cs"):
    """Text, kterým nástroj skončí, když ho někdo pustí bez argumentů."""
    with mock.patch.dict(os.environ), mock.patch.object(sys, "argv", [f"{tool}.py", *lang_args(lang)]):
        os.environ.pop(LANG_ENV, None)               # jazyk z prostředí vývojáře tu nesmí rozhodovat
        try:
            runpy.run_path(str(ROOT / f"{tool}.py"), run_name="__main__")
        except SystemExit as e:
            return str(e.code) + "\n"
    raise AssertionError(f"{tool}.py bez argumentů neskončil nápovědou")


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def fake_urlopen(req, timeout=None):
    """Podstrčená Ollama: odpověď závisí jen na těle dotazu (kontrolní součet), takže je pokaždé stejná."""
    body = req.data
    doc, seed = json.loads(body), zlib.crc32(body)
    answers = {}
    for i, (key, question) in enumerate(doc["questions"].items()):
        if question.get("type") == "score":                 # otázka score: očekávaný index stupně OD NULY (jako Ollama), legend, confidence
            labels = question["criteria"]
            answers[key] = {"type": "score", "score": ((seed + i) % (10 * len(labels) - 9)) / 10,
                            "legend": {str(k): label for k, label in enumerate(labels)}, "confidence": 0.5 + (seed % 50) / 100}
            continue
        if question.get("type") == "noul":                  # otázka ano/ne: pravděpodobnost „ano“ 0.00–1.00, bez confidence (jako Ollama)
            answers[key] = {"noul": ((seed + i) % 101) / 100}
            continue
        options = list(question["criteria"])
        answers[key] = {"choice": options[(seed + i) % len(options)], "confidence": 0.5 + (seed % 50) / 100}
    out = {"model": doc["model"], "answers": answers, "usage": {"input_tokens": len(body) // 4, "output_tokens": 1}}
    return FakeResponse(json.dumps(out).encode())


class Clock:
    """Podstrčené hodiny: každé čtení poskočí o 5 až 17 ms podle pořadí čtení."""

    def __init__(self):
        self.t, self.k = 0.0, 0

    def __call__(self):
        self.k += 1
        self.t += 0.005 + 0.001 * (self.k % 13)
        return self.t


def capture(name, lang="cs", sent=None):
    """Celý výstup jednoho příkazu jako text. sent = seznam, do kterého se sbírají těla dotazů, jak odešla do modelu."""
    def urlopen(req, timeout=None):
        if sent is not None:
            sent.append(req.data)
        return fake_urlopen(req, timeout)
    with mock.patch.dict(os.environ), mock.patch.object(urllib.request, "urlopen", urlopen), mock.patch.object(time, "perf_counter", Clock()):
        os.environ.pop(LANG_ENV, None)               # jazyk z prostředí vývojáře tu nesmí rozhodovat
        if name in BENCH:
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["bench.py", *BENCH[name], *lang_args(lang)]), contextlib.redirect_stdout(out):
                runpy.run_path(str(ROOT / "bench.py"), run_name="__main__")
            return out.getvalue()
        import hwbench
        lines = []
        hwbench.main([*HWBENCH[name], *lang_args(lang)], out=lines.append)
        return "\n".join(lines) + "\n"


def dataset_texts(name):
    """Texty, které výstup příkazu přebírá ze sady (název sady, zdroj, licence, názvy kategorií): ty se nepřekládají."""
    args = BENCH.get(name, [])
    for flag, fields in (("--emails", ("name",)), ("--public", ("name", "source", "license"))):
        if flag in args:
            doc = json.loads(Path(args[args.index(flag) + 1]).read_text(encoding="utf-8-sig"))
            return [doc[field] for field in fields] + [c["label"] for c in doc.get("categories", [])]
    return []


class Golden(unittest.TestCase):
    def test_output_for_local_models_is_unchanged(self):
        for name in [*BENCH, *HWBENCH]:
            with self.subTest(name=name):
                want = (GOLDEN / f"{name}.txt").read_text(encoding="utf-8")
                self.assertIn("m1", want)
                self.assertEqual(capture(name), want)

    def test_usage_text_is_unchanged(self):
        for tool in USAGE:
            with self.subTest(tool=tool):
                want = (GOLDEN / f"{tool}-usage.txt").read_text(encoding="utf-8")
                self.assertIn(f"python {tool}.py", want)
                self.assertEqual(usage(tool), want)

    def test_the_fake_answers_are_repeatable(self):
        self.assertEqual(capture("bench-lanes"), capture("bench-lanes"))


class English(unittest.TestCase):
    """Anglický výstup: vlastní zlaté soubory, stejná čísla a stejné dotazy do modelu jako česky."""

    def test_english_output_matches_its_golden_files(self):
        for name in ENGLISH:
            with self.subTest(name=name):
                want = (GOLDEN / "en" / f"{name}.txt").read_text(encoding="utf-8")
                self.assertIn("m1", want)
                self.assertEqual(capture(name, "en"), want)
                self.assertNotEqual(want, (GOLDEN / f"{name}.txt").read_text(encoding="utf-8"))

    def test_english_usage_matches_its_golden_file(self):
        for tool in USAGE_EN:
            with self.subTest(tool=tool):
                want = (GOLDEN / "en" / f"{tool}-usage.txt").read_text(encoding="utf-8")
                self.assertIn(f"python {tool}.py", want)
                self.assertEqual(usage(tool, "en"), want)

    def test_usage_names_the_same_options_in_both_languages(self):
        for tool in USAGE_EN:
            with self.subTest(tool=tool):
                cs, en = usage(tool), usage(tool, "en")
                self.assertEqual(set(OPTION.findall(en)), set(OPTION.findall(cs)))
                self.assertEqual(len(en.splitlines()), len(cs.splitlines()))      # nápověda se překládá celá, řádek po řádku
                self.assertEqual(CZECH.findall(en), [])
                for text in (cs, en):
                    self.assertEqual(len(re.findall(r"^--lang cs\|en:", text, re.M)), 1)
                    self.assertIn(LANG_ENV, text)

    def test_numbers_are_the_same_in_both_languages(self):
        for name in ENGLISH:
            with self.subTest(name=name):
                cs, en = capture(name), capture(name, "en")
                self.assertNotEqual(en, cs)
                self.assertGreater(len(NUMBER.findall(cs)), 15 if name in BENCH else 10)      # nejkratší výstup hwbench.py je záhlaví a jeden řádek modelu
                self.assertEqual(NUMBER.findall(en), NUMBER.findall(cs))

    def test_table_keeps_its_columns_in_both_languages(self):                 # záhlaví stejně dlouhé a se stejným počtem slov, řádky modelů shodné až po poznámku
        for name in BENCH:                                                    # bench.py má šířky sloupců pevné; hwbench.py je bere z délky záhlaví (test níže)
            with self.subTest(name=name):
                cs, en = capture(name).splitlines(), capture(name, "en").splitlines()
                self.assertEqual(len(en), len(cs))
                at = [i for i, line in enumerate(cs) if line.startswith("model ")]
                self.assertEqual(len(at), 1)
                head_cs, head_en = cs[at[0]], en[at[0]]
                self.assertEqual(len(head_en), len(head_cs))
                self.assertEqual(len(head_en.split()), len(head_cs.split()))  # slovo přesně na šířku sloupce by se slepilo se sousedem
                rows = [i for i, line in enumerate(cs) if line.startswith(("m1 ", "m2 "))]
                self.assertGreaterEqual(len(rows), 1)
                for i in rows:
                    self.assertEqual(en[i][:len(head_cs)], cs[i][:len(head_cs)])

    def test_hwbench_rows_line_up_under_the_header_in_both_languages(self):   # šířku sloupce určuje délka záhlaví: řádky se zarovnají samy, buňky jsou stejné
        for name in HWBENCH:
            with self.subTest(name=name):
                cs, en = capture(name).splitlines(), capture(name, "en").splitlines()
                self.assertEqual(len(en), len(cs))
                self.assertGreaterEqual(len(cs), 2)
                self.assertNotEqual(en[0], cs[0])
                self.assertTrue(cs[0].startswith("model ") and en[0].startswith("model "))
                for lines in (cs, en):
                    self.assertEqual({len(line) for line in lines}, {len(lines[0])})      # každý řádek končí pod koncem záhlaví
                self.assertEqual([line.split() for line in en[1:]], [line.split() for line in cs[1:]])      # řádky modelů se liší nejvýš mezerami

    def test_english_output_keeps_czech_only_in_texts_taken_from_the_dataset(self):
        for name in ENGLISH:
            with self.subTest(name=name):
                en, taken = capture(name, "en"), dataset_texts(name)
                for text in taken[:1]:
                    self.assertIn(f'"{text}"', en)                            # název sady stojí v úvodním řádku, v anglických uvozovkách
                for text in taken:                                            # název sady, zdroj, licence a názvy kategorií se nepřekládají
                    en = en.replace(text, "")
                self.assertEqual(CZECH.findall(en), [])

    def test_requests_to_the_model_are_the_same_bytes_in_both_languages(self):      # co jde do modelu, se s jazykem nemění ani o bajt
        for name in ("bench-hint", "bench-hint-mirror", "bench-lanes-volby", "bench-numbers", "bench-emails", "bench-public", "bench-signs", "bench-signs-pairs", "bench-memory", "bench-memory-count", "bench-barrier", "bench-barrier-plain", "bench-lookahead", "bench-lookahead-rows", "bench-lookahead-blocked", "bench-rule", "bench-rule-matched-by", "bench-rule-labeled", "bench-queue", "bench-queue-weighted", "bench-bet", "bench-bet-lanes", "bench-cruise", "bench-cruise-mirror", "bench-cruise-score", "bench-timetable", "bench-timetable-shuffled", "bench-diff", "bench-diff-lists", "bench-pattern", "bench-pattern-counter", "bench-pattern-mirror", "hwbench-latency", "hwbench-questions", "hwbench-context", "hwbench-cache", "hwbench-repeat"):
            with self.subTest(name=name):
                cs, en = [], []
                capture(name, "cs", cs)
                capture(name, "en", en)
                self.assertGreater(len(cs), 10)                               # zahřívací dotaz a úlohy
                self.assertTrue(all(isinstance(body, bytes) for body in cs))
                self.assertEqual(en, cs)


if __name__ == "__main__":
    if sys.argv[1:] == ["--write"]:
        GOLDEN.mkdir(exist_ok=True)
        for name in [*BENCH, *HWBENCH]:
            (GOLDEN / f"{name}.txt").write_text(capture(name), encoding="utf-8", newline="\n")
            print("zapsáno", f"tests/golden/{name}.txt")
    elif sys.argv[1:] == ["--write-usage"]:              # jen nápověda: stávající zlaté soubory se nepřepisují
        for tool in USAGE:
            (GOLDEN / f"{tool}-usage.txt").write_text(usage(tool), encoding="utf-8", newline="\n")
            print("zapsáno", f"tests/golden/{tool}-usage.txt")
    elif sys.argv[1:] == ["--write-en"]:                 # jen anglické: české zlaté soubory se nepřepisují
        (GOLDEN / "en").mkdir(exist_ok=True)
        for name in ENGLISH:
            (GOLDEN / "en" / f"{name}.txt").write_text(capture(name, "en"), encoding="utf-8", newline="\n")
            print("zapsáno", f"tests/golden/en/{name}.txt")
        for tool in USAGE_EN:
            (GOLDEN / "en" / f"{tool}-usage.txt").write_text(usage(tool, "en"), encoding="utf-8", newline="\n")
            print("zapsáno", f"tests/golden/en/{tool}-usage.txt")
    else:
        unittest.main()
