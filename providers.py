"""Hostované rozhodovací modely: Jev (TypeSafe AI) a Clef / Clef-flash (Cloudflare Workers AI).

Jediné místo, které hostované modely volá; bench.py, hwbench.py i server.py jdou přes post().
Model s předponou určuje poskytovatele: jev:<model> → TypeSafe, cf:<model> → Cloudflare. Model bez předpony je
lokální Ollama a sem se nedostane. Nabízené modely a jejich adresy jsou pevné (MODELS, PROVIDERS): volající
adresu ani cestu neurčuje a do těla dotazu jdou jen pole model, state a questions.

Klíče se čtou jen z proměnných prostředí procesu (TYPESAFE_API_KEY, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN)
a nikam se nevracejí: každý text chyby prochází přes scrub() a u stavů 401 a 403 se tělo odpovědi poskytovatele
do chyby nedává vůbec. Věty chyb a řádky výpisu jsou v jazyce, který platí ve chvíli volání (i18n: jazyk požadavku
na serveru, jinak jazyk nástroje nebo PICKBENCH_LANG); pravidlo o klíčích platí v obou jazycích stejně.

Tělo dotazu i odpovědi je u obou poskytovatelů stejné jako u Ollamy (/v1/systemone); Cloudflare odpověď balí
do {"result": …}. Co se liší jinak, řeší se tady a nikde jinde.

Zkušební poskytovatel (test:echo, test:drahy) existuje jen s proměnnou prostředí PICKBENCH_TEST_PROVIDER=1:
nic nikam neposílá a odpovídá sám. Slouží testům a ověření hry v prohlížeči bez klíčů.
"""
import functools
import http.client
import json
import os
import re
import socket
import ssl
import threading
import time

import i18n

PRICES_AS_OF = "2026-10-06"                          # ke kterému dni platí ceny v MODELS (zdroj: PROVIDERS[…]["prices"])
TEST_ENV = "PICKBENCH_TEST_PROVIDER"                 # "1" = zkušební poskytovatel existuje
TIMEOUT = 30                                         # s na jeden pokus
MAX_ATTEMPTS = 4                                     # pokusů na dotaz celkem
BACKOFF = (1, 2, 4, 8)                               # s čekání před 2., 3., 4. (a případným 5.) pokusem, když poskytovatel neřekne Retry-After
MAX_WAIT = 60                                        # s; delší Retry-After dotaz ukončí chybou (běh bez dozoru nemá stát hodinu)
MAX_BODY = 64_000                                    # bajtů; větší tělo se hostovanému modelu neposílá (jeden omyl nesmí stát tisíce tokenů)
CONNECT_TIMEOUT = 10                                 # s na zkušební spojení (odhad sítě)
DEFAULT_MAX_USD = 1.0                                # strop útraty procesu v dolarech; mění ho PICKBENCH_MAX_USD
DEFAULT_MAX_HOSTED = 5000                            # strop počtu odeslaných hostovaných dotazů; mění ho PICKBENCH_MAX_HOSTED
EST_BYTES_PER_TOKEN = 3                              # odhad tokenů z délky dotazu, jen pro strop útraty (stejné pravidlo jako odhad kroku fronty ve hře)

PROVIDERS = {
    "jev": {"label": "Jev", "who": "TypeSafe AI", "host": "api.typesafe.ai", "keys": ("TYPESAFE_API_KEY",),
            "prices": "https://docs.typesafe.ai/models"},
    "cf": {"label": "Cloudflare", "who": "Cloudflare Workers AI", "host": "api.cloudflare.com",
           "keys": ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"),
           "prices": "https://developers.cloudflare.com/workers-ai/platform/pricing/"},
    "test": {"label": "Test", "who_key": "prov.testWho", "host": "", "keys": (), "prices": ""},      # who_key: věta, ne vlastní jméno — text až při použití (who)
}
# usd_per_m = cena za milion vstupních tokenů v dolarech (výstup žádný z poskytovatelů neúčtuje)
MODELS = {
    "jev:jev-1.13.0": {"provider": "jev", "name": "jev-1.13.0", "usd_per_m": 0.042},     # pevná verze, aby šla čísla zopakovat
    "cf:clef-flash": {"provider": "cf", "name": "clef-flash", "usd_per_m": 0.09},
    "cf:clef": {"provider": "cf", "name": "clef", "usd_per_m": 0.24},
    "test:echo": {"provider": "test", "name": "echo", "usd_per_m": 0.1},
    "test:drahy": {"provider": "test", "name": "drahy", "usd_per_m": 1000.0},            # ať jde strop útraty ověřit pár dotazy
}

