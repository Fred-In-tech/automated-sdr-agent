"""Why replies didn't turn into interest (core/reply_reasons.py)."""

import os
import tempfile
import unittest
from unittest import mock

from core import db
from core.reply_reasons import breakdown, reason_for


class TestReasonFor(unittest.TestCase):
    def test_reasons_from_the_words_people_use(self):
        cases = {
            "Thanks but we already use HoneyBook for this.": "has_tool",
            "Not right now, it's our busy season. Maybe in the fall.": "timing",
            "We don't have the budget for new software this year.": "price",
            "I'm not the right person, you'll want to talk to Dana.": "wrong_person",
            "Please remove me from your list.": "opted_out",
            "We're good, thanks.": "no_need",
            "We don't do weddings anymore.": "not_a_fit",
        }
        for text, reason in cases.items():
            with self.subTest(text=text):
                self.assertEqual(reason_for("not_interested", text), reason)

    def test_the_intent_decides_when_the_words_dont(self):
        self.assertEqual(reason_for("not_interested", "No."), "no_reason")
        self.assertEqual(reason_for("not_now", "Ping me in March."), "timing")
        self.assertEqual(reason_for("unsubscribe", "STOP"), "opted_out")
        self.assertEqual(reason_for("other", "Who is this?"), "unclear")

    def test_positive_and_automatic_mail_is_not_a_no(self):
        for intent in ("interested", "question", "auto_reply", "bounce", None, ""):
            self.assertIsNone(reason_for(intent, "we already use something"))


class TestBreakdown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "t.db")),
                        mock.patch.object(db, "DB_DIR", self.tmp.name)]
        for p in self.patches:
            p.start()
        db.init_db()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def reply(self, lead_id, intent, text, n):
        conn = db.get_connection()
        conn.execute("INSERT INTO inbound_messages (message_id, lead_id, from_email, excerpt, intent, received_at)"
                     " VALUES (?,?,?,?,?,?)", (f"<m{n}>", lead_id, f"l{lead_id}@x.test", text, intent, "2026-10-20"))
        conn.commit()
        conn.close()

    def test_counts_one_reason_per_lead_most_common_first(self):
        self.reply(1, "not_interested", "We already use Dubsado.", 1)
        self.reply(2, "not_interested", "already have a system, thanks", 2)
        self.reply(3, "not_now", "Busy season, try next year.", 3)
        self.reply(4, "interested", "Sounds great, send it!", 4)
        self.reply(5, "auto_reply", "I'm out of the office", 5)
        self.reply(3, "auto_reply", "Out of office", 6)          # an autoreply later doesn't hide their answer
        self.reply(6, "not_now", "maybe later", 7)
        self.reply(6, "not_interested", "Actually, we're all set.", 8)   # their latest answer counts
        conn = db.get_connection()
        rows = breakdown(conn)
        conn.close()
        self.assertEqual([(r["reason"], r["count"]) for r in rows], [("has_tool", 2), ("no_need", 1), ("timing", 1)])
        self.assertEqual(rows[0]["label"], "Already use something")
        self.assertTrue(rows[0]["example"])


if __name__ == "__main__":
    unittest.main()
