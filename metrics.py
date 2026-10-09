"""Metriky kvality klasifikace z dvojic (správný štítek, odpověď modelu) a goodput.

Stejný výpočet je v index.html (blok „sdíleno s Pythonem“); shodu hlídá tests/test_parity.py.
Neplatná odpověď (štítek mimo nabídku, chyba) se předává jako None a počítá se jako špatně.
"""


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