KEY_RE = re.compile(r"[\x21-\x7e]+")                            # klíč: tisknutelné ASCII bez mezer (jinak by ho http.client vypsal ve své chybě)
ACCOUNT_RE = re.compile(r"[A-Za-z0-9]+")                        # ID účtu jde do adresy


def _env(env):
    return os.environ if env is None else env


def provider_of(model, env=None):
    """Poskytovatel podle předpony modelu ("jev", "cf", v testovacím režimu "test"); None = lokální Ollama."""
    if not isinstance(model, str) or ":" not in model:
        return None
    pid = model.split(":", 1)[0]
    if pid == "test":
        return "test" if _env(env).get(TEST_ENV) == "1" else None
    return pid if pid in PROVIDERS else None


def hosted(model, env=None):
    return provider_of(model, env) is not None


def _key(env, name):
    """Hodnota proměnné prostředí bez okrajových mezer; prázdná = nenastavená (None)."""
    return (_env(env).get(name) or "").strip() or None


def secrets(env=None):
    """Hodnoty všech nastavených klíčů (i ID účtu) — to, co se nikde nesmí objevit."""
    return [value for p in PROVIDERS.values() for name in p["keys"] if (value := _key(env, name))]


def _units(char):
    """Kódové jednotky znaku v zápisu \\uXXXX: jedna, nad U+FFFF dvojice (jak to píše JSON)."""
    code = ord(char)
    return [code] if code < 0x10000 else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]


@functools.lru_cache(maxsize=32)
def _secret_pattern(secret):
    """Výraz, který najde hodnotu klíče v každém zápisu, který JSON dovoluje: každý znak buď doslova, nebo jako \\uXXXX
    (číslice a–f malými i velkými písmeny), a " \\ / ještě krátce (\\" \\\\ \\/). Zápisy se v jednom výskytu smějí míchat."""
    parts = []
    for char in secret:
        forms = ["".join(r"\\u" + "".join(f"[{d}{d.upper()}]" if d.isalpha() else d for d in f"{unit:04x}") for unit in _units(char))]
        if char in '"\\/':
            forms.append(re.escape("\\" + char))
        forms.append(re.escape(char))                # delší zápisy jsou ve výrazu první
        parts.append("(?:" + "|".join(forms) + ")")
    return re.compile("".join(parts))


def scrub(text, env=None):
    """Text bez klíčů: každou hodnotu klíče delší než 3 znaky nahradí *** — v každém zápisu, který dovoluje JSON (doslova,
    \\uXXXX, krátké \\" \\\\ \\/, i smíchané): tělo odpovědi se čistí ještě jako text, než ho json.loads dekóduje."""
    for secret in sorted(secrets(env), key=len, reverse=True):
        if len(secret) > 3:
            text = _secret_pattern(secret).sub("***", text)
    return text


def who(pid):
    """Kdo modely poskytovatele provozuje. U Jev a Cloudflare vlastní jméno; u zkušebního poskytovatele věta v jazyce, který platí
    ve chvíli volání (jazyk požadavku nebo procesu) — při importu modulu se žádná věta neskládá."""
    spec = PROVIDERS[pid]
    return i18n.t(spec["who_key"]) if "who_key" in spec else spec["who"]


