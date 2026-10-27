"""Měření obsluhy rozhodovacího modelu a hardwaru z příkazové řádky (bez hry).

Použití:  python hwbench.py <test> [--n N] [--minutes M] [--p99-ms L] [--rates a,b,c] [--csv soubor]
                                   [--idle S] [--hosted-soak] [--max-usd X] [--max-hosted N] model [model ...]
Příklad:  python hwbench.py latency --n 200 tev1
          python hwbench.py soak --minutes 3 --csv soak.csv tev1
          python hwbench.py server --p99-ms 1000 tev1
          python hwbench.py quant nimble:9b-q4_K_M nimble:latest

Testy:
  latency      N dotazů po sobě (výchozí 1000): p50, p90, p95, p99, max, rozh./s
  cold         ollama stop <model>, první dotaz, pak 5 teplých; 3×: studený start (medián) a teplý medián
  concurrency  1, 2, 4, 8 souběžných klientů, každý N/klientů dotazů (výchozí N 200): rozh./s a p95
  questions    1, 2, 4, 8, 16 otázek v jednom dotazu, 30 dotazů na stupeň: ms na dotaz a na otázku;
               stupeň, který server odmítne (HTTP 400), se vypíše jako „nevešlo se“ a větší se nezkouší
  context      stav vycpaný na přibližně 100, 500, 1000, 2000, 4000 tokenů, 20 dotazů na stupeň;
               odmítnutý stupeň (HTTP 400) se vypíše jako „nevešlo se“ a větší se nezkouší
  soak         dotazy po dobu M minut (výchozí 10), GPU se vzorkuje přibližně po 1 s: souhrn po minutách,
               J na rozhodnutí, poměr latence poslední ÷ první minuta; CSV má řádek na každé rozhodnutí
               --idle S: před měřením S sekund bez dotazů (výchozí 0 = ne); pod souhrnem klidový příkon a J na rozhodnutí
               bez klidu. Od 2 minut ještě průběh p50, příkonu a teploty po minutách (znaky ▁ až █).
  server       scénář Server podle MLPerf: dotazy přicházejí náhodně (Poissonův proces, pevné semínko) zadaným tempem
               bez ohledu na to, jestli předchozí odpověď dorazila; latence se měří od plánovaného příchodu (fronta se počítá).
               Stupně tempa jsou 0,25 až 2násobek sériové rychlosti modelu (nebo --rates v dotazech za sekundu),
               N dotazů na stupeň (výchozí 200); hledá se nejvyšší tempo, při kterém p99 zůstane do --p99-ms
               (výchozí 1000). Stupeň, který limit nesplní nebo nedoběhne, je poslední.
  cache        stejný stav N× po sobě (výchozí 10) proti N pokaždé jiným stavům, 3 kola: medián prvního dotazu,
               p50 opakování, p50 jiných stavů a poměr stejný ÷ jiný (pod 1 = opakovaný stav je rychlejší)
  repeat       20 stavů, každý N× (výchozí 10), po kolech: u kolika stavů byla volba pokaždé stejná, u kolika i jistota,
               a největší rozptyl jistoty (opakovatelnost odpovědí)
  quant        N dotazů (výchozí 100): kvantizace a parametry modelu, GB na disku a v paměti podle Ollamy (/api/show,
               /api/tags, /api/ps), p50, p95 a správně proti ideálnímu řidiči; dva tagy téhož modelu = dva modely v příkazu

Pojmenování podle MLPerf: latency = SingleStream (uvádí p90), concurrency = Offline (nejvyšší propustnost),
server = Server. Jde o měření ve stylu MLPerf, ne o výsledek podaný podle pravidel MLCommons.

Dotaz je vždy stejný a malý: překážky, 3 pruhy (stejné úlohy jako bench.py).
Před každým testem kromě cold jde zahřívací dotaz, který se nepočítá.
--lang cs|en:  jazyk výstupu; bez volby platí proměnná prostředí PICKBENCH_LANG, jinak cs.
Hostovaný model (jev:jev-1.13.0, cf:clef-flash, cf:clef — viz bench.py) běží u poskytovatele a stojí peníze:
testy cold, concurrency, server a quant ho přeskočí; soak běží jen s přepínačem --hosted-soak a nevzorkuje GPU;
pod tabulkou je útrata a odhad sítě a latence zahrnuje síť. --max-usd a --max-hosted jsou strop útraty jako v bench.py.
Během měření nesmí běžet hra v prohlížeči (dotazy by se praly o kartu).
"""
USAGE_EN = """Measures how this machine serves a decision model, and its hardware, from the command line (without the game).

Usage:    python hwbench.py <test> [--n N] [--minutes M] [--p99-ms L] [--rates a,b,c] [--csv file]
                                   [--idle S] [--hosted-soak] [--max-usd X] [--max-hosted N] model [model ...]
Examples: python hwbench.py latency --n 200 tev1
          python hwbench.py soak --minutes 3 --csv soak.csv tev1
          python hwbench.py server --p99-ms 1000 tev1
          python hwbench.py quant nimble:9b-q4_K_M nimble:latest

Tests:
  latency      N requests one after another (default 1000): p50, p90, p95, p99, max, dec./s
  cold         ollama stop <model>, the first request, then 5 warm ones; 3×: cold start (median) and warm median
  concurrency  1, 2, 4, 8 concurrent clients, each sends N/clients requests (default N 200): dec./s and p95
  questions    1, 2, 4, 8, 16 questions in one request, 30 requests per level: ms per request and per question;
               hwbench.py prints a level that the server refuses (HTTP 400) as "did not fit" and does not try larger ones
  context      the state padded to about 100, 500, 1000, 2000, 4000 tokens, 20 requests per level;
               hwbench.py prints a refused level (HTTP 400) as "did not fit" and does not try larger ones
  soak         requests for M minutes (default 10), with the GPU sampled about once a second: a summary per minute,
               J per decision, the latency ratio last ÷ first minute; the CSV has a row for every decision
               --idle S: S seconds without requests before the measurement (default 0 = no); below the summary the idle power draw
               and J per decision without idle. From 2 minutes on also the course of p50, power draw and temperature by minute (▁ to █).
  server       MLPerf's Server scenario: requests arrive at random (a Poisson process, fixed seed) at the given rate
               whether or not the previous answer has arrived; the test measures latency from the planned arrival (waiting in the queue counts).
               The rate levels are 0.25 to 2 times the serial speed of the model (or --rates in requests per second),
               N requests per level (default 200); the test looks for the highest rate at which p99 stays within --p99-ms
               (default 1000). A level that misses the limit or does not finish is the last one.
  cache        the same state N times in a row (default 10) against N different states, 3 rounds: the median of the first request,
               p50 of the repeats, p50 of the different states and the ratio same ÷ different (below 1 = the repeated state is faster)
  repeat       20 states, each N times (default 10), in rounds: for how many states the choice was the same every time, for how many
               also the confidence, and the largest spread of the confidence (repeatability of the answers)
  quant        N requests (default 100): the quantization and parameters of the model, GB on disk and in memory as Ollama reports them
               (/api/show, /api/tags, /api/ps), p50, p95 and correct against the ideal driver; two tags of one model = two models in the command

The names follow MLPerf: latency = SingleStream (reports p90), concurrency = Offline (the highest throughput),
server = Server. This is a measurement in the style of MLPerf, not a result submitted under the MLCommons rules.

The request is always the same small one: obstacles, 3 lanes (the same tasks as bench.py).
Before every test except cold a warm-up request goes first, and it does not count.
--lang cs|en:  output language; without the option the environment variable PICKBENCH_LANG applies, otherwise cs.
A hosted model (jev:jev-1.13.0, cf:clef-flash, cf:clef — see bench.py) runs at the provider and costs money:
the cold, concurrency, server and quant tests skip it; soak runs only with the --hosted-soak option and does not sample the GPU;
below the table are the spend and the network estimate, and the latency includes the network. --max-usd and --max-hosted are the spend cap as in bench.py.
The game must not run in the browser during the measurement (the requests would compete for the GPU).
"""
import argparse
import csv
import os
import random
import shutil
import statistics
import subprocess
import sys
import threading
import json
import time
import urllib.error
import urllib.request

