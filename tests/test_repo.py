import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import providers


def private(*path):
    """Text souboru, který git nesleduje (docs/, CLAUDE.md: pracovní dokumentace jen na disku autora); None, když tu není (veřejný klon)."""
    file = ROOT.joinpath(*path)
    return file.read_text(encoding="utf-8") if file.is_file() else None


class License(unittest.TestCase):
    def test_mit_with_the_right_holder(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("MIT License\n"))
        self.assertIn("\nCopyright (c) 2026 František Janoš\n", text)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND', text)


class Readme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_is_english_and_names_the_project(self):
        self.assertTrue(self.text.startswith("# PickBench\n"))
        self.assertNotRegex(self.text, r"[ěščřžýůĚŠČŘŽÝŮ]")

    def test_does_not_point_into_docs(self):                           # docs/ do veřejného repozitáře nepůjdou
        self.assertNotIn("docs/", self.text)

    def test_every_file_it_mentions_exists(self):
        names = set(re.findall(r"`((?:datasets/|tests/)?[\w-]+\.(?:py|json|html))`", self.text))
        self.assertGreaterEqual(len(names), 7)
        for name in sorted(names):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_covers_what_the_spec_asks_for(self):
        for part in ("Python 3", "Ollama", "0.35", "NVIDIA", "python server.py", "http://127.0.0.1:8741", "PICKBENCH_PORT", "PICKBENCH_OLLAMA",
                     "PICKBENCH_DB", "bench.py", "hwbench.py", "categories", "emails", "Czech", "MIT", "LICENSE"):
            self.assertIn(part, self.text, part)

    def test_has_no_measured_numbers(self):                            # čísla z měření patří jen tam, kde je doložený běh
        self.assertNotRegex(self.text, r"\d\s*(ms|J\b|W\b|°C|tok/s|decisions/s)")


class OldName(unittest.TestCase):
    def test_old_project_name_is_not_back(self):
        files = [*ROOT.glob("*.py"), ROOT / "index.html", ROOT / "README.md", ROOT / "LICENSE", *ROOT.glob("tests/*.py")]
        old = "r4" + "c3r"                                             # složeno, aby tento test sám sebe nenašel
        for path in files:
            self.assertNotIn(old, path.read_text(encoding="utf-8").lower(), path.name)


class PublicDatasets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_readme_names_the_public_sets_their_licences_and_authors(self):
        for part in ("fetch_datasets.py", "--public", "--deadline-ms", "macro-F1", "goodput", "BANKING77", "CC-BY-4.0", "Casanueva",
                     "CLINC150", "CC-BY-3.0", "Larson", "Horizon-Labs", "ODC-BY", "FineWeb", "CFPB", "CC0", "MLPerf-style", "server",
                     "MASSIVE", "CC BY 4.0", "FitzGerald", "SLURP", "BEE-spoke-data/consumer-finance-complaints", "February 2024"):
            self.assertIn(part, self.readme, part)

    def test_readme_does_not_claim_an_official_mlperf_result(self):
        self.assertNotRegex(self.readme, r"MLPerf (result|score|submission)s?\b(?! under)")
        self.assertIn("not an MLPerf submission", self.readme)

    def test_public_sets_folder_is_ignored_by_git(self):
        self.assertIn("datasets/public/", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

    def test_no_unfilled_measurement_slot_is_left(self):
        for path in [ROOT / "README.md", *(p for p in [ROOT / "CLAUDE.md", *sorted((ROOT / "docs").glob("*.md"))] if p.is_file())]:
            self.assertNotIn("⟦", path.read_text(encoding="utf-8"), path.name)      # znak, kterým plán značí místo pro naměřené číslo


class Secrets(unittest.TestCase):
    def test_env_files_are_ignored_by_git(self):                       # klíče patří jen do proměnných prostředí; soubor .env se nesmí dát commitnout
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".env", lines)
        self.assertIn(".env.*", lines)


class HostedModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.doc = private("docs", "hosted-models.md")
        cls.claude = private("CLAUDE.md")
        cls.page = (ROOT / "index.html").read_text(encoding="utf-8")

    def need(self, text, name):
        if text is None:
            self.skipTest(f"{name} není v tomhle klonu (git ho nesleduje)")

    def test_readme_says_how_to_switch_them_on_and_what_they_cost(self):
        for part in ("providers.py", "Jev", "TypeSafe", "Clef", "Cloudflare Workers AI", "jev:jev-1.13.0", "cf:clef-flash", "cf:clef",
                     "TYPESAFE_API_KEY", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "PICKBENCH_MAX_USD", "PICKBENCH_MAX_HOSTED",
                     "--max-usd", "--max-hosted", "--hosted-soak", "leave this computer", "never"):
            self.assertIn(part, self.readme, part)

    def test_doc_prices_and_limits_match_the_code(self):                 # ceník žije v providers.py; dokument ho jen opisuje
        self.need(self.doc, "docs/hosted-models.md")
        self.assertIn(providers.PRICES_AS_OF, self.doc)
        for model in ("jev:jev-1.13.0", "cf:clef-flash", "cf:clef"):
            price = str(providers.MODELS[model]["usd_per_m"]).replace(".", ",")
            self.assertRegex(self.doc, rf"`{re.escape(model)}`[^\n]*\| {re.escape(price)} \$", model)
        self.assertIn(f"{providers.DEFAULT_MAX_USD:g} $", self.doc)
        self.assertIn(f"{providers.DEFAULT_MAX_HOSTED} dotazů".replace("5000", "5 000"), self.doc)
        for pid in ("jev", "cf"):
            self.assertIn(providers.PROVIDERS[pid]["prices"], self.doc)

    def test_doc_names_what_the_user_must_know(self):
        self.need(self.doc, "docs/hosted-models.md")
        for part in ("TYPESAFE_API_KEY", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "PICKBENCH_MAX_USD", "PICKBENCH_MAX_HOSTED",
                     "PICKBENCH_TEST_PROVIDER", "včetně sítě", "Retry-After", "--hosted-soak",
                     "zůstatek u poskytovatele", "## Naměřeno", "## Known Limitations", "## Key Files"):
            self.assertIn(part, self.doc, part)

    def test_any_set_may_go_to_a_hosted_model(self):                     # rozhodnutí uživatele 2026-10-07: pravidlo o sadách zrušeno, pojistkou je jen varování
        for part in ("any dataset", "your own"):
            self.assertIn(part, self.readme, part)
        self.assertNotIn("are refused", self.readme)
        self.need(self.doc, "docs/hosted-models.md")
        for part in ("kterékoli zvolené sady", "i vlastní"):
            self.assertIn(part, self.doc, part)
        for gone in ("HOSTED_DATASETS", "`scenario`", "ven nesmí"):
            self.assertNotIn(gone, self.doc, gone)

    def test_doc_claims_no_measurement_before_one_exists(self):
        self.need(self.doc, "docs/hosted-models.md")
        section = self.doc.split("## Naměřeno")[1].split("\n## ")[0]
        if "neměřeno" in section:                                        # po měření (úkol B5) tu jsou čísla s datem a tento test hlídá jen tvar
            self.assertNotRegex(section, r"\d\s*ms\b")
        else:
            self.assertRegex(section, r"20\d\d-\d\d-\d\d")

    def test_claude_md_points_to_the_doc_and_names_the_module(self):
        self.need(self.claude, "CLAUDE.md")
        for part in ("docs/hosted-models.md", "providers.py", "TYPESAFE_API_KEY", "PICKBENCH_MAX_USD"):
            self.assertIn(part, self.claude, part)

    def test_page_warns_that_everything_in_the_set_leaves_and_costs_money(self):      # pravidlo o sadách je zrušené: varování při prvním použití poskytovatele je jediná pojistka
        sys.path.insert(0, str(Path(__file__).parent))
        import test_i18n                                                 # věty varování nese blok textů stránky pod klíčem, v obou jazycích; čte je node
        body = self.page.split("function renderHosted()")[1].split("\n}\n")[0]
        self.assertIn("t(modelFallback ? 'hosted.warningFallback' : 'hosted.warning', ", body)      # tělo funkce věty nenese, bere je z bloku textů
        want = {"cs": ("odejde všechno, co je ve zvolené sadě nebo scénáři", "vaše vlastní", "každý dotaz stojí peníze"),
                "en": ("everything in the selected dataset or scenario", "your own", "every request costs money")}
        for key in ("hosted.warning", "hosted.warningFallback"):         # i varování u hostovaného modelu, který stránka předvolila sama
            warning = test_i18n.node(f"TEXTS['{key}']")
            self.assertEqual(sorted(warning), ["cs", "en"], key)
            for code, parts in want.items():
                for part in parts:
                    self.assertIn(part, warning[code], (key, code, part))

    def test_page_adds_no_scenario_field_to_the_request(self):          # k poskytovateli jdou jen model, state a questions; sada se k dotazu nepřipisuje
        self.assertEqual(re.findall(r"body(?:\.|\[\s*['\"])scenario", self.page), [])         # findall, ne assertNotRegex: ten při selhání vypíše celou stránku

    def test_git_tracks_no_file_that_looks_like_a_key_file(self):     # soubor .env na disku (ignorovaný, hra ho nečte) nevadí: ptáme se gitu, ne disku
        try:
            done = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.skipTest(f"git není k dispozici ({type(e).__name__})")
        if done.returncode != 0:
            self.skipTest("složka není repozitář git")
        tracked = done.stdout.decode("utf-8", errors="replace").split("\0")
        self.assertGreater(len(tracked), 10)                              # seznam je opravdu seznam sledovaných souborů, ne prázdný výstup
        self.assertEqual([path for path in tracked if path.rsplit("/", 1)[-1].startswith(".env")], [])
        private_files = [path for path in tracked if path.startswith(("docs/", ".superpowers/", ".claude/", ".worktrees/", "mereni/")) or path == "CLAUDE.md"]
        self.assertEqual(private_files, [])                               # pracovní dokumentace (deníky, plány, reporty) do repozitáře nepatří
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for line in ("docs/", "CLAUDE.md", ".superpowers/", ".claude/", "mereni/"):
            self.assertIn(line, ignored, line)


