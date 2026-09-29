"""Importing your own leads: CSV parsing, the safety checks, `sdr import` and the dashboard."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

import core.db
from core.cli_import import cmd_import, read_csv_file, summary_lines
from core.config import EXAMPLE_PROFILE_PATH, load_profile
from core.lead_import import (IMPORTED_FIT_SCORE, MAX_IMPORT_ROWS, TEMPLATE_CSV, LeadImportError, import_csv_text,
                              import_leads, map_columns, parse_leads_csv)
from core.tui import UI

PROFILE = load_profile(EXAMPLE_PROFILE_PATH)


def always_ok(email):
    return True, "ok"


class TestParsing(unittest.TestCase):
    def test_the_template_parses(self):
        rows = parse_leads_csv(TEMPLATE_CSV)
        self.assertEqual(rows[0]["email"], "jamie@brightline.example")
        self.assertEqual(rows[0]["location"], "Austin, TX")
        self.assertEqual(rows[0]["first_name"], "Jamie")

    def test_crm_style_headers_and_semicolons(self):
        text = "﻿E-mail Address;Full Name;Company Name;Job Title;City\nSAM@Acme.test;Sam Lee;Acme;Owner;Leeds\n"
        row = parse_leads_csv(text)[0]
        self.assertEqual((row["email"], row["first_name"], row["company"], row["title"], row["location"]),
                         ("sam@acme.test", "Sam", "Acme", "Owner", "Leeds"))

    def test_first_matching_column_wins(self):
        self.assertEqual(map_columns(["Email", "Work Email"])["email"], "Email")

    def test_unusable_files_say_why(self):
        for text, fragment in (("", "empty"), ("name,company\nSam,Acme\n", 'No "email" column'),
                               ("email\n", "no leads"), ("email\n" + "a@b.test\n" * (MAX_IMPORT_ROWS + 1), "more than"),
                               ("email\n" + "x" * (1024 * 1024 + 1), "larger than 1 MB")):
            with self.assertRaises(LeadImportError) as caught:
                parse_leads_csv(text)
            self.assertIn(fragment, str(caught.exception))

    def test_blank_lines_are_ignored(self):
        self.assertEqual(len(parse_leads_csv("email,company\n\n,\nsam@acme.test,Acme\n")), 1)


class TestImport(unittest.TestCase):
    def run_import(self, text, **options):
        saved = []
        options.setdefault("verify", always_ok)
        options.setdefault("blocked", set())
        options.setdefault("known", set())
        summary = import_csv_text(text, PROFILE, save=lambda leads, ideal: saved.extend(leads) or leads, **options)
        return summary, saved

    def test_good_rows_become_new_leads(self):
        summary, saved = self.run_import("email,first_name,company\nsam@acme.test,Sam,Acme\nhello@brightline-events.test,,\n")
        self.assertEqual((summary.imported, summary.invalid), (2, 0))
        self.assertEqual(saved[0]["status"], "new")
        self.assertEqual(saved[0]["source"], "Imported")
        self.assertEqual(saved[0]["fit_score"], IMPORTED_FIT_SCORE)
        self.assertEqual(saved[1]["company"], "Brightline Events")
        self.assertIsNone(saved[1]["first_name"])

    def test_known_leads_are_never_brought_back(self):
        """Someone who unsubscribed or bounced is already in the database; importing skips them."""
        summary, saved = self.run_import("email\ngone@acme.test\nnew@acme.test\nNEW@acme.test\n", known={"gone@acme.test"})
        self.assertEqual((summary.imported, summary.duplicates), (1, 2))
        self.assertEqual([lead["email"] for lead in saved], ["new@acme.test"])

    def test_do_not_contact_list_is_respected(self):
        summary, saved = self.run_import("email\nsam@blocked.test\n", blocked={"blocked.test"})
        self.assertEqual((summary.imported, summary.blocked), (0, 1))
        self.assertEqual(saved, [])
        self.assertIn("do-not-contact", summary.problems[0])

    def test_unusable_addresses_are_reported_with_their_row(self):
        text = "email\nnot-an-email\nnoreply@acme.test\n\"\"\ndead@nomail.test\n"
        summary, saved = self.run_import(
            text, verify=lambda e: (not e.endswith("nomail.test"), "No MX records"))
        self.assertEqual((summary.imported, summary.invalid), (0, 3))
        self.assertIn("Row 2: not-an-email: not a valid email address", summary.problems)
        self.assertTrue(any("no-reply" in p for p in summary.problems))
        self.assertTrue(any("can't receive email" in p for p in summary.problems))

    def test_one_mail_server_lookup_per_domain(self):
        calls = []
        self.run_import("email\na@acme.test\nb@acme.test\nc@other.test\n",
                        verify=lambda e: calls.append(e) or (True, "ok"))
        self.assertEqual(len(calls), 2)

    def test_no_verify_never_touches_the_network(self):
        def explode(email):
            raise AssertionError("looked up a mail server")
        summary, _ = self.run_import("email\nsam@acme.test\n", verify=explode, check_mail_server=False)
        self.assertEqual(summary.imported, 1)

    def test_dry_run_saves_nothing(self):
        summary, saved = self.run_import("email\nsam@acme.test\n", dry_run=True)
        self.assertEqual((summary.imported, saved), (1, []))
        self.assertIn("would be imported", summary_lines(summary)[0])


class TestDatabaseRoundTrip(unittest.TestCase):
    def test_imported_leads_are_saved_once_and_sent_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(core.db, "DB_DIR", tmp), \
                mock.patch.object(core.db, "DB_PATH", os.path.join(tmp, "t.db")):
            core.db.init_db()
            rows = parse_leads_csv("email,company,location\nsam@acme.test,Acme,\"Austin, TX\"\n")
            first = import_leads(rows, PROFILE, verify=always_ok, blocked=set())
            again = import_leads(rows, PROFILE, verify=always_ok, blocked=set())
            conn = core.db.get_connection()
            leads = [dict(r) for r in conn.execute("SELECT * FROM leads").fetchall()]
            sent = conn.execute("SELECT COUNT(*) FROM email_logs").fetchone()[0]
            conn.close()
        self.assertEqual((first.imported, again.imported, again.duplicates), (1, 0, 1))
        self.assertEqual(len(leads), 1)
        self.assertEqual((leads[0]["status"], leads[0]["location"], leads[0]["fit_score"]),
                         ("new", "Austin, TX", IMPORTED_FIT_SCORE))
        self.assertEqual(sent, 0)


class TestCommand(unittest.TestCase):
    def run_cmd(self, *args, **kwargs):
        out = io.StringIO()
        ui = UI(plain=True, interactive=False, out=out, environ={})
        with contextlib.redirect_stdout(out):
            code = cmd_import(ui, *args, **kwargs)
        return code, out.getvalue()

    def test_template_prints_an_example(self):
        code, out = self.run_cmd(None, False, False, True, {})
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("email,first_name,company"))

    def test_missing_file_is_a_friendly_error(self):
        code, out = self.run_cmd(os.path.join(tempfile.gettempdir(), "nope-leads.csv"), False, False, False, PROFILE)
        self.assertEqual(code, 1)
        self.assertIn("No file at", out)

    def test_no_file_argument(self):
        code, out = self.run_cmd(None, False, False, False, PROFILE)
        self.assertEqual(code, 2)
        self.assertIn("sdr import leads.csv", out)

    def test_excel_ansi_files_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "leads.csv")
            with open(path, "wb") as f:
                f.write("email,company\nzoe@cafe.test,Caf\xe9 Zo\xeb\n".encode("cp1252"))
            self.assertIn("Café Zoë", read_csv_file(path))


if __name__ == "__main__":
    unittest.main()
