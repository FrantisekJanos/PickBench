"""Stažení veřejných sad pro PickBench (jen Python stdlib, bez Parquetu).

Použití:  python fetch_datasets.py [--n N] [--seed S] [--full] sada [sada ...]
Příklad:  python fetch_datasets.py banking77 clinc150 massive
          python fetch_datasets.py --full banking77
          python fetch_datasets.py all

Sady:
  banking77   dotaz klienta banky → 77 záměrů (PolyAI, CC-BY-4.0), testovací část
  clinc150    věta → 150 záměrů nebo „mimo rozsah“ (CLINC, CC-BY-3.0), testovací část
  massive     věta pro hlasového asistenta → 18 scénářů (Amazon MASSIVE 1.1, en-US, CC BY 4.0), testovací část;
              čte se proudem z archivu 40 MB (stáhne se z něj necelá polovina, na disk se archiv neukládá)
  horizon-cs  český text → jeden z 2–10 nabídnutých štítků (Horizon-Labs, ODC-BY 1.0; texty psal model, člověk je neověřil)
  cfpb        stížnost → finanční produkt, 6 tříd (CFPB, CC0); vždy vzorek --per-class K z každé třídy (výchozí 50),
              ukládá se jako sada e-mailů do datasets/cfpb-complaints-<počet>.json; --n a --full se ho netýkají;
              texty jsou z kopie databáze na Hugging Face (BEE-spoke-data/consumer-finance-complaints, únor 2024)
  all         všechny sady výše

horizon-cs a cfpb se čtou po stovkách řádků ze služby datasets-server.huggingface.co, která počet dotazů omezuje
(po HTTP 429 se čeká minuty). Když služba zrovna neumí filtrovat podle jazyka, čte horizon-cs náhodné stránky
celé sady, nejvýš 400 — trvá to až hodinu a úloh je pak jen kolem stovky.

Štítek bez vlastního popisu se ukládá jen jako klíč (bez description), aby šel do promptu modelu jednou.
Výchozí je vrstvený vzorek N úloh (výchozí 500) s pevným semínkem (výchozí 1); --full uloží celou testovací část
do souboru se jménem končícím -full. Soubory jdou do datasets/public/ (složka není v gitu) ve formátu veřejné sady
(viz bench.validate_public); měří je  python bench.py --public datasets/public/<soubor>.json model.
--lang cs|en:  jazyk výstupu; bez volby platí proměnná prostředí PICKBENCH_LANG, jinak cs.
Chyba sítě nebo změněný tvar zdroje skončí jednou větou a nenulovým kódem; rozpracovaný soubor nezůstane.
"""
USAGE_EN = """Downloads public datasets for PickBench (Python stdlib only, no Parquet).

Usage:    python fetch_datasets.py [--n N] [--seed S] [--full] dataset [dataset ...]
Examples: python fetch_datasets.py banking77 clinc150 massive
          python fetch_datasets.py --full banking77
          python fetch_datasets.py all

Datasets:
  banking77   a bank customer's query → 77 intents (PolyAI, CC-BY-4.0), the test split
  clinc150    a sentence → 150 intents or "out of scope" (CLINC, CC-BY-3.0), the test split
  massive     a sentence for a voice assistant → 18 scenarios (Amazon MASSIVE 1.1, en-US, CC BY 4.0), the test split;
              fetch_datasets.py reads it as a stream from a 40 MB archive (it downloads less than half of it and does not save the archive to disk)
  horizon-cs  a Czech text → one of 2–10 offered labels (Horizon-Labs, ODC-BY 1.0; a model wrote the texts and no human verified them)
  cfpb        a complaint → a financial product, 6 classes (CFPB, CC0); always a sample of --per-class K from every class (default 50),
              fetch_datasets.py saves it as an e-mail dataset to datasets/cfpb-complaints-<count>.json; --n and --full do not apply to it;
              the texts come from a copy of the database on Hugging Face (BEE-spoke-data/consumer-finance-complaints, February 2024)
  all         all the datasets above

fetch_datasets.py reads horizon-cs and cfpb a hundred rows at a time from the datasets-server.huggingface.co service, which limits the number of requests
(after HTTP 429 fetch_datasets.py waits several minutes). If the service cannot filter by language at the moment, horizon-cs reads random pages
of the whole dataset, at most 400 — that takes up to an hour and then gives only about a hundred tasks.

fetch_datasets.py saves a label without a description of its own as a key only (without description), so that it goes into the model prompt once.
The default is a stratified sample of N tasks (default 500) with a fixed seed (default 1); --full saves the whole test split
to a file whose name ends in -full. The files go to datasets/public/ (the folder is not in git) in the public dataset format
(see bench.validate_public); measure them with  python bench.py --public datasets/public/<file>.json model.
--lang cs|en:  output language; without the option the environment variable PICKBENCH_LANG applies, otherwise cs.
After a network error or a changed source format, fetch_datasets.py ends with one sentence and a non-zero exit code; it leaves no unfinished file behind.
"""
import argparse
import csv
import http.client
import io
import json
import os
import random
import re
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import date
from pathlib import Path

