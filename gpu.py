"""Stav GPU z nvidia-smi — sdílí server.py (GET /gpu) a hwbench.py.

Stroj bez GPU NVIDIA nebo bez nvidia-smi nic nerozbije: read() vrátí {"available": False}.
"""
import subprocess

CMD = ["nvidia-smi", "--query-gpu=temperature.gpu,power.draw,clocks.gr,pstate,memory.used,utilization.gpu",
       "--format=csv,noheader,nounits"]
TIMEOUT = 2                                          # s; první čtení po klidu karty může trvat déle a vyprší (viz GIVE_UP)
FIELDS = (("temp_c", int), ("power_w", float), ("clock_mhz", int), ("pstate", str), ("mem_mb", int), ("util_pct", int))
MIN_SAMPLES = 5                                      # energie se počítá až od tolika vzorků
MAX_GAP = 5                                          # s; delší mezera mezi vzorky = energii nejde spočítat
GIVE_UP = 3                                          # tolik nepovedených čtení po sobě, než se GPU prohlásí za chybějící


def _run(cmd, timeout):
    """Spustí program a vrátí jeho standardní výstup; chyba nebo vypršení limitu → výjimka."""
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return done.stdout


def _value(raw, kind):
    if kind is str:                                  # text: zástupná hodnota v hranatých závorkách ("[N/A]", "[Not Supported]", "[Unknown Error]") a prázdná → None
        return raw if raw and "N/A" not in raw and not raw.startswith("[") else None
    try:
        return kind(float(raw))                      # číslo: totéž, zástupná ani prázdná hodnota se na číslo nepřevede → None
    except ValueError:
        return None


def parse(text):
    """První řádek výstupu nvidia-smi (první GPU) → slovník pro /gpu; nečitelný výstup → {"available": False}."""
    lines = [line for line in text.splitlines() if line.strip()]
    parts = [p.strip() for p in lines[0].split(",")] if lines else []
    if len(parts) != len(FIELDS):
        return {"available": False}
    return {"available": True, **{name: _value(raw, kind) for (name, kind), raw in zip(FIELDS, parts)}}


def read(run=None, timeout=TIMEOUT):
    """Aktuální stav GPU. run(cmd, timeout) -> text je náhrada spouštění pro testy."""
    try:
        return parse((run or _run)(CMD, timeout))
    except Exception:                                # chybí program, chyba ovladače, vypršel limit
        return {"available": False}


def energy_j(samples, start=None, end=None):
    """Energie v joulech z dvojic (čas v s, příkon ve W): součet průměrného příkonu × interval mezi vzorky.

    Vzorky bez příkonu se vynechají. Méně než MIN_SAMPLES vzorků nebo mezera delší než MAX_GAP → NaN.
    start, end = začátek a konec měřeného běhu (s, stejné hodiny jako vzorky). Když jsou zadané, vzorky musí běh
    pokrývat (první i poslední nejdál MAX_GAP od kraje, jinak NaN) a energie se dopočítá na celý běh:
    průměrný příkon mezi vzorky × délka běhu. Bez nich by se energie jen části běhu dělila všemi rozhodnutími.
    """
    pts = [(t, p) for t, p in samples if type(p) in (int, float) and p == p]
    if len(pts) < MIN_SAMPLES:
        return float("nan")
    total = 0.0
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if t1 - t0 > MAX_GAP:
            return float("nan")
        total += (p0 + p1) / 2 * (t1 - t0)
    if start is None or end is None:
        return total
    span = pts[-1][0] - pts[0][0]
    if span <= 0 or pts[0][0] - start > MAX_GAP or end - pts[-1][0] > MAX_GAP:
        return float("nan")
    return total / span * (end - start)
