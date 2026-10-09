"""Úložiště PickBench v SQLite: výsledky měření, poslední nastavení a fronta plánovače.

Spravuje ho jen server.py. Soubor pickbench.db leží vedle server.py; cestu mění proměnná PICKBENCH_DB.
Každá operace si otevře vlastní spojení a všechny jdou přes jeden zámek, takže vícevláknový server
zapisuje bezpečně. Chyba databáze (soubor nejde otevřít, je poškozený, disk jen pro čtení, drží ho jiný
program) → StoreError s větou pro uživatele; hra pak běží dál, jen bez ukládání.
Věty jsou v jazyce, který platí ve chvíli chyby (i18n: jazyk požadavku na serveru, jinak PICKBENCH_LANG).
"""
import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import i18n

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS results (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
MAX_RESULTS = 1000                                   # tolik nejnovějších výsledků vrací results()


class StoreError(Exception):
    """Databázi nejde číst nebo zapsat; text je věta pro uživatele."""


def default_path(root, env=None):
    """Cesta k databázi: proměnná PICKBENCH_DB, jinak pickbench.db ve složce root."""
    env = os.environ if env is None else env
    return env.get("PICKBENCH_DB") or str(Path(root) / "pickbench.db")


def _not_finite(text):
    raise ValueError(i18n.t("store.notFinite", text=text))


def _float(text):
    value = float(text)
    if value != value or value in (float("inf"), float("-inf")):       # 1e999 přečte float() jako nekonečno
        _not_finite(text)
    return value


def finite_json(raw):
    """Jako json.loads, ale NaN, Infinity, -Infinity a číslo mimo rozsah → ValueError.

    Python je čte i zapisuje, prohlížeč takový JSON nepřečte: jeden takový výsledek by rozbil načtení celé tabulky."""
    return json.loads(raw, parse_constant=_not_finite, parse_float=_float)


def _load(raw):
    try:
        return finite_json(raw)
    except (ValueError, TypeError):
        return None


class Store:
    def __init__(self, path, now=None, timeout=5):
        self.path = str(path)
        self.timeout = timeout                       # s; čekání na zámek, který drží jiný program
        self._now = now or (lambda: datetime.now().isoformat(timespec="seconds"))
        self._lock = threading.Lock()

    def _run(self, work):
        """Otevře spojení, založí tabulky, provede work(con) v jedné transakci a spojení zavře."""
        with self._lock:
            con = None
            try:
                con = sqlite3.connect(self.path, timeout=self.timeout)
                with con:                            # commit, při výjimce rollback
                    for sql in SCHEMA:
                        con.execute(sql)
                    return work(con)
            except sqlite3.Error as e:
                raise StoreError(i18n.t("store.unusable", path=self.path, error=e)) from e
            finally:
                if con is not None:
                    con.close()

    def results(self, limit=MAX_RESULTS):
        """Nejnovější výsledky, nejnovější první: [{"id", "ts", "data"}]. Řádek s nečitelnými daty se vynechá."""
        rows = self._run(lambda con: con.execute(
            "SELECT id, ts, data FROM results ORDER BY id DESC LIMIT ?", (limit,)).fetchall())
        out = []
        for rid, ts, raw in rows:
            data = _load(raw)
            if isinstance(data, dict):
                out.append({"id": rid, "ts": ts, "data": data})
        return out

    def add_result(self, data):
        """Uloží jeden výsledek (slovník) a vrátí ho s číslem a časem uložení."""
        if not isinstance(data, dict):
            raise ValueError(i18n.t("store.resultObject"))
        ts, raw = self._now(), json.dumps(data, allow_nan=False)     # jen ASCII: text s osamocenou půlkou znaku by jinak zápis shodil; NaN → ValueError
        rid = self._run(lambda con: con.execute("INSERT INTO results (ts, data) VALUES (?, ?)", (ts, raw)).lastrowid)
        return {"id": rid, "ts": ts, "data": data}

    def clear_results(self):
        """Smaže všechny výsledky; vrací, kolik jich bylo."""
        return self._run(lambda con: con.execute("DELETE FROM results").rowcount)

    def delete_result(self, rid):
        """Smaže jeden výsledek podle čísla; vrací, jestli takový byl. Ostatní řádky ani jejich pořadí se nemění."""
        if not 0 < rid < 2 ** 63:                    # takové číslo v databázi být nemůže; větší by sqlite3 odmítlo výjimkou OverflowError
            return False
        return self._run(lambda con: con.execute("DELETE FROM results WHERE id = ?", (rid,)).rowcount) > 0

    def get(self, key):
        """Uložená hodnota (nastavení, fronta), nebo None."""
        row = self._run(lambda con: con.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone())
        return _load(row[0]) if row else None

    def put(self, key, value):
        raw = json.dumps(value, allow_nan=False)     # NaN a Infinity → ValueError (prohlížeč by je nepřečetl)
        self._run(lambda con: con.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, raw)))
