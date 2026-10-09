"""Snímek textů stránky: co prohlídka tests/page_tour.js posbírala v prohlížeči, se tady porovná se zlatým souborem.

Prohlídka běží v prohlížeči (Playwright) proti zkušebnímu serveru s prázdnou databází a výsledek uloží přes POST /results.
Tento skript ho ze serveru přečte (GET /results), řádek smaže (DELETE /results/<id>) a:

  python tests/page_snapshot.py <port> --load      uloží zdroják prohlídky na server (POST /queue), odkud si ho stránka vezme:
                                                   v prohlížeči pak stačí pustit krátký zavaděč LOADER (vypíše ho tento příkaz);
                                                   na server s frontou nebo s výsledky nic neuloží a skončí větou
  python tests/page_snapshot.py <port> --write     zapíše tests/golden/page-cs.json (jen když se český text stránky mění záměrně;
                                                   postup je v docs/i18n.md)
  python tests/page_snapshot.py <port> --check     česká prohlídka musí dát stejné texty jako zlatý soubor (plus seznam ADDED)
  python tests/page_snapshot.py <port> --english   anglická prohlídka: žádná česká diakritika, žádný text shodný s českým
  python tests/page_snapshot.py --fragments        zapíše tests/golden/page-fragments-cs.json: úlomky českých textů stránky
                                                   před změnou (jen v úkolu, který snímek zakládá; bez serveru)

Texty sad (názvy, kategorie, e-maily) a hlášky systému se před porovnáním nahradí znakem § (jen jako celá slova), čísla znakem #.
Řádek druhu „asks“ je otisk těl všech dotazů, které prohlídka poslala do modelu (POST /decide): musí být stejný před změnou
i po ní (--check) a stejný česky i anglicky (--english) — co jde do modelu, se nemění ani o znak.
Není to test pro unittest: potřebuje prohlížeč a běžící server.
"""
import json
import re
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

GOLDEN = Path(__file__).parent / "golden" / "page-cs.json"
FRAGMENTS = Path(__file__).parent / "golden" / "page-fragments-cs.json"
PAGE = Path(__file__).parent.parent / "index.html"
TOUR = Path(__file__).parent / "page_tour.js"
LOADER = "async () => { const q = await (await fetch('/queue')).json(); return await (0, eval)('(' + q.queue.tour + ')')(); }"
CZECH = re.compile(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„“]")
# České texty, které na stránce přibyly s přepínačem jazyka (řádky „druh<TAB>text“); nic jiného česky přibýt nesmí.
ADDED = {
    "text\tCS", "text\tEN", "aria-label\tJazyk stránky",
    "tip\tPřepne texty stránky mezi češtinou a angličtinou. Volba se pamatuje v tomto prohlížeči; nastavení, fronta ani výsledky se nemění. "
    "Za běhu hry, benchmarku nebo fronty je přepínač vypnutý.",
}
# Texty, které jsou v češtině i v angličtině stejné záměrně (vlastní jména, zkratky, jednotky, slova stejná v obou jazycích),
# zapsané tak, jak je --english vypíše za tabulátorem.
SAME = {
    "PickBench", "/v#/systemone",                                         # jméno aplikace a adresa rozhraní Ollamy v záhlaví
    "CS", "EN",                                                           # tlačítka přepínače jazyka (česky jsou v ADDED)
    "test:echo", "test:drahy",                                            # jména modelů zkušebního poskytovatele
    "ECE", "macro-F#", "precision", "recall", "goodput",                  # zkratky a názvy metrik, které se nepřekládají
    "min", "max", "max °C",                                               # zkratky v záhlaví tabulky výsledků, jednotka
    "# ms", "# MB", "# MHz",                                              # číslo s jednotkou
    "Start", "Stop", "Model", "Hardware", "Export CSV", "benchmark",      # slova stejná v obou jazycích
    "text: §",                                                            # „Poslední rozhodnutí“ veřejné sady: slovo text a text úlohy
}
# Co prohlídka sama zadává do polí stránky (semínko v BASE v page_tour.js) a stránka to opakuje v buňce tabulky výsledků:
# není to text rozhraní, s češtinou se neporovnává.
TYPED = {"snimek"}
# České řetězce bez diakritiky (kontrola diakritikou je nenajde).
PLAIN = ("pruh ", "← auto", "nestihl", " s pokynem", "podlehl", "shoda barev", "Zdroj: ", "Licence: ", "jen benchmark bez hry", " bez ceny",
         "jistota modelu", "rozh./s", "stabilita", "chyby", "ujeto", "J/rozh.", "hotovo", "chyba", ", limit ", "zrcadla", "posunout nahoru",
         "kopie", "zduplikovat krok", "znovu", "odebrat krok", "pauza: ", "Zastavuji frontu", "teplota GPU", "takt GPU", "stav (pstate)",
         "Chyba", "Benchmark: ", "Benchmark hotov", "benchmark")