import bench
import gpu
import i18n
import providers

TESTS = ("latency", "cold", "concurrency", "questions", "context", "soak", "server", "cache", "repeat", "quant")
SERVER_SHARES = (0.25, 0.5, 0.7, 0.85, 1.0, 1.25, 1.5, 2.0)      # stupně tempa testu server jako podíl sériové rychlosti (1000 ÷ medián latence)
SERVER_SLACK, SERVER_DRAIN = 30.0, 120.0                         # s: rezerva stupně nad poslední příchod; čekání na dotazy, které po stupni ještě běží
CACHE_ROUNDS = 3                                                 # test cache: kola (nový stav N× po sobě, pak N jiných stavů)
REPEAT_STATES = 20                                               # test repeat: počet stavů, každý se ptá N× po kolech
GB = 1e9                                                         # test quant: GB jako Ollama (10⁹ B)
NO_INFO = {"quant": None, "params": None, "disk_gb": None, "mem_gb": None}      # údaje o modelu, které se nezjistily
SPARK = "▁▂▃▄▅▆▇█"                                               # průběh soaku po minutách: nejnižší až nejvyšší hodnota
NAN = float("nan")
KEYS = bench.lane_keys(3)
QUESTION = bench.lane_question(3)
FILLER = "the weather report for today mentions light wind and a few clouds over the hills in the afternoon".split()


# ---------- dotazy ----------
def lane_state(lane, dists):
    return {"car_is_in": KEYS[lane], **{k: bench.describe(d) for k, d in zip(KEYS, dists)}}


def payloads(n, seed=1):
    """N dotazů na stejných úlohách jako bench.py (překážky, 3 pruhy)."""
    return [{"state": lane_state(lane, dists), "questions": QUESTION} for lane, dists in bench.make_cases(n, 3, seed)]


def warm(model, post):
    """Zahřívací dotaz (načtení modelu), nepočítá se. Vrací odpověď."""
    return post(model, {"state": lane_state(1, [99, 99, 99]), "questions": QUESTION})[0]


def percentile(values, p):
    """Stejný výpočet jako ve hře: prvek na pozici floor(p × n) seřazených hodnot. Bez dat NaN."""
    if not values:
        return NAN
    s = sorted(values)
    return s[min(len(s) - 1, int(p * len(s)))]


def mean(values):
    """Průměr hodnot, které nejsou None. Bez dat NaN."""
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else NAN


def multi_question(k):
    """K kopií téže otázky pod klíči q1..qK."""
    return {f"q{i + 1}": QUESTION["target"] for i in range(k)}


def filler(words):
    return " ".join(FILLER[i % len(FILLER)] for i in range(words))


def pad_state(state, words):
    """Stav s polem background o daném počtu slov (přibližně 1 slovo = 1 token); 0 slov = stav beze změny."""
    return {**state, "background": filler(words)} if words > 0 else dict(state)


