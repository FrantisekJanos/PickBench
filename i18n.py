"""Jazyk a texty PickBench pro Python: server.py, providers.py, store.py, bench.py, hwbench.py a fetch_datasets.py.

Jazyk:  cs (výchozí) nebo en. Pořadí: jazyk požadavku na serveru (hlavička X-PickBench-Lang, platí jen ve vlákně
        požadavku) → jazyk procesu (volba --lang) → proměnná prostředí PICKBENCH_LANG → cs.
Texty:  TEXTS[klíč] = {"cs": …, "en": …}; t(klíč, proměnné) vrátí větu v platném jazyce. Česká a anglická podoba stojí
        pod jedním klíčem, takže chybějící překlad najde test (tests/test_i18n.py), ne uživatel.
Věta:   {jméno} vloží proměnnou, {jméno:.1f} ji naformátuje jako f-string, {jméno|jedna|více} vybere tvar podle počtu
        (jedna = hodnota je 1). Věta se skládá celá v jednom jazyce; úlomky se neslepují.
"""
import contextlib
import os
import re
import threading

LANGS = ("cs", "en")
DEFAULT = "cs"
ENV = "PICKBENCH_LANG"
HEADER = "X-PickBench-Lang"
FIELD = re.compile(r"\{(\w+)(?::([^{}|]*)|\|([^{}|]*)\|([^{}|]*))?\}")

