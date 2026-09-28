"""Offline tests for the files that hold lead data: they are created readable by this user only
(on a shared computer the database, CSV export and cron log used to be world-readable while .env
was 0600), and the CSV export can't smuggle spreadsheet formulas. Temp dirs only: no network,
no email, the real data/ folder is never touched."""

import csv
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import core.db as db
from bots import lead_store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSIX = os.name != "nt"
DEFAULT_UMASK = 0o022  # the common default, under which the files came out 0644


def mode_of(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class UmaskCase(unittest.TestCase):
    """Runs under the default umask so the tests prove the code, not a strict shell setting."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        if POSIX:
            self.addCleanup(os.umask, os.umask(DEFAULT_UMASK))

    def use_database(self, path: str) -> None:
        for name, value in (("DB_PATH", path), ("DB_DIR", os.path.dirname(path))):
            patcher = mock.patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


# ── The database ─────────────────────────────────────────────────────────────


@unittest.skipUnless(POSIX, "file modes (Windows uses the user folder's ACLs)")
class TestDatabaseFiles(UmaskCase):
    def test_new_data_folder_and_database_are_owner_only(self):
        data_dir = os.path.join(self.tmp.name, "data")
        self.use_database(os.path.join(data_dir, "automations.db"))
        db.init_db()
        self.assertEqual(mode_of(data_dir), 0o700)
        self.assertEqual(mode_of(os.path.join(data_dir, "automations.db")), 0o600)

    def test_an_existing_world_readable_database_is_tightened(self):
        path = os.path.join(self.tmp.name, "old.db")
        self.use_database(path)
        db.init_db()
        os.chmod(path, 0o644)
        db.get_connection().close()
        self.assertEqual(mode_of(path), 0o600)

    def test_an_existing_folder_keeps_its_permissions(self):
        """AUTOMATIONS_DB_PATH may point into a folder the user shares with other programs;
        only folders the tool creates itself are locked down."""
        os.chmod(self.tmp.name, 0o755)
        self.use_database(os.path.join(self.tmp.name, "x.db"))
        db.init_db()
        self.assertEqual(mode_of(self.tmp.name), 0o755)

    def test_make_private_never_raises(self):
        db.make_private(os.path.join(self.tmp.name, "missing"))
        db.ensure_private_dir(self.tmp.name)  # already there: nothing to do


# ── The CSV export ───────────────────────────────────────────────────────────


FORMULAS = ('=HYPERLINK("https://evil.test/login","Verify leads")', "=cmd|' /C calc'!A0",
            "@SUM(1+1)", "+1 555 0100", "-Acme", "\tAcme", "\rAcme")
HARMLESS = ("Acme Studio", "O'Brien & Sons", "Café Crème", "a=b", "x - y", " =not a formula")


class TestCsvExport(UmaskCase):
    def setUp(self):
        super().setUp()
        self.use_database(os.path.join(self.tmp.name, "t.db"))
        db.init_db()
        self.csv_path = os.path.join(self.tmp.name, "export", "leads.csv")

    def add_lead(self, email: str, **fields) -> None:
        row = {"name": "Ann", "company": "Acme", "email": email, "status": "new",
               "created_at": datetime.now(timezone.utc).isoformat(), **fields}
        conn = db.get_connection()
        conn.execute(f"INSERT INTO leads ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     tuple(row.values()))
        conn.commit()
        conn.close()

    def export(self) -> tuple[list[str], list[list[str]]]:
        path = lead_store.export_leads_to_csv(self.csv_path)
        self.assertEqual(path, self.csv_path)
        with open(path, newline="", encoding="utf-8") as f:
            header, *rows = list(csv.reader(f))
        return header, rows

    def test_header_names_every_column_the_rows_have(self):
        self.add_lead("ann@acme.test", phone="+1 555 0100", fit_score=88, website="https://acme.test")
        header, rows = self.export()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(header), len(rows[0]))
        self.assertEqual(header[:5], ["id", "name", "title", "company", "email"])
        for column in db.LEAD_COLUMNS:
            self.assertIn(column, header)
        row = dict(zip(header, rows[0]))
        self.assertEqual(row["email"], "ann@acme.test")
        self.assertEqual(row["fit_score"], "88")
        self.assertEqual(row["website"], "https://acme.test")

    def test_cells_that_spreadsheets_would_run_as_formulas_are_neutralised(self):
        for i, value in enumerate(FORMULAS):
            self.add_lead(f"lead{i}@acme.test", company=value, name=value)
        header, rows = self.export()
        exported = {dict(zip(header, row))["email"]: dict(zip(header, row)) for row in rows}
        for i, value in enumerate(FORMULAS):
            with self.subTest(value=value):
                row = exported[f"lead{i}@acme.test"]
                self.assertEqual(row["company"], "'" + value)
                self.assertEqual(row["name"], "'" + value)

    def test_ordinary_text_and_numbers_are_exported_unchanged(self):
        for i, value in enumerate(HARMLESS):
            self.add_lead(f"lead{i}@acme.test", company=value, fit_score=42)
        header, rows = self.export()
        exported = {dict(zip(header, row))["email"]: dict(zip(header, row)) for row in rows}
        for i, value in enumerate(HARMLESS):
            with self.subTest(value=value):
                self.assertEqual(exported[f"lead{i}@acme.test"]["company"], value)
                self.assertEqual(exported[f"lead{i}@acme.test"]["fit_score"], "42")

    def test_csv_safe_only_touches_strings_with_a_formula_prefix(self):
        for value in FORMULAS:
            self.assertEqual(lead_store.csv_safe(value), "'" + value)
        for value in HARMLESS + ("", None, 7, 3.5):
            self.assertEqual(lead_store.csv_safe(value), value)

    @unittest.skipUnless(POSIX, "file modes")
    def test_export_is_owner_only_even_over_an_old_world_readable_copy(self):
        self.add_lead("ann@acme.test")
        self.export()
        self.assertEqual(mode_of(self.csv_path), 0o600)
        self.assertEqual(mode_of(os.path.dirname(self.csv_path)), 0o700)  # folder it created
        os.chmod(self.csv_path, 0o644)
        self.export()
        self.assertEqual(mode_of(self.csv_path), 0o600)

    def test_default_export_lands_in_the_install_data_folder(self):
        self.assertEqual(lead_store.EXPORT_PATH, os.path.join(REPO_ROOT, "data", "leads_export.csv"))


# ── The cron script ──────────────────────────────────────────────────────────


FAKE_RUNNER = (
    "import os\n"
    "here = os.path.dirname(os.path.abspath(__file__))\n"
    "with open(os.path.join(here, 'data', 'written-by-python.txt'), 'w') as f:\n"
    "    f.write('lead data')\n"
    "print('fake ran')\n"
)


@unittest.skipUnless(POSIX and shutil.which("bash"), "bash script")
class TestCronScriptFilePermissions(UmaskCase):
    """run_cron_pipeline.sh appends sent-email lines and runner JSON to data/cron.log, and the
    Python it starts creates the database and CSV: all of it must be readable by this user only."""

    def setUp(self):
        super().setUp()
        shutil.copy(os.path.join(REPO_ROOT, "run_cron_pipeline.sh"), self.tmp.name)
        with open(os.path.join(self.tmp.name, "runner.py"), "w", encoding="utf-8") as f:
            f.write(FAKE_RUNNER)

    def test_log_data_folder_and_files_written_by_the_python_child_are_owner_only(self):
        result = subprocess.run(["bash", os.path.join(self.tmp.name, "run_cron_pipeline.sh")],
                                env={**os.environ, "PYTHON": sys.executable},
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        data_dir = os.path.join(self.tmp.name, "data")
        self.assertEqual(mode_of(data_dir), 0o700)
        self.assertEqual(mode_of(os.path.join(data_dir, "cron.log")), 0o600)
        self.assertEqual(mode_of(os.path.join(data_dir, "written-by-python.txt")), 0o600)
        with open(os.path.join(data_dir, "cron.log"), encoding="utf-8") as f:
            self.assertIn("fake ran", f.read())


if __name__ == "__main__":
    unittest.main()
