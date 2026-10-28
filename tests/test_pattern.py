"""Scénář Vzor soupeře (bench.py --pattern). Žádný test nevolá Ollamu, síť ani hostovaného poskytovatele."""
import contextlib
import io
import itertools
import json
import os
import random
import re
import runpy
import shutil
import sys
import time
import unittest
import urllib.request
from collections import Counter
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import bench
import i18n
from test_bench import fake_post, obedient
from test_golden import Clock, fake_urlopen
from test_barrier import REGISTRY, page_function, page_source, run_page_js      # kus skriptu stránky v node, registr scénářů
from test_queue import method                                                   # víceřádková metoda objektu scénáře

LANG_ENV = "PICKBENCH_LANG"
MOVES = ("rock", "paper", "scissors")
BEATS = {"rock": "paper", "paper": "scissors", "scissors": "rock"}      # BEATS[x] = tah, který porazí x
BEATEN = {v: k for k, v in BEATS.items()}                              # BEATEN[x] = tah, který x porazí
FORM = "predict"                                      # výchozí podoba (Volby po pokusu V1)
KEYS = {"counter": "throw", "predict": "opponent_throw"}               # klíče otázek (Volby po pokusu V4)
SPEC = ("rock", "rock", "paper", "scissors", "rock", "paper", "scissors", "rock", "paper")     # příklad ze specu: perioda 3, další scissors
SPEC_CASE = (SPEC, "scissors", "p3", "ABC", 8, 1)
CACHE = {}