# ---------- úlomky českých textů stránky ----------
# Prohlídka vidí jen texty, které se při ní ukážou (asi tři čtvrtiny). Zbytek (věty chyb, validátory, hlášky fronty) hlídá tento
# seznam: pevné části všech českých řetězců stránky před změnou. Každý úlomek musí i potom stát buď dál ve skriptu, nebo
# v české podobě některého textu v TEXTS (tests/test_i18n.py, test_czech_texts_of_the_page_are_kept).
def js_chunks(src):
    """Pevné části řetězců skriptu tak, jak stojí ve zdrojáku: obsah '…' a "…" a části šablony `…` mezi ${…}. Komentáře přeskakuje."""
    out, n = [], len(src)

    def string(i, quote):
        start = i
        while i < n and src[i] != quote and src[i] != "\n":
            i += 2 if src[i] == "\\" else 1
        out.append(src[start:i])
        return i + 1

    def template(i):
        start = i
        while i < n and src[i] != "`":
            if src[i] == "\\":
                i += 2
            elif src[i:i + 2] == "${":
                out.append(src[start:i])
                start = i = code(i + 2, True)
            else:
                i += 1
        out.append(src[start:i])
        return i + 1

    def code(i, inner):
        depth = 0
        while i < n:
            ch = src[i]
            if src[i:i + 2] == "//":
                end = src.find("\n", i)
                i = n if end < 0 else end
            elif src[i:i + 2] == "/*":
                end = src.find("*/", i + 2)
                i = n if end < 0 else end + 2
            elif ch in "'\"":
                i = string(i + 1, ch)
            elif ch == "`":
                i = template(i + 1)
            elif inner and ch == "{":
                depth, i = depth + 1, i + 1
            elif inner and ch == "}":
                if not depth:
                    return i + 1
                depth, i = depth - 1, i + 1
            else:
                i += 1
        return i

    code(0, False)
    return out


class StaticTexts(HTMLParser):
    """Texty statického HTML před skriptem: textové uzly, čitelné atributy a text z vlastnosti content ve <style>. Titulek se nebere."""

    def __init__(self):
        super().__init__()
        self.out, self.inside = [], None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "title"):
            self.inside = tag
        self.out += [value for key, value in attrs if key in ("placeholder", "title", "aria-label", "label") and value]

    def handle_endtag(self, tag):
        if tag == self.inside:
            self.inside = None

    def handle_data(self, data):
        if self.inside == "style":
            self.out += re.findall(r'content:\s*"([^"]*)"', data)
        elif self.inside is None and data.strip():
            self.out.append(" ".join(data.split()))


def static_texts(page):
    """Texty, které nese samo HTML stránky (bez skriptu a titulku)."""
    parser = StaticTexts()
    parser.feed(page.replace("\r\n", "\n").split("<script>\n", 1)[0])
    return parser.out


def czech_fragments(page):
    """Úlomky českých textů stránky (statické HTML a skript bez bloku textů), seřazené a bez opakování."""
    script = page.replace("\r\n", "\n").split("<script>\n", 1)[1].split("</script>")[0]
    script = re.sub(r"// ==== texty: začátek ====\n.*?// ==== texty: konec ====\n", "", script, flags=re.S)
    czech = lambda s: len(re.sub(r"\W", "", s)) >= 3 and (CZECH.search(s) or any(word in s for word in PLAIN))
    return sorted({s for s in static_texts(page) + js_chunks(script) if czech(s)})


def plain_text(fragment):
    """Úlomek ze zdrojáku bez zpětných lomítek (\\n → nový řádek, \\' → ')."""
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), fragment)