def available(env=None):
    """Hostované modely, které jde nabídnout: jejich klíče jsou nastavené (zkušební jen v testovacím režimu)."""
    out = []
    for full, spec in MODELS.items():
        pid = spec["provider"]
        if provider_of(full, env) == pid and all(_key(env, name) for name in PROVIDERS[pid]["keys"]):
            out.append({"name": full, "provider": pid, "label": PROVIDERS[pid]["label"], "who": who(pid),
                        "usd_per_m": spec["usd_per_m"]})
    return out


class ProviderError(RuntimeError):
    """Chyba hostovaného modelu. Text je věta pro uživatele a nikdy neobsahuje klíč.

    Druh chyby se pozná z atributů, ne z věty: status = stav HTTP poskytovatele (None, když žádný nebyl),
    text = věta nebo tělo odpovědi bez popisku poskytovatele, code = druh chyby pro odpověď serveru.
    Věta je v jazyce, který platil, když chyba vznikla (i18n); status a code na jazyce nezávisí."""

    code = "provider"

    def __init__(self, provider, status, text):
        label = PROVIDERS[provider]["label"] if provider in PROVIDERS else i18n.t("prov.label")      # popisky poskytovatelů jsou vlastní jména
        super().__init__(f"{label} {status}: {text}" if status else f"{label}: {text}")
        self.provider, self.status, self.text = provider, status, text


class Refused(ProviderError):
    """Dotaz se vůbec neposlal: model není v seznamu, chybí klíč, tělo je moc velké."""

    code = "refused"


class BudgetExceeded(ProviderError):
    """Strop útraty: další hostovaný dotaz se neposlal."""

    code = "budget"