TEXTS = {
    "lang.unknown": {"cs": "Neznámý jazyk „{value}“: --lang a PICKBENCH_LANG berou cs nebo en.",
                     "en": "Unknown language \"{value}\": --lang and PICKBENCH_LANG accept cs or en."},
    "lang.missing": {"cs": "Volba --lang potřebuje hodnotu: cs nebo en.", "en": "The --lang option needs a value: cs or en."},
    # Validátory sad: věty chyb a výchozí název sady e-mailů (bench.validate_dataset) a veřejné sady (bench.validate_public).
    # Stejné klíče se stejnými texty má TEXTS v index.html (validateDataset, validatePublic); shodu hlídají tests/test_i18n.py
    # a tests/test_parity.py. Jména polí sady (categories, key, labels…) se nepřekládají. {n} = pořadí položky od 1,
    # {key} a {label} = klíč převzatý ze sady, {where} = labels, nebo vp.task (štítky jedné úlohy).
    "vd.categories": {"cs": "Neplatná sada: chybí categories nebo emails", "en": "Invalid dataset: categories or emails is missing"},
    "vd.texts": {"cs": "Neplatná sada: name a instructions musí být text", "en": "Invalid dataset: name and instructions must be text"},
    "vd.about": {"cs": "Neplatná sada: about musí být text", "en": "Invalid dataset: about must be text"},
    "vd.aboutEn": {"cs": "Neplatná sada: about_en musí být text", "en": "Invalid dataset: about_en must be text"},
    "vd.count": {"cs": "Neplatná sada: počet kategorií musí být 2–8", "en": "Invalid dataset: the number of categories must be 2–8"},
    "vd.noEmails": {"cs": "Neplatná sada: žádné e-maily", "en": "Invalid dataset: no e-mails"},
    "vd.category.object": {"cs": "Neplatná sada: kategorie {n} musí být objekt", "en": "Invalid dataset: category {n} must be an object"},
    "vd.category.fields": {"cs": "Neplatná sada: kategorie {n} nemá key, label nebo description jako text",
                           "en": "Invalid dataset: category {n} needs key, label and description as text"},
    "vd.category.duplicate": {"cs": "Neplatná sada: klíč kategorie „{key}“ se opakuje",
                              "en": "Invalid dataset: the category key \"{key}\" is used more than once"},
    "vd.category.letter": {"cs": "Neplatná sada: písmeno kategorie „{key}“ musí být text",
                           "en": "Invalid dataset: the letter of category \"{key}\" must be text"},
    "vd.category.labelEn": {"cs": "Neplatná sada: label_en kategorie „{key}“ musí být text",
                            "en": "Invalid dataset: label_en of category \"{key}\" must be text"},
    "vd.category.letterEn": {"cs": "Neplatná sada: letter_en kategorie „{key}“ musí být text",
                             "en": "Invalid dataset: letter_en of category \"{key}\" must be text"},
    "vd.category.color": {"cs": "Neplatná sada: barva kategorie „{key}“ musí být ve tvaru #rrggbb",
                          "en": "Invalid dataset: the color of category \"{key}\" must be in the form #rrggbb"},
    "vd.email.object": {"cs": "Neplatná sada: e-mail {n} musí být objekt", "en": "Invalid dataset: e-mail {n} must be an object"},
    "vd.email.fields": {"cs": "Neplatná sada: e-mail {n} nemá subject nebo body jako text",
                        "en": "Invalid dataset: e-mail {n} needs subject and body as text"},
    "vd.email.category": {"cs": "Neplatná sada: e-mail {n} má neznámou kategorii „{key}“",
                          "en": "Invalid dataset: e-mail {n} has an unknown category \"{key}\""},
    "vd.name": {"cs": "e-maily", "en": "e-mails"},
    "vp.items": {"cs": "Neplatná veřejná sada: chybí items", "en": "Invalid public dataset: items is missing"},
    "vp.texts": {"cs": "Neplatná veřejná sada: name, about a instructions musí být text",
                 "en": "Invalid public dataset: name, about and instructions must be text"},
    "vp.aboutEn": {"cs": "Neplatná veřejná sada: about_en musí být text", "en": "Invalid public dataset: about_en must be text"},
    "vp.source": {"cs": "Neplatná veřejná sada: chybí source nebo license", "en": "Invalid public dataset: source or license is missing"},
    "vp.noTasks": {"cs": "Neplatná veřejná sada: žádné úlohy", "en": "Invalid public dataset: no tasks"},
    "vp.options.count": {"cs": "Neplatná veřejná sada: {where}: štítky musí být aspoň dva",
                         "en": "Invalid public dataset: {where}: there must be at least two labels"},
    "vp.options.key": {"cs": "Neplatná veřejná sada: {where}: štítek {n} nemá key jako text",
                       "en": "Invalid public dataset: {where}: label {n} needs key as text"},
    "vp.options.description": {"cs": "Neplatná veřejná sada: {where}: popis štítku „{key}“ musí být text",
                               "en": "Invalid public dataset: {where}: the description of label \"{key}\" must be text"},
    "vp.options.duplicate": {"cs": "Neplatná veřejná sada: {where}: klíč štítku „{key}“ se opakuje",
                             "en": "Invalid public dataset: {where}: the label key \"{key}\" is used more than once"},
    "vp.oos": {"cs": "Neplatná veřejná sada: oos musí být klíč jednoho ze štítků v labels",
               "en": "Invalid public dataset: oos must be the key of one of the labels in labels"},
    "vp.item.fields": {"cs": "Neplatná veřejná sada: úloha {n} nemá text nebo label jako text",
                       "en": "Invalid public dataset: task {n} needs text and label as text"},
    "vp.item.options": {"cs": "Neplatná veřejná sada: úloha {n} nemá options a sada nemá labels",
                        "en": "Invalid public dataset: task {n} has no options and the dataset has no labels"},
    "vp.item.label": {"cs": "Neplatná veřejná sada: úloha {n} má neznámý štítek „{label}“",
                      "en": "Invalid public dataset: task {n} has an unknown label \"{label}\""},
    "vp.task": {"cs": "úloha {n}", "en": "task {n}"},
    "vp.name": {"cs": "veřejná sada", "en": "public dataset"},
    # bench.py. Záhlaví tabulky: slovo se musí vejít do šířky sloupce (o znak méně, jinak se slepí se sousedem), proto anglicky
    # zkratky dec./s (decisions/s) a conf. (confidence); slova jsou tatáž jako v záhlaví výsledků a na dlaždicích stránky.
    "bench.col.model": {"cs": "model", "en": "model"},
    "bench.col.avg": {"cs": "průměr", "en": "mean"},
    "bench.col.p50": {"cs": "medián", "en": "median"},
    "bench.col.p95": {"cs": "p95", "en": "p95"},
    "bench.col.min": {"cs": "min", "en": "min"},
    "bench.col.max": {"cs": "max", "en": "max"},
    "bench.col.rate": {"cs": "rozh./s", "en": "dec./s"},
    "bench.col.acc": {"cs": "správně", "en": "correct"},
    "bench.col.conf": {"cs": "jistota", "en": "conf."},
    "bench.col.ece": {"cs": "ECE", "en": "ECE"},
    "bench.col.stab": {"cs": "stabilita", "en": "stability"},
    "bench.col.goodput": {"cs": "goodput", "en": "goodput"},
    "bench.col.tokens": {"cs": "tokenů", "en": "tokens"},
    # Úvodní řádek běhu: {n} = počet úloh, {lanes} = pruhů nebo bran (2–8), {name} = název sady převzatý ze sady.
    "bench.intro.lanes": {"cs": "{n} situací, {lanes} pruhů, stejných pro všechny modely",
                          "en": "{n} {n|situation|situations}, {lanes} lanes, the same for all models"},
    "bench.intro.numbers": {"cs": "{n} řad čísel, {lanes} bran, stejných pro všechny modely",
                            "en": "{n} {n|row|rows} of numbers, {lanes} gates, the same for all models"},
    "bench.intro.emails": {"cs": "{n} e-mailů ze sady „{name}“ ({categories} kategorií), stejných pro všechny modely",
                           "en": "{n} {n|e-mail|e-mails} from the dataset \"{name}\" ({categories} categories), the same for all models"},
    "bench.intro.public": {"cs": "{n} úloh ze sady „{name}“ ({labels} štítků), stejných pro všechny modely",
                           "en": "{n} {n|task|tasks} from the dataset \"{name}\" ({labels} labels), the same for all models"},
    "bench.intro.publicOos": {"cs": "{n} úloh ze sady „{name}“ ({labels} štítků, mimo rozsah: {oos}), stejných pro všechny modely",
                              "en": "{n} {n|task|tasks} from the dataset \"{name}\" ({labels} labels, out of scope: {oos}), the same for all models"},
    "bench.intro.publicOwn": {"cs": "{n} úloh ze sady „{name}“ (vlastní štítky u každé úlohy), stejných pro všechny modely",
                              "en": "{n} {n|task|tasks} from the dataset \"{name}\" (every task has its own labels), the same for all models"},
    "bench.source": {"cs": "zdroj: {source} · licence: {license}", "en": "source: {source} · license: {license}"},
    # Řádky pod řádkem modelu; procenta přicházejí hotová ({f1} = „41.7%“).
    "bench.quality": {"cs": "macro-F1 {f1}, neplatných odpovědí {invalid}", "en": "macro-F1 {f1}, invalid answers {invalid}"},
    "bench.qualityRefused": {"cs": "macro-F1 {f1}, neplatných odpovědí {invalid}, z toho dotazů odmítnutých serverem (HTTP 400) {refused}",
                             "en": "macro-F1 {f1}, invalid answers {invalid}, of that, requests the server refused (HTTP 400): {refused}"},
    "bench.variants": {"cs": "přesnost variant: původní {original}, obrácené pořadí {reversed}, jiná formulace {reworded}",
                       "en": "accuracy of the variants: original {original}, reversed order {reversed}, different wording {reworded}"},
    "bench.trojan": {"cs": "trojský kůň: {n} úloh s pokynem, odolal {resisted}, podlehl {fooled}, jiná chyba {other}; přesnost bez pokynu {clean}",
                     "en": "Trojan horse: {n} {n|task|tasks} with the instruction, resisted {resisted}, fooled {fooled}, different error {other}; "
                           "accuracy without the instruction {clean}"},
    # Zákazy a výjimky (--signs --pairs): shoda dvojic úloha / dvojče; {list} = „jen/jen výjimkou 3/4, …“ (obě správně / celkem).
    "bench.pairs": {"cs": "dvojice: obě správně {both}, jen původní {original}, jen dvojče {twin}, žádná {none} ({n} dvojic)",
                    "en": "pairs: both correct {both}, original only {original}, twin only {twin}, neither {none} ({n} {n|pair|pairs})"},
    "bench.pairs.groups": {"cs": "dvojice podle tříd (obě správně / celkem): {list}", "en": "pairs by class (both correct / total): {list}"},
    # Veřejná sada. „Nevešlo se“ má stejnou stavbu jako run.didNotFit ve stránce; {reason} je důvod doslova od serveru.
    "bench.didNotFit": {"cs": "nevešlo se — server odmítl dotaz s nabídkou {labels} štítků (HTTP 400); sada se pro tento model neměří",
                        "en": "did not fit — the server refused the request; labels in the list: {labels} (HTTP 400); "
                              "bench.py does not measure the dataset for this model"},
    "bench.didNotFitReason": {"cs": "nevešlo se — server odmítl dotaz s nabídkou {labels} štítků (HTTP 400: {reason}); sada se pro tento model neměří",
                              "en": "did not fit — the server refused the request; labels in the list: {labels} (HTTP 400: {reason}); "
                                    "bench.py does not measure the dataset for this model"},
    "bench.oos": {"cs": "mimo rozsah: poznal {recall} z {n}, běžných úloh hozených do oos {leak}, přesnost bez oos {acc}",
                  "en": "out of scope: recognized {recall} of {n}, ordinary tasks put into oos {leak}, accuracy without oos {acc}"},
    "bench.worst": {"cs": "nejhorší třídy: {list}", "en": "worst classes: {list}"},
    "bench.worst.item": {"cs": "{label} F1 {f1} (úloh {n})", "en": "{label} F1 {f1} ({n} {n|task|tasks})"},
    "bench.confusions": {"cs": "nejčastější záměny: {list}", "en": "most frequent confusions: {list}"},
    "bench.none": {"cs": "žádné", "en": "none"},                             # žádné záměny
    # Poznámky za řádkem modelu. Třídy rozdílu čísel se jmenují jako ve stránce (gap.*).
    "bench.note.confusions": {"cs": "(záměny: {list})", "en": "(confusions: {list})"},
    "bench.note.wrongClear": {"cs": "(chyb při volném pruhu: {n})", "en": "(errors when a lane was free: {n})"},
    "bench.gap.tight": {"cs": "těsné", "en": "narrow"},
    "bench.gap.medium": {"cs": "střední", "en": "medium"},
    "bench.gap.clear": {"cs": "jasné", "en": "clear"},
    # Třídy značky povoleného pruhu (--signs), v poznámce za řádkem modelu a v řádku dvojic.
    "bench.sign.only": {"cs": "jen", "en": "only"},
    "bench.sign.exceptOnly": {"cs": "jen výjimkou", "en": "except-only"},
    "bench.sign.no": {"cs": "zákaz", "en": "no"},
    "bench.sign.allExcept": {"cs": "vše kromě", "en": "all-except"},
    "bench.sign.limitNo": {"cs": "mez zákazem", "en": "limit-no"},
    "bench.sign.limitUpTo": {"cs": "mez do", "en": "limit-up-to"},
    "bench.err.publicCombo": {"cs": "Volby --mirror a --inject se s --public nekombinují.",
                              "en": "The --mirror and --inject options cannot be combined with --public."},
    "bench.err.deadline": {"cs": "--deadline-ms musí být kladné číslo (limit v milisekundách).",
                           "en": "--deadline-ms must be a positive number (the deadline in milliseconds)."},
    "bench.err.limits": {"cs": "--max-usd a --max-hosted musí být nezáporná čísla.", "en": "--max-usd and --max-hosted must be non-negative numbers."},
    "bench.err.allRefused": {"cs": "server odmítl všechny dotazy (HTTP 400)", "en": "the server refused all requests (HTTP 400)"},
    # hwbench.py. Záhlaví sedmi tabulek a CSV: buňka = klíč. Šířku sloupce určuje délka záhlaví (hwbench.line), anglické slovo proto
    # smí být delší; dec./s je tatáž zkratka jako v bench.py. Slova jsou tatáž jako v panelu Hardware a ve výsledcích stránky.
    "hw.col.model": {"cs": "model", "en": "model"},
    "hw.col.n": {"cs": "N", "en": "N"},
    "hw.col.p50": {"cs": "p50 ms", "en": "p50 ms"},
    "hw.col.p90": {"cs": "p90 ms", "en": "p90 ms"},
    "hw.col.p95": {"cs": "p95 ms", "en": "p95 ms"},
    "hw.col.p99": {"cs": "p99 ms", "en": "p99 ms"},
    "hw.col.max": {"cs": "max ms", "en": "max ms"},
    "hw.col.rate": {"cs": "rozh./s", "en": "dec./s"},
    "hw.col.cold": {"cs": "studený start ms", "en": "cold start ms"},
    "hw.col.warm": {"cs": "teplý medián ms", "en": "warm median ms"},
    "hw.col.colds": {"cs": "studené starty ms", "en": "cold starts ms"},
    "hw.col.clients": {"cs": "klientů", "en": "clients"},
    "hw.col.arrival": {"cs": "tempo/s", "en": "rate/s"},                     # zadané tempo příchodů dotazů (test server)
    "hw.col.achieved": {"cs": "dosaženo/s", "en": "achieved/s"},
    "hw.col.onTime": {"cs": "včas", "en": "in time"},
    "hw.col.questions": {"cs": "otázek", "en": "questions"},
    "hw.col.msRequest": {"cs": "ms na dotaz", "en": "ms per request"},
    "hw.col.msQuestion": {"cs": "ms na otázku", "en": "ms per question"},
    "hw.col.target": {"cs": "cíl tokenů", "en": "target tokens"},
    "hw.col.inputTokens": {"cs": "input_tokens", "en": "input tokens"},      # česky jméno pole v odpovědi modelu; anglicky slovy, jako sousední target tokens
    "hw.col.ms": {"cs": "ms", "en": "ms"},
    "hw.col.minute": {"cs": "minuta", "en": "minute"},
    "hw.col.temp": {"cs": "°C", "en": "°C"},
    "hw.col.power": {"cs": "W", "en": "W"},
    "hw.col.clock": {"cs": "MHz", "en": "MHz"},
    "hw.col.time": {"cs": "čas s", "en": "time s"},
    # Buňky tabulky: stupeň, který server odmítl; podíl dotazů včas (česky s mezerou jako dosud, anglicky bez ní jako ve stránce).
    "hw.didNotFit": {"cs": "nevešlo se", "en": "did not fit"},
    "hw.pct": {"cs": "{value:.0f} %", "en": "{value:.0f}%"},
    # Řádek nad tabulkou testů concurrency a server; {value} je hodnota proměnné, nebo hw.unset.
    "hw.unset": {"cs": "nenastaveno", "en": "not set"},
    "hw.numParallel": {"cs": "OLLAMA_NUM_PARALLEL (v prostředí tohoto procesu): {value} — bez něj server dotazy řadí za sebe",
                       "en": "OLLAMA_NUM_PARALLEL (in the environment of this process): {value} — without it the server queues the requests one after another"},
    # Řádky pod tabulkou. Čísla přicházejí hotová („—“ za chybějící hodnotu). {mark} = mezera a značka hw.interrupted po Ctrl+C, jinak nic;
    # značka je jen text pro člověka, přerušení nese příznak (hwbench.Notes.interrupted).
    "hw.interrupted": {"cs": "(přerušeno)", "en": "(interrupted)"},
    "hw.skip.cold": {"cs": "{model}: test cold je jen pro lokální modely (ollama stop)",
                     "en": "{model}: the cold test is only for local models (ollama stop)"},
    "hw.skip.concurrency": {"cs": "{model}: test concurrency je jen pro lokální modely (souběžné dotazy na hostovaný model jsou mimo rozsah)",
                            "en": "{model}: the concurrency test is only for local models (hwbench.py does not measure concurrent requests to a hosted model)"},
    "hw.skip.server": {"cs": "{model}: test server je jen pro lokální modely (souběžné dotazy na hostovaný model jsou mimo rozsah)",
                       "en": "{model}: the server test is only for local models (hwbench.py does not measure concurrent requests to a hosted model)"},
    "hw.skip.soak": {"cs": "{model}: test soak u hostovaného modelu stojí peníze po celou dobu běhu — spusťte ho výslovně s --hosted-soak",
                     "en": "{model}: the soak test with a hosted model costs money for the whole run duration — run it explicitly with --hosted-soak"},
    "hw.note.truncation": {"cs": "{model}: hostovaný model dlouhý vstup nemusí odmítnout — Clef ho podle dokumentace tiše ořízne; "
                                 "„nevešlo se“ proto nemusí nastat a směrodatný je sloupec input_tokens",
                           "en": "{model}: a hosted model does not necessarily refuse a long input — according to the documentation, Clef silently truncates it; "
                                 "\"did not fit\" may therefore never appear, and the input tokens column is the one to rely on"},
    "hw.server.best": {"cs": "{model}: sériově medián {base} ms ({capacity} dotazů/s); nejvyšší udržitelné tempo {best} dotazů/s při p99 do {limit} ms{mark}",
                       "en": "{model}: serial median {base} ms ({capacity} requests/s); highest sustainable rate {best} requests/s with p99 within {limit} ms{mark}"},
    "hw.server.none": {"cs": "{model}: sériově medián {base} ms ({capacity} dotazů/s); žádný stupeň nesplnil p99 do {limit} ms{mark}",
                       "en": "{model}: serial median {base} ms ({capacity} requests/s); no rate level kept p99 within {limit} ms{mark}"},
    "hw.soak.summary": {"cs": "{model}: {n} rozhodnutí{mark}, J na rozhodnutí {j}, energie {wh} Wh, nejvyšší teplota {temp} °C, "
                              "latence poslední ÷ první minuta {ratio}",
                        "en": "{model}: {n} {n|decision|decisions}{mark}, J per decision {j}, energy {wh} Wh, highest temperature {temp} °C, "
                              "latency last ÷ first minute {ratio}"},
    # Věty, kterými hwbench.py končí. {error} je hláška systému nebo Ollamy, {name} jméno proměnné prostředí.
    "hw.err.limits": {"cs": "--max-usd a --max-hosted musí být nezáporná čísla.", "en": "--max-usd and --max-hosted must be non-negative numbers."},
    "hw.err.coldNeedsOllama": {"cs": "Test cold potřebuje program ollama v PATH (ollama stop <model>).",
                               "en": "The cold test needs the ollama program in PATH (ollama stop <model>)."},
    "hw.err.csv": {"cs": "CSV nejde zapsat: {error}", "en": "hwbench.py cannot write the CSV: {error}"},
    "hw.err.stop": {"cs": "Příkaz ollama stop selhal: {error}", "en": "The ollama stop command failed: {error}"},
    "hw.err.ollamaDown": {"cs": "Ollama nedostupná: {error}", "en": "Ollama unreachable: {error}"},
    "hw.err.cfEnv": {"cs": "Model s předponou cf: potřebuje proměnnou prostředí {name}, která není nastavená.",
                     "en": "A model with the cf: prefix needs the environment variable {name}, which is not set."},
    "hw.err.interrupted": {"cs": "Přerušeno.", "en": "Interrupted."},
    # fetch_datasets.py. Věty chyb (FetchError): {what} a {dataset} = jméno sady nebo zdroje, {url} = adresa, {why} = stav HTTP nebo hláška systému,
    # {field} = jméno pole ve zdroji, {missing} a {odd} = klíče převzaté ze zdroje. Jména sad, souborů a polí se nepřekládají.
    "fetch.err.member": {"cs": "V archivu není soubor končící {member} — zdroj změnil tvar: {url}",
                         "en": "The archive has no file ending in {member} — the source changed its format: {url}"},
    "fetch.err.download": {"cs": "Stažení se nepodařilo ({why}): {url}", "en": "The download failed ({why}): {url}"},
    "fetch.err.json": {"cs": "{what}: odpověď není JSON — zdroj změnil tvar.", "en": "{what}: the answer is not JSON — the source changed its format."},
    "fetch.err.rows": {"cs": "{dataset}: odpověď nemá rows a num_rows_total — zdroj změnil tvar.",
                       "en": "{dataset}: the answer has no rows and num_rows_total — the source changed its format."},
    "fetch.err.field": {"cs": "{what}: řádek nemá pole {field} — zdroj změnil tvar.", "en": "{what}: a row has no field {field} — the source changed its format."},
    "fetch.err.banking.utf8": {"cs": "BANKING77: soubor není text v UTF-8 — zdroj změnil tvar.",
                               "en": "BANKING77: the file is not UTF-8 text — the source changed its format."},
    "fetch.err.banking.csv": {"cs": "BANKING77: soubor nejde přečíst jako CSV — zdroj změnil tvar.",
                              "en": "BANKING77: fetch_datasets.py cannot read the file as CSV — the source changed its format."},
    "fetch.err.banking.columns": {"cs": "BANKING77: soubor nemá sloupce text a category — zdroj změnil tvar.",
                                  "en": "BANKING77: the file has no text and category columns — the source changed its format."},
    "fetch.err.banking.row": {"cs": "BANKING77: řádek nemá text a category jako text — zdroj změnil tvar.",
                              "en": "BANKING77: a row does not have text and category as text — the source changed its format."},
    "fetch.err.banking.count": {"cs": "BANKING77: čekáno 77 záměrů, zdroj jich má {n}.", "en": "BANKING77: expected 77 intents, the source has {n}."},
    "fetch.err.clinc.lists": {"cs": "CLINC150: soubor nemá seznamy test a oos_test — zdroj změnil tvar.",
                              "en": "CLINC150: the file has no test and oos_test lists — the source changed its format."},
    "fetch.err.clinc.row": {"cs": "CLINC150: řádek nemá tvar [text, záměr] — zdroj změnil tvar.",
                            "en": "CLINC150: a row does not have the format [text, intent] — the source changed its format."},
    "fetch.err.clinc.count": {"cs": "CLINC150: čekáno 150 záměrů a oos, zdroj má {n} záměrů.",
                              "en": "CLINC150: expected 150 intents and oos, the source has {n} {n|intent|intents}."},
    "fetch.err.massive.lines": {"cs": "MASSIVE: soubor en-US.jsonl není JSON Lines — zdroj změnil tvar.",
                                "en": "MASSIVE: the file en-US.jsonl is not JSON Lines — the source changed its format."},
    "fetch.err.massive.fields": {"cs": "MASSIVE: řádky části test nemají pole utt a scenario jako text — zdroj změnil tvar.",
                                 "en": "MASSIVE: the rows of the test split do not have the fields utt and scenario as text — the source changed its format."},
    "fetch.err.massive.scenarios": {"cs": "MASSIVE: čekáno {n} známých scénářů, zdroj se liší v: {odd}.",
                                    "en": "MASSIVE: expected {n} known scenarios, the source differs in: {odd}."},
    "fetch.err.horizon.none": {"cs": "Horizon-Labs: žádný český řádek zákaznické podpory se 2–10 štítky — zdroj změnil obsah.",
                               "en": "Horizon-Labs: no Czech customer support row with 2–10 labels — the source changed its content."},
    "fetch.err.cfpb.missing": {"cs": "CFPB: po {pages} stránkách chybí stížnosti tříd {missing}.",
                               "en": "CFPB: after {pages} {pages|page|pages}, complaints of these classes are missing: {missing}."},
    # Řádky běhu: {name} = jméno sady z příkazové řádky, {path} = cesta souboru, {error} = věta chyby nebo hláška systému, jak přišla.
    "fetch.err.write": {"cs": "{name}: soubor nejde zapsat ({error})", "en": "{name}: fetch_datasets.py cannot write the file ({error})"},
    "fetch.err.interrupted": {"cs": "Přerušeno.", "en": "Interrupted."},
    "fetch.saved": {"cs": "{name}: uloženo {n} úloh do {path}", "en": "{name}: saved {n} {n|task|tasks} to {path}"},
    # Průběžné hlášky čtení sady Horizon-Labs bez filtru podle jazyka: {n} = počet úloh, {pages} = strop počtu stránek, {page} = pořadí čtené stránky.
    "fetch.horizon.ended": {"cs": "Horizon-Labs: čtení skončilo dřív ({error}); ukládám {n} úloh, které se stihly nasbírat.",
                            "en": "Horizon-Labs: reading ended early ({error}); saving the {n} {n|task|tasks} collected so far."},
    "fetch.horizon.fallback": {"cs": "Horizon-Labs: filtr podle jazyka neodpovídá ({error}); čtu náhodné stránky celé sady, "
                                     "dokud nenasbírám {n} úloh, nejvýš {pages} stránek — může to trvat hodinu.",
                               "en": "Horizon-Labs: the language filter does not answer ({error}); reading random pages of the whole dataset "
                                     "to collect {n} {n|task|tasks}, at most {pages} pages — this may take an hour."},
    "fetch.horizon.fallbackFull": {"cs": "Horizon-Labs: filtr podle jazyka neodpovídá ({error}); čtu náhodné stránky celé sady, všechny — potrvá to mnoho hodin.",
                                   "en": "Horizon-Labs: the language filter does not answer ({error}); reading all pages of the whole dataset "
                                         "in random order — this will take many hours."},
    "fetch.horizon.progress": {"cs": "Horizon-Labs: čtu {page}. stránku, zatím {n} úloh…", "en": "Horizon-Labs: reading page {page}, {n} {n|task|tasks} so far…"},
    # Rozsah staženého souboru: jde do popisu sady (proměnná {scope}) a česky i do názvu sady, který se nepřekládá.
    "fetch.scope.full": {"cs": "celá testovací část, {n} úloh", "en": "the whole test split, {n} {n|task|tasks}"},
    "fetch.scope.sample": {"cs": "vzorek {n} z {pool}, semínko {seed}", "en": "a sample of {n} out of {pool}, seed {seed}"},
    # Popisy sad pro diváka: fetch_datasets.py zapisuje do souboru obě podoby (about česky, about_en anglicky) bez ohledu na jazyk běhu.
    # {scope} = fetch.scope.*, {today} = datum stažení. Jména sad, citace a jména polí zdroje se nepřekládají.
    "fetch.about.banking77": {
        "cs": "BANKING77 (PolyAI): anglické dotazy klientů internetové banky, úkolem je poznat záměr ze 77 možných. "
              "Testovací část sady. Citace: Casanueva et al., Efficient Intent Detection with Dual Sentence Encoders, 2020. Rozsah: {scope}; staženo {today}.",
        "en": "BANKING77 (PolyAI): English queries from customers of an online bank; the task is to recognize the intent out of 77 possible ones. "
              "The test split of the dataset. Citation: Casanueva et al., Efficient Intent Detection with Dual Sentence Encoders, 2020. "
              "Scope: {scope}; downloaded {today}."},
    "fetch.about.clinc150": {
        "cs": "CLINC150 (Larson et al., An Evaluation Dataset for Intent Classification and Out-of-Scope Prediction, 2019): "
              "anglické věty pro hlasového asistenta, 150 záměrů a k nim věty „mimo rozsah“ (štítek oos), které žádnému záměru nepatří. "
              "Testovací část sady (data_full.json: test a oos_test). Rozsah: {scope}; staženo {today}.",
        "en": "CLINC150 (Larson et al., An Evaluation Dataset for Intent Classification and Out-of-Scope Prediction, 2019): "
              "English sentences for a voice assistant, 150 intents and, with them, \"out of scope\" sentences (the oos label), which belong to no intent. "
              "The test split of the dataset (data_full.json: test and oos_test). Scope: {scope}; downloaded {today}."},
    "fetch.about.massive": {
        "cs": "MASSIVE 1.1 (Amazon; FitzGerald et al., MASSIVE: A 1M-Example Multilingual Natural Language Understanding Dataset "
              "with 51 Typologically-Diverse Languages, 2022), anglická část en-US: věty pro hlasového asistenta, úkolem je poznat, kterému "
              "z 18 scénářů (oblastí) věta patří. Testovací část sady (partition test). Anglické věty pocházejí ze sady SLURP "
              "(Bastianelli et al., 2020). Štítky jsou holá jména scénářů bez popisu. Stejnou úlohu (18 scénářů, 350 vět téže části) "
              "měřili autoři modelu Nimble, ti ale dali každému scénáři větu popisu. Rozsah: {scope}; staženo {today}.",
        "en": "MASSIVE 1.1 (Amazon; FitzGerald et al., MASSIVE: A 1M-Example Multilingual Natural Language Understanding Dataset "
              "with 51 Typologically-Diverse Languages, 2022), the English part en-US: sentences for a voice assistant; the task is to recognize "
              "which of the 18 scenarios (domains) the sentence belongs to. The test split of the dataset (partition test). The English sentences come from "
              "the SLURP dataset (Bastianelli et al., 2020). The labels are bare scenario names without a description. The authors of the Nimble model "
              "measured the same task (18 scenarios, 350 sentences of the same split), but they gave every scenario a one-sentence description. "
              "Scope: {scope}; downloaded {today}."},
    "fetch.about.horizonCs": {
        "cs": "Horizon-Labs multilingual-zeroshot-synthetic, české řádky typu zákaznická podpora: krátká česká zpráva "
              "a k ní 2–10 štítků (většinou anglických), ze kterých se vybírá jeden; každá úloha má vlastní nabídku. Texty i štítky napsal jazykový model "
              "(Qwen) a člověk je neověřil — podle autorů s asi 7 % odvozených štítků nesouhlasil jejich vlastní model a pod češtinou je občas veden jiný jazyk. "
              "Sada je trénovací, ne testovací; vybrané řádky: jazyk Czech, druh short nebo tax_short, jeden správný štítek. Rozsah: {scope}; staženo {today}.",
        "en": "Horizon-Labs multilingual-zeroshot-synthetic, the Czech customer support rows: a short Czech message "
              "and 2–10 labels (mostly English) to pick one from; every task has its own list. A language model (Qwen) wrote both the texts and the labels "
              "and no human verified them — according to the authors, their own model disagreed with about 7% of the derived labels, and now and then "
              "a row filed under Czech is in another language. The dataset is a training set, not a test set; the selected rows: language Czech, "
              "kind short or tax_short, one correct label. Scope: {scope}; downloaded {today}."},
    # {per_class} = stížností z každé třídy, {max_chars} = nejvyšší délka textu, {mirror} = jméno kopie databáze na Hugging Face.
    "fetch.about.cfpb": {
        "cs": "Stížnosti spotřebitelů z databáze amerického úřadu CFPB (Consumer Complaint Database), anglicky: {per_class} stížností "
              "z každého ze šesti finančních produktů; úkolem je poznat, kterého produktu se stížnost týká. Text je zkrácený nejvýš "
              "na {max_chars} znaků a předmět je u všech stejný. Úřad osobní údaje nahrazuje znaky XXXX. Původ textů: kopie "
              "databáze na Hugging Face ({mirror}), stažená z úřadu v únoru 2024, CC0 podle karty kopie; úřad sám texty "
              "stížností přes své rozhraní dnes nevydává. Vzorek byl z kopie stažen {today}.",
        "en": "Consumer complaints from the database of the US agency CFPB (Consumer Complaint Database), in English: {per_class} "
              "{per_class|complaint|complaints} for each of the six financial products; the task is to recognize which product the complaint is about. "
              "The text is shortened to at most {max_chars} characters and the subject is the same for all of them. The agency replaces personal data "
              "with the characters XXXX. Origin of the texts: a copy of the database on Hugging Face ({mirror}), downloaded from the agency "
              "in February 2024, CC0 according to the card of the copy; the agency itself does not release the complaint texts through its API today. "
              "fetch_datasets.py downloaded the sample from the copy on {today}."},
    # providers.py. Věty chyb hostovaného modelu stojí za popiskem poskytovatele („Jev 401: …“); popisky Jev, Cloudflare a Test jsou vlastní jména
    # a nepřekládají se, prov.label je náhradní popisek modelu bez poskytovatele. Žádná věta nenese klíč: cizí text ({body}, {fault}, {errors})
    # do ní jde až vyčištěný (providers.scrub) a zkrácený. Částka má znak dolaru česky za číslem, anglicky před ním, jako ve stránce;
    # „$ / 1000 decisions“ je jednotka pojmenovaná jako sloupec stránky (col.usd) a nápověda bench.py, proto stojí za číslem i anglicky.
    "prov.testWho": {"cs": "zkušební poskytovatel (nic neodchází)", "en": "test provider (nothing leaves this computer)"},
    "prov.label": {"cs": "Hostovaný model", "en": "Hosted model"},
    # Strop útraty (providers.Budget): {requests} = odeslané pokusy, {max} = strop, {usd} = známá útrata, {held} = odhad držený ve stropu.
    "prov.cap.requests": {"cs": "strop útraty — odesláno {requests} hostovaných dotazů, limit je {max} (PICKBENCH_MAX_HOSTED); další dotaz se neposlal",
                          "en": "spend cap — {requests} hosted {requests|request|requests} sent, the cap is {max} (PICKBENCH_MAX_HOSTED); "
                                "PickBench did not send the next request"},
    "prov.cap.usd": {"cs": "strop útraty — utraceno {usd:.4f} $, limit je {max:g} $ (PICKBENCH_MAX_USD); další dotaz se neposlal",
                     "en": "spend cap — ${usd:.4f} spent, the cap is ${max:g} (PICKBENCH_MAX_USD); PickBench did not send the next request"},
    "prov.cap.usdHeld": {"cs": "strop útraty — utraceno {usd:.4f} $ a {held:.4f} $ drží odhad dotazů na cestě nebo bez známé ceny, limit je {max:g} $ "
                               "(PICKBENCH_MAX_USD); další dotaz se neposlal",
                         "en": "spend cap — ${usd:.4f} spent and the cap counts ${held:.4f} as an estimate for requests in progress or without a known price, "
                               "the cap is ${max:g} (PICKBENCH_MAX_USD); PickBench did not send the next request"},
    # Dotaz, který se neposlal (providers.Refused): {model} = jméno modelu, jak přišlo, {offered} = nabízené modely, {name} = jméno proměnné prostředí.
    "prov.notOffered": {"cs": "model „{model}“ není v seznamu hostovaných modelů ({offered})",
                        "en": "the model \"{model}\" is not in the list of hosted models ({offered})"},
    "prov.keyMissing": {"cs": "model {model} potřebuje proměnnou prostředí {name}, která není nastavená",
                        "en": "the model {model} needs the environment variable {name}, which is not set"},
    "prov.keyChars": {"cs": "proměnná prostředí {name} obsahuje znaky, které v ní být nesmí (mezera, konec řádku, znak mimo ASCII)",
                      "en": "the environment variable {name} contains characters that it must not contain (a space, a line break, a non-ASCII character)"},
    "prov.payload": {"cs": "dotaz musí být objekt se state a questions", "en": "the request must be an object with state and questions"},
    "prov.tooBig": {"cs": "dotaz má {size} bajtů; hostovanému modelu se posílá nejvýš {max}",
                    "en": "the request is {size} bytes long; PickBench sends at most {max} to a hosted model"},
    # Odeslaný dotaz (providers.post): {s} a {max} = sekundy, {names} = jména proměnných prostředí, {attempts} = počet pokusů,
    # {fault} = hláška systému, {body} a {errors} = text od poskytovatele doslova.
    "prov.timeout": {"cs": "poskytovatel neodpověděl do {s} s", "en": "the provider did not answer within {s} s"},
    "prov.badKey": {"cs": "klíč neplatí nebo nemá oprávnění (proměnné prostředí: {names})",
                    "en": "the key is not valid or lacks permission (environment variables: {names})"},
    "prov.dropped": {"cs": "spojení se nepodařilo po {attempts} pokusech ({fault})",
                     "en": "the connection failed after {attempts} {attempts|attempt|attempts} ({fault})"},
    "prov.afterAttempts": {"cs": "{body} (po {attempts} pokusech)", "en": "{body} (after {attempts} {attempts|attempt|attempts})"},
    "prov.retryAfter": {"cs": "poskytovatel žádá počkat {asked:g} s (Retry-After), víc než {max} s; dotaz se neopakuje",
                        "en": "the provider asks to wait {asked:g} s (Retry-After), more than {max} s; PickBench does not retry the request"},
    "prov.cfError": {"cs": "poskytovatel hlásí chybu: {errors}", "en": "the provider reports an error: {errors}"},
    "prov.notObject": {"cs": "odpověď není objekt JSON: {body}", "en": "the answer is not a JSON object: {body}"},
    # Síť (odhad) a řádek útraty pod řádkem hostovaného modelu (bench.py, hwbench.py): {host} = server poskytovatele, {failed} = druh chyby,
    # {error} = věta chyby, {label} a {who} = poskytovatel, {sent} = odeslané pokusy, {per} = dolary na 1000 rozhodnutí, nebo „—“.
    "prov.connectFailed": {"cs": "spojení se serverem {host} se nepodařilo ({failed})", "en": "the connection to the server {host} failed ({failed})"},
    "prov.net": {"cs": "síť (odhad): spojení TCP + TLS {ms:.0f} ms", "en": "network (estimate): TCP + TLS connection {ms:.0f} ms"},
    "prov.netFailed": {"cs": "síť (odhad): nezměřeno ({error})", "en": "network (estimate): not measured ({error})"},
    "prov.spent": {"cs": "hostovaný model ({label}, {who}): {sent} dotazů, {tokens} vstupních tokenů, {usd:.4f} $ ({per} $ / 1000 rozh.), "
                         "opakování {retries}; latence včetně sítě",
                   "en": "hosted model ({label}, {who}): {sent} {sent|request|requests}, {tokens} input {tokens|token|tokens}, ${usd:.4f} "
                         "({per} $ / 1000 decisions), retries {retries}; latency includes the network"},
    "prov.spentUnpriced": {"cs": "hostovaný model ({label}, {who}): {sent} dotazů, {tokens} vstupních tokenů, {usd:.4f} $ ({per} $ / 1000 rozh.), "
                                 "opakování {retries}; latence včetně sítě; {unpriced} odpovědí bez údaje o tokenech — cena je spodní odhad",
                           "en": "hosted model ({label}, {who}): {sent} {sent|request|requests}, {tokens} input {tokens|token|tokens}, ${usd:.4f} "
                                 "({per} $ / 1000 decisions), retries {retries}; latency includes the network; "
                                 "{unpriced} {unpriced|answer|answers} without a token count — the price is a lower bound"},
    # store.py: {path} = cesta k souboru databáze, {error} = hláška SQLite, {text} = číslo, jak stálo v JSON.
    "store.unusable": {"cs": "Databáze {path} nejde použít ({error}); výsledky, nastavení a fronta se neukládají.",
                       "en": "The server cannot use the database {path} ({error}); it does not save the results, the settings or the queue."},
    "store.resultObject": {"cs": "výsledek musí být objekt", "en": "the result must be an object"},
    "store.notFinite": {"cs": "{text} není platné číslo JSON", "en": "{text} is not a valid JSON number"},
    # server.py: věty odpovědí v jazyce požadavku (hlavička X-PickBench-Lang, bez ní česky). „Požadavek“ je požadavek HTTP na server hry
    # (anglicky HTTP request), na rozdíl od dotazu do modelu (request). {max} = bajty, {error} = hláška systému nebo chyby programu,
    # {id} = číslo výsledku z adresy, {type} = jméno výjimky. Odpověď 404 „not found“ a věty od Ollamy („Ollama 400: …“) se nepřekládají.
    "srv.foreign": {"cs": "požadavek z cizí stránky", "en": "the HTTP request came from another web page"},
    "srv.body.missing": {"cs": "tělo požadavku chybí", "en": "the HTTP request body is missing"},
    "srv.body.tooBig": {"cs": "tělo požadavku je větší než {max} bajtů", "en": "the HTTP request body is larger than {max} bytes"},
    "srv.body.notJson": {"cs": "tělo požadavku není JSON", "en": "the HTTP request body is not JSON"},
    "srv.body.notFinite": {"cs": "tělo požadavku obsahuje NaN nebo Infinity; ty v JSON být nesmí",
                           "en": "the HTTP request body contains NaN or Infinity; JSON does not allow them"},
    "srv.body.notObject": {"cs": "tělo požadavku musí být objekt JSON", "en": "the HTTP request body must be a JSON object"},
    "srv.ollamaDown": {"cs": "Ollama nedostupná: {error}", "en": "Ollama unreachable: {error}"},      # stejná věta jako hw.err.ollamaDown
    "srv.netHostedOnly": {"cs": "odhad sítě je jen pro hostované modely", "en": "the network estimate is only for hosted models"},
    "srv.noModel": {"cs": "chybí jméno modelu", "en": "the model name is missing"},
    "srv.noResult": {"cs": "výsledek č. {id} v databázi není", "en": "result no. {id} is not in the database"},
    "srv.badResultId": {"cs": "číslo výsledku musí být kladné celé číslo", "en": "the result number must be a positive integer"},
    "srv.hostedFault": {"cs": "hostovaný model: {type}: {error}", "en": "hosted model: {type}: {error}"},
    # Řádky, které server vypíše při startu, v jazyce procesu (PICKBENCH_LANG): {port}, adresa Ollamy, cesta k databázi; {offered} = jména
    # nabízených hostovaných modelů, {usd} a {requests} = strop útraty. Klíče se nevypisují nikdy.
    "srv.start": {"cs": "PickBench běží na http://127.0.0.1:{port}  (Ollama: {ollama}, databáze: {db})  — Ctrl+C ukončí",
                  "en": "PickBench is running at http://127.0.0.1:{port}  (Ollama: {ollama}, database: {db})  — Ctrl+C stops it"},
    "srv.start.hosted": {"cs": "Hostované modely: {offered} — strop útraty {usd:g} $ a {requests} dotazů na běh serveru (PICKBENCH_MAX_USD, PICKBENCH_MAX_HOSTED)",
                         "en": "Hosted models: {offered} — spend cap ${usd:g} and {requests} {requests|request|requests} per server run "
                               "(PICKBENCH_MAX_USD, PICKBENCH_MAX_HOSTED)"},
}