def call(port, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch(port):
    """Nejnovější výsledek prohlídky ze serveru; jeho řádek (i starší prohlídky) z databáze smaže."""
    tours = [r for r in call(port, "GET", "/results")["results"] if isinstance(r["data"].get("pickbench_tour"), dict)]
    if not tours:
        sys.exit("V databázi serveru není výsledek prohlídky: nejdřív pusťte prohlídku v prohlížeči (--load).")
    for r in tours:
        call(port, "DELETE", f"/results/{r['id']}")
    return tours[0]["data"]["pickbench_tour"]


def normalize(tour):
    """Množina řádků „druh<TAB>text“: bez textů sad, čísla jako #, mezery sjednocené."""
    own = [re.compile(r"(?<!\w)" + re.escape(d) + r"(?!\w)")      # text sady jen jako celá slova: štítek „low“ nesmí vykousnout kus slova „below“
           for d in sorted({d for d in tour["data"] if d}, key=len, reverse=True)]
    out = set()
    for line in tour["texts"]:
        kind, text = line.split("\t", 1)
        for d in own:
            text = d.sub("§", text)
        text = re.sub(r"\d+(?:[.,]\d+)?", "#", " ".join(text.split()))
        if len(text) > 1 and text not in ("# %", "#%", "#/#", "# / #"):       # jednotlivé znaky jsou písmena kategorií a značky tlačítek
            out.add(f"{kind}\t{text}")
    return out


def main(argv):
    if argv == ["--fragments"]:                      # jen v úkolu 1, na stránce před první změnou; server ani prohlížeč nepotřebuje
        found = czech_fragments(PAGE.read_text(encoding="utf-8"))
        FRAGMENTS.write_text(json.dumps(found, ensure_ascii=False, indent=0) + "\n", encoding="utf-8", newline="\n")
        print(f"zapsáno {FRAGMENTS.name}: {len(found)} úlomků")
        return
    if len(argv) != 2 or not argv[0].isdigit() or argv[1] not in ("--load", "--write", "--check", "--english"):
        sys.exit(__doc__)
    port = int(argv[0])
    if argv[1] == "--load":                          # fronta i výsledky zkušebního serveru musí být prázdné: prohlídka frontu přepíše, přidá výsledky a změní nastavení
        if (call(port, "GET", "/queue")["queue"] or {}).get("steps"):
            sys.exit("Fronta na serveru není prázdná: prohlídka patří jen na zkušební server s prázdnou databází.")
        if call(port, "GET", "/results")["results"]:
            sys.exit("Na serveru jsou uložené výsledky: prohlídka patří jen na zkušební server s prázdnou databází.")
        call(port, "POST", "/queue", {"tour": TOUR.read_text(encoding="utf-8")})
        print("Prohlídka je na serveru. V prohlížeči načtěte stránku a pusťte (browser_evaluate, parametr function):")
        print(LOADER)
        return
    tour = fetch(port)
    got = normalize(tour)
    print(f"prohlídka: jazyk {tour['lang']}, {len(got)} různých textů; konce běhů: {' '.join(tour['log'])}")
    if argv[1] == "--write":                         # texty přepínače jazyka do snímku nejdou: hlídá je seznam ADDED (--check je smí vidět navíc)
        GOLDEN.write_text(json.dumps(sorted(got - ADDED), ensure_ascii=False, indent=0) + "\n", encoding="utf-8", newline="\n")
        print("zapsáno", GOLDEN)
        return
    want = set(json.loads(GOLDEN.read_text(encoding="utf-8")))
    if argv[1] == "--check":
        if tour["lang"] != "cs":
            sys.exit(f"Prohlídka běžela v jazyce {tour['lang']}, ne cs: stránku načtěte s ?lang=cs.")
        missing, extra = want - got, got - want - ADDED
        for sign, lines in (("chybí", missing), ("navíc", extra)):
            for line in sorted(lines):
                print(f"{sign}: {line}")
        if missing or extra:
            sys.exit(f"Česká stránka se liší od snímku: chybí {len(missing)}, navíc {len(extra)}.")
        print("česká stránka: shodná se snímkem")
        return
    if tour["lang"] != "en":
        sys.exit(f"Prohlídka běžela v jazyce {tour['lang']}, ne en: stránku načtěte s ?lang=en.")
    asks = sorted(line for line in got ^ want if line.startswith("asks\t"))
    if asks or not any(line.startswith("asks\t") for line in got):
        sys.exit(f"Dotazy do modelu se v angličtině liší od českých (otisk těl POST /decide): {asks}")
    czech = sorted(line for line in got if CZECH.search(line))
    same = sorted(line for line in got & (want | ADDED)
                  if not line.startswith("asks\t") and line.split("\t", 1)[1] not in SAME | TYPED and re.search(r"[A-Za-z]{2}", line.split("\t", 1)[1]))
    for line in czech:
        print("česky:", line)
    for line in same:
        print("stejné jako česky:", line)
    if czech or same:
        sys.exit(f"Anglická stránka: {len(czech)} textů s českou diakritikou, {len(same)} textů shodných s českými mimo seznam SAME.")
    print("anglická stránka: bez české diakritiky a bez textů shodných s českými")


if __name__ == "__main__":
    main(sys.argv[1:])