import bench
import i18n

ROOT = Path(__file__).parent
PUBLIC = "datasets/public"
AGENT = "PickBench-fetch/1.0 (Python-urllib)"
RETRY = (429, 500, 502, 503, 504)                    # stavy, po kterých má smysl zkusit to znovu

BANKING_URL = "https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/master/banking_data/test.csv"
CLINC_URL = "https://raw.githubusercontent.com/clinc/oos-eval/master/data/data_full.json"
MASSIVE_URL = "https://amazon-massive-nlu-dataset.s3.amazonaws.com/amazon-massive-dataset-1.1.tar.gz"      # 40 MB, 52 jazyků
MASSIVE_FILE = "/data/en-US.jsonl"                   # konec jména souboru uvnitř archivu (1.1/data/en-US.jsonl)
MASSIVE_SCENARIOS = ("alarm", "audio", "calendar", "cooking", "datetime", "email", "general", "iot", "lists", "music", "news", "play",
                     "qa", "recommendation", "social", "takeaway", "transport", "weather")


class FetchError(Exception):
    """Sadu se nepodařilo stáhnout nebo převést; text je věta pro uživatele."""


# ---------- jádro ----------
def tar_member(stream, suffix):
    """Z proudu archivu .tar.gz vrátí obsah prvního souboru, jehož jméno končí suffix; None, když v archivu není.

    Čte se proudem (režim r|gz): archiv se neukládá na disk ani celý do paměti a čtení skončí, jakmile je soubor nalezen."""
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        for member in archive:
            if member.isfile() and member.name.endswith(suffix):
                return archive.extractfile(member).read()
    return None


def http_get(url, timeout=120, tries=5, pause=5, opener=urllib.request.urlopen, sleep=time.sleep, member=None):
    """Stáhne adresu. Přetížený server a chybu sítě zkusí znovu (pauza roste: pause, 2×pause, …), jiný stav HTTP hned vzdá.

    Po HTTP 429 (příliš mnoho dotazů) čeká dvanáctkrát déle — minuty, ne sekundy: Hugging Face při psaní plánu
    po zhruba stovce dotazů odpovídal 429 ještě po 50 sekundách čekání.

    S member se adresa čte jako archiv .tar.gz a vrací se jen obsah souboru, jehož jméno končí member (viz tar_member).
    Utnutý nebo poškozený archiv se zkusí znovu jako chyba sítě; archiv, ve kterém takový soubor není, je chyba hned."""
    why = "?"
    for attempt in range(tries):
        wait = pause * (attempt + 1)
        try:
            with opener(urllib.request.Request(url, headers={"User-Agent": AGENT}), timeout=timeout) as r:
                if member is None:
                    return r.read()
                found = tar_member(r, member)
            if found is None:
                raise FetchError(i18n.t("fetch.err.member", member=member, url=url))
            return found
        except urllib.error.HTTPError as e:
            why = f"HTTP {e.code}"
            if e.code not in RETRY:
                break
            if e.code == 429:
                wait *= 12
        except (urllib.error.URLError, OSError, http.client.HTTPException,           # síť, DNS, vypršení času i spojení utnuté uprostřed těla (IncompleteRead)
                tarfile.TarError, EOFError, zlib.error) as e:                        # utnutý nebo poškozený archiv
            why = str(getattr(e, "reason", e)) or type(e).__name__
        if attempt < tries - 1:
            sleep(wait)
    raise FetchError(i18n.t("fetch.err.download", why=why, url=url))