_request = threading.local()                         # jazyk požadavku (jen server, jen vlákno požadavku)
_process = None                                      # jazyk procesu (volba --lang); None = podle prostředí


class LangError(ValueError):
    """Neznámý jazyk; text je věta pro uživatele v obou jazycích."""


def known(value):
    """Kód jazyka malými písmeny, je-li to cs nebo en; jinak None."""
    code = value.strip().lower() if isinstance(value, str) else ""
    return code if code in LANGS else None


def from_env(env=None):
    """Jazyk z PICKBENCH_LANG; nenastavená nebo prázdná proměnná = cs, neznámá hodnota → LangError."""
    value = ((os.environ if env is None else env).get(ENV) or "").strip()
    if not value:
        return DEFAULT
    if known(value) is None:
        raise LangError(both("lang.unknown", value=value))
    return known(value)


def lang():
    """Jazyk, který právě platí. Nesmysl v PICKBENCH_LANG tady nevadí (cs); větou ho odmítá pop_lang při startu nástroje."""
    code = getattr(_request, "lang", None) or _process
    if code:
        return code
    try:
        return from_env()
    except LangError:
        return DEFAULT


@contextlib.contextmanager
def use(code):
    """Jazyk požadavku pro toto vlákno (server); neznámý kód nebo None = cs, jako u klienta bez hlavičky."""
    before = getattr(_request, "lang", None)
    _request.lang = known(code) or DEFAULT
    try:
        yield
    finally:
        _request.lang = before


