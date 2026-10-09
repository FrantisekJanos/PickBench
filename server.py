"""PickBench — lokální server demonstrátoru rozhodovacích modelů (Ollama /v1/systemone).

Servíruje index.html a přeposílá dotazy na Ollamu, přičemž měří čistý čas modelu.
GET /gpu vrací stav GPU z nvidia-smi (gpu.py); bez GPU {"available": false}.
GET /datasets vypíše sady e-mailů (datasets/) a veřejné sady (datasets/public/, stahuje je fetch_datasets.py).
Výsledky, poslední nastavení a fronta plánovače se ukládají do SQLite (store.py): /results, /settings, /queue; DELETE /results/<id> smaže jeden výsledek.
POST /unload uvolní model z paměti Ollamy (plánovač mezi kroky s různými modely).
Hostované modely (jev:…, cf:… — providers.py) se nabízejí, jen když má proces jejich klíče v proměnných prostředí
(TYPESAFE_API_KEY, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN). Klíč se nikdy nevrací v odpovědi, v chybě ani ve výpisu.
POST /decide je směruje k poskytovateli, GET /spend vrací útratu a strop (PICKBENCH_MAX_USD, PICKBENCH_MAX_HOSTED),
POST /net odhad sítě. Strop se čte při startu a žádná cesta ho nemění.
Věty v odpovědích jsou v jazyce požadavku (hlavička X-PickBench-Lang: cs nebo en; bez ní česky), výpisy do konzole v jazyce
procesu (PICKBENCH_LANG) — texty jsou v i18n.py.
Spuštění: python server.py  →  http://127.0.0.1:8741
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import gpu
import i18n
import providers
import store

# 127.0.0.1, ne localhost: na Windows zkouší localhost nejdřív IPv6 a každý dotaz zdrží o ~2 s
OLLAMA = os.environ.get("PICKBENCH_OLLAMA", "http://127.0.0.1:11434")
PORT = int(os.environ.get("PICKBENCH_PORT", "8741"))
ROOT = Path(__file__).parent
STORE = store.Store(store.default_path(ROOT))        # pickbench.db vedle server.py, nebo cesta z PICKBENCH_DB
MAX_BODY = 2_000_000                                 # bajtů; větší tělo požadavku se odmítne
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")    # jména, pod kterými smí stránka hry se serverem mluvit
RESULT_ID_RE = re.compile(r"/results/([0-9]{1,40})")     # jen ASCII číslice, nejvýš 40 (int() u tisíců číslic hlásí chybu); DELETE /results/<id> maže jeden výsledek

DATASET_RE = re.compile(r"/datasets/([A-Za-z0-9_-]+\.json)")


def list_datasets():
    """Sady e-mailů ve složce datasets/ (jen bezpečná jména souborů)."""
    out = []
    for p in sorted((ROOT / "datasets").glob("*.json")):
        if not DATASET_RE.fullmatch("/datasets/" + p.name):
            continue
        try:
            name = json.loads(p.read_text(encoding="utf-8-sig")).get("name") or p.stem
        except Exception:
            name = p.stem
        out.append({"file": p.name, "name": name})
    return out


PUBLIC_RE = re.compile(r"/datasets/public/([A-Za-z0-9_-]+\.json)")


def list_public():
    """Veřejné sady ve složce datasets/public/ (jen bezpečná jména souborů; složka nemusí existovat).

    file je cesta za /datasets/, pod kterou si stránka sadu načte."""
    out = []
    for p in sorted((ROOT / "datasets" / "public").glob("*.json")):
        if not PUBLIC_RE.fullmatch("/datasets/public/" + p.name):
            continue
        try:
            name = json.loads(p.read_text(encoding="utf-8-sig")).get("name") or p.stem
        except Exception:
            name = p.stem
        out.append({"file": "public/" + p.name, "name": name})
    return out


def ollama(path, payload=None, timeout=300):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(OLLAMA + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def decision_models():
    """Nainstalované modely; pokud některé hlásí schopnost 'decision', vrátí jen ty."""
    names = [m["name"] for m in ollama("/api/tags", timeout=10).get("models", [])]
    decision = []
    for name in names:
        try:
            if "decision" in ollama("/api/show", {"model": name}, timeout=10).get("capabilities", []):
                decision.append(name)
        except Exception:
            pass
    return decision or names


def unload_model(name):
    """Uvolní model z paměti Ollamy: dotaz bez promptu s keep_alive 0. Model, který nahraný není, nechá být."""
    return ollama("/api/generate", {"model": name, "keep_alive": 0, "stream": False}, timeout=30)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")                          # cizí stránka nesmí hru vložit do rámu:
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")    # načtení stránky nenese Origin a kontrolou v _foreign projde
        self.end_headers()
        self.wfile.write(raw)

    def _lang(self):
        """Jazyk požadavku z hlavičky X-PickBench-Lang; bez ní nebo s neznámou hodnotou česky (stávající klienti)."""
        return i18n.known(self.headers.get(i18n.HEADER)) or i18n.DEFAULT

    def do_GET(self):
        with i18n.use(self._lang()):
            self._get()

    def do_POST(self):
        with i18n.use(self._lang()):
            self._post()

    def do_DELETE(self):
        with i18n.use(self._lang()):
            self._delete()

    def _get(self):
        if self._foreign():
            return
        if self.path.split("?", 1)[0] in ("/", "/index.html"):          # stránka smí nést ?lang=en; ostatní cesty parametry nemají
            self._send(200, (ROOT / "index.html").read_bytes(), "text/html")
        elif self.path == "/models":
            self._models()
        elif self.path == "/spend":
            self._send(200, providers.BUDGET.state())
        elif self.path == "/gpu":
            self._send(200, gpu.read())
        elif self.path == "/results":
            self._store(lambda: {"results": STORE.results()})
        elif self.path in ("/settings", "/queue"):
            key = self.path[1:]
            self._store(lambda: {key: STORE.get(key)})
        elif self.path == "/datasets":
            self._send(200, {"datasets": list_datasets(), "public": list_public()})
        elif (m := DATASET_RE.fullmatch(self.path)) and (ROOT / "datasets" / m.group(1)).is_file():
            self._send(200, (ROOT / "datasets" / m.group(1)).read_bytes())
        elif (m := PUBLIC_RE.fullmatch(self.path)) and (ROOT / "datasets" / "public" / m.group(1)).is_file():
            self._send(200, (ROOT / "datasets" / "public" / m.group(1)).read_bytes())
        else:
            self._send(404, {"error": "not found"})

    def _size(self):
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return 0

    def _drain(self):
        """Dočte a zahodí tělo požadavku, které se nebude zpracovávat (nejvýš 8 × MAX_BODY).

        Nepřečtené tělo by na Windows shodilo spojení dřív, než klient dostane odpověď."""
        left = min(self._size(), 8 * MAX_BODY)
        while left > 0 and (chunk := self.rfile.read(min(left, 65536))):
            left -= len(chunk)

    def _foreign(self):
        """Požadavek, který nepřišel ze stránky hry → odpoví 403 a vrátí True.

        Server poslouchá jen na 127.0.0.1, ale prohlížeč na tomto počítači na tu adresu pošle požadavek z jakékoli
        otevřené webové stránky. Proto: Host musí jmenovat tento počítač (jinak DNS rebinding) a Origin, pokud je,
        musí být stránka hry. Požadavky bez Origin (curl, testy) projdou."""
        host = self.headers.get("Host") or ""
        name = host[:host.find("]") + 1] if host.startswith("[") else host.split(":")[0]
        origin = self.headers.get("Origin")
        if name.lower() in LOCAL_HOSTS and origin in (None, "http://" + host):
            return False
        self._drain()
        self._send(403, {"error": i18n.t("srv.foreign")})
        return True

    def _body(self):
        """Tělo požadavku jako objekt JSON; cokoli jiného → ValueError s větou pro odpověď 400."""
        size = self._size()
        if size <= 0:
            raise ValueError(i18n.t("srv.body.missing"))
        if size > MAX_BODY:
            self._drain()
            raise ValueError(i18n.t("srv.body.tooBig", max=MAX_BODY))
        raw = self.rfile.read(size)
        try:
            body = store.finite_json(raw)
        except ValueError:                           # i text, který není UTF-8
            try:
                json.loads(raw)
            except ValueError:
                raise ValueError(i18n.t("srv.body.notJson")) from None
            raise ValueError(i18n.t("srv.body.notFinite")) from None
        if not isinstance(body, dict):
            raise ValueError(i18n.t("srv.body.notObject"))
        return body

    def _store(self, work):
        """Odpoví výsledkem work(); chyba databáze = 503 s větou pro uživatele (hra běží dál bez ukládání)."""
        try:
            self._send(200, work())
        except store.StoreError as e:
            self._send(503, {"error": str(e)})

    def _models(self):
        """Lokální modely jako pole jmen (tvar se nemění). Hostované modely jsou v klíči hosted, jen když má server jejich klíč;
        pak nedostupná Ollama seznam neshodí. Bez klíčů je odpověď stejná jako dřív, i 502 při nedostupné Ollamě."""
        hosted, error = providers.available(), None
        try:
            local = [name for name in decision_models() if not providers.hosted(name)]      # jméno s předponou poskytovatele by /decide poslal ven
        except Exception as e:
            local, error = [], i18n.t("srv.ollamaDown", error=e)
        if error and not hosted:
            return self._send(502, {"error": error})
        out = {"models": local}
        if hosted:
            out["hosted"] = hosted
        if error:
            out["ollama_error"] = error
        self._send(200, out)

    def _decide_error(self, http, sentence, code, upstream=None, **extra):
        """Chyba /decide: věta pro člověka a vedle ní strojově čitelný druh chyby (code) a stav HTTP poskytovatele nebo Ollamy
        (upstream_status, jinak None). Stránka druh chyby pozná podle nich, ne podle věty."""
        self._send(http, {"error": sentence, "code": code, "upstream_status": upstream, **extra})

    def _decide_hosted(self, model, payload):
        """Dotaz hostovanému modelu. Adresu ani model stránka neurčuje (providers.MODELS) a text každé chyby je už bez klíče."""
        try:
            out, ms, meta = providers.post(model, payload)
        except providers.BudgetExceeded as e:
            return self._decide_error(429, str(e), e.code, _spend=providers.BUDGET.state())
        except providers.Refused as e:
            return self._decide_error(400, str(e), e.code)
        except providers.ProviderError as e:
            return self._decide_error(502, str(e), e.code, e.status)
        except Exception as e:                       # chyba programu: věta bez klíče místo výpisu chyby do okna serveru; řez až po vyčištění, jinak by z klíče zbyl začátek
            return self._decide_error(502, providers.scrub(i18n.t("srv.hostedFault", type=type(e).__name__, error=e))[:300], "internal")
        out.update({"_provider": meta["provider"], "_ms": ms, "_usd": meta["usd"], "_retries": meta["retries"],
                    "_waited_ms": meta["waited_ms"], "_spend": providers.BUDGET.state()})
        if meta["model_ms"] is not None:
            out["_model_ms"] = meta["model_ms"]
        self._send(200, out)

    def _net(self, model):
        """Síť (odhad): doba jednoho spojení TCP + TLS k serveru poskytovatele hostovaného modelu."""
        if not providers.hosted(model):
            return self._send(400, {"error": i18n.t("srv.netHostedOnly")})
        try:
            ms = providers.connect_ms(model)
        except providers.Refused as e:
            return self._send(400, {"error": str(e)})
        except providers.ProviderError as e:
            return self._send(502, {"error": str(e)})
        self._send(200, {"provider": providers.provider_of(model), "connect_ms": ms})

    def _unload(self, name):
        if not isinstance(name, str) or not name:
            return self._send(400, {"error": i18n.t("srv.noModel")})
        if providers.hosted(name):                   # hostovaný model v paměti Ollamy není: úspěch bez volání
            return self._send(200, {"unloaded": name})
        try:
            unload_model(name)
        except urllib.error.HTTPError as e:
            return self._send(502, {"error": f"Ollama {e.code}: {e.read().decode(errors='replace')[:500]}"})
        except Exception as e:
            return self._send(502, {"error": f"Ollama: {e}"})
        self._send(200, {"unloaded": name})

    def _delete_result(self, rid):
        """Smaže jeden výsledek: 200 a {"deleted": 1}, nebo 404, když takový v databázi není; chyba databáze = 503 jako jinde."""
        try:
            found = STORE.delete_result(rid)
        except store.StoreError as e:
            return self._send(503, {"error": str(e)})
        if found:
            self._send(200, {"deleted": 1})
        else:
            self._send(404, {"error": i18n.t("srv.noResult", id=rid)})

    def _delete(self):
        if self._foreign():
            return
        if self.path.startswith("/results/"):        # jeden výsledek: číslo musí být kladné celé, jinak 400; číslo, které v databázi není, je 404
            m = RESULT_ID_RE.fullmatch(self.path)
            rid = int(m.group(1)) if m else 0
            if rid < 1:
                return self._send(400, {"error": i18n.t("srv.badResultId")})
            return self._delete_result(rid)
        if self.path != "/results":
            return self._send(404, {"error": "not found"})
        self._store(lambda: {"deleted": STORE.clear_results()})

    def _post(self):
        if self._foreign():
            return
        if self.path in ("/results", "/settings", "/queue", "/unload", "/net"):
            try:
                body = self._body()
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            if self.path == "/unload":
                return self._unload(body.get("model"))
            if self.path == "/net":
                return self._net(body.get("model"))
            if self.path == "/results":
                return self._store(lambda: STORE.add_result(body))
            key = self.path[1:]
            return self._store(lambda: STORE.put(key, body) or {"ok": True})
        if self.path != "/decide":
            self._drain()
            return self._send(404, {"error": "not found"})
        try:
            payload = self._body()                   # stejná kontrola jako u ostatních cest: mez velikosti, platný objekt JSON
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        model = payload.get("model")
        if providers.hosted(model):                  # jev:…, cf:… → poskytovatel; všechno ostatní beze změny na Ollamu
            return self._decide_hosted(model, payload)
        t0 = time.perf_counter()
        try:
            out = ollama("/v1/systemone", payload)
        except urllib.error.HTTPError as e:
            return self._decide_error(502, f"Ollama {e.code}: {e.read().decode(errors='replace')[:500]}", "ollama", e.code)
        except Exception as e:
            return self._decide_error(502, f"Ollama: {e}", "ollama_unreachable")
        out["_ollama_ms"] = out["_ms"] = (time.perf_counter() - t0) * 1000
        out["_provider"] = "ollama"
        self._send(200, out)

    def log_message(self, *args):
        pass


def banner():
    """Řádky, které server vypíše při startu, v jazyce procesu (PICKBENCH_LANG; klientovi bez hlavičky server i tak odpovídá česky).

    Nesmysl v PICKBENCH_LANG končí větou a nenulovým kódem dřív, než se otevře port."""
    try:
        i18n.from_env()
    except i18n.LangError as e:
        sys.exit(str(e))
    lines = [i18n.t("srv.start", port=PORT, ollama=OLLAMA, db=STORE.path)]
    offered = ", ".join(m["name"] for m in providers.available())
    if offered:                                      # jen jména modelů a limity; klíče se nevypisují nikdy
        limits = providers.BUDGET.state()
        lines.append(i18n.t("srv.start.hosted", offered=offered, usd=limits["max_usd"], requests=limits["max_requests"]))
    return lines


if __name__ == "__main__":
    for line in banner():
        print(line)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