def as_json(raw, what):
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except ValueError:                               # i bajty, které nejsou UTF-8
        raise FetchError(i18n.t("fetch.err.json", what=what)) from None


def stratified(items, key, n, seed=1):
    """Vrstvený vzorek n položek: každá vrstva (podle key) dostane podíl podle své velikosti.

    Zbytky po dělení dostanou vrstvy s největší desetinnou částí; shodu rozhodne semínko. Stejné položky ve stejném
    pořadí a stejné semínko dají vždy stejný vzorek. Když je n aspoň tolik, kolik je položek, vrací všechny beze změny."""
    if n >= len(items):
        return list(items)
    rng = random.Random(seed)
    layers = {}
    for it in items:
        layers.setdefault(key(it), []).append(it)
    names = sorted(layers)
    rng.shuffle(names)                               # pořadí pro rozhodování shod
    quota = {name: len(layers[name]) * n // len(items) for name in names}
    by_rest = sorted(names, key=lambda name: -(len(layers[name]) * n % len(items)))      # stabilní řazení: shody v pořadí names
    for name in by_rest[:n - sum(quota.values())]:
        quota[name] += 1
    out = [it for name in sorted(layers) for it in rng.sample(layers[name], quota[name])]
    rng.shuffle(out)
    return out


def write_json(path, doc):
    """Zapíše dokument tak, aby po chybě nezůstal rozpracovaný soubor a původní soubor zůstal celý."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        with open(part, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(part, path)
    finally:
        if part.exists():
            part.unlink()


def in_both(sentence):
    """Tatáž věta česky a anglicky, (cs, en), ať nástroj běží v kterémkoli jazyce: soubor sady na jazyku běhu nezávisí.

    sentence je funkce bez parametrů, která větu složí přes i18n.t; zavolá se jednou pro každý jazyk."""
    texts = []
    for code in i18n.LANGS:
        with i18n.process(code):
            texts.append(sentence())
    return texts


def scope(items, pool, a):
    """Rozsah souboru slovy, v platném jazyce: celá testovací část, nebo vzorek se semínkem."""
    return i18n.t("fetch.scope.full", n=len(items)) if a.full else i18n.t("fetch.scope.sample", n=len(items), pool=pool, seed=a.seed)


def public_doc(title, about, about_en, source, license, instructions, items, pool, a, today, labels=None, oos=None):
    """Dokument ve formátu veřejné sady; nese počet úloh, velikost zdroje, semínko a datum stažení.

    about je popis sady česky, about_en anglicky (oba už s rozsahem a datem stažení, viz in_both). Název sady se nepřekládá:
    rozsah v něm stojí vždy česky."""
    czech, _ = in_both(lambda: scope(items, pool, a))
    doc = {"name": f"{title} ({czech})", "about": about, "about_en": about_en,
           "source": source, "license": license, "instructions": instructions}
    if labels:
        doc["labels"] = labels
    if oos:
        doc["oos"] = oos
    doc["sample"] = {"n": len(items), "of": pool, "seed": a.seed, "full": a.full, "fetched": today}
    doc["items"] = items
    try:
        bench.validate_public(doc)
    except ValueError as e:
        raise FetchError(f"{title}: {e}") from None
    return doc


def slug(label):
    """Štítek jako klíč možnosti: malá písmena, číslice a podtržítka (co endpoint snese v klíčích jinak, se neověřovalo)."""
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def option(name):
    """Štítek zdroje jako možnost otázky. Popis (původní jméno) dostane, jen když nese víc než klíč.

    Jméno, které se od klíče liší jen velkými písmeny, mezerami a podtržítky („Billing Error“ → billing_error), by šlo
    do promptu modelu dvakrát — jednou jako klíč a jednou jako popis; proto se popis vynechá a model dostane jen klíč."""
    key, plain = slug(name), " ".join(name.replace("_", " ").split())
    return {"key": key} if plain.lower().replace(" ", "_") == key else {"key": key, "description": plain}


def pick(items, a):
    return list(items) if a.full else stratified(items, lambda it: it["label"], a.n, a.seed)


def file_name(name, a):
    return f"{PUBLIC}/{name}{'-full' if a.full else ''}.json"


# ---------- BANKING77 ----------
def banking77(get, a, out, today):
    raw = get(BANKING_URL)
    try:
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    except ValueError:                               # bajty, které nejsou UTF-8
        raise FetchError(i18n.t("fetch.err.banking.utf8")) from None
    except csv.Error:                                # není ValueError; např. pole delší, než modul csv dovolí
        raise FetchError(i18n.t("fetch.err.banking.csv")) from None
    if not rows or "text" not in rows[0] or "category" not in rows[0]:
        raise FetchError(i18n.t("fetch.err.banking.columns"))
    if not all(isinstance(r["text"], str) and isinstance(r["category"], str) for r in rows):      # DictReader doplní chybějící sloupec jako None
        raise FetchError(i18n.t("fetch.err.banking.row"))
    names = sorted({r["category"].strip() for r in rows})              # jména záměrů ve zdroji; některá mají velké písmeno nebo otazník
    items = [{"text": r["text"].strip(), "label": slug(r["category"])} for r in rows if r["text"] and r["text"].strip()]
    if len(names) != 77 or len({slug(name) for name in names}) != 77:
        raise FetchError(i18n.t("fetch.err.banking.count", n=len(names)))
    chosen = pick(items, a)
    about, about_en = in_both(lambda: i18n.t("fetch.about.banking77", scope=scope(chosen, len(items), a), today=today))
    return file_name("banking77", a), public_doc(
        "BANKING77", about, about_en,
        "https://github.com/PolyAI-LDN/task-specific-datasets (banking_data/test.csv)",
        "CC-BY-4.0 (Creative Commons Attribution 4.0 International; soubor LICENSE v repozitáři zdroje)",
        "Classify the customer's banking query into exactly one intent.",
        chosen, len(items), a, today, labels=[option(name) for name in names])


# ---------- CLINC150 ----------
def clinc150(get, a, out, today):
    d = as_json(get(CLINC_URL), "CLINC150")
    if not isinstance(d, dict) or not isinstance(d.get("test"), list) or not isinstance(d.get("oos_test"), list):
        raise FetchError(i18n.t("fetch.err.clinc.lists"))
    rows = d["test"] + d["oos_test"]
    if not all(isinstance(r, list) and len(r) == 2 and all(isinstance(v, str) and v for v in r) for r in rows):
        raise FetchError(i18n.t("fetch.err.clinc.row"))
    items = [{"text": text.strip(), "label": intent} for text, intent in rows]
    keys = sorted({it["label"] for it in items} - {"oos"})
    if len(keys) != 150 or not d["oos_test"] or any(intent != "oos" for _, intent in d["oos_test"]):
        raise FetchError(i18n.t("fetch.err.clinc.count", n=len(keys)))
    labels = [{"key": k} for k in keys]                                # jméno záměru je klíč; popis by byl jeho kopie
    labels.append({"key": "oos", "description": "none of the listed intents; the request is out of scope"})
    chosen = pick(items, a)
    about, about_en = in_both(lambda: i18n.t("fetch.about.clinc150", scope=scope(chosen, len(items), a), today=today))
    return file_name("clinc150", a), public_doc(
        "CLINC150", about, about_en,
        "https://github.com/clinc/oos-eval (data/data_full.json)",
        "CC-BY-3.0 (Creative Commons Attribution 3.0 Unported; soubor LICENSE v repozitáři zdroje)",
        "Classify the request into exactly one intent. Choose oos when none of the listed intents fits.",
        chosen, len(items), a, today, labels=labels, oos="oos")


# ---------- MASSIVE, en-US ----------
def massive(get, a, out, today):
    """Část test souboru en-US z archivu MASSIVE 1.1; štítek je scénář (18 možností), ne záměr (59)."""
    raw = get(MASSIVE_URL, member=MASSIVE_FILE)
    try:
        rows = [json.loads(line) for line in raw.decode("utf-8-sig").split("\n") if line.strip()]
    except ValueError:                               # i bajty, které nejsou UTF-8
        rows = None
    if not rows or not all(isinstance(r, dict) for r in rows):
        raise FetchError(i18n.t("fetch.err.massive.lines"))
    test = [r for r in rows if r.get("partition") == "test"]
    if not test or not all(isinstance(r.get("utt"), str) and isinstance(r.get("scenario"), str) for r in test):
        raise FetchError(i18n.t("fetch.err.massive.fields"))
    items = [{"text": r["utt"].strip(), "label": r["scenario"]} for r in test if r["utt"].strip()]
    odd = sorted({it["label"] for it in items} ^ set(MASSIVE_SCENARIOS))
    if odd:
        raise FetchError(i18n.t("fetch.err.massive.scenarios", n=len(MASSIVE_SCENARIOS), odd=", ".join(odd)))
    chosen = pick(items, a)
    about, about_en = in_both(lambda: i18n.t("fetch.about.massive", scope=scope(chosen, len(items), a), today=today))
    return file_name("massive-en-us", a), public_doc(
        "MASSIVE en-US", about, about_en,
        "https://github.com/alexa/massive (archiv amazon-massive-dataset-1.1.tar.gz, soubor 1.1/data/en-US.jsonl)",
        "CC BY 4.0 (Creative Commons Attribution 4.0 International; soubor LICENSE v archivu, karta AmazonScience/massive: cc-by-4.0)",
        "Decide which voice assistant scenario (domain) the request belongs to. Choose exactly one.",
        chosen, len(items), a, today, labels=[{"key": name} for name in MASSIVE_SCENARIOS])


# ---------- Hugging Face: řádky sady jako JSON ----------
HF = "https://datasets-server.huggingface.co"
HF_PAGE = 100                                        # víc řádků na dotaz služba nedá (HTTP 422)
HF_PAUSE = 1.0                                       # s mezi dotazy; služba při rychlém čtení odpovídá HTTP 429


def hf_url(endpoint, dataset, config, split, offset, where=None):
    query = {"dataset": dataset, "config": config, "split": split, "offset": offset, "length": HF_PAGE}
    if where:
        query["where"] = where
    return f"{HF}/{endpoint}?" + urllib.parse.urlencode(query)


def hf_page(get, endpoint, dataset, config, split, offset, where=None):
    """Jedna stránka: (seznam řádků jako slovníky, celkový počet řádků). Před dotazem chvíli počká, ať službu nezahltí."""
    if HF_PAUSE:
        time.sleep(HF_PAUSE)
    d = as_json(get(hf_url(endpoint, dataset, config, split, offset, where)), dataset)
    rows = d.get("rows") if isinstance(d, dict) else None
    if (not isinstance(rows, list) or not isinstance(d.get("num_rows_total"), int)
            or not all(isinstance(r, dict) and isinstance(r.get("row"), dict) for r in rows)):
        raise FetchError(i18n.t("fetch.err.rows", dataset=dataset))
    return [r["row"] for r in rows], d["num_rows_total"]


def field(row, name, what):
    try:
        return row[name]
    except KeyError:
        raise FetchError(i18n.t("fetch.err.field", what=what, field=name)) from None


# ---------- Horizon-Labs, česky ----------
HORIZON = "Horizon-Labs/multilingual-zeroshot-synthetic"
HORIZON_MAX_PAGES = 400                              # strop náhradního čtení bez filtru: služba pustí asi 30 dotazů a pak minutu i déle odpovídá 429
SUPPORT = re.compile(r"customer|support|complaint|chat|intent|ticket|issue|urgency|e-?mail|helpdesk")    # úlohy typu zákaznická podpora, podle pole task
CZECH = re.compile(r"[áčďéěíňóřšťúůýž]", re.I)     # sada občas pod češtinou vede anglický text; český bez diakritiky je vzácný


def horizon_item(row):
    """Řádek sady → úloha, nebo None, když do výběru nepatří."""
    text, lang, task = (field(row, name, "Horizon-Labs") for name in ("text", "lang", "task"))
    cands, gold = field(row, "candidate_labels", "Horizon-Labs"), field(row, "gold_labels", "Horizon-Labs")
    kind, origin = field(row, "kind", "Horizon-Labs"), field(row, "text_origin", "Horizon-Labs")
    if lang != "Czech" or kind not in ("short", "tax_short") or origin != "qwen-generated":      # jen krátké psané zprávy, ne pasáže z webu
        return None
    if not (isinstance(text, str) and isinstance(task, str) and isinstance(cands, list) and isinstance(gold, list)):
        return None
    if len(gold) != 1 or not 2 <= len(cands) <= 10 or gold[0] not in cands or not all(isinstance(c, str) for c in cands):      # mez 2–10: rozhodnutí uživatele 2026-10-06
        return None
    if not SUPPORT.search(task.lower()) or not CZECH.search(text):
        return None
    keys = [slug(c) for c in cands]
    if "" in keys or len(set(keys)) != len(keys):
        return None
    return {"text": text.strip(), "label": slug(gold[0]), "options": [option(c) for c in cands], "task": task}


def horizon_cs(get, a, out, today):
    """České řádky zákaznické podpory. Nejdřív filtrem podle jazyka na straně zdroje (pár desítek dotazů, všechny české řádky).

    Když filtr neodpovídá, čtou se náhodné stránky celé sady v pořadí daném semínkem, dokud není dost úloh
    (s --full všechny stránky). Stránka, která uprostřed čtení selže, čtení ukončí a uloží se, co je nasbíráno."""
    items, seen = [], set()

    def take(rows):
        for row in rows:
            item = horizon_item(row)
            if item and item["text"] not in seen:
                seen.add(item["text"])
                items.append(item)

    def ended(e):
        """Čtení se zastavilo: s už nasbíranými úlohami jen oznámí, že skončilo dřív (uloží se, co je); bez nich je to chyba."""
        if not items:
            raise e
        out(i18n.t("fetch.horizon.ended", error=e, n=len(items)))

    where = "\"lang\"='Czech'"
    filtered, why = True, None
    try:
        rows, total = hf_page(get, "filter", HORIZON, "default", "train", 0, where)
    except FetchError as e:
        filtered, why = False, e
    if filtered:
        take(rows)
        for offset in range(HF_PAGE, total, HF_PAGE):
            try:
                rows = hf_page(get, "filter", HORIZON, "default", "train", offset, where)[0]
            except FetchError as e:                  # filtr přestal odpovídat uprostřed: nasbírané zůstává, dočte se náhodnými stránkami
                filtered, why = False, e
                break
            take(rows)
    if not filtered:
        out(i18n.t("fetch.horizon.fallbackFull", error=why) if a.full else i18n.t("fetch.horizon.fallback", error=why, n=a.n, pages=HORIZON_MAX_PAGES))
        try:
            rows, total = hf_page(get, "rows", HORIZON, "default", "train", 0)
        except FetchError as e:                      # služba odmítá i náhradní čtení: úlohy nasbírané z filtru se nezahodí
            ended(e)
            rows, total = [], 0
        take(rows)
        pages = list(range(1, (total + HF_PAGE - 1) // HF_PAGE))
        random.Random(a.seed).shuffle(pages)
        for count, page in enumerate(pages if a.full else pages[:HORIZON_MAX_PAGES - 1], start=2):
            if not a.full and len(items) >= a.n:
                break
            if count % 50 == 0:
                out(i18n.t("fetch.horizon.progress", page=count, n=len(items)))
            try:
                take(hf_page(get, "rows", HORIZON, "default", "train", page * HF_PAGE)[0])
            except FetchError as e:
                ended(e)
                break
    if not items:
        raise FetchError(i18n.t("fetch.err.horizon.none"))
    chosen = list(items) if a.full else stratified(items, lambda it: it["task"], a.n, a.seed)
    about, about_en = in_both(lambda: i18n.t("fetch.about.horizonCs", scope=scope(chosen, len(items), a), today=today))
    return file_name("horizon-cs", a), public_doc(
        "Horizon-Labs česky", about, about_en,
        "https://huggingface.co/datasets/Horizon-Labs/multilingual-zeroshot-synthetic",
        "ODC-BY 1.0 (Open Data Commons Attribution License; karta sady, oddíl License)",
        "Classify the text into exactly one of the offered labels.",
        chosen, len(items), a, today)


# ---------- CFPB: stížnosti spotřebitelů ----------
CFPB_MIRROR = "BEE-spoke-data/consumer-finance-complaints"       # oficiální rozhraní CFPB texty stížností nevydává (ověřeno 2026-10-06)
CFPB_SUBJECT = "Consumer complaint"                  # u všech stejný, aby předmět neprozrazoval třídu
CFPB_MAX_CHARS, CFPB_MIN_CHARS, CFPB_MAX_PAGES = 1200, 100, 300
CFPB_CATEGORIES = [                                  # (název produktu ve zdroji, kategorie sady)
    ("Mortgage", {"key": "mortgage", "label": "Hypotéka", "label_en": "Mortgage",
                  "description": "Home mortgage: applying for, servicing or paying a loan secured by a house, escrow, foreclosure."}),
    ("Credit card", {"key": "credit_card", "label": "Kreditní karta", "label_en": "Credit card",
                     "description": "Credit card: charges, fees, interest, billing disputes or closing of a credit card account."}),
    ("Checking or savings account", {"key": "bank_account", "label": "Běžný nebo spořicí účet", "label_en": "Bank account",
                                     "description": "Checking or savings account: deposits, withdrawals, overdraft fees, opening or closing a bank account."}),
    ("Student loan", {"key": "student_loan", "label": "Studentská půjčka", "label_en": "Student loan",
                      "description": "Student loan: federal or private loans for education, their servicing and repayment."}),
    ("Vehicle loan or lease", {"key": "vehicle_loan", "label": "Auto na úvěr nebo leasing", "label_en": "Vehicle loan or lease",
                               "description": "Vehicle loan or lease: financing or leasing a car or another vehicle, repossession."}),
    ("Debt collection", {"key": "debt_collection", "label": "Vymáhání dluhů", "label_en": "Debt collection",
                         "description": "Debt collection: a collector contacting the consumer about a debt, including debts the consumer says are not owed."}),
]


def clip(text, limit):
    """Mezery a konce řádků na jednu mezeru; delší text se uřízne na hranici slova a dostane výpustku (i s ní se vejde do limitu)."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    return (cut[:cut.rfind(" ") + 1] if " " in cut else cut) + "…"


def cfpb(get, a, out, today):
    key_of = {product: cat["key"] for product, cat in CFPB_CATEGORIES}
    found, seen = {key: [] for key in key_of.values()}, set()
    missing = lambda: [key for key, rows in found.items() if len(rows) < a.per_class]

    def take(rows):
        for row in rows:
            product, text = field(row, "Product", "CFPB"), field(row, "Consumer complaint narrative", "CFPB")
            if not isinstance(product, str) or not isinstance(text, str):      # pole, které není text (seznam, slovník, null): řádek se přeskočí
                continue                                                       # seznam nebo slovník by jako klíč do key_of spadl na TypeError
            key = key_of.get(product)
            if key is None or len(found[key]) >= a.per_class:
                continue
            body = clip(text, CFPB_MAX_CHARS)
            if len(body) < CFPB_MIN_CHARS or body in seen:
                continue
            seen.add(body)
            found[key].append({"subject": CFPB_SUBJECT, "body": body, "category": key})

    rows, total = hf_page(get, "rows", CFPB_MIRROR, "has-text", "train", 0)
    take(rows)
    rest = list(range(1, (total + HF_PAGE - 1) // HF_PAGE))
    random.Random(a.seed).shuffle(rest)              # další stránky v pořadí daném semínkem: vzorek je napříč celou sadou a opakovatelný
    read = 1
    for page in rest[:CFPB_MAX_PAGES - 1]:
        if not missing():
            break
        take(hf_page(get, "rows", CFPB_MIRROR, "has-text", "train", page * HF_PAGE)[0])
        read += 1
    if missing():
        raise FetchError(i18n.t("fetch.err.cfpb.missing", pages=read, missing=", ".join(missing())))
    emails = [e for _, cat in CFPB_CATEGORIES for e in found[cat["key"]]]
    about, about_en = in_both(lambda: i18n.t("fetch.about.cfpb", per_class=a.per_class, max_chars=CFPB_MAX_CHARS, mirror=CFPB_MIRROR, today=today))
    doc = {"name": f"CFPB stížnosti ({len(emails)})", "about": about, "about_en": about_en,      # název sady se nepřekládá
           "source": f"https://www.consumerfinance.gov/data-research/consumer-complaints/ (kopie: https://huggingface.co/datasets/{CFPB_MIRROR}, config has-text)",
           "license": "CC0 1.0 (podle karty kopie na Hugging Face: cc0-1.0)",
           "instructions": "Classify the consumer complaint by the financial product it is about. Choose exactly one category.",
           "sample": {"n": len(emails), "per_class": a.per_class, "seed": a.seed, "fetched": today, "max_chars": CFPB_MAX_CHARS},
           "categories": [cat for _, cat in CFPB_CATEGORIES], "emails": emails}
    try:
        bench.validate_dataset(doc)
    except ValueError as e:
        raise FetchError(f"CFPB: {e}") from None
    return f"datasets/cfpb-complaints-{len(emails)}.json", doc


SETS = {"banking77": banking77, "clinc150": clinc150, "massive": massive, "horizon-cs": horizon_cs, "cfpb": cfpb}


# ---------- příkazová řádka ----------
def parse_args(argv):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--n", type=int, default=500)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--full", action="store_true")
    p.add_argument("--per-class", type=int, default=50)
    p.add_argument("rest", nargs="*")
    a = p.parse_intermixed_args(argv)
    a.names = list(SETS) if "all" in a.rest else a.rest
    return a


def usage():
    """Nápověda v platném jazyce: česky dokumentační řetězec modulu, anglicky USAGE_EN."""
    return i18n.pick({"cs": __doc__, "en": USAGE_EN})


def run(argv, get=http_get, out=print, root=ROOT, today=None):
    """Celý běh nástroje v jazyce, který už platí (volbu --lang vyjímá main)."""
    a = parse_args(argv)
    if not a.names or any(name not in SETS for name in a.names) or a.n < 1 or a.per_class < 1:
        sys.exit(usage())
    today = today or date.today().isoformat()
    failed = False
    for name in a.names:
        try:
            path, doc = SETS[name](get, a, out, today)
            write_json(Path(root) / path, doc)
            out(i18n.t("fetch.saved", name=name, n=len(doc.get("items") or doc.get("emails")), path=path))
        except FetchError as e:                      # věta chyby je už v jazyce běhu; před ní stojí jen jméno sady
            failed = True
            out(f"{name}: {e}")
        except OSError as e:
            failed = True
            out(i18n.t("fetch.err.write", name=name, error=e))
    if failed:
        sys.exit(1)


def main(argv, **kwargs):
    lang, rest = i18n.pop_lang(argv)                 # --lang smí stát kdekoli; neznámý jazyk končí větou
    with i18n.process(lang):
        try:
            return run(rest, **kwargs)
        except KeyboardInterrupt:                    # Ctrl+C kdekoli v běhu: věta v jazyce běhu místo výpisu zásobníku
            sys.exit(i18n.t("fetch.err.interrupted"))


if __name__ == "__main__":
    main(sys.argv[1:])