class Languages(unittest.TestCase):
    """Čeština a angličtina v dokumentaci: co stránka, server a nástroje umějí v obou jazycích a kde je to popsané."""

    DOCS = {                                                             # dokument → co z angličtiny se ho týká
        "demonstrator.md": ("CS / EN", "?lang=en", "PICKBENCH_LANG", "about_en", "data.vals", "pickbench-results.csv"),
        "hardware-bench.md": ("--lang", "PICKBENCH_LANG", "tests/golden/en/"),
        "public-benchmarks.md": ("--lang", "PICKBENCH_LANG", "about_en", "tests/golden/en/"),
        "scheduler.md": ("PICKBENCH_LANG", "X-PickBench-Lang", "summary", "data.vals"),
        "email-datasets.md": ("about_en",),
    }

    def missing(self, parts, *path):
        """Které z částí v souboru chybí. Vrací seznam, ne assertIn: ten by při selhání vypsal celý dokument."""
        file = ROOT.joinpath(*path)
        if not file.is_file() and path[0] in ("docs", "CLAUDE.md"):      # pracovní dokumentace je jen na disku autora; ve veřejném klonu není
            self.skipTest("/".join(path) + " není v tomhle klonu (git ho nesleduje)")
        self.assertTrue(file.is_file(), "/".join(path))
        text = file.read_text(encoding="utf-8")
        return [part for part in parts if part not in text]

    def test_readme_says_what_works_in_both_languages(self):
        want = ("--lang", "PICKBENCH_LANG", "CS / EN", "Czech and English", "about_en", "?lang=en", "pickbench-results.csv")
        self.assertEqual(self.missing(want, "README.md"), [])
        self.assertEqual(self.missing(("The user interface is in Czech",), "README.md"), ["The user interface is in Czech"])      # věta z doby před angličtinou

    def test_readme_limits_the_percent_space_to_the_page(self):          # mezeru před % píše česky jen stránka; nástroje ne, kromě jednoho sloupce hwbench.py server
        want = ("On the page", "without the space in both languages", "`hwbench.py server`", "`100 %`", "`100%`")
        self.assertEqual(self.missing(want, "README.md"), [])
        gone = ("Two formats differ: a percentage",)                     # znění, které platilo jen pro stránku a tvářilo se obecně
        self.assertEqual(self.missing(gone, "README.md"), list(gone))

    def test_readme_names_things_as_the_application_does(self):          # slova, kterými stejnou věc jmenuje anglická stránka a nástroje
        self.assertEqual(self.missing(("mean of F1 over the classes", "unloads the previous step's model", "more than one error per level"), "README.md"), [])
        gone = ("per-label F1", "frees the previous step's model", "mistake")
        self.assertEqual(self.missing(gone, "README.md"), list(gone))

    def test_i18n_doc_says_how_texts_work_and_what_does_not_translate(self):
        want = ("TEXTS", "i18n.py", "X-PickBench-Lang", "tests/test_i18n.py", "page_snapshot.py", "## Known Limitations", "## Key Files",
                "LANGS", "chooseLang", "pctOf", "fmtTime", "USAGE_EN", "about_en", "PICKBENCH_LANG", "--lang", "argparse")
        self.assertEqual(self.missing(want, "docs", "i18n.md"), [])

    def test_i18n_doc_tour_can_be_repeated_and_names_what_the_english_check_prints(self):
        want = ("PowerShell", "Bash", "samostatném okně", "stejné jako česky", "`SAME`", "otisk `asks`")      # shell u každého kroku, prostředí okna, výpis --english, důsledek cizí sady
        self.assertEqual(self.missing(want, "docs", "i18n.md"), [])

    def test_i18n_doc_glossary_and_key_patterns_follow_the_texts(self):  # termíny a vzory klíčů, které slovník stránky a i18n.TEXTS opravdu mají
        import i18n
        self.assertEqual(self.missing(("$ / 1000 decisions", "| failed |", "tile.<jméno>.label", 'if __name__ == "__main__"'), "docs", "i18n.md"), [])
        self.assertIn("$ / 1000 decisions", i18n.TEXTS["prov.spent"]["en"])
        self.assertIn("'tile.conf.label': {", self.page_texts())
        self.assertIn("counts as failed", self.page_texts())

    def page_texts(self):
        return (ROOT / "index.html").read_text(encoding="utf-8").split("// ==== texty: začátek ====")[1].split("// ==== texty: konec ====")[0]

    def test_every_file_the_i18n_doc_mentions_exists(self):              # dokument jmenuje soubory, které má čtenář otevřít: přejmenovaný soubor v něm nesmí zůstat
        text = private("docs", "i18n.md")
        if text is None:
            self.skipTest("docs/i18n.md není v tomhle klonu (git ho nesleduje)")
        names = set(re.findall(r"`((?:tests/|datasets/|docs/)?[\w./-]+\.(?:py|js|json|html|md))`", text))
        self.assertGreaterEqual(len(names), 12)
        self.assertEqual([name for name in sorted(names) if not (ROOT / name).is_file()], [])

    def test_claude_md_names_the_module_and_points_to_the_doc(self):
        self.assertEqual(self.missing(("i18n.py", "docs/i18n.md", "--lang", "PICKBENCH_LANG", "CS / EN"), "CLAUDE.md"), [])

    def test_each_doc_covers_its_part_and_points_to_the_i18n_doc(self):
        for name, parts in self.DOCS.items():
            with self.subTest(doc=name):
                self.assertEqual(self.missing((*parts, "docs/i18n.md"), "docs", name), [])


if __name__ == "__main__":
    unittest.main()
