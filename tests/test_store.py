import os
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
import i18n
import store


def setUpModule():
    """Testy českých vět volají úložiště přímo a to čte jazyk z prostředí: PICKBENCH_LANG vývojáře se na dobu modulu odstraní a po něm vrátí."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop(i18n.ENV, None)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "test.db"
        self.ticks = iter(f"2026-10-06T21:00:{i:02d}" for i in range(60))
        self.store = store.Store(self.path, now=lambda: next(self.ticks))


class Results(Base):
    def test_add_returns_the_record_with_id_and_time(self):
        rec = self.store.add_result({"cells": {"driver": "tev1"}})
        self.assertEqual(rec, {"id": 1, "ts": "2026-10-06T21:00:00", "data": {"cells": {"driver": "tev1"}}})

    def test_results_are_newest_first_and_keep_nested_czech_data(self):
        first = {"cells": {"scenario": "překážky", "n": 12}, "settings": {"seed": "noc-1", "extra": False}, "batch": None}
        self.store.add_result(first)
        self.store.add_result({"cells": {"scenario": "číselné duely"}})
        got = self.store.results()
        self.assertEqual([r["id"] for r in got], [2, 1])
        self.assertEqual(got[1]["data"], first)
        self.assertEqual(got[0]["ts"], "2026-10-06T21:00:01")

    def test_limit_keeps_the_newest(self):
        for i in range(5):
            self.store.add_result({"i": i})
        self.assertEqual([r["data"]["i"] for r in self.store.results(limit=2)], [4, 3])

    def test_default_limit_is_a_thousand(self):
        self.assertEqual(store.MAX_RESULTS, 1000)

    def test_clear_returns_count_and_ids_are_not_reused(self):
        self.store.add_result({"a": 1})
        self.store.add_result({"a": 2})
        self.assertEqual(self.store.clear_results(), 2)
        self.assertEqual(self.store.results(), [])
        self.assertEqual(self.store.clear_results(), 0)
        self.assertEqual(self.store.add_result({"a": 3})["id"], 3)

    def test_delete_result_removes_just_that_one_and_keeps_the_order_of_the_rest(self):
        for i in range(3):
            self.store.add_result({"i": i})
        self.assertTrue(self.store.delete_result(2))
        got = self.store.results()
        self.assertEqual([(r["id"], r["data"]["i"]) for r in got], [(3, 2), (1, 0)])
        self.assertEqual(got[1]["ts"], "2026-10-06T21:00:00")

    def test_delete_missing_result_says_so_and_changes_nothing(self):
        self.store.add_result({"a": 1})
        for rid in (2, 0, -1, 10 ** 30):                     # poslední je nad rozsahem celých čísel SQLite, takové id v databázi být nemůže
            with self.subTest(rid=rid):
                self.assertFalse(self.store.delete_result(rid))
        self.assertTrue(self.store.delete_result(1))
        self.assertFalse(self.store.delete_result(1))        # podruhé už není
        self.assertEqual(self.store.results(), [])

    def test_delete_one_does_not_change_clear_and_ids_are_not_reused(self):
        for i in range(3):
            self.store.add_result({"i": i})
        self.assertTrue(self.store.delete_result(3))
        self.assertEqual(self.store.clear_results(), 2)       # počítá jen to, co zbylo
        self.assertEqual(self.store.add_result({"i": 9})["id"], 4)

    def test_result_must_be_an_object(self):
        for bad in ([1, 2], "text", 5, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.add_result(bad)
        self.assertEqual(self.store.results(), [])

    def test_real_clock_gives_local_iso_seconds(self):
        rec = store.Store(self.path).add_result({})
        self.assertRegex(rec["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")

    def test_text_that_is_not_valid_unicode_survives(self):           # osamocená půlka znaku z prohlížeče nesmí shodit zápis
        data = {"name": "sada \ud83d a 🚗 a „uvozovky“"}
        self.store.add_result(data)
        self.assertEqual(self.store.results()[0]["data"], data)

    def test_unreadable_row_is_skipped(self):
        self.store.add_result({"ok": 1})
        con = sqlite3.connect(self.path)
        con.execute("INSERT INTO results (ts, data) VALUES ('x', '{nope')")
        con.execute("INSERT INTO results (ts, data) VALUES ('x', '[1, 2]')")
        con.commit()
        con.close()
        self.assertEqual([r["data"] for r in self.store.results()], [{"ok": 1}])


class KeyValue(Base):
    def test_missing_key_is_none(self):
        self.assertIsNone(self.store.get("settings"))

    def test_put_get_and_overwrite(self):
        self.store.put("settings", {"scenario": "numbers", "lanes": 4})
        self.store.put("queue", {"batch": 2, "steps": []})
        self.store.put("settings", {"scenario": "obstacles"})
        self.assertEqual(self.store.get("settings"), {"scenario": "obstacles"})
        self.assertEqual(self.store.get("queue"), {"batch": 2, "steps": []})

    def test_unreadable_value_is_none(self):
        self.store.put("settings", {"a": 1})
        con = sqlite3.connect(self.path)
        con.execute("UPDATE kv SET value = '{nope' WHERE key = 'settings'")
        con.commit()
        con.close()
        self.assertIsNone(self.store.get("settings"))

    def test_data_survive_a_new_store_on_the_same_file(self):
        self.store.put("queue", {"steps": [1]})
        self.store.add_result({"a": 1})
        again = store.Store(self.path)
        self.assertEqual(again.get("queue"), {"steps": [1]})
        self.assertEqual(len(again.results()), 1)


class Trouble(Base):
    def test_missing_file_is_created(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(self.store.results(), [])
        self.assertTrue(self.path.exists())

    def test_missing_folder_is_an_error_with_the_path(self):
        s = store.Store(Path(self.tmp.name) / "neni" / "test.db")
        with self.assertRaises(store.StoreError) as ctx:
            s.results()
        self.assertIn("test.db", str(ctx.exception))
        self.assertIn("neukládají", str(ctx.exception))

    def test_corrupt_file_is_an_error_and_is_left_alone(self):
        junk = b"toto neni databaze " * 200
        self.path.write_bytes(junk)
        for call in (self.store.results, lambda: self.store.add_result({"a": 1}), self.store.clear_results,
                     lambda: self.store.delete_result(1),
                     lambda: self.store.get("settings"), lambda: self.store.put("settings", {})):
            with self.assertRaises(store.StoreError):
                call()
        self.assertEqual(self.path.read_bytes(), junk)

    def test_read_only_file_can_be_read_but_not_written(self):
        self.store.add_result({"a": 1})
        self.store.put("settings", {"lanes": 5})
        os.chmod(self.path, stat.S_IREAD)
        self.addCleanup(os.chmod, self.path, stat.S_IREAD | stat.S_IWRITE)
        if os.access(self.path, os.W_OK):
            self.skipTest("soubor nejde nastavit jen pro čtení (správce)")
        self.assertEqual(len(self.store.results()), 1)
        self.assertEqual(self.store.get("settings"), {"lanes": 5})
        with self.assertRaises(store.StoreError):
            self.store.add_result({"a": 2})
        with self.assertRaises(store.StoreError):
            self.store.put("settings", {})

    def test_file_locked_by_another_program_is_an_error_not_a_hang(self):
        self.store.add_result({"a": 1})
        other = sqlite3.connect(self.path)
        other.execute("BEGIN EXCLUSIVE")
        self.addCleanup(other.close)
        quick = store.Store(self.path, timeout=0.2)
        with self.assertRaises(store.StoreError):
            quick.add_result({"a": 2})
        other.rollback()
        self.assertEqual(quick.add_result({"a": 3})["id"], 2)


class Concurrency(Base):
    def test_parallel_writes_all_land(self):
        s = store.Store(self.path)
        errors = []

        def work(t):
            try:
                for i in range(25):
                    s.add_result({"thread": t, "i": i})
                    s.put("settings", {"thread": t, "i": i})
                    s.results(limit=3)
            except Exception as e:                                     # pragma: no cover — test má selhat níže
                errors.append(e)
        threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        got = s.results()
        self.assertEqual(len(got), 200)
        self.assertEqual(sorted(r["id"] for r in got), list(range(1, 201)))
        self.assertEqual({(r["data"]["thread"], r["data"]["i"]) for r in got}, {(t, i) for t in range(8) for i in range(25)})
        self.assertEqual(s.get("settings")["i"], 24)


class DefaultPath(unittest.TestCase):
    def test_next_to_the_server_by_default(self):
        self.assertEqual(store.default_path(Path("x") / "y", {}), str(Path("x") / "y" / "pickbench.db"))

    def test_variable_wins_and_empty_variable_does_not(self):
        self.assertEqual(store.default_path("x", {"PICKBENCH_DB": "jinde.db"}), "jinde.db")
        self.assertEqual(store.default_path("x", {"PICKBENCH_DB": ""}), str(Path("x") / "pickbench.db"))

    def test_reads_the_process_environment(self):
        old = os.environ.get("PICKBENCH_DB")
        os.environ["PICKBENCH_DB"] = "z-prostredi.db"
        try:
            self.assertEqual(store.default_path("x"), "z-prostredi.db")
        finally:
            if old is None:
                del os.environ["PICKBENCH_DB"]
            else:
                os.environ["PICKBENCH_DB"] = old


class NotFinite(Base):
    def test_finite_json_refuses_what_a_browser_cannot_read(self):
        self.assertEqual(store.finite_json('{"a": [1, -2.5, 1e3], "b": "NaN"}'), {"a": [1, -2.5, 1000.0], "b": "NaN"})
        for raw in ('{"a": NaN}', '[Infinity]', '{"a": {"b": -Infinity}}', '{"a": 1e999}', '{"a": -1e999}'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    store.finite_json(raw)

    def test_add_result_and_put_refuse_nan(self):
        for bad in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.store.add_result({"cells": {"avg": bad}})
                with self.assertRaises(ValueError):
                    self.store.put("settings", {"speed": bad})
        self.assertEqual(self.store.results(), [])
        self.assertIsNone(self.store.get("settings"))

    def test_rows_written_by_an_older_version_are_skipped_not_fatal(self):
        self.store.add_result({"ok": 1})
        con = sqlite3.connect(self.path)
        con.execute("INSERT INTO results (ts, data) VALUES ('t', '{\"avg\": NaN}')")
        con.execute("INSERT INTO results (ts, data) VALUES ('t', '{\"avg\": 1e999}')")
        con.execute("INSERT INTO kv (key, value) VALUES ('queue', '{\"batch\": Infinity}')")
        con.commit()
        con.close()
        self.assertEqual([r["data"] for r in self.store.results()], [{"ok": 1}])
        self.assertIsNone(self.store.get("queue"))


class English(Base):
    """Věty úložiště v obou jazycích: česky doslova jako před překladem, anglicky celé."""

    def sentences(self, call, error):
        """Věta chyby, kterou volání skončí, česky a anglicky."""
        out = []
        for code in i18n.LANGS:
            with i18n.process(code), self.assertRaises(error) as ctx:
                call()
            out.append(str(ctx.exception))
        return out

    def test_database_trouble(self):
        missing = Path(self.tmp.name) / "neni" / "test.db"
        cs, en = self.sentences(store.Store(missing).results, store.StoreError)
        self.assertEqual(cs, f"Databáze {missing} nejde použít (unable to open database file); výsledky, nastavení a fronta se neukládají.")
        self.assertEqual(en, f"The server cannot use the database {missing} (unable to open database file); it does not save the results, the settings or the queue.")
        self.path.write_bytes(b"toto neni databaze " * 200)
        for call in (self.store.results, lambda: self.store.add_result({"a": 1}), self.store.clear_results, lambda: self.store.delete_result(1),
                     lambda: self.store.get("settings"), lambda: self.store.put("settings", {})):
            cs, en = self.sentences(call, store.StoreError)
            self.assertEqual(cs, f"Databáze {self.path} nejde použít (file is not a database); výsledky, nastavení a fronta se neukládají.")
            self.assertEqual(en, f"The server cannot use the database {self.path} (file is not a database); it does not save the results, the settings or the queue.")

    def test_result_that_is_not_an_object_and_number_that_is_not_json(self):
        self.assertEqual(self.sentences(lambda: self.store.add_result([1, 2]), ValueError), ["výsledek musí být objekt", "the result must be an object"])
        for raw, number in (('{"a": NaN}', "NaN"), ("[-Infinity]", "-Infinity"), ('{"a": 1e999}', "1e999")):
            with self.subTest(raw=raw):
                self.assertEqual(self.sentences(lambda: store.finite_json(raw), ValueError),
                                 [f"{number} není platné číslo JSON", f"{number} is not a valid JSON number"])

    def test_language_follows_the_environment_when_nothing_else_sets_it(self):
        self.assertNotIn(i18n.ENV, os.environ)                           # setUpModule: testy českých vět na prostředí vývojáře nezávisí
        with mock.patch.dict(os.environ, {i18n.ENV: "en"}), self.assertRaises(ValueError) as ctx:
            self.store.add_result("text")
        self.assertEqual(str(ctx.exception), "the result must be an object")


if __name__ == "__main__":
    unittest.main()