def setUpModule():
    """Testy českých vět volají funkce nástroje přímo a ty čtou jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(LANG_ENV, None)


def cases(n=3000):
    """Úlohy ze semínka 1; velká sada se losuje jednou za běh modulu."""
    if n not in CACHE:
        CACHE[n] = bench.pattern_cases(n)
    return CACHE[n]


def periods_of(h):
    """Periody 1–4, se kterými souhlasí posledních 8 tahů (nezávisle na bench.py)."""
    return [q for q in range(1, 5) if all(h[-i] == h[-i - q] for i in range(1, 8 - q + 1))]


def next_move(h):
    """Nezávislý řešitel: další tah soupeře z nejmenší periody posledních 8 tahů."""
    return h[-periods_of(h)[0]]


def solver(form):
    """Kontrolní model ze stavu: správná odpověď v dané podobě."""
    return lambda state: BEATS[next_move(state["opponent_moves"])] if form == "counter" else next_move(state["opponent_moves"])


def most(seq):
    c = Counter(seq)
    return [x for x in MOVES if c[x] == max(c.values())]


def least_seen(h):
    c = Counter(h)
    return [x for x in MOVES if x in c and c[x] == min(c.values())]


def least_all(h):
    c = Counter(h)
    return [x for x in MOVES if c[x] == min(c[m] for m in MOVES)]


def follow_last(h):
    """Co soupeř zahrál po posledním dřívějším výskytu svého posledního tahu."""
    return next(([h[j + 1]] for j in range(len(h) - 2, -1, -1) if h[j] == h[-1]), list(MOVES))


def follow_major(h):
    f = [h[j + 1] for j in range(len(h) - 1) if h[j] == h[-1]]
    return most(f) if f else list(MOVES)


# Levné zkratky ze specu (rodina, funkce historie → kandidáti na další tah soupeře), stejné jako v prototypu.
BASE = {"last": ("cheap", lambda h: [h[-1]]), "first": ("cheap", lambda h: [h[0]]), "most": ("cheap", most),
        "least_seen": ("cheap", least_seen), "least_all": ("cheap", least_all), "recent3": ("cheap", lambda h: most(h[-3:])),
        "recent4": ("cheap", lambda h: most(h[-4:])), "missing2": ("cheap", lambda h: [x for x in MOVES if x not in h[-2:]]),
        "follow_last": ("transition", follow_last), "follow_major": ("transition", follow_major)}
BASE.update({f"lag{k}": ("lag", (lambda k: lambda h: [h[-k]])(k)) for k in range(2, 7)})
TRANSFORMS = {"id": lambda x: x, "beats": BEATS.get, "beaten": BEATEN.get}


def shortcut_hits(h, nxt, p):
    """{jméno: (rodina, očekávaná trefa)}; zkratka = základ × převod, takže pokrývá obě podoby. Odstup dělitelný periodou je řešení."""
    out = {}
    for name, (family, f) in BASE.items():
        if family == "lag" and int(name[3:]) % p == 0:
            continue
        cand = f(h)
        for t, g in TRANSFORMS.items():
            out[f"{t}:{name}"] = (family, sum(g(x) == nxt for x in cand) / len(cand))
    return out


def printed(lang, call, *args):
    buf = io.StringIO()
    with i18n.process(lang), contextlib.redirect_stdout(buf):
        call(*args)
    return buf.getvalue().splitlines()


class Basics(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(bench.PATTERN_MOVES, MOVES)
        self.assertEqual((bench.PATTERN_BEATS, bench.PATTERN_BEATEN), (BEATS, BEATEN))
        self.assertEqual(bench.PATTERN_CLASSES, ("p1", "p2", "p3", "p4"))
        self.assertEqual((bench.PATTERN_FORMS, bench.PATTERN_FORM), (("counter", "predict"), FORM))       # Volby po pokusu V1
        self.assertEqual((bench.PATTERN_RUN, bench.PATTERN_WARMUP, bench.PATTERN_CHECK), ((8, 10), (1, 3), 8))
        self.assertEqual(bench.PATTERN_WEIGHTS, {                                     # opsané z prototypu doslova (spec)
            "p1": {"A": (1, 1, 1)}, "p2": {"AB": (1, 1, 1)},
            "p3": {"AAB": (2, 2, 13), "ABB": (41, 2, 2), "ABC": (14, 2, 2), "ABA": (2, 12, 2)},
            "p4": {"AAAB": (1, 14, 1), "AABB": (1, 1, 1), "AABC": (1, 1, 1), "ABBB": (1, 1, 1), "ABBC": (1, 1, 1),
                   "ABCC": (10, 1, 25), "AABA": (1, 1, 1), "ABAA": (3, 1, 1), "ABAC": (1, 1, 1), "ABBA": (11, 1, 1),
                   "ABCA": (1, 1, 1), "ABCB": (5, 1, 7)}})

    def test_solve_by_hand(self):
        self.assertEqual(bench.pattern_solve(SPEC), (3, "scissors"))
        self.assertEqual(bench.pattern_solve(("paper",) * 9), (1, "paper"))
        self.assertEqual(bench.pattern_solve(("scissors", "rock") + ("paper", "rock") * 4), (2, "paper"))
        self.assertEqual(bench.pattern_solve(("rock", "paper") + ("scissors", "scissors", "rock", "paper") * 2 + ("scissors",)), (4, "scissors"))
        self.assertEqual(bench.pattern_solve(("rock", "rock") + ("paper",) * 8), (1, "paper"))          # rozehrávka smí mít tah dvakrát

    def test_solve_rejects_ambiguous_histories(self):
        for h in (("rock", "paper") * 3 + ("rock",),                                   # kratší než 8
                  ("rock", "paper", "scissors", "scissors", "paper", "rock", "rock", "paper", "scissors"),     # bez periody do 4
                  ("rock", "paper", "scissors", "paper", "rock") * 2):                  # perioda 5
            with self.subTest(h=h), self.assertRaises(RuntimeError):
                bench.pattern_solve(h)

    def test_every_agreeing_period_gives_the_same_move(self):      # jednoznačnost (Fine–Wilf): výčet všech 3^8 konců historie
        solved = 0
        for tail in itertools.product(MOVES, repeat=8):
            qs = periods_of(tail)
            if not qs:
                with self.assertRaises(RuntimeError):
                    bench.pattern_solve(tail)
                continue
            self.assertEqual(len({tail[-q] for q in qs}), 1, tail)
            self.assertEqual(bench.pattern_solve(tail), (qs[0], tail[-qs[0]]))
            solved += 1
        self.assertEqual(solved, 3 + 6 + 24 + 72)                                       # primitivní cykly délky 1–4 nad třemi tahy


class Generator(unittest.TestCase):
    def test_cases_keep_the_guarantees(self):
        for case in cases():
            h, nxt, cls, shape, run, warm = case
            p = int(cls[1])
            with self.subTest(case=case):
                self.assertIsInstance(h, tuple)
                self.assertTrue(set(h) <= set(MOVES))
                self.assertEqual(len(h), run + warm)
                self.assertTrue(8 <= run <= 10 and 1 <= warm <= 3)
                self.assertIn(shape, bench.PATTERN_WEIGHTS[cls])
                letters = dict(zip(shape, h[-p:]))                                           # posledních p tahů je tvar
                self.assertEqual("".join(sorted(letters)), "ABC"[:len(set(shape))])
                self.assertEqual(len(set(letters.values())), len(letters))
                self.assertEqual([letters[ch] for ch in shape], list(h[-p:]))
                self.assertEqual(letters["A"], nxt)
                self.assertEqual(periods_of(h)[0], p)
                self.assertEqual({h[-q] for q in periods_of(h)}, {nxt})                       # každá souhlasící perioda dá stejný tah
                body = h[-run:]
                self.assertTrue(all(body[i] == body[i - p] for i in range(p, run)))
                self.assertNotEqual(h[-run - 1], h[-run - 1 + p])                             # rozehrávka cyklus neprodlužuje
                self.assertEqual(min(q for q in range(1, 5) if all(body[i] == body[i - q] for i in range(q, run))), p)

    def test_classes_and_answers_are_spread(self):
        c = cases()
        classes = Counter(x[2] for x in c)
        for cls in bench.PATTERN_CLASSES:
            self.assertTrue(0.22 <= classes[cls] / len(c) <= 0.28, classes)
            answers = Counter(x[1] for x in c if x[2] == cls)
            for move in MOVES:
                self.assertTrue(0.28 <= answers[move] / classes[cls] <= 0.39, (cls, answers))
        self.assertEqual({len(x[0]) for x in c}, set(range(9, 14)))

    def test_shortcuts_stay_near_chance(self):
        for cls, top, cheap_top in (("p2", 0.56, 0.56), ("p3", 0.48, 0.48), ("p4", 0.48, 0.48)):
            sub = [x for x in cases() if x[2] == cls]
            hits = {}
            for h, nxt, *_ in sub:
                for name, (family, v) in shortcut_hits(h, nxt, int(cls[1])).items():
                    hits[name] = (family, hits.get(name, (family, 0))[1] + v / len(sub))
            with self.subTest(cls=cls):
                for name, (family, v) in hits.items():
                    if cls == "p2" and family == "transition":
                        continue                                                    # přechod je v p2 přesné řešení (spec)
                    self.assertLessEqual(v, top if family != "cheap" else cheap_top, name)
        p2 = [x for x in cases() if x[2] == "p2"]
        self.assertEqual(sum(x[0][-1] == x[1] for x in p2), 0)                       # opakuj poslední v p2: nikdy

    def test_exact_shortcut_rates(self):                     # výčet rozdělení generátoru (tvar × délka × rozehrávka × permutace), bez šumu
        for cls, top in (("p3", 0.43), ("p4", 0.41)):
            p, acc, total = int(cls[1]), {}, 0.0
            for shape, ws in bench.PATTERN_WEIGHTS[cls].items():
                for run, w in zip(range(8, 11), ws):
                    for warm in range(1, 4):
                        for perm in itertools.permutations(MOVES):
                            cycle = [dict(zip("ABC", perm))[ch] for ch in shape]
                            body = [cycle[(i - run) % p] for i in range(run)]
                            lasts = [m for m in MOVES if m != cycle[(-1 - run) % p]]
                            opens = [list(a) + [b] for a in itertools.product(MOVES, repeat=warm - 1) for b in lasts]
                            for opening in opens:
                                weight = w / 3 / 6 / len(opens)
                                for name, (_, v) in shortcut_hits(opening + body, cycle[0], p).items():
                                    acc[name] = acc.get(name, 0) + weight * v
                                total += weight
            with self.subTest(cls=cls):
                self.assertLessEqual(max(v / total for v in acc.values()), top)

    def test_same_seed_same_tasks(self):
        self.assertEqual(bench.pattern_cases(40), bench.pattern_cases(40, seed=1))
        self.assertNotEqual(bench.pattern_cases(40), bench.pattern_cases(40, seed=2))

    def test_unknown_class_and_broken_guarantee_raise(self):
        with self.assertRaises(RuntimeError):
            bench.pattern_case(random.Random(1), "p5")
        with mock.patch.dict(bench.PATTERN_WEIGHTS, {"p2": {"AA": (1, 1, 1)}}), self.assertRaises(RuntimeError):     # tvar s periodou 1 ve třídě p2
            bench.pattern_case(random.Random(1), "p2")


class StateAndQuestion(unittest.TestCase):
    COUNTER = ("You play rock-paper-scissors against an opponent. Rock beats scissors, scissors beat paper, and paper beats rock. "
               "opponent_moves lists the opponent's moves so far, oldest first. The opponent opened with a few moves of its own "
               "and then settled into a fixed cycle of 1 to 4 moves, which it has repeated for at least its last 8 moves. "
               "Work out the opponent's next move and choose the move that beats it.")
    PREDICT = ("You play rock-paper-scissors against an opponent. opponent_moves lists the opponent's moves so far, oldest first. "
               "The opponent opened with a few moves of its own and then settled into a fixed cycle of 1 to 4 moves, which it has "
               "repeated for at least its last 8 moves. Work out the opponent's next move and choose it.")
    ALT_COUNTER = ("In this game of rock-paper-scissors, paper wins over rock, rock wins over scissors and scissors win over paper. "
                   "opponent_moves holds every move the opponent has made, from the first to the latest. After a short opening it "
                   "keeps repeating the same sequence of one to four moves, and its last 8 moves or more follow that sequence. "
                   "Continue the sequence by one move and pick the move that wins against it.")
    ALT_PREDICT = ("In this game of rock-paper-scissors, opponent_moves holds every move the opponent has made, from the first to the "
                   "latest. After a short opening it keeps repeating the same sequence of one to four moves, and its last 8 moves or "
                   "more follow that sequence. Continue the sequence by one move and pick that move.")

    def test_spec_state(self):
        self.assertEqual(json.dumps(bench.pattern_state(SPEC_CASE)),
                         '{"opponent_moves": ["rock", "rock", "paper", "scissors", "rock", "paper", "scissors", "rock", "paper"]}')
        self.assertEqual((bench.pattern_answer(SPEC_CASE, "counter"), bench.pattern_answer(SPEC_CASE, "predict")), ("rock", "scissors"))
        self.assertEqual(bench.pattern_answer(SPEC_CASE), bench.pattern_answer(SPEC_CASE, FORM))
        with self.assertRaises(ValueError):
            bench.pattern_question("guess")

    def test_questions_and_alternative_wording(self):
        for form, text, alt, label in (("counter", self.COUNTER, self.ALT_COUNTER, "Play {}"),
                                       ("predict", self.PREDICT, self.ALT_PREDICT, "The opponent plays {}")):
            q = bench.pattern_question(form)
            key = KEYS[form]
            with self.subTest(form=form):
                self.assertEqual(list(q), [key])
                self.assertEqual((q[key]["type"], q[key]["instructions"]), ("choice", text))
                self.assertEqual(json.dumps(q[key]["criteria"]), json.dumps({m: label.format(m) for m in MOVES}))
                self.assertEqual(bench.ALT[key], alt)
                self.assertEqual(bench.variant(q, 2)[key]["instructions"], alt)
                self.assertEqual(list(bench.variant(q, 1)[key]["criteria"]), list(MOVES)[::-1])
        self.assertEqual(bench.pattern_question(), bench.pattern_question(FORM))

    def test_prompt_is_short(self):                          # odhad: 4 znaky JSON na token; zadání spec odhaduje na 200–250 tokenů
        longest = max(len(json.dumps(bench.with_note(bench.pattern_state(x), "rock"))) for x in cases())
        for form in bench.PATTERN_FORMS:
            self.assertLess((longest + len(json.dumps(bench.pattern_question(form)))) / 4, 300)


# Ručně ověřené úlohy pro rozbor chyb (counter: y = tah, který volba porazí).
P2 = (("rock", "rock", "paper", "rock", "paper", "rock", "paper", "rock", "paper", "rock"), "paper", "p2", "AB", 9, 1)
P4 = (("rock", "paper", "scissors", "rock", "paper", "paper", "scissors", "rock", "paper", "paper"), "scissors", "p4", "ABCC", 8, 2)
P4_FOLLOW = (("paper", "rock", "scissors", "scissors", "scissors", "paper", "scissors", "scissors", "scissors", "paper"), "scissors", "p4", "AAAB", 8, 2)
P1 = (("paper", "rock") + ("scissors",) * 8, "scissors", "p1", "A", 8, 2)


class RunPattern(unittest.TestCase):
    def run_with(self, answer, *args, **kw):
        with mock.patch.object(bench, "post", fake_post(answer)):
            return bench.run_pattern(*args, **kw)

    def test_error_kinds_by_hand(self):
        kind = bench.pattern_error_kind
        self.assertEqual([kind(SPEC_CASE, m, "counter") for m in MOVES], [None, "frequent", "last"])        # paper: y = rock, nejčastější
        self.assertEqual([kind(SPEC_CASE, m, "predict") for m in MOVES], ["counter_given", "last", None])
        self.assertEqual(kind(P2, "rock", "counter"), "other")                     # y = scissors: v historii není
        self.assertEqual(kind(P4, "paper", "counter"), "period")                   # y = rock = tah o 3 zpět (perioda 3 místo 4)
        self.assertEqual(kind(P4_FOLLOW, "paper", "counter"), "transition")        # y = rock: po paper přišel rock (rozehrávka)
        self.assertEqual(kind(P1, "paper", "predict"), "other")
        for choice in (None, "lizard", "Rock", 3, ["rock"]):
            with self.subTest(choice=choice):
                self.assertEqual((kind(SPEC_CASE, choice, "counter"), kind(SPEC_CASE, choice, "predict")), ("invalid", "invalid"))
                self.assertEqual(bench.pattern_outcome(SPEC_CASE, choice), "invalid")
        self.assertEqual([bench.pattern_outcome(SPEC_CASE, m) for m in MOVES], ["win", "loss", "draw"])     # další je scissors

    def test_summary_by_hand(self):
        c = [SPEC_CASE] * 4 + [P2, P4, P4_FOLLOW, P1]
        choices = ["rock", "paper", "scissors", None, "rock", "paper", "paper", "paper"]
        s = bench.pattern_summary(c, choices, "counter")
        self.assertEqual(list(s), ["by_period", "errors", "outcome"])
        self.assertEqual(s["by_period"], {"p1": [0, 1], "p2": [0, 1], "p3": [1, 4], "p4": [0, 2]})
        self.assertEqual(json.dumps(s["errors"]), json.dumps({"last": 1, "frequent": 1, "period": 1, "transition": 1, "other": 2, "invalid": 1}))
        self.assertEqual(json.dumps(s["outcome"]), json.dumps({"win": 1, "draw": 1, "loss": 5, "invalid": 1}))
        s = bench.pattern_summary(c, choices, "predict")
        self.assertEqual(list(s), ["by_period", "errors"])
        self.assertEqual(json.dumps(s["errors"]), json.dumps({"counter_given": 1, "last": 4, "frequent": 0, "period": 0, "transition": 0, "other": 1, "invalid": 1}))
        self.assertEqual(s["by_period"], {"p1": [0, 1], "p2": [0, 1], "p3": [1, 4], "p4": [0, 2]})

    def test_perfect_model(self):
        c = cases(120)
        for form in bench.PATTERN_FORMS:
            with self.subTest(form=form):
                r = self.run_with(solver(form), "m", c, form=form)
                self.assertEqual(r["acc"], 1)
                self.assertEqual(list(r["by_period"]), list(bench.PATTERN_CLASSES))
                self.assertEqual(sum(n for _, n in r["by_period"].values()), 120)
                self.assertTrue(all(ok == n > 0 for ok, n in r["by_period"].values()))
                self.assertEqual(sum(r["errors"].values()), 0)
                self.assertEqual("outcome" in r, form == "counter")
        self.assertEqual(self.run_with(solver("counter"), "m", c, form="counter")["outcome"], {"win": 120, "draw": 0, "loss": 0, "invalid": 0})

    def test_control_models_fall_into_their_class(self):
        c = cases(300)
        moves = lambda state: state["opponent_moves"]
        r = self.run_with(lambda s: BEATS[moves(s)[-1]], "m", c, form="counter")          # hraj, co porazí poslední tah
        self.assertEqual(r["errors"]["last"], sum(r["errors"].values()))
        self.assertEqual((r["by_period"]["p1"][0], r["by_period"]["p2"][0]), (r["by_period"]["p1"][1], 0))
        r = self.run_with(lambda s: next_move(moves(s)), "m", c, form="counter")           # správná předpověď, bez kroku „co porazí“
        self.assertEqual(r["outcome"], {"win": 0, "draw": 300, "loss": 0, "invalid": 0})
        r = self.run_with(lambda s: BEATEN[next_move(moves(s))], "m", c, form="counter")   # obrácená porážka
        self.assertEqual(r["outcome"], {"win": 0, "draw": 0, "loss": 300, "invalid": 0})
        r = self.run_with(lambda s: BEATS[next_move(moves(s))], "m", c, form="predict")    # protitah místo předpovědi
        self.assertEqual(r["errors"]["counter_given"], 300)

    def test_invalid_answers(self):
        c = cases(30)
        for answer in (None, "Rock", "lizard", ["rock"], 3):
            for form in bench.PATTERN_FORMS:
                with self.subTest(answer=answer, form=form):
                    r = self.run_with(lambda state: answer, "m", c, form=form)
                    self.assertEqual((r["acc"], r["errors"]["invalid"]), (0, 30))

    def test_requests_carry_the_state_and_the_question(self):
        c = bench.pattern_cases(5)
        for form in bench.PATTERN_FORMS:
            sent = []

            def post(model, payload):
                sent.append(payload)
                return {"answers": {next(iter(payload["questions"])): {"choice": "rock", "confidence": 0.5}}}, 10.0
            with mock.patch.object(bench, "post", post):
                bench.run_pattern("m", c, form=form)
            with self.subTest(form=form):
                self.assertEqual(len(sent), 6)                                  # zahřátí a pět úloh
                self.assertEqual([p["state"] for p in sent], [bench.pattern_state(c[0])] + [bench.pattern_state(x) for x in c])
                self.assertTrue(all(p["questions"] == bench.pattern_question(form) for p in sent))

    def test_mirror_and_inject(self):
        c = cases(40)
        for form in bench.PATTERN_FORMS:
            with self.subTest(form=form):
                r = self.run_with(solver(form), "m", c, mirror=True, form=form)
                self.assertEqual((r["stab"], r["acc"]), (1, 1))
                r = self.run_with(obedient(solver(form)), "m", c, inject=True, form=form)
                self.assertEqual((r["inject"]["n"], r["inject"]["fooled"], r["inject"]["clean_acc"]), (10, 1, 1))
                self.assertEqual(r["acc"], 0.75)
                self.assertEqual((sum(r["errors"].values()) - r["errors"]["invalid"], r["errors"]["invalid"]), (10, 0))
                if form == "counter":
                    self.assertEqual(r["outcome"]["win"], 30)


class Lines(unittest.TestCase):
    def test_note_by_period(self):
        r = {"by_period": {"p1": [9, 10], "p2": [5, 11], "p3": [4, 12], "p4": [3, 9]}}
        for lang, want in (("cs", "(perioda 1 9/10, perioda 2 5/11, perioda 3 4/12, perioda 4 3/9)"),
                           ("en", "(period 1 9/10, period 2 5/11, period 3 4/12, period 4 3/9)")):
            with i18n.process(lang):
                self.assertEqual(bench.pattern_note(r), want)

    def test_lines_in_both_languages(self):
        pad = " " * 20
        r = {"outcome": {"win": 20, "draw": 9, "loss": 10, "invalid": 1},
             "errors": {"last": 4, "frequent": 3, "period": 5, "transition": 1, "other": 6, "invalid": 1}}
        self.assertEqual(printed("cs", bench.print_pattern, r),
                         [pad + "výsledek: výhra 20, remíza 9, prohra 10, neplatné 1",
                          pad + "chyby: opakuje poslední 4, nejčastější tah 3, špatná perioda 5, přechod 1, jiné 6, neplatné 1"])
        self.assertEqual(printed("en", bench.print_pattern, r),
                         [pad + "outcome: win 20, draw 9, loss 10, invalid 1",
                          pad + "errors: repeats the last move 4, most frequent move 3, wrong period 5, transition 1, other 6, invalid 1"])
        r = {"errors": {"counter_given": 2, "last": 4, "frequent": 3, "period": 5, "transition": 1, "other": 6, "invalid": 1}}
        self.assertEqual(printed("cs", bench.print_pattern, r),
                         [pad + "chyby: protitah místo předpovědi 2, opakuje poslední 4, nejčastější tah 3, špatná perioda 5, přechod 1, jiné 6, neplatné 1"])
        self.assertEqual(printed("en", bench.print_pattern, r),
                         [pad + "errors: counter instead of prediction 2, repeats the last move 4, most frequent move 3, wrong period 5, transition 1, other 6, invalid 1"])

    def test_lines_with_empty_classes(self):
        r = {"by_period": {"p1": [1, 1], "p2": [0, 0], "p3": [0, 0], "p4": [0, 0]}, "outcome": {"win": 1, "draw": 0, "loss": 0, "invalid": 0},
             "errors": {"last": 0, "frequent": 0, "period": 0, "transition": 0, "other": 0, "invalid": 0}}
        with i18n.process("en"):
            self.assertEqual(bench.pattern_note(r), "(period 1 1/1, period 2 0/0, period 3 0/0, period 4 0/0)")
        self.assertEqual(len(printed("en", bench.print_pattern, r)), 2)


class Cli(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)                            # jazyk z prostředí vývojáře tu nesmí rozhodovat
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(LANG_ENV, None)
        self.sent = []

    def cli(self, *argv):
        """Pustí bench.py jako příkaz proti podstrčené Ollamě. Vrací (výstup, čím skončil: None nebo argument sys.exit)."""
        def urlopen(req, timeout=None):
            self.sent.append(req.data)
            return fake_urlopen(req, timeout)
        out, code = io.StringIO(), None
        with mock.patch.object(sys, "argv", ["bench.py", *argv]), mock.patch.object(urllib.request, "urlopen", urlopen), \
                mock.patch.object(time, "perf_counter", Clock()), contextlib.redirect_stdout(out):
            try:
                runpy.run_path(str(ROOT / "bench.py"), run_name="__main__")
            except SystemExit as e:
                code = e.code
        return out.getvalue(), code

    def test_parse(self):
        a = bench.parse_args(["40", "--pattern", "m1"])
        self.assertEqual((a.n, a.pattern, a.counter, a.predict, a.lanes_given, a.models), (40, True, False, False, False, ["m1"]))
        a = bench.parse_args(["--predict", "--pattern", "m1"])
        self.assertEqual((a.pattern, a.counter, a.predict), (True, False, True))
        self.assertTrue(bench.parse_args(["--pattern", "--counter", "m1"]).counter)
        self.assertTrue(bench.parse_args(["--memory", "--count", "m1"]).count)          # --count (paměťová stopa) se s --counter neplete

    def test_first_line_and_rows(self):
        words = {"counter": ("protitah", "counter"), "predict": ("předpověď", "prediction")}
        for argv, n, form in ((["1", "--pattern", "m1"], 1, FORM), (["7", "--pattern", "--predict", "m1"], 7, "predict"),
                              (["3", "--counter", "--pattern", "m1"], 3, "counter")):
            cs = {1: "1 úloha", 3: "3 úlohy", 7: "7 úloh"}[n] + f" se vzorem soupeře (kámen–nůžky–papír, otázka {words[form][0]}); sada je pro všechny modely stejná"
            en = {1: "1 task", 3: "3 tasks", 7: "7 tasks"}[n] + f" against an opponent's pattern (rock-paper-scissors, {words[form][1]} question), the same for all models"
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[0].splitlines()[0], cs)
                self.assertEqual(self.cli(*argv, "--lang", "en")[0].splitlines()[0], en)
        self.assertTrue(self.cli("--pattern", "m1")[0].startswith("100 úloh se vzorem soupeře"))      # výchozí počet úloh
        lines = self.cli("6", "--pattern", "--counter", "m1", "--lang", "en")[0].splitlines()
        self.assertEqual(len(lines), 6)
        self.assertRegex(lines[3], r"^m1 .*   \(period 1 \d+/\d+, period 2 \d+/\d+, period 3 \d+/\d+, period 4 \d+/\d+\)$")
        self.assertRegex(lines[4], r"^ {20}outcome: win \d+, draw \d+, loss \d+, invalid \d+$")
        self.assertRegex(lines[5], r"^ {20}errors: repeats the last move \d+, most frequent move \d+, wrong period \d+, transition \d+, other \d+, invalid \d+$")
        lines = self.cli("6", "--pattern", "--predict", "m1")[0].splitlines()
        self.assertEqual(len(lines), 5)
        self.assertRegex(lines[4], r"^ {20}chyby: protitah místo předpovědi \d+, opakuje poslední \d+, ")
        self.assertEqual(len(self.cli("1", "--pattern", "m1")[0].splitlines()), 6 if FORM == "counter" else 5)    # jediná úloha: prázdné třídy 0/0

    def test_option_errors_in_both_languages(self):
        form_cs, form_en = "Volby --counter a --predict jdou jen s --pattern.", "The --counter and --predict options work only with --pattern."
        for argv, cs, en in (
                (["6", "--pattern", "--lanes", "3", "m1"], "Volba --lanes se s --pattern nekombinuje.", "The --lanes option cannot be combined with --pattern."),
                (["6", "--predict", "m1"], form_cs, form_en),
                (["--counter", "--rule", "m1"], form_cs, form_en),
                (["--pattern", "--rows", "m1"], "Volby --rows a --blocked jdou jen s --lookahead.", "The --rows and --blocked options work only with --lookahead."),
                (["--pattern", "--count", "m1"], "Volba --count jde jen s --memory.", "The --count option works only with --memory.")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", cs))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", en))
        for argv in (["--pattern", "--rule", "m1"], ["--pattern", "--timetable", "m1"], ["--pattern", "--counter", "--predict", "m1"],
                     ["--pattern", "--lanes", "9", "m1"], ["--pattern"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv), ("", bench.__doc__))
                self.assertEqual(self.cli(*argv, "--lang", "en"), ("", bench.USAGE_EN))
        self.assertEqual(self.sent, [])

    def test_both_forms_send_the_same_states(self):
        self.cli("6", "--pattern", "--counter", "m1")
        counter, self.sent = [json.loads(body) for body in self.sent], []
        self.cli("6", "--pattern", "--predict", "m1")
        predict = [json.loads(body) for body in self.sent]
        self.assertEqual((len(counter), len(predict)), (7, 7))                   # zahřátí a šest úloh
        self.assertEqual([p["state"] for p in counter], [p["state"] for p in predict])
        self.assertEqual({next(iter(p["questions"])) for p in counter}, {KEYS["counter"]})
        self.assertEqual({next(iter(p["questions"])) for p in predict}, {KEYS["predict"]})

    def test_mirror_and_inject_work_with_pattern(self):
        out, code = self.cli("8", "--pattern", "--counter", "--mirror", "--inject", "--deadline-ms", "9", "m1")
        self.assertIsNone(code)
        lines = out.splitlines()
        self.assertIn("stabilita", lines[2])
        self.assertIn("goodput", lines[2])
        self.assertTrue(lines[4].strip().startswith("přesnost variant: "), lines[4])
        self.assertTrue(lines[5].strip().startswith("trojský kůň: 2 úloh s pokynem"), lines[5])
        self.assertTrue(lines[6].strip().startswith("výsledek: výhra "), lines[6])
        self.assertTrue(lines[7].strip().startswith("chyby: opakuje poslední "), lines[7])

    def test_usage_names_the_options(self):
        for text in (bench.__doc__, bench.USAGE_EN):
            self.assertIn("python bench.py [N] --pattern [--counter | --predict] [--mirror] [--inject] [--deadline-ms L] model1 model2 ...", text)
            self.assertIn("python bench.py 100 --pattern tev1", text)
            self.assertIn("\n--pattern:  ", text)
            self.assertIn("\n--counter, --predict:  ", text)
        self.assertIn(f"Bez volby platí {FORM}.", bench.__doc__)
        self.assertIn(f"Without the option: {FORM}.", bench.USAGE_EN)


def pattern_body():
    return page_source().split("\nconst pattern = {", 1)[1].split("\n};\n", 1)[0]


class Page(unittest.TestCase):              # scénář ve stránce; řešitel, generátor a rozbor hlídá tests/test_parity.py (Pattern)
    def test_scenario_is_offered_selected_and_wired(self):
        page = page_source()
        self.assertEqual(page.count('<option value="pattern" data-i18n="opt.scenario.pattern"></option>'), 1)
        self.assertEqual(page.count(REGISTRY), 1)
        self.assertIn(" pattern }[v];", REGISTRY)
        self.assertEqual(page.count("// ---------- scénář: vzor soupeře ----------"), 1)
        # zahřívací úloha je pevná a bez pokynu: nebere z rnd, jinak by se řady posunuly podle řidiče
        self.assertEqual(page.count("  warmTask() { return this.task(patternWarm(), null, null); },"), 1)
        self.assertEqual(page.count("  spawnRow(g, y) { g.rows.push(this.newRow(g, y, patternRow(rnd), 3)); },"), 1)
        self.assertEqual(page.count("  randomTask() { return this.task(patternRow(rnd)); },"), 1)
        body = pattern_body()
        for call in ("rollInject([d.truth], 3)", "patternState(d.moves)", "injectText(INJECT_NOTE, PATTERN_MOVES[inject])", "variantOf(patternQuestion(), v)",
                     "a.opponent_throw?.choice", "patternSummary(", "fixedLanes: 3, lanes: () => 3,", "panels: ['confPanel', 'trail', 'legend']"):
            self.assertIn(call, body)                                                             # definice ze sdíleného bloku, ne opsané

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_summary_runs_once_per_new_answer(self):               # quality i renderExtra čtou tentýž rozbor jako u nápovědy a Co se změnilo
        body = pattern_body()
        for name in ("quality", "renderExtra"):
            self.assertIn("this.summary(g)", method(body, name))
            self.assertNotIn("patternSummary(", method(body, name))
        code = ("let calls = 0; const patternSummary = (c, ch) => { calls++; return { errors: { counter_given: c.length } }; };"
                "const o = {" + method(body, "summary") + "};"
                "const g = { cases: [1, 2], choices: ['rock', 'paper'] }, a = o.summary(g), b = o.summary(g);"
                "g.cases.push(3); g.choices.push(null); const c = o.summary(g);"
                "process.stdout.write(JSON.stringify({ calls, same: a === b, n: c.n, k: c.errors.counter_given }));")
        self.assertEqual(run_page_js(code), {"calls": 2, "same": True, "n": 3, "k": 3})

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_tally_keeps_the_task_and_the_choice(self):            # počítají se všechny odpovědi; neplatná je null
        tally = re.search(r"\n  tally\(g, task, move\) \{.*?\n  \},", pattern_body(), re.S).group(0)[1:-1]
        code = ("const PATTERN_MOVES = ['rock', 'paper', 'scissors']; const base = { tally() {} }; const o = { __proto__: base," + tally + "};"
                "const g = { cases: [], choices: [] }, task = { moves: ['rock'], next: 'paper', cls: 'p1', truth: 1, state: {} };"
                "[1, 0, -1].forEach(m => o.tally(g, task, m)); process.stdout.write(JSON.stringify(g));")
        case = {"moves": ["rock"], "next": "paper", "cls": "p1"}
        self.assertEqual(run_page_js(code), {"cases": [case] * 3, "choices": ["paper", "rock", None]})

    def test_drawing_writes_only_through_t(self):
        self.assertNotIn("fillText(", page_function("drawPatternMove"))                         # tahy jen kresbou
        body = pattern_body()
        for part in ("drawTrail(g) {", "memoryTrailRow(g)", "t('pattern.trailTitle')", "t('pattern.now')", "t('pattern.cycle'",
                     "t(PATTERN_MOVE_NAME[", "drawPatternMove(ctx, ", "drawPatternMove(tc, ", "patternLabel(r)"):
            self.assertIn(part, body)

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_labels_name_the_outcome(self):             # štítek: trefa, chyba s druhem, chyba bez druhu, neplatná, nestihl
        from test_parity import run_js
        page = page_source()
        consts = "".join(re.search(r"\nconst %s = [^\n]*\n" % n, page).group(0) for n in ("RESULT", "PATTERN_MOVE_NAME", "PATTERN_KIND_NAME"))
        code = "(() => {" + consts + page_function("patternLabel") + (
            "const moves = ['rock', 'rock', 'paper', 'scissors', 'rock', 'paper', 'scissors', 'rock', 'paper'];"      # příklad ze specu: další scissors
            "const r = (result, chosen) => ({ moves, next: 'scissors', cls: 'p3', truth: 2, result, chosen });"
            "return [r('ok', 2), r('bad', 0), r('bad', 1), r('bad', -1), r('late', undefined)].map(patternLabel); })()")
        cs, en = run_js(code, None), run_js(code, None, "en")
        self.assertEqual([x["text"] for x in cs], ["✓ soupeř zahrál nůžky", "✗ zvolil kámen, soupeř zahrál nůžky (protitah místo předpovědi)",
                                                   "✗ zvolil papír, soupeř zahrál nůžky (opakuje poslední tah)", "✗ neplatná odpověď", "⏱ nestihl"])
        self.assertEqual(en[1]["text"], "✗ picked rock, the opponent played scissors (counter instead of prediction)")
        self.assertEqual([x["color"] for x in cs], ["#3fb950", "#f85149", "#f85149", "#8b9aa8", "#8b9aa8"])

    @unittest.skipUnless(shutil.which("node"), "program node není v PATH")
    def test_strip_hides_the_cycle_until_the_row_is_passed(self):   # divák vidí totéž co model; po průjezdu cyklus a skutečný tah
        from test_parity import run_js
        drawtrail = re.search(r"\n  drawTrail\(g\) \{.*?\n  \},", pattern_body(), re.S).group(0)[1:-1]
        page = page_source()
        consts = "".join(re.search(r"\nconst %s = [^\n]*\n" % n, page).group(0) for n in ("PATTERN_MOVE_NAME", "PATTERN_RUN", "PATTERN_WARMUP"))
        code = "(() => { const texts = [], W = 300;" + consts + (
            "const tc = new Proxy({ fillText(s) { texts.push(s); } }, { get: (o, k) => k in o ? o[k] : () => {}, set: (o, k, v) => { o[k] = v; return true; } });"
            "const c = { width: 300, height: 70, hidden: false, getContext: () => tc }, $ = () => c; let shown;"
            "const memoryTrailRow = () => shown, drawPatternMove = () => {}; const o = {" + drawtrail + "};"
            "shown = { moves: ['rock', 'rock', 'paper', 'scissors', 'rock', 'paper', 'scissors', 'rock', 'paper'], next: 'scissors', cls: 'p3', run: 8, result: null };"
            "o.drawTrail({}); const a = texts.splice(0); shown = { ...shown, result: 'ok' }; o.drawTrail({}); return [a, texts]; })()")
        before, after = run_js(code, None)
        self.assertEqual(before, ["soupeř hrál (nejstarší vlevo)", "?", "teď"])
        self.assertEqual(after, ["soupeř hrál (nejstarší vlevo)", "cyklus: nůžky, kámen, papír (perioda 3)"])


if __name__ == "__main__":
    unittest.main()
