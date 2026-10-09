import json
import re
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import bench
import i18n
import server

PATH = Path(__file__).parent.parent / "datasets" / "cz-firemni-emaily.json"


class ShippedDataset(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = json.loads(PATH.read_text(encoding="utf-8-sig"))

    def test_categories(self):
        self.assertEqual([c["key"] for c in self.ds["categories"]], ["invoice", "order", "support", "complaint", "spam"])
        for c in self.ds["categories"]:
            self.assertRegex(c["color"], r"^#[0-9a-fA-F]{6}$")
            self.assertTrue(c["label"] and c["description"])
        self.assertEqual(len({c["letter"] for c in self.ds["categories"]}), 5)

    def test_sixty_emails_twelve_per_category(self):
        self.assertEqual(Counter(e["category"] for e in self.ds["emails"]),
                         {k: 12 for k in ("invoice", "order", "support", "complaint", "spam")})

    def test_emails_are_distinct_short_and_czech(self):
        subjects = [e["subject"] for e in self.ds["emails"]]
        self.assertEqual(len(set(subjects)), 60)
        for e in self.ds["emails"]:
            self.assertTrue(40 <= len(e["body"]) <= 600, e["subject"])
        text = " ".join(e["body"] for e in self.ds["emails"])
        self.assertTrue(any(ch in text for ch in "ěščřžýáíé"))

    def test_no_bom(self):
        self.assertFalse(PATH.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_brief_samples_present_verbatim(self):
        samples = [
            ("Upomínka – objednávka č. 2025/0418 stále nedorazila",
             "Dobrý den, před třemi týdny jsme objednali 40 ks vodoměrů a zboží jsme dosud neobdrželi. Můžete mi sdělit termín dodání?",
             "order"),
            ("Faktura 2250113 – účtujete nám vadné kusy",
             "Dobrý den, na faktuře je 12 měřičů tepla, z toho 3 přišly s prasklým displejem. Žádáme o výměnu vadných kusů v rámci záruky.",
             "complaint"),
            ("Jak stáhnout fakturu z portálu?",
             "Dobrý den, v zákaznickém portálu nemohu najít, kde se stahují faktury za loňský rok. Poradíte mi prosím, kde to je?",
             "support"),
            ("Vaše faktura čeká na úhradu – klikněte zde",
             "Vážený zákazníku, Vaše faktura nebyla uhrazena. Pro zobrazení klikněte na odkaz a zadejte přihlašovací údaje k internetovému bankovnictví.",
             "spam"),
            ("Druhá upomínka k úhradě faktury 2240987",
             "Dobrý den, evidujeme neuhrazenou fakturu č. 2240987 po splatnosti 21 dní. Prosíme o úhradu do 5 pracovních dnů.",
             "invoice"),
        ]
        emails = {(e["subject"], e["body"], e["category"]) for e in self.ds["emails"]}
        for s in samples:
            self.assertIn(s, emails, s[0])


KOS = PATH.with_name("cz-firemni-emaily-kos.json")
OTHER = {"key": "other", "label": "Jiné", "label_en": "Other", "letter": "J", "letter_en": "T", "color": "#bbbbbb",
         "description": "None of the other categories: a message that is not an invoice, an order, "
                        "a support request, a complaint or spam."}


class DatasetWithOther(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.loads(PATH.read_text(encoding="utf-8-sig"))
        cls.ds = json.loads(KOS.read_text(encoding="utf-8-sig"))
        cls.new = cls.ds["emails"][60:]

    def test_sixth_category_is_other(self):
        self.assertEqual(self.ds["categories"][:5], self.base["categories"])
        self.assertEqual(self.ds["categories"][5:], [OTHER])
        self.assertEqual(len({c["letter"] for c in self.ds["categories"]}), 6)
        self.assertEqual(len({c["color"].lower() for c in self.ds["categories"]}), 6)

    def test_first_sixty_emails_are_the_base_set(self):
        self.assertEqual(self.ds["emails"][:60], self.base["emails"])
        self.assertEqual(self.ds["instructions"], self.base["instructions"])

    def test_fifteen_new_emails_in_other(self):
        self.assertEqual(len(self.ds["emails"]), 75)
        self.assertEqual(Counter(e["category"] for e in self.ds["emails"])["other"], 15)
        self.assertTrue(all(e["category"] == "other" and set(e) == {"subject", "body", "category"} for e in self.new))

    def test_new_emails_are_distinct_short_czech_and_anonymous(self):
        self.assertEqual(len({e["subject"] for e in self.ds["emails"]}), 75)
        self.assertEqual(len({e["body"] for e in self.ds["emails"]}), 75)
        for e in self.new:
            self.assertTrue(40 <= len(e["body"]) <= 600, e["subject"])
            self.assertTrue(any(ch in e["body"] for ch in "ěščřžýáíéůú"), e["subject"])
            self.assertNotRegex(e["subject"] + " " + e["body"], r"@|https?://|www\.|\d{3} ?\d{3} ?\d{3}", e["subject"])

    def test_name_differs_from_the_base_set(self):                     # jinak by ve voliči Scénář byly dvě stejné položky
        self.assertEqual(self.ds["name"], "České firemní e-maily s košem")
        self.assertNotEqual(self.ds["name"], self.base["name"])

    def test_no_bom(self):
        self.assertFalse(KOS.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_plan_samples_present_verbatim(self):
        samples = [
            ("Pozvánka na poradu vedoucích středisek 14. října",
             "Dobrý den, zvu vás na poradu vedoucích středisek ve středu 14. října v 9:00 v zasedací místnosti ve 2. patře. Program pošlu den předem, prosím o potvrzení účasti."),
            ("Oddělení reklamací má od 23. do 27. prosince dovolenou",
             "Dobrý den, oznamujeme, že oddělení reklamací bude od 23. do 27. prosince čerpat celozávodní dovolenou. Od 28. prosince jsme vám opět k dispozici v běžné pracovní době."),
            ("Žádost o referenci pro výběrové řízení",
             "Dobrý den, hlásíme se do výběrového řízení na správu bytových domů ve městě a zadavatel požaduje reference od dodavatelů. Mohli byste nám potvrdit, že s námi spolupracujete od roku 2019?"),
            ("Blahopřání k 30 letům firmy",
             "Vážení obchodní přátelé, dovolte nám poblahopřát k 30. výročí založení vaší společnosti. Děkujeme za dlouholetou spolupráci a přejeme mnoho dalších úspěšných let."),
            ("Školení k novému fakturačnímu systému – změna termínu",
             "Kolegové, školení k novému fakturačnímu systému se přesouvá z úterý na čtvrtek 16. října ve 13:00. Místo zůstává stejné, učebna v přízemí. Kdo nemůže, ať mi dá vědět."),
            ("Dotaz na volné místo servisního technika",
             "Dobrý den, na vašem webu jsem viděl, že hledáte servisního technika pro Moravskoslezský kraj. Je pozice stále volná a komu mohu poslat životopis?"),
        ]
        emails = {(e["subject"], e["body"], e["category"]) for e in self.ds["emails"]}
        for subject, body in samples:
            self.assertIn((subject, body, "other"), emails, subject)

    def test_bench_and_server_accept_the_set(self):
        ds = bench.load_dataset(str(KOS))
        self.assertEqual((len(ds["categories"]), len(ds["emails"]), ds["index"]["other"]), (6, 75, 5))
        self.assertIn({"file": KOS.name, "name": "České firemní e-maily s košem"}, server.list_datasets())


BIG = PATH.with_name("cz-emaily-300.json")


class Dataset300(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.loads(PATH.read_text(encoding="utf-8-sig"))
        cls.ds = json.loads(BIG.read_text(encoding="utf-8"))
        cls.emails = cls.ds["emails"]

    def test_no_bom(self):
        self.assertFalse(BIG.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_bench_and_server_accept_the_set(self):
        ds = bench.load_dataset(str(BIG))
        self.assertEqual((len(ds["categories"]), len(ds["emails"])), (5, 300))
        self.assertIn({"file": BIG.name, "name": "České firemní e-maily (300)"}, server.list_datasets())

    def test_header_matches_the_base_set(self):
        self.assertEqual(self.ds["categories"], self.base["categories"])
        self.assertEqual(self.ds["instructions"], self.base["instructions"])
        self.assertEqual(self.ds["name"], "České firemní e-maily (300)")

    def test_three_hundred_emails_sixty_per_category(self):
        self.assertEqual(len(self.emails), 300)
        self.assertEqual(Counter(e["category"] for e in self.emails),
                         {k: 60 for k in ("invoice", "order", "support", "complaint", "spam")})
        self.assertTrue(all(set(e) == {"subject", "body", "category"} for e in self.emails))

    def test_subjects_unique_and_not_in_other_datasets(self):
        subjects = [e["subject"] for e in self.emails]
        self.assertEqual(len(set(subjects)), 300)
        existing = set()
        for path in (PATH, KOS):
            existing |= {e["subject"] for e in json.loads(path.read_text(encoding="utf-8-sig"))["emails"]}
        self.assertEqual(set(subjects) & existing, set())

    def test_body_length(self):
        for e in self.emails:
            self.assertTrue(40 <= len(e["body"]) <= 600, e["subject"])

    def test_nothing_that_identifies_a_real_party(self):
        pattern = r"@|https?://|www\.|\b[\w-]+\.(?:cz|com|eu|net|org|sk)\b|\d{3} ?\d{3} ?\d{3}|\d{9,}|\+\d"
        for e in self.emails:
            text = e["subject"] + " " + e["body"]
            self.assertNotRegex(text, pattern, e["subject"])

    def test_shuffled_without_long_runs(self):
        run = longest = 1
        for a, b in zip(self.emails, self.emails[1:]):
            run = run + 1 if a["category"] == b["category"] else 1
            longest = max(longest, run)
        self.assertLessEqual(longest, 6)


CZECH = re.compile(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„“]")
cat = lambda k, **more: {"key": k, "label": k.upper(), "description": "d " + k, **more}
mail = lambda c: {"subject": "s", "body": "b", "category": c}
GOOD = {"categories": [cat("a"), cat("b")], "emails": [mail("a")]}
# Špatné sady e-mailů (stejné jako v testu shody se stránkou v tests/test_i18n.py) a věta, kterou validátor česky říkal před překladem.
BAD_SETS = [
    ([], "Neplatná sada: chybí categories nebo emails"),
    ({}, "Neplatná sada: chybí categories nebo emails"),
    ({**GOOD, "name": 5}, "Neplatná sada: name a instructions musí být text"),
    ({**GOOD, "about": 5}, "Neplatná sada: about musí být text"),
    ({**GOOD, "categories": [cat("a")]}, "Neplatná sada: počet kategorií musí být 2–8"),
    ({**GOOD, "emails": []}, "Neplatná sada: žádné e-maily"),
    ({**GOOD, "categories": [cat("a"), 7]}, "Neplatná sada: kategorie 2 musí být objekt"),
    ({**GOOD, "categories": [cat("a"), {"key": "b"}]}, "Neplatná sada: kategorie 2 nemá key, label nebo description jako text"),
    ({**GOOD, "categories": [cat("a"), cat("a")]}, "Neplatná sada: klíč kategorie „a“ se opakuje"),
    ({**GOOD, "categories": [cat("a"), cat("b", letter=5)]}, "Neplatná sada: písmeno kategorie „b“ musí být text"),
    ({**GOOD, "categories": [cat("a"), cat("b", color="red")]}, "Neplatná sada: barva kategorie „b“ musí být ve tvaru #rrggbb"),
    ({**GOOD, "emails": [7]}, "Neplatná sada: e-mail 1 musí být objekt"),
    ({**GOOD, "emails": [{"subject": "s"}]}, "Neplatná sada: e-mail 1 nemá subject nebo body jako text"),
    ({**GOOD, "emails": [mail("zzz")]}, "Neplatná sada: e-mail 1 má neznámou kategorii „zzz“"),
]
PUBLIC = {"source": "s", "license": "l", "labels": [{"key": "a"}, {"key": "b"}], "items": [{"text": "t", "label": "a"}]}


class ValidatorLanguage(unittest.TestCase):
    """Validátor sady mluví jazykem nástroje; česky doslova jako před překladem. Co jde do modelu, na jazyce nezávisí."""

    def sentence(self, d, lang):
        with i18n.process(lang), self.assertRaises(ValueError) as ctx:
            bench.validate_dataset(d)
        return str(ctx.exception)

    def test_czech_sentences_read_as_before(self):
        for d, czech in BAD_SETS:
            self.assertEqual(self.sentence(d, "cs"), czech)

    def test_bad_sets_get_an_english_sentence(self):
        seen = set()
        for d, czech in BAD_SETS:
            text = self.sentence(d, "en")
            self.assertTrue(text.startswith("Invalid dataset: "), text)
            self.assertEqual(CZECH.findall(text), [], text)                   # klíče kategorií ve zkušebních sadách diakritiku nemají
            for number in re.findall(r" (\d+) ", czech):                      # věta říká i anglicky, kde chyba je: pořadí položky…
                self.assertIn(f" {number} ", text, czech)
            for key in re.findall(r"„(\w+)“", czech):                         # …nebo klíč převzatý ze sady
                self.assertIn(f'"{key}"', text, czech)
            seen.add(text)
        self.assertEqual(len(seen), 13)                                       # každá chyba má vlastní větu

    def test_default_name_is_in_the_language_of_the_tool(self):
        for lang, emails, public in (("cs", "e-maily", "veřejná sada"), ("en", "e-mails", "public dataset")):
            with i18n.process(lang):
                self.assertEqual((bench.validate_dataset(GOOD)["name"], bench.validate_public(PUBLIC)["name"]), (emails, public), lang)

    def test_default_instructions_go_to_the_model_and_do_not_change_with_the_language(self):
        want = ("Classify the e-mail into exactly one category.", "Classify the text into exactly one of the labels.")
        for lang in ("cs", "en"):
            with i18n.process(lang):
                got = (bench.validate_dataset(GOOD)["instructions"], bench.validate_public(PUBLIC)["instructions"])
                asked = bench.email_question(bench.validate_dataset(GOOD))["category"]["instructions"]
            self.assertEqual(got, want, lang)
            self.assertEqual(asked, want[0], lang)                            # tentýž text jde do dotazu
        for key, entry in i18n.TEXTS.items():                                 # text pro model mezi texty rozhraní nepatří
            for sentence in want:
                self.assertNotIn(sentence, entry["cs"] + entry["en"], key)


TESTS = Path(__file__).parent
SHIPPED = [PATH, KOS, BIG, PATH.with_name("cfpb-complaints-300.json"), TESTS / "public-mini.json", TESTS / "public-options.json"]


class EnglishDescription(unittest.TestCase):
    """Popis sady pro diváka má volitelnou anglickou podobu about_en: validátory ji berou a kontrolují jako about, přibalené sady ji nesou vyplněnou."""

    def sentence(self, validate, d, lang):
        with i18n.process(lang), self.assertRaises(ValueError) as ctx:
            validate(d)
        return str(ctx.exception)

    def test_about_en_of_another_type_is_refused_with_its_own_sentence(self):
        for bad in (5, ["x"], {"en": "x"}, True):
            with self.subTest(bad=bad):
                self.assertEqual(self.sentence(bench.validate_dataset, {**GOOD, "about_en": bad}, "cs"), "Neplatná sada: about_en musí být text")
                self.assertEqual(self.sentence(bench.validate_dataset, {**GOOD, "about_en": bad}, "en"), "Invalid dataset: about_en must be text")
                self.assertEqual(self.sentence(bench.validate_public, {**PUBLIC, "about_en": bad}, "cs"), "Neplatná veřejná sada: about_en musí být text")
                self.assertEqual(self.sentence(bench.validate_public, {**PUBLIC, "about_en": bad}, "en"), "Invalid public dataset: about_en must be text")
        both = {"about": 5, "about_en": 5}                                    # obě pole špatně: první je věta o about, doslova jako před změnou
        self.assertEqual(self.sentence(bench.validate_dataset, {**GOOD, **both}, "cs"), "Neplatná sada: about musí být text")
        self.assertEqual(self.sentence(bench.validate_public, {**PUBLIC, **both}, "cs"), "Neplatná veřejná sada: name, about a instructions musí být text")

    def test_category_carries_its_english_name_only_when_it_has_one(self):      # label_en a letter_en: název a písmeno kategorie pro diváka anglické stránky
        def first(**fields):
            return bench.validate_dataset({**GOOD, "categories": [{**cat("a"), **fields}, cat("b")]})["categories"][0]
        for lang in ("cs", "en"):                                              # validátor nevybírá jazyk: nese obě podoby
            with self.subTest(lang=lang), i18n.process(lang):
                self.assertEqual((first(label_en="Invoice")["label_en"], first(label_en="Invoice")["letter_en"]), ("Invoice", "I"))
                self.assertEqual(first(label_en=" invoice ")["letter_en"], "I")                  # výchozí písmeno: první znak názvu bez okrajových mezer
                self.assertEqual(first(label_en="Invoice", letter_en="X")["letter_en"], "X")
                for fields in ({}, {"label_en": None}, {"label_en": ""}, {"label_en": " 	 "}, {"letter_en": "X"}):      # bez názvu se nenese ani písmeno
                    self.assertEqual(first(**fields), first(), fields)
                    self.assertNotIn("label_en", first(**fields))
        for field, cs, en in (("label_en", "Neplatná sada: label_en kategorie „a“ musí být text", 'Invalid dataset: label_en of category "a" must be text'),
                              ("letter_en", "Neplatná sada: letter_en kategorie „a“ musí být text", 'Invalid dataset: letter_en of category "a" must be text')):
            for bad in (5, ["x"], True):
                d = {**GOOD, "categories": [{**cat("a"), field: bad}, cat("b")]}
                self.assertEqual((self.sentence(bench.validate_dataset, d, "cs"), self.sentence(bench.validate_dataset, d, "en")), (cs, en))

    def test_shipped_sets_name_every_category_in_english_with_its_own_letter(self):
        for path in sorted(PATH.parent.glob("*.json")):
            with self.subTest(path=path.name):
                categories = bench.load_dataset(str(path))["categories"]
                self.assertTrue(all(CZECH.findall(c["label_en"]) == [] for c in categories))
                for field in ("letter", "letter_en"):                          # dvě kategorie se stejným písmenem by na silnici nešly rozeznat
                    self.assertEqual(len({c[field] for c in categories}), len(categories), field)

    def test_command_line_names_categories_in_the_language_of_the_run(self):
        named, plain = {"key": "a", "label": "Faktura", "label_en": "Invoice"}, {"key": "b", "label": "Podpora"}
        with i18n.process("cs"):
            self.assertEqual([bench.label_of(named), bench.label_of(plain)], ["Faktura", "Podpora"])
        with i18n.process("en"):
            self.assertEqual([bench.label_of(named), bench.label_of(plain)], ["Invoice", "Podpora"])

    def test_about_en_is_optional_and_the_result_carries_it_only_when_the_set_has_it(self):
        for validate, good in ((bench.validate_dataset, GOOD), (bench.validate_public, PUBLIC)):
            for lang in ("cs", "en"):
                with self.subTest(validate=validate.__name__, lang=lang), i18n.process(lang):
                    ds = validate({**good, "about": "česky", "about_en": "in English"})
                    self.assertEqual((ds["about"], ds["about_en"]), ("česky", "in English"))      # validátor nevybírá jazyk: nese obě podoby
                    self.assertEqual(validate({**good, "about_en": "in English"})["about"], "")
                    for d in (good, {**good, "about": "česky"}, {**good, "about": "česky", "about_en": None}, {**good, "about": "česky", "about_en": ""},
                              {**good, "about": "česky", "about_en": "   "}, {**good, "about": "česky", "about_en": "\t\r\n\xa0"}):      # jen bílé znaky = prázdný
                        self.assertNotIn("about_en", validate(d))             # sada bez anglického popisu (třeba vlastní sada uživatele): stránka ukáže about
                    self.assertEqual(validate({**good, "about_en": "  in English "})["about_en"], "  in English ")      # popis s textem se neořezává

    def test_every_shipped_set_has_a_whole_english_description(self):
        self.assertEqual(len(SHIPPED), 6)
        for path in SHIPPED:
            with self.subTest(path=path.name):
                doc = json.loads(path.read_text(encoding="utf-8-sig"))
                about, english = doc["about"], doc.get("about_en")
                self.assertIsInstance(english, str)
                self.assertTrue(english.strip())
                self.assertEqual(CZECH.findall(english), [])                  # anglický popis bez české diakritiky
                self.assertTrue(CZECH.search(about))                          # český popis zůstal český
                self.assertNotEqual(english, about)
                keys = list(doc)
                self.assertEqual(keys[keys.index("about") + 1], "about_en")   # hned za českým popisem
                self.assertEqual(sorted(re.findall(r"\d+", english)), sorted(re.findall(r"\d+", about)))      # počty, meze a data jsou stejné
                self.assertGreater(len(english), 0.8 * len(about))            # celý popis, nezkrácený

    def test_model_does_not_get_either_description(self):                     # about ani about_en do dotazu nejdou
        for path in SHIPPED:
            with self.subTest(path=path.name):
                doc = json.loads(path.read_text(encoding="utf-8-sig"))
                if "emails" in doc:
                    ds = bench.validate_dataset(doc)
                    asked = json.dumps(bench.email_question(ds), ensure_ascii=False)
                else:
                    ds = bench.validate_public(doc)
                    asked = json.dumps([bench.public_question(ds, item) for item in ds["items"]], ensure_ascii=False)
                self.assertEqual(ds["about_en"], doc["about_en"])
                for text in (doc["about"], doc["about_en"]):
                    self.assertNotIn(text[:25], asked)


if __name__ == "__main__":
    unittest.main()
