"""Metriky kvality klasifikace z dvojic (správný štítek, odpověď modelu) a goodput.

Stejný výpočet je v index.html (blok „sdíleno s Pythonem“); shodu hlídá tests/test_parity.py.
Neplatná odpověď (štítek mimo nabídku, chyba) se předává jako None a počítá se jako špatně.
Totéž platí pro AUROC a nejlepší práh otázky noul na konci souboru. Brierovo skóre, pokrytí nad prahem jistoty a třídy délky textu jsou za nimi; stejný výpočet bude ve stránce.
"""
from fractions import Fraction


def classification(pairs, oos=None):
    """pairs = [(správný štítek, odpověď nebo None)]; oos = štítek „mimo rozsah“, pokud ho sada má.

    Vrací n, acc, macro_f1 (průměr F1 přes třídy s aspoň jednou úlohou), invalid (počet neplatných odpovědí),
    classes (od nejhorší: label, n, precision, recall, f1), confusions ([správný, zvolený, počet] od nejčastější)
    a oos (None, nebo n, recall, leak = podíl běžných úloh hozených do oos, in_n, in_acc = přesnost bez oos).
    Bez dat jsou podíly None."""
    stats, conf = {}, {}                             # štítek → [úloh, správně, cizích odpovědí tímto štítkem]
    correct = invalid = leak = 0
    for truth, pred in pairs:
        s = stats.setdefault(truth, [0, 0, 0])
        s[0] += 1
        if pred is None:
            invalid += 1
        if pred == truth:
            correct += 1
            s[1] += 1
            continue
        if pred is not None:
            stats.setdefault(pred, [0, 0, 0])[2] += 1
        if pred == oos and oos is not None:
            leak += 1
        conf[(truth, pred)] = conf.get((truth, pred), 0) + 1
    classes = []
    for label, (n, tp, fp) in stats.items():
        if not n:
            continue                                 # štítek, který se objevil jen jako odpověď
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / n
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        classes.append({"label": label, "n": n, "precision": precision, "recall": recall, "f1": f1})
    classes.sort(key=lambda c: (c["f1"], -c["n"], c["label"]))
    confusions = sorted(([t, p, k] for (t, p), k in conf.items()), key=lambda c: (-c[2], c[0], c[1] or ""))
    total = len(pairs)
    out = {"n": total, "acc": correct / total if total else None,
           "macro_f1": sum(c["f1"] for c in classes) / len(classes) if classes else None,
           "invalid": invalid, "classes": classes, "confusions": confusions, "oos": None}
    if oos is not None:
        n, tp, _ = stats.get(oos, [0, 0, 0])
        inside = total - n
        out["oos"] = {"n": n, "recall": tp / n if n else None, "leak": leak / inside if inside else None,
                      "in_n": inside, "in_acc": (correct - tp) / inside if inside else None}
    return out


def goodput(results, deadline_ms):
    """Podíl rozhodnutí správných a zároveň včas. results = [(správně?, ms)]; bez limitu nebo bez dat None."""
    if deadline_ms is None or not results:
        return None
    return sum(1 for ok, ms in results if ok and ms <= deadline_ms) / len(results)


def auroc(probs, truths):
    """AUROC z noul proti pravdě jeď; shody jako polovina, neplatné (None) se vynechají. Bez úloh jedné třídy None."""
    pos = [p for p, go in zip(probs, truths) if go and p is not None]
    neg = [p for p, go in zip(probs, truths) if not go and p is not None]
    if not pos or not neg:
        return None
    return sum((a > b) + 0.5 * (a == b) for a in pos for b in neg) / (len(pos) * len(neg))


def best_threshold(probs, truths):
    """Nejlepší práh t z vrácených hodnot noul a 0,5 podle vyvážené přesnosti rozhodnutí noul >= t: průměr podílu
    správných na úlohách jeď a na úlohách stůj (neplatná = chyba; chybí-li úlohy jedné třídy, jen podíl té druhé).
    Při shodě vyhrává práh nejblíž 0,5, potom nižší. Vrací (t, vyvážená přesnost), bez úloh None. Zlomky, aby shoda byla přesná."""
    groups = [(want, [p for p, go in zip(probs, truths) if go == want]) for want in (True, False)]
    groups = [(want, ps) for want, ps in groups if ps]
    if not groups:                                   # bez úloh není co hledat (stránka vrací null)
        return None

    def balanced(t):
        return sum(Fraction(sum(p is not None and (p >= t) == want for p in ps), len(ps)) for want, ps in groups) / len(groups)
    t = min({0.5, *(p for p in probs if p is not None)}, key=lambda t: (-balanced(t), abs(t - 0.5), t))
    return t, float(balanced(t))


def brier(pairs):
    """Brierovo skóre z dvojic (jistota, správně?): průměr (jistota − správně)², 0 = dokonalé. Bez dat None."""
    return sum((c - bool(ok)) ** 2 for c, ok in pairs) / len(pairs) if pairs else None


def coverage(pairs, n, threshold):
    """Práh jistoty: úlohy s jistotou aspoň threshold rozhodne model, ostatní se předají (člověku, pravidlům).

    pairs = [(jistota, správně?)] jen z odpovědí s jistotou; n = počet všech úloh, i bez jistoty (ty jdou pod práh).
    Vrací threshold, n, covered (nad prahem), coverage (covered ÷ n), acc (správně nad prahem ÷ covered) a handed (n − covered);
    bez úloh, nebo bez úloh nad prahem, je podíl None."""
    above = [bool(ok) for c, ok in pairs if c >= threshold]
    return {"threshold": threshold, "n": n, "covered": len(above), "coverage": len(above) / n if n else None,
            "acc": sum(above) / len(above) if above else None, "handed": n - len(above)}


LENGTH_CLASSES = ("short", "medium", "long")


def length_classes(lengths):
    """Třídy délky textu z délek celé sady: seřadit podle (délka, pořadí v sadě) a rozdělit na třetiny po pořadí
    (hranice n // 3 a 2n // 3). Stejně dlouhé texty na hranici mohou padnout do dvou tříd.

    Vrací classes (třída každého textu v pořadí sady) a ranges ({třída: [nejkratší, nejdelší]}, prázdná třída None)."""
    n = len(lengths)
    order = sorted(range(n), key=lambda i: (lengths[i], i))
    classes, ranges = [None] * n, dict.fromkeys(LENGTH_CLASSES)
    for rank, i in enumerate(order):
        cls = LENGTH_CLASSES[0 if rank < n // 3 else 1 if rank < 2 * n // 3 else 2]
        classes[i] = cls
        ranges[cls] = [lengths[i], lengths[i]] if ranges[cls] is None else [ranges[cls][0], lengths[i]]      # pořadí je vzestupné
    return {"classes": classes, "ranges": ranges}