def _limit(value, default, whole=False):
    """Limit z čísla nebo textu (i s desetinnou čárkou); co není konečné nezáporné číslo, je výchozí hodnota (strop nejde vypnout nesmyslem)."""
    try:
        number = float(value.strip().replace(",", ".") if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return default
    if not 0 <= number < float("inf"):
        return default
    return int(number) if whole else number


class Budget:
    """Počítadlo hostovaných dotazů a dolarů v tomto procesu s tvrdým stropem.

    Restartem se nuluje: skutečným stropem je předplacený zůstatek u poskytovatele. Počítá se každý odeslaný pokus.
    Do dolarového stropu se vedle známé útraty počítá i odhad: cena pokusů, které jsou právě na cestě (souběžné
    dotazy tak strop nepřestřelí), a pokusů, jejichž cenu se nedozvíme (odpověď bez počtu tokenů nebo s nulou, nečitelná
    odpověď, vypršení časového limitu). Odhad slouží jen stropu; do výsledků ani do state() nejde."""

    def __init__(self, max_usd=None, max_requests=None, env=None):
        env = _env(env)
        self.max_usd = _limit(env.get("PICKBENCH_MAX_USD") if max_usd is None else max_usd, DEFAULT_MAX_USD)
        self.max_requests = _limit(env.get("PICKBENCH_MAX_HOSTED") if max_requests is None else max_requests, DEFAULT_MAX_HOSTED, whole=True)
        self.requests = self.answered = self.tokens = self.retries = self.unpriced = 0
        self.usd = 0.0
        self._held = 0                               # odhad držený ve stropu, v miliardtinách dolaru (celé číslo: přičtení a odečtení je přesné)
        self._lock = threading.Lock()

    def reserve(self, provider, est=0.0):
        """Před každým pokusem: je-li strop dosažený, BudgetExceeded a nic se nepošle; jinak se pokus započítá
        a jeho odhad ceny est (dolary) se drží ve stropu, dokud ho neuvolní settle() nebo paid()."""
        with self._lock:
            if self.requests >= self.max_requests:
                raise BudgetExceeded(provider, None, i18n.t("prov.cap.requests", requests=self.requests, max=self.max_requests))
            if self.usd + self._held / 1e9 >= self.max_usd:
                if self._held:                       # ve stropu je vedle známé útraty i odhad dotazů na cestě nebo bez známé ceny
                    raise BudgetExceeded(provider, None, i18n.t("prov.cap.usdHeld", usd=self.usd, held=self._held / 1e9, max=self.max_usd))
                raise BudgetExceeded(provider, None, i18n.t("prov.cap.usd", usd=self.usd, max=self.max_usd))
            self.requests += 1
            self._held += round(est * 1e9)

    def settle(self, est, keep=False):
        """Po pokusu, který neskončil odpovědí 200: jeho odhad se uvolní. keep = cenu se nedozvíme, ale poskytovatel
        pokus mohl účtovat (vypršení časového limitu) — odhad ve stropu zůstává."""
        if not keep:
            with self._lock:
                self._held = max(0, self._held - round(est * 1e9))

    def paid(self, tokens, usd, est=0.0):
        """Po odpovědi 200: tokeny a cena místo odhadu est. Odpověď bez počtu tokenů (post() předává None i za nulu) cenu
        nemá, počítá se zvlášť a ve stropu za ni odhad zůstává."""
        with self._lock:
            self.answered += 1
            if usd is None:
                self.unpriced += 1
            else:
                self.tokens += tokens
                self.usd += usd
                self._held = max(0, self._held - round(est * 1e9))

    def retried(self):
        with self._lock:
            self.retries += 1

    def state(self):
        with self._lock:
            return {"requests": self.requests, "answered": self.answered, "tokens": self.tokens, "usd": self.usd, "retries": self.retries,
                    "unpriced": self.unpriced, "max_requests": self.max_requests, "max_usd": self.max_usd}


BUDGET = Budget()                                    # počítadlo tohoto procesu; limity z PICKBENCH_MAX_USD a PICKBENCH_MAX_HOSTED při startu


def cost_usd(model, tokens):
    """Cena dotazu v dolarech = vstupní tokeny × cena modelu; bez tokenů nebo u modelu mimo ceník None."""
    spec = MODELS.get(model) if isinstance(model, str) else None
    return None if spec is None or tokens is None else tokens * spec["usd_per_m"] / 1_000_000


def estimate_usd(model, size):
    """Odhad ceny dotazu o size bajtech — jen pro strop útraty, do výsledků nejde: bajty ÷ EST_BYTES_PER_TOKEN × cena modelu."""
    return cost_usd(model, -(-size // EST_BYTES_PER_TOKEN)) or 0.0


def find_error(e):
    """Chyba hostovaného modelu v řetězu příčin (bench.ask ji balí do RuntimeError), nebo None. Na větě nezáleží."""
    while e is not None:
        if isinstance(e, ProviderError):
            return e
        e = e.__cause__
    return None


def is_provider_error(e):
    """Je to chyba hostovaného modelu? Pozná ji i zabalenou do jiné (bench.ask)."""
    return find_error(e) is not None


def resolve(model, env=None):
    """(id poskytovatele, záznam modelu, {jméno proměnné: hodnota klíče}); co nejde zavolat → Refused."""
    pid, spec = provider_of(model, env), MODELS.get(model) if isinstance(model, str) else None
    if pid is None or spec is None or spec["provider"] != pid:
        offered = ", ".join(m for m in MODELS if provider_of(m, env))
        raise Refused(pid, None, i18n.t("prov.notOffered", model=str(model)[:80], offered=offered))
    keys = {}
    for name in PROVIDERS[pid]["keys"]:
        value = _key(env, name)
        if value is None:
            raise Refused(pid, None, i18n.t("prov.keyMissing", model=model, name=name))
        if not (ACCOUNT_RE if name.endswith("_ID") else KEY_RE).fullmatch(value):
            raise Refused(pid, None, i18n.t("prov.keyChars", name=name))
        keys[name] = value
    return pid, spec, keys


def _request(pid, spec, keys):
    """Cesta a hlavičky dotazu. Jediné místo, kde se klíč dostane do dotazu."""
    headers = {"Content-Type": "application/json"}
    if pid == "jev":
        path, token = "/v1/systemone", keys["TYPESAFE_API_KEY"]
    elif pid == "cf":
        path, token = f"/client/v4/accounts/{keys['CLOUDFLARE_ACCOUNT_ID']}/ai/run/@cf/cloudflare/{spec['name']}", keys["CLOUDFLARE_API_TOKEN"]
    else:
        path, token = "/test/" + spec["name"], None
    if token:
        headers["Authorization"] = "Bearer " + token
    return path, headers


_pool = {}                                           # volná trvalá spojení: {server: [spojení, …]}
_pool_lock = threading.Lock()


def https_transport(host, path, headers, body, timeout):
    """Jeden dotaz po trvalém spojení (bez opakovaného TLS handshake).

    Spojení si vlákno na dobu dotazu půjčí ze zásoby a po odpovědi ho vrátí: dvě vlákna ho nikdy nemají naráz
    a spojení přežije vlákno, které ho otevřelo (server hry obsluhuje každý požadavek v novém vlákně — spojení
    vázané na vlákno by se tam otevíralo pro každý dotaz znovu a každá latence by nesla TLS handshake).
    Vrací (stav, hlavičky s klíči malými písmeny, tělo). Při chybě spojení zavře a zahodí a chybu pustí dál. Selže-li
    spojení půjčené ze zásoby (server ho zavřel), zavře i všechna ostatní volná spojení téhož serveru — zavřel je s největší
    pravděpodobností všechna — a opakovaný pokus (post) si tak otevře čerstvé spojení, místo aby bral jedno mrtvé za druhým.
    Selže-li čerstvě otevřené spojení, ostatní se nezahazují. Neopakuje: každý odeslaný pokus musí projít stropem útraty v post()."""
    with _pool_lock:
        free = _pool.setdefault(host, [])
        conn = free.pop() if free else None
    pooled = conn is not None
    if conn is None:
        conn = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    except BaseException:
        conn.close()
        if pooled:
            with _pool_lock:
                idle, _pool[host] = _pool.get(host, []), []
            for other in idle:
                other.close()
        raise
    with _pool_lock:
        _pool.setdefault(host, []).append(conn)
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw


def echo_transport(host, path, headers, body, timeout):
    """Zkušební poskytovatel: nic nikam neposílá. Každé otázce s nabídkou vrátí její první možnost, tokeny = délka těla ÷ 4."""
    doc = json.loads(body)
    answers = {}
    for key, question in (doc.get("questions") or {}).items():
        options = list((question or {}).get("criteria") or {})
        answers[key] = {"choice": options[0], "confidence": 0.9} if options else {"noul": 0.5}
    out = {"model": doc.get("model"), "answers": answers, "usage": {"input_tokens": len(body) // 4, "output_tokens": 1}}
    return 200, {"server-timing": "model;dur=1.5"}, json.dumps(out).encode()


def _retry_after(headers):
    """Sekundy z hlavičky Retry-After; None, když chybí nebo to není nezáporné číslo."""
    try:
        value = float(headers.get("retry-after", ""))
    except ValueError:
        return None
    return value if 0 <= value < float("inf") else None


def _model_ms(headers):
    """Čas modelu z hlavičky Server-Timing (položka model;dur=…, jak ji posílá otevřená implementace openjev); jinak None."""
    m = re.search(r"(?:^|,)\s*model\s*;[^,]*?dur=([0-9]+(?:\.[0-9]+)?)(?=\s*(?:[;,]|$))", headers.get("server-timing", ""))
    return float(m.group(1)) if m else None


def _tokens(out):
    """Počet vstupních tokenů z odpovědi; None, když chybí nebo to není kladné celé číslo. Nula také: dotaz s tělem nemůže
    skutečně stát nula tokenů, takže poskytovatel, který hlásí 0, cenu neřekl — a strop útraty za něj drží odhad."""
    usage = out.get("usage")
    tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    return tokens if type(tokens) is int and tokens > 0 else None


def _text(raw, env, limit=400):
    return scrub(raw.decode("utf-8", errors="replace"), env)[:limit]


_sleep = time.sleep                                  # čekání mezi pokusy; testy ho podstrkují


_tls_context = None                                  # kontext TLS (načtení certifikátů trvá desítky ms procesoru): staví se jednou a znovu se používá


def _context():
    global _tls_context
    if _tls_context is None:
        _tls_context = ssl.create_default_context()
    return _tls_context


def tls_connect(host, timeout):
    """Otevře a zavře jedno spojení TCP + TLS (bez dotazu, bez klíče)."""
    with socket.create_connection((host, 443), timeout=timeout) as sock:
        with _context().wrap_socket(sock, server_hostname=host):
            pass


def connect_ms(model, env=None, connect=None, clock=None):
    """Síť (odhad): doba jednoho spojení TCP + TLS k serveru poskytovatele v ms; první volání zahrnuje i dotaz na DNS.
    Kontext TLS se staví před měřením: měří se síť, ne procesor."""
    clock = clock or time.perf_counter
    pid, _, _ = resolve(model, env)
    if pid == "test":
        return 1.5
    host, failed = PROVIDERS[pid]["host"], None
    try:
        if connect is None:
            _context()                               # stavba kontextu je mimo měřený úsek, ale její selhání (ssl.SSLError) je stejná věta jako selhání spojení
        t0 = clock()
        (connect or tls_connect)(host, CONNECT_TIMEOUT)
    except OSError as e:
        failed = type(e).__name__
    if failed:                                       # mimo except: původní výjimka se s chybou nenese
        raise ProviderError(pid, None, i18n.t("prov.connectFailed", host=host, failed=failed))
    return (clock() - t0) * 1000


def net_line(model, env=None, connect=None):
    """Řádek výpisu s odhadem sítě; neúspěch je věta, ne výjimka."""
    try:
        return i18n.t("prov.net", ms=connect_ms(model, env, connect))
    except ProviderError as e:
        return i18n.t("prov.netFailed", error=e)


def spent_line(model, before, after):
    """Věta o útratě z rozdílu dvou stavů počítadla (Budget.state()); None, když mezi nimi žádný dotaz neodešel."""
    sent = after["requests"] - before["requests"]
    if sent <= 0:
        return None
    pid = (MODELS.get(model) or {}).get("provider")
    label, name = (PROVIDERS[pid]["label"], who(pid)) if pid in PROVIDERS else ("?", "?")
    answered, usd = after["answered"] - before["answered"], after["usd"] - before["usd"]
    per = f"{usd / answered * 1000:.4f}" if answered else "—"
    values = {"label": label, "who": name, "sent": sent, "tokens": after["tokens"] - before["tokens"], "usd": usd, "per": per,
              "retries": after["retries"] - before["retries"]}
    unpriced = after["unpriced"] - before["unpriced"]
    if unpriced:                                     # dovětek o odpovědích bez tokenů: celá věta pod vlastním klíčem
        return i18n.t("prov.spentUnpriced", unpriced=unpriced, **values)
    return i18n.t("prov.spent", **values)


def post(model, payload, transport=None, env=None, sleep=None, clock=None, budget=None):
    """Pošle dotaz hostovanému modelu. Vrací (odpověď, ms posledního pokusu, meta).

    Opakuje při 429, 529, 5xx a spadlém spojení, nejvýš MAX_ATTEMPTS pokusů; čekání ani neúspěšné pokusy se do ms
    nepočítají (meta["waited_ms"], meta["retries"]). Před každým pokusem hlídá strop útraty (budget, jinak počítadlo
    procesu BUDGET). Chyba → ProviderError s větou bez klíče."""
    sleep, clock = sleep or _sleep, clock or time.perf_counter
    budget = BUDGET if budget is None else budget
    pid, spec, keys = resolve(model, env)
    if not isinstance(payload, dict):
        raise Refused(pid, None, i18n.t("prov.payload"))
    path, headers = _request(pid, spec, keys)
    body = json.dumps({"model": spec["name"], "state": payload.get("state"), "questions": payload.get("questions")}).encode()
    if len(body) > MAX_BODY:
        raise Refused(pid, None, i18n.t("prov.tooBig", size=len(body), max=MAX_BODY))
    send = transport or (echo_transport if pid == "test" else https_transport)
    attempts, waited, dropped = 0, 0.0, False
    est = estimate_usd(model, len(body))             # odhad ceny pokusu: drží se ve stropu, dokud pokus neskončí
    while True:
        budget.reserve(pid, est)                     # strop útraty: dosažený → BudgetExceeded a nic neodejde
        attempts += 1
        t0 = clock()
        fault = timed_out = None
        try:
            status, heads, raw = send(PROVIDERS[pid]["host"], path, headers, body, TIMEOUT)
        except TimeoutError:
            status, heads, raw, timed_out = None, {}, b"", True
        except (http.client.HTTPException, OSError) as e:
            status, heads, raw, fault = None, {}, b"", scrub(f"{type(e).__name__}: {e}", env)[:300]
        ms = (clock() - t0) * 1000
        if status == 200:
            break
        budget.settle(est, keep=bool(timed_out))     # neúspěšný pokus svůj odhad uvolní; vypršení limitu ne — dotaz mohl doběhnout a poskytovatel ho účtovat
        if timed_out:                                # chyby se vyhazují až tady, mimo except: původní výjimka (s cizím textem) se s nimi nenese
            raise ProviderError(pid, None, i18n.t("prov.timeout", s=TIMEOUT))
        if status in (401, 403):                     # tělo se neukazuje: poskytovatelé v něm klíč nebo jeho začátek opakují
            raise ProviderError(pid, status, i18n.t("prov.badKey", names=", ".join(PROVIDERS[pid]["keys"])))
        again = status is None or status in (429, 529) or 500 <= status <= 599
        if not again:
            raise ProviderError(pid, status, _text(raw, env))
        if attempts >= MAX_ATTEMPTS:                 # cizí text (fault, tělo odpovědi) jde do věty už vyčištěný a zkrácený; věta se doplňuje jedním průchodem
            if status is None:
                raise ProviderError(pid, None, i18n.t("prov.dropped", attempts=attempts, fault=fault))
            raise ProviderError(pid, status, i18n.t("prov.afterAttempts", body=_text(raw, env, 300), attempts=attempts))
        asked = _retry_after(heads)
        if asked is not None and asked > MAX_WAIT:
            raise ProviderError(pid, status, i18n.t("prov.retryAfter", asked=asked, max=MAX_WAIT))
        if status is None and not dropped:
            wait, dropped = 0, True                  # první spadlé spojení: server mezitím zavřel trvalé spojení, zkusit hned znovu
        else:
            wait = asked if asked is not None else BACKOFF[min(attempts, len(BACKOFF)) - 1]
        budget.retried()
        if wait:
            sleep(wait)
        waited += ms + wait * 1000
    try:                                             # i odpověď 200 jde přes scrub: server ji posílá stránce celou a klíč v ní být nesmí, ani kdyby ho poskytovatel zopakoval
        out = json.loads(scrub(raw.decode("utf-8", errors="replace"), env))
    except ValueError:
        out = None
    if pid == "cf" and isinstance(out, dict) and out.get("success") is False:
        raise ProviderError(pid, status, i18n.t("prov.cfError", errors=scrub(json.dumps(out.get("errors"), ensure_ascii=False), env)[:300]))
    if pid == "cf" and isinstance(out, dict) and isinstance(out.get("result"), dict):
        out = out["result"]                          # REST API Cloudflare balí odpověď do {"result": …}
    if not isinstance(out, dict):
        raise ProviderError(pid, status, i18n.t("prov.notObject", body=_text(raw, env, 200)))
    tokens = _tokens(out)
    usd = cost_usd(model, tokens)
    budget.paid(tokens, usd, est)
    return out, ms, {"provider": pid, "attempts": attempts, "retries": attempts - 1, "waited_ms": waited,
                     "tokens": tokens, "model_ms": _model_ms(heads), "usd": usd}