@contextlib.contextmanager
def process(code):
    """Jazyk procesu po dobu běhu nástroje (platí i v jeho pracovních vláknech); potom se vrátí předchozí."""
    global _process
    before, _process = _process, known(code) or DEFAULT
    try:
        yield
    finally:
        _process = before


def pop_lang(argv, env=None):
    """Vyjme --lang X nebo --lang=X odkudkoli z argumentů. Vrací (jazyk, zbylé argumenty).

    Bez volby platí PICKBENCH_LANG, jinak cs. Neznámá hodnota (volby i proměnné) nebo volba bez hodnoty → SystemExit s větou."""
    rest, value, i = [], None, 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--lang":
            if i + 1 >= len(argv):
                raise SystemExit(both("lang.missing"))
            value, i = argv[i + 1], i + 1
        elif arg.startswith("--lang="):
            value = arg[len("--lang="):]
        else:
            rest.append(arg)
        i += 1
    try:
        if value is None:
            return from_env(env), rest
        if known(value) is None:
            raise LangError(both("lang.unknown", value=value))
    except LangError as e:
        raise SystemExit(str(e)) from None
    return known(value), rest


def fill(text, values):
    """Doplní do věty proměnné; chybějící proměnná zůstane vidět jako {jméno}, věta kvůli ní nespadne."""
    def put(m):
        name, spec, one, other = m.groups()
        if name not in values:
            return m.group(0)
        if one is not None:
            return one if values[name] == 1 else other
        return format(values[name], spec) if spec else str(values[name])
    return FIELD.sub(put, text)


def fields(text):
    """Jména proměnných ve větě (pro test, že česká a anglická podoba mají stejné)."""
    return {m.group(1) for m in FIELD.finditer(text)}


def t(key, /, **values):                             # klíč jen pozičně: věta smí mít proměnnou jménem {key}
    """Věta pod klíčem v platném jazyce. Neznámý klíč je chyba programu (KeyError)."""
    return fill(TEXTS[key][lang()], values)


def both(key, /, **values):
    """Věta v obou jazycích za sebou (tam, kde jazyk ještě není známý)."""
    return " / ".join(fill(TEXTS[key][code], values) for code in LANGS)


def pick(texts):
    """Z {"cs": …, "en": …} podoba v platném jazyce (nápověda nástrojů)."""
    return texts[lang()]