def refused(e):
    """Odmítl server dotaz stavem HTTP 400, nebo jde o jinou chybu (jiný stav HTTP, síť, Ollama neběží)?

    Stav 400 znamená jen to, že server dotaz odmítl; proč, se tu nezjišťuje. Ollama tak odmítá příliš dlouhý prompt
    a verze 0.35.1 podle své hlášky mimo jiné i nabídku mimo 2–26 možností.
    U hostovaných modelů nese stav ProviderError.status; providers.find_error ji najde v řetězu příčin, z věty se nečte."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code == 400
    found = providers.find_error(e)                  # stav nese chyba sama; z věty se nečte
    return found is not None and found.status == 400


# ---------- testy ----------
def run_latency(model, n=1000, post=None):
    post = post or bench.post
    warm(model, post)
    lat = [post(model, p)[1] for p in payloads(n)]
    return {"n": n, "p50": percentile(lat, .5), "p90": percentile(lat, .9), "p95": percentile(lat, .95),
            "p99": percentile(lat, .99), "max": max(lat), "rate": 1000 * n / sum(lat)}


def ollama_stop(model):
    subprocess.run(["ollama", "stop", model], capture_output=True, timeout=60, check=True)


def run_cold(model, post=None, stop=ollama_stop, rounds=3, warm_n=5):
    post = post or bench.post
    work = payloads(rounds * (warm_n + 1))
    colds, warms = [], []
    for r in range(rounds):
        batch = work[r * (warm_n + 1):(r + 1) * (warm_n + 1)]
        stop(model)                                  # model z paměti ven → první dotaz ho načítá znovu
        colds.append(post(model, batch[0])[1])
        warms += [post(model, p)[1] for p in batch[1:]]
    return {"cold": statistics.median(colds), "warm": statistics.median(warms), "colds": colds}


def run_concurrency(model, n=200, levels=(1, 2, 4, 8), post=None, clock=time.perf_counter):
    post = post or bench.post
    warm(model, post)
    rows = []
    for clients in levels:
        work = payloads(max(1, n // clients) * clients)
        lat, errors = [], []

        def client(chunk):
            try:
                for p in chunk:
                    lat.append(post(model, p)[1])
            except Exception as e:                   # chyba ve vlákně se nesmí ztratit
                errors.append(e)

        threads = [threading.Thread(target=client, args=(work[i::clients],), daemon=True) for i in range(clients)]
        t0 = clock()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        secs = clock() - t0
        if errors:
            raise errors[0]
        rows.append({"clients": clients, "n": len(lat), "rate": len(lat) / secs, "p95": percentile(lat, .95)})
    return rows


def run_questions(model, levels=(1, 2, 4, 8, 16), reps=30, post=None):
    post = post or bench.post
    warm(model, post)
    states = [p["state"] for p in payloads(reps)]
    rows = []
    for k in levels:
        questions = multi_question(k)
        try:
            ms = statistics.median(post(model, {"state": s, "questions": questions})[1] for s in states)
        except Exception as e:
            if not refused(e):
                raise
            rows.append({"questions": k, "ms": None, "per_question": None})      # prompt s tolika otázkami server odmítl
            break                                    # víc otázek se nevejde tím spíš; změřené stupně zůstávají
        rows.append({"questions": k, "ms": ms, "per_question": ms / k})
    return rows


def run_context(model, sizes=(100, 500, 1000, 2000, 4000), reps=20, post=None):
    post = post or bench.post
    base = (warm(model, post).get("usage") or {}).get("input_tokens") or 0       # tokeny dotazu bez vycpávky
    states = [p["state"] for p in payloads(reps)]
    rows = []
    for size in sizes:
        lat, tokens = [], None
        try:
            for s in states:
                out, ms = post(model, {"state": pad_state(s, size - base), "questions": QUESTION})
                lat.append(ms)
                tokens = (out.get("usage") or {}).get("input_tokens") or tokens
        except Exception as e:
            if not refused(e):
                raise
            rows.append({"target": size, "fit": False, "ms": None, "tokens": None})
            break                                    # větší stupně se nevejdou tím spíš
        rows.append({"target": size, "fit": True, "ms": statistics.median(lat), "tokens": tokens})
    return rows


def arrivals(rate, n, seed=1):
    """Časy příchodů n dotazů v sekundách od začátku: Poissonův proces o tempu rate dotazů/s, pevné semínko."""
    rng, t, out = random.Random(seed), 0.0, []
    for _ in range(n):
        t += rng.expovariate(rate)
        out.append(t)
    return out


def start_thread(work, *args):
    t = threading.Thread(target=work, args=args, daemon=True)
    t.start()
    return t


def run_server_level(model, rate, n, limit_ms, post, clock=time.perf_counter, sleep=time.sleep, spawn=start_thread, seed=1):
    """Jeden stupeň testu server: n dotazů přichází tempem rate/s bez ohledu na to, jestli předchozí odpověď už dorazila.

    Latence = dokončení − plánovaný příchod, takže čekání ve frontě se počítá. Stupeň má horní mez trvání (poslední
    příchod + rezerva); co se do ní neodešle nebo nedokončí, je pozdě. ok = všech n dokončeno a p99 do limitu."""
    due_times, work = arrivals(rate, n, seed), payloads(n)
    budget = due_times[-1] + max(SERVER_SLACK, 20 * limit_ms / 1000)
    lat, errors, lock, jobs = [], [], threading.Lock(), []
    t0 = clock()

    def one(payload, due):
        try:
            post(model, payload)
            ms = (clock() - t0 - due) * 1000
            with lock:
                lat.append(ms)
        except Exception as e:                       # chyba ve vlákně se nesmí ztratit
            errors.append(e)

    for payload, due in zip(work, due_times):
        wait = due - (clock() - t0)
        if wait > 0:
            sleep(wait)
        if clock() - t0 > budget:
            break
        jobs.append(spawn(one, payload, due))
    for job in jobs:
        job.join(max(0.0, budget - (clock() - t0)))
    elapsed = clock() - t0
    with lock:
        got = list(lat)
    if errors:
        raise errors[0]
    if len(got) < len(jobs):                         # přetíženo: nechat doběhnout rozdělané dotazy, ať nezkreslí další měření
        drain = clock() + SERVER_DRAIN
        for job in jobs:
            job.join(max(0.0, drain - clock()))
    p99 = percentile(got, .99)
    return {"rate": rate, "sent": len(jobs), "done": len(got), "achieved": len(got) / elapsed if elapsed > 0 else NAN,
            "p50": percentile(got, .5), "p99": p99, "on_time": sum(1 for ms in got if ms <= limit_ms) / n,
            "ok": len(got) == n and p99 <= limit_ms}


def run_server(model, n=200, limit_ms=1000, rates=None, post=None, clock=time.perf_counter, sleep=time.sleep, spawn=start_thread):
    """Stupně tempa od nejnižšího; první, který limit p99 nesplní, je poslední. Ctrl+C vrátí dosud změřené stupně."""
    post = post or bench.post
    rows, interrupted, base = [], False, NAN
    try:
        warm(model, post)
        base = statistics.median(post(model, p)[1] for p in payloads(20))        # sériová latence → rychlost po jednom
        levels = sorted(rates) if rates else [round(1000 / base * share, 2) for share in SERVER_SHARES]      # --rates smí přijít v jakémkoli pořadí
        for rate in levels:
            row = run_server_level(model, rate, n, limit_ms, post, clock, sleep, spawn)
            rows.append(row)
            if not row["ok"]:
                break                                # přetížený stupeň: vyšší tempo se nezkouší
    except KeyboardInterrupt:                        # Ctrl+C: dosud změřené stupně se nezahodí
        interrupted = True
    return {"base_ms": base, "capacity": 1000 / base if base == base else NAN, "rows": rows,
            "best": max((r["rate"] for r in rows if r["ok"]), default=None), "interrupted": interrupted, "limit_ms": limit_ms}


def run_cache(model, n=10, rounds=CACHE_ROUNDS, post=None):
    """Vliv cache promptu. Každé kolo: nový stav n× po sobě, pak n pokaždé jiných stavů (úlohy jako bench.py, překážky, 3 pruhy).

    Vrací first = medián prvních dotazů s novým stavem, same = p50 opakování (2. až n. dotaz), fresh = p50 jiných stavů
    a ratio = same ÷ fresh (pod 1 = opakovaný stav je rychlejší). n = 1: opakování nejsou, same a ratio jsou NaN."""
    post = post or bench.post
    warm(model, post)
    work = payloads(rounds * (n + 1))
    firsts, same, fresh = [], [], []
    for r in range(rounds):
        block = work[r * (n + 1):(r + 1) * (n + 1)]
        lat = [post(model, block[0])[1] for _ in range(n)]
        firsts.append(lat[0])
        same += lat[1:]
        fresh += [post(model, p)[1] for p in block[1:]]
    s, f = percentile(same, .5), percentile(fresh, .5)
    return {"n": n, "rounds": rounds, "first": statistics.median(firsts), "same": s, "fresh": f,
            "ratio": s / f if s == s and f == f and f > 0 else NAN}


def run_repeat(model, n=10, states=REPEAT_STATES, post=None):
    """Opakovatelnost: states stavů, každý n× po kolech (kolo = každý stav jednou, ve stejném pořadí), takže stejný dotaz
    nejde dvakrát po sobě. Vrací same_choice = počet stavů se stejnou volbou ve všech n odpovědích (i neplatná None je hodnota),
    same_conf = počet stavů s číselnou jistotou, všechny n hodnoty přesně stejné, a spread = největší (max − min) jistoty
    přes stavy (bez číselné jistoty NaN)."""
    post = post or bench.post
    warm(model, post)
    work = payloads(states)
    seen = [[] for _ in work]
    for _ in range(n):
        for i, p in enumerate(work):
            out = post(model, p)[0]
            answers = out.get("answers") if isinstance(out, dict) else None
            a = answers.get("target") if isinstance(answers, dict) else None
            a = a if isinstance(a, dict) else {}
            c, choice = a.get("confidence"), a.get("choice")
            seen[i].append((choice if isinstance(choice, str) else None, c if type(c) in (int, float) and c == c else None))
    confs = [[c for _, c in s] for s in seen]
    spreads = [max(v) - min(v) for v in ([c for c in cs if c is not None] for cs in confs) if v]
    return {"states": states, "n": n, "same_choice": sum(len({ch for ch, _ in s}) == 1 for s in seen),
            "same_conf": sum(None not in cs and len(set(cs)) == 1 for cs in confs), "spread": max(spreads) if spreads else NAN}


def ollama_info(model, base=None, opener=None, timeout=10):
    """Kvantizace, počet parametrů a velikost modelu podle Ollamy: POST /api/show (details.quantization_level,
    details.parameter_size), GET /api/tags (size na disku), GET /api/ps (size_vram v paměti; po zahřátí).

    Jméno bez tagu se hledá i jako „:latest“. Co Ollama nevrátí nebo co nejde přečíst (Ollama neběží, starší verze), je None.
    opener se bere při volání, ne při importu: testy podstrkují urllib.request.urlopen."""
    base, opener = base or bench.OLLAMA, opener or urllib.request.urlopen
    names = {model, model if ":" in model else model + ":latest"}

    def ask(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with opener(req, timeout=timeout) as r:
                doc = json.loads(r.read())
        except Exception:                            # Ollama neběží, nezná cestu, odpověď není JSON
            return {}
        return doc if isinstance(doc, dict) else {}

    def entry(doc):
        models = doc.get("models") if isinstance(doc.get("models"), list) else []
        return next((m for m in models if isinstance(m, dict) and (m.get("name") in names or m.get("model") in names)), {})
    details = ask("/api/show", {"model": model}).get("details")
    details = details if isinstance(details, dict) else {}
    text = lambda v: v if isinstance(v, str) and v else None
    gb = lambda v: round(v / GB, 1) if type(v) in (int, float) and v > 0 else None
    return {"quant": text(details.get("quantization_level")), "params": text(details.get("parameter_size")),
            "disk_gb": gb(entry(ask("/api/tags")).get("size")), "mem_gb": gb(entry(ask("/api/ps")).get("size_vram"))}


def run_quant(model, n=100, post=None, info=None):
    """Srovnání tagů téhož modelu: zahřátí, údaje o modelu (info, výchozí ollama_info; po zahřátí ho už zná /api/ps),
    pak n dotazů na úlohy bench.make_cases(n, 3): p50, p95 a correct = počet odpovědí, které vybral i ideální řidič."""
    post = post or bench.post
    warm(model, post)
    facts = (info or ollama_info)(model)
    lat, ok = [], 0
    for (lane, dists), p in zip(bench.make_cases(n, 3), payloads(n)):
        out, ms = post(model, p)
        lat.append(ms)
        answers = out.get("answers") if isinstance(out, dict) else None
        a = answers.get("target") if isinstance(answers, dict) else None
        choice = a.get("choice") if isinstance(a, dict) else None
        ok += isinstance(choice, str) and choice in {KEYS[i] for i in bench.oracle(lane, dists)}
    return {"n": n, "p50": percentile(lat, .5), "p95": percentile(lat, .95), "correct": ok, **NO_INFO, **facts}


class GpuSampler:
    """Vlákno, které po dobu měření čte stav GPU. Vzorek = (čas od startu v s, teplota, příkon, takt)."""

    def __init__(self, read=gpu.read, interval=1.0, clock=time.perf_counter):
        self.read, self.interval, self.clock = read, interval, clock
        self.samples = []
        self._halt = threading.Event()
        self._thread = None

    def start(self):
        self.t0 = self.clock()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        seen, misses = False, 0
        while True:
            g = self.read()
            if g.get("available"):
                seen, misses = True, 0
                self.samples.append((self.clock() - self.t0, g.get("temp_c"), g.get("power_w"), g.get("clock_mhz")))
            else:                                    # jedno nepovedené čtení může být jen pomalé probuzení karty
                misses += 1
                if not seen and misses >= gpu.GIVE_UP:
                    return                           # GPU tu není, další čtení nemá smysl
            if self._halt.wait(self.interval):
                return

    def stop(self):
        self._halt.set()
        if self._thread:
            self._thread.join()
        return self.samples


def soak_minutes(decisions, samples):
    """Souhrn po minutách z rozhodnutí [(čas v s, ms)] a vzorků GPU [(čas v s, teplota, příkon, takt)]."""
    rows = []
    for m in range(int(decisions[-1][0] // 60) + 1 if decisions else 0):
        lat = [ms for t, ms in decisions if m * 60 <= t < (m + 1) * 60]
        smp = [s for s in samples if m * 60 <= s[0] < (m + 1) * 60]
        rows.append({"minute": m + 1, "n": len(lat), "p50": percentile(lat, .5), "p95": percentile(lat, .95),
                     "temp": mean(s[1] for s in smp), "power": mean(s[2] for s in smp), "clock": mean(s[3] for s in smp)})
    return rows


def soak_rows(decisions, samples):
    """Řádky CSV: ke každému rozhodnutí poslední vzorek GPU pořízený před ním (nebo prázdné hodnoty)."""
    rows, i = [], -1
    for t, ms in decisions:
        while i + 1 < len(samples) and samples[i + 1][0] <= t:
            i += 1
        s = samples[i] if i >= 0 else (None, None, None, None)
        rows.append([round(t, 3), round(ms, 1), s[1], s[2], s[3]])
    return rows


def soak_summary(decisions, samples, duration=None):
    """duration = délka běhu v s: vzorky pak musí pokrývat celý běh a energie se počítá na celý běh (viz gpu.energy_j)."""
    minutes = soak_minutes(decisions, samples)
    ratio = minutes[-1]["p50"] / minutes[0]["p50"] if len(minutes) >= 2 and minutes[-1]["n"] and minutes[0]["n"] else NAN
    joules = gpu.energy_j([(s[0], s[2]) for s in samples], *((0, duration) if duration is not None else ()))
    temps = [s[1] for s in samples if s[1] is not None]
    return {"minutes": minutes, "ratio": ratio, "joules": joules,
            "j_per_decision": joules / len(decisions) if decisions else NAN,
            "max_temp": max(temps) if temps else None}


def run_idle(sampler, seconds, sleep=time.sleep):
    """Klidový příkon: seconds s bez dotazů se vzorkuje GPU. watts = průměr příkonu ze vzorků; méně než gpu.MIN_SAMPLES → NaN."""
    sampler.start()
    try:
        sleep(seconds)
    finally:
        samples = sampler.stop()
    watts = [s[2] for s in samples if type(s[2]) in (int, float) and s[2] == s[2]]
    return {"seconds": seconds, "samples": len(watts), "watts": sum(watts) / len(watts) if len(watts) >= gpu.MIN_SAMPLES else NAN}


def spark(values):
    """Řádek znaků SPARK od nejnižší po nejvyšší hodnotu; všechny stejné → prostřední znak; chybějící hodnota (None, NaN) → „·“."""
    ok = lambda v: type(v) in (int, float) and v == v
    nums = [v for v in values if ok(v)]
    lo, hi = (min(nums), max(nums)) if nums else (0, 0)
    pick = lambda v: SPARK[3] if hi == lo else SPARK[round((v - lo) / (hi - lo) * (len(SPARK) - 1))]
    return "".join(pick(v) if ok(v) else "·" for v in values)


def spark_notes(model, minutes):
    """Průběh soaku po minutách (od 2 minut): p50, příkon a teplota; řádek bez jediné hodnoty se vynechá. Klíče stojí doslova."""
    if len(minutes) < 2:
        return []
    out = []

    def part(field):
        vals = [m[field] for m in minutes]
        nums = [v for v in vals if v is not None and v == v]
        return (spark(vals), min(nums), max(nums)) if nums else None
    if p := part("p50"):
        out.append(i18n.t("hw.soak.chartLatency", model=model, line=p[0], lo=num(p[1], 1), hi=num(p[2], 1)))
    if p := part("power"):
        out.append(i18n.t("hw.soak.chartPower", model=model, line=p[0], lo=num(p[1], 1), hi=num(p[2], 1)))
    if p := part("temp"):
        out.append(i18n.t("hw.soak.chartTemp", model=model, line=p[0], lo=num(p[1]), hi=num(p[2])))
    return out


def run_soak(model, minutes=10, post=None, clock=time.perf_counter, sampler=None):
    """Dotazy po dobu M minut. sampler má start() a stop() -> vzorky; None = GPU se nevzorkuje."""
    post = post or bench.post
    warm(model, post)
    work = payloads(1000)
    decisions, interrupted = [], False
    if sampler:
        sampler.start()
    t0 = clock()
    try:
        while (t := clock() - t0) < minutes * 60:
            decisions.append((t, post(model, work[len(decisions) % len(work)])[1]))
    except KeyboardInterrupt:                        # Ctrl+C: dosud naměřené se nezahodí
        interrupted = True
    finally:
        duration = clock() - t0
        samples = sampler.stop() if sampler else []
    return {"decisions": decisions, "samples": samples, "interrupted": interrupted, "duration": duration, **soak_summary(decisions, samples, duration)}


# ---------- výstup ----------
def num(x, digits=0):
    return "—" if x is None or x != x else f"{x:.{digits}f}"


def num_parallel(env=None):
    return (os.environ if env is None else env).get("OLLAMA_NUM_PARALLEL") or i18n.t("hw.unset")


def usage():
    """Nápověda v platném jazyce: česky dokumentační řetězec modulu, anglicky USAGE_EN."""
    return i18n.pick({"cs": __doc__, "en": USAGE_EN})


def interrupted_mark():
    """Značka přerušeného měření ve větě pod tabulkou, v platném jazyce. Čte se při použití: konstanta by zůstala v jazyce, ve kterém se modul načetl.

    Jen text pro člověka: Ctrl+C pozná run podle Notes.interrupted, ne podle věty."""
    return i18n.t("hw.interrupted")


def heads(test):
    """Záhlaví tabulky a záhlaví CSV jednoho testu v platném jazyce: (záhlaví, záhlaví CSV). Texty se berou při volání, ne při importu.

    Šířku sloupce určuje délka záhlaví (line), takže řádky stojí pod záhlavím v obou jazycích. CSV má jiné sloupce jen u testu soak
    (řádek na každé rozhodnutí)."""
    model, n = i18n.t("hw.col.model"), i18n.t("hw.col.n")
    if test == "latency":
        head = [model, n, i18n.t("hw.col.p50"), i18n.t("hw.col.p90"), i18n.t("hw.col.p95"), i18n.t("hw.col.p99"), i18n.t("hw.col.max"), i18n.t("hw.col.rate")]
    elif test == "cold":
        head = [model, i18n.t("hw.col.cold"), i18n.t("hw.col.warm"), i18n.t("hw.col.colds")]
    elif test == "concurrency":
        head = [model, i18n.t("hw.col.clients"), n, i18n.t("hw.col.rate"), i18n.t("hw.col.p95")]
    elif test == "server":
        head = [model, i18n.t("hw.col.arrival"), i18n.t("hw.col.achieved"), i18n.t("hw.col.p50"), i18n.t("hw.col.p99"), i18n.t("hw.col.onTime")]
    elif test == "questions":
        head = [model, i18n.t("hw.col.questions"), i18n.t("hw.col.msRequest"), i18n.t("hw.col.msQuestion")]
    elif test == "context":
        head = [model, i18n.t("hw.col.target"), i18n.t("hw.col.inputTokens"), i18n.t("hw.col.ms")]
    elif test == "cache":
        head = [model, n, i18n.t("hw.col.first"), i18n.t("hw.col.sameP50"), i18n.t("hw.col.freshP50"), i18n.t("hw.col.ratio")]
    elif test == "repeat":
        head = [model, i18n.t("hw.col.states"), n, i18n.t("hw.col.sameChoice"), i18n.t("hw.col.sameConf"), i18n.t("hw.col.confSpread")]
    elif test == "quant":
        head = [model, i18n.t("hw.col.quant"), i18n.t("hw.col.params"), i18n.t("hw.col.diskGb"), i18n.t("hw.col.memGb"),
                i18n.t("hw.col.p50"), i18n.t("hw.col.p95"), i18n.t("hw.col.correct")]
    else:                                            # soak
        card = [i18n.t("hw.col.temp"), i18n.t("hw.col.power"), i18n.t("hw.col.clock")]
        return ([model, i18n.t("hw.col.minute"), n, i18n.t("hw.col.p50"), i18n.t("hw.col.p95"), *card],
                [model, i18n.t("hw.col.time"), i18n.t("hw.col.ms"), *card])
    return head, head


class Notes(list):
    """Řádky pod tabulku. interrupted = měření přerušil uživatel (Ctrl+C): run další model nezačne.

    Příznak, ne značka ve větě: věta je v jazyce uživatele a jen pro člověka."""

    interrupted = False


def noted(lines, interrupted):
    out = Notes(lines)
    out.interrupted = bool(interrupted)
    return out


def hosted_skip(a):
    """Test, který hostovaný model vůbec nepouští (a nic za něj neplatí)."""
    return a.test in ("cold", "concurrency", "server", "quant") or (a.test == "soak" and not a.hosted_soak)


def spent_notes(model, before, net):
    """Řádky pod tabulku u hostovaného modelu: útrata od stavu počítadla before a odhad sítě. Kde nic neodešlo, nic."""
    spent = providers.spent_line(model, before, providers.BUDGET.state())
    return [f"{model}: {text}" for text in (spent, net) if text]


def measure(a, model, post=None, sampler=None, stop=ollama_stop, clock=time.perf_counter, info=None, idle_sampler=None, sleep=time.sleep):
    """Jeden test pro jeden model. Vrací (záhlaví, řádky tabulky, záhlaví CSV, řádky CSV, řádky textu pod tabulku)."""
    local = not providers.hosted(model)
    head, csv_head = heads(a.test)
    mark = lambda interrupted: f" {interrupted_mark()}" if interrupted else ""      # proměnná {mark} věty: mezera a značka přerušení, nebo nic
    if a.test == "latency":
        r = run_latency(model, a.n or 1000, post)
        rows = [[model, r["n"], num(r["p50"], 1), num(r["p90"], 1), num(r["p95"], 1), num(r["p99"], 1), num(r["max"], 1), num(r["rate"], 1)]]
        return head, rows, csv_head, rows, []
    if a.test == "cold":
        if not local:
            return head, [], csv_head, [], [i18n.t("hw.skip.cold", model=model)]
        r = run_cold(model, post, stop)
        rows = [[model, num(r["cold"]), num(r["warm"], 1), " / ".join(num(c) for c in r["colds"])]]
        return head, rows, csv_head, rows, []
    if a.test == "concurrency":
        if not local:
            return head, [], csv_head, [], [i18n.t("hw.skip.concurrency", model=model)]
        rows = [[model, r["clients"], r["n"], num(r["rate"], 1), num(r["p95"], 1)] for r in run_concurrency(model, a.n or 200, post=post, clock=clock)]
        return head, rows, csv_head, rows, []
    if a.test == "server":
        if not local:
            return head, [], csv_head, [], [i18n.t("hw.skip.server", model=model)]
        r = run_server(model, a.n or 200, a.p99_ms, a.rates, post, time.perf_counter, time.sleep, start_thread)
        rows = [[model, num(x["rate"], 2), num(x["achieved"], 2), num(x["p50"], 1), num(x["p99"], 1), i18n.t("hw.pct", value=x["on_time"] * 100)] for x in r["rows"]]
        serial = {"model": model, "base": num(r["base_ms"], 1), "capacity": num(r["capacity"], 1), "limit": num(r["limit_ms"]), "mark": mark(r["interrupted"])}
        if r["best"] is not None:                    # dvě podoby verdiktu, každá jako celá věta pod svým klíčem
            note = i18n.t("hw.server.best", best=num(r["best"], 2), **serial)
        else:
            note = i18n.t("hw.server.none", **serial)
        return head, rows, csv_head, rows, noted([note], r["interrupted"])
    if a.test == "questions":
        rows = [[model, r["questions"], i18n.t("hw.didNotFit") if r["ms"] is None else num(r["ms"], 1), num(r["per_question"], 1)]
                for r in run_questions(model, post=post)]
        return head, rows, csv_head, rows, []
    if a.test == "context":
        rows = [[model, r["target"], r["tokens"] or "—", num(r["ms"], 1) if r["fit"] else i18n.t("hw.didNotFit")] for r in run_context(model, post=post)]
        return head, rows, csv_head, rows, [] if local else [i18n.t("hw.note.truncation", model=model)]
    if a.test == "cache":
        r = run_cache(model, a.n or 10, post=post)
        rows = [[model, r["n"], num(r["first"], 1), num(r["same"], 1), num(r["fresh"], 1), num(r["ratio"], 2)]]
        return head, rows, csv_head, rows, []
    if a.test == "repeat":
        r = run_repeat(model, a.n or 10, post=post)
        rows = [[model, r["states"], r["n"], f"{r['same_choice']}/{r['states']}", f"{r['same_conf']}/{r['states']}", num(r["spread"], 3)]]
        return head, rows, csv_head, rows, []
    if a.test == "quant":
        if not local:
            return head, [], csv_head, [], [i18n.t("hw.skip.quant", model=model)]
        facts = info or (ollama_info if post is None else (lambda m: NO_INFO))      # s podstrčeným post (testy) se na Ollamu nesahá
        r = run_quant(model, a.n or 100, post, facts)
        rows = [[model, r["quant"] or "—", r["params"] or "—", num(r["disk_gb"], 1), num(r["mem_gb"], 1), num(r["p50"], 1), num(r["p95"], 1),
                 f"{r['correct']}/{r['n']}"]]
        return head, rows, csv_head, rows, []
    if not local and not a.hosted_soak:
        return head, [], csv_head, [], [i18n.t("hw.skip.soak", model=model)]
    idle = run_idle(idle_sampler or GpuSampler(), a.idle, sleep) if local and a.idle > 0 else None      # klid před zahřátím; hostovaný model GPU nemá
    r = run_soak(model, a.minutes, post, clock, (sampler or GpuSampler()) if local else None)
    rows = [[model, m["minute"], m["n"], num(m["p50"], 1), num(m["p95"], 1), num(m["temp"]), num(m["power"], 1), num(m["clock"])] for m in r["minutes"]]
    notes = [i18n.t("hw.soak.summary", model=model, n=len(r["decisions"]), mark=mark(r["interrupted"]), j=num(r["j_per_decision"], 2),
                    wh=num(r["joules"] / 3600, 3), temp=num(r["max_temp"]), ratio=num(r["ratio"], 2))]
    if idle is not None:                             # J bez klidu = (energie běhu − klidový příkon × délka běhu) ÷ rozhodnutí
        net = (r["joules"] - idle["watts"] * r["duration"]) / len(r["decisions"]) if r["decisions"] else NAN
        notes.append(i18n.t("hw.soak.idle", model=model, watts=num(idle["watts"], 1), k=idle["samples"], seconds=num(a.idle), net=num(net, 2)))
    notes += spark_notes(model, r["minutes"])
    return head, rows, csv_head, [[model, *row] for row in soak_rows(r["decisions"], r["samples"])], noted(notes, r["interrupted"])


def parse_args(argv):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--n", type=int)
    p.add_argument("--minutes", type=float, default=10)
    p.add_argument("--p99-ms", type=float, default=1000)
    p.add_argument("--rates", type=lambda text: [float(x) for x in text.split(",")])
    p.add_argument("--csv")
    p.add_argument("--hosted-soak", action="store_true")
    p.add_argument("--idle", type=float, default=0)              # soak: sekund klidu před měřením; 0 = bez odečtu klidu
    p.add_argument("--max-usd", type=float)
    p.add_argument("--max-hosted", type=int)
    p.add_argument("rest", nargs="*")
    a = p.parse_intermixed_args(argv)                # volby smí stát mezi testem a modely
    a.test = a.rest[0] if a.rest else None
    a.models = a.rest[1:]
    return a


def line(cells, head):
    """Řádek tabulky: první sloupec zleva, ostatní zprava na šířku záhlaví; buňka širší než sloupec („nevešlo se“) dostane mezeru navíc."""
    return f"{cells[0]:<20}" + "".join(f"{str(c):>{max(len(h) + 2, 10, len(str(c)) + 1)}}" for c, h in zip(cells[1:], head[1:]))


def run(argv, post=None, sampler=None, stop=ollama_stop, which=shutil.which, out=print, clock=time.perf_counter, info=None, idle_sampler=None, sleep=time.sleep):
    """Celý běh nástroje v jazyce, který už platí (volbu --lang vyjímá main)."""
    try:
        a = parse_args(argv)
    except (SystemExit, ValueError):                 # nečitelná volba (třeba --rates bez čísel)
        sys.exit(usage())
    if a.test not in TESTS or not a.models or (a.n is not None and a.n < 1) or not a.minutes > 0:
        sys.exit(usage())
    if not a.p99_ms > 0 or (a.rates is not None and not all(rate > 0 for rate in a.rates)):
        sys.exit(usage())
    if not a.idle >= 0 or (a.idle and a.test != "soak"):           # i NaN; --idle jen se soak
        sys.exit(usage())
    if a.max_usd is not None or a.max_hosted is not None:
        if (a.max_usd is not None and not 0 <= a.max_usd < float("inf")) or (a.max_hosted is not None and a.max_hosted < 0):
            sys.exit(i18n.t("hw.err.limits"))
        providers.BUDGET = providers.Budget(a.max_usd, a.max_hosted)             # strop útraty tohoto spuštění
    if a.test == "cold" and not which("ollama"):
        sys.exit(i18n.t("hw.err.coldNeedsOllama"))
    try:                                             # soubor otevřít hned: chyba cesty se má ukázat před měřením, ne po něm
        f = open(a.csv, "w", newline="", encoding="utf-8-sig") if a.csv else None
    except OSError as e:
        sys.exit(i18n.t("hw.err.csv", error=e))
    writer = csv.writer(f, delimiter=";") if f else None
    if a.test in ("concurrency", "server"):
        out(i18n.t("hw.numParallel", value=num_parallel()) + "\n")
    try:
        for i, model in enumerate(a.models):
            paid = post is None and providers.hosted(model) and not hosted_skip(a)       # s podstrčeným post (testy) se neplatí a na síť se nesahá
            if paid:
                providers.resolve(model)             # model, který nejde zavolat (není v seznamu, chybí klíč), skončí větou před prvním dotazem
            before, net = providers.BUDGET.state(), providers.net_line(model) if paid else None
            try:
                head, rows, csv_head, csv_rows, notes = measure(a, model, post, sampler, stop, clock, info, idle_sampler, sleep)
            except BaseException:                    # i po chybě nebo Ctrl+C: co se utratilo, se vypíše
                for text in spent_notes(model, before, net):
                    out(text)
                raise
            interrupted = getattr(notes, "interrupted", False)
            notes = notes + spent_notes(model, before, net)
            if i == 0:
                out(line(head, head))
                if writer:
                    try:
                        writer.writerow(csv_head)
                    except OSError as e:             # chyba zápisu do CSV se nesmí hlásit jako nedostupná Ollama (viz níže)
                        sys.exit(i18n.t("hw.err.csv", error=e))
            for row in rows:
                out(line(row, head))
            for note in notes:
                out(note)
            if writer:
                try:
                    writer.writerows(csv_rows)
                    f.flush()
                except OSError as e:
                    sys.exit(i18n.t("hw.err.csv", error=e))
            if interrupted:
                break                                # Ctrl+C při měření: tento model je vypsaný, další se nezačíná
    except urllib.error.HTTPError as e:              # stav a tělo odpovědi Ollamy: vlastní jméno, číslo a cizí text, žádné slovo k překladu
        sys.exit(f"Ollama {e.code}: {e.read().decode(errors='replace')[:300] if e.fp else e.reason}")
    except subprocess.SubprocessError as e:          # ollama stop skončil chybou nebo nedoběhl (Ollama neběží, model neexistuje)
        sys.exit(i18n.t("hw.err.stop", error=e))
    except (urllib.error.URLError, OSError) as e:
        sys.exit(i18n.t("hw.err.ollamaDown", error=e))
    except RuntimeError as e:                        # chyba hostovaného modelu (providers.ProviderError: „Cloudflare {stav}: …“, strop útraty, chybějící klíč); jiná RuntimeError je chyba programu
        if not providers.is_provider_error(e):
            raise
        sys.exit(str(e))
    except KeyError as e:                            # model cf: bez přihlašovacích proměnných (os.environ v bench.post); jiná KeyError je chyba programu
        if e.args[:1] not in (("CLOUDFLARE_ACCOUNT_ID",), ("CLOUDFLARE_API_TOKEN",)):
            raise
        sys.exit(i18n.t("hw.err.cfEnv", name=e.args[0]))
    except KeyboardInterrupt:
        sys.exit(i18n.t("hw.err.interrupted"))
    finally:
        if f:
            f.close()


def main(argv, **kwargs):
    lang, rest = i18n.pop_lang(argv)                 # --lang smí stát kdekoli; neznámý jazyk končí větou
    with i18n.process(lang):                         # jazyk procesu: platí i ve vláknech testů concurrency a server
        return run(rest, **kwargs)


if __name__ == "__main__":
    main(sys.argv[1:])
