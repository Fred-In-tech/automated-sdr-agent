"""Offline tests for the SDR inbox. Fake IMAP server + temporary database + fake sender:
no network, no emails."""

import os
import ssl
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from unittest import mock

import core.db as db
from core.config import EXAMPLE_PROFILE_PATH, ProfileError, load_profile
from bots import email_marketing, inbox_listener
from bots.inbox_listener import InboxListenerEngine, latest_reply_text

ME = "me@acme.test"
MAIL_ENV = {"SMTP_USER": ME, "SMTP_PASS": "x", "SMTP_HOST": "smtp.acme.test", "SMTP_PORT": "465",
            "IMAP_HOST": "imap.acme.test", "IMAP_PORT": "993", "SDR_ALERT_EMAIL": "",
            "TELEGRAM_BOT_TOKEN": "", "DISCORD_WEBHOOK_URL": ""}


def make_mail(sender: str, subject: str, body: str, msg_id: str, **headers) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ME
    msg["Subject"] = subject
    msg["Message-ID"] = msg_id
    for key, value in headers.items():
        msg[key.replace("_", "-")] = value
    msg.set_content(body)
    return msg.as_bytes()


class FakeIMAP:
    """Just enough of imaplib.IMAP4_SSL for the listener."""
    def __init__(self, messages: list):
        self.messages = messages
        self.flags_changed = False

    def login(self, user, password):
        return "OK", [b"logged in"]

    def select(self, mailbox, readonly=False):
        self.flags_changed = self.flags_changed or not readonly
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, criteria):
        return "OK", [" ".join(str(i + 1) for i in range(len(self.messages))).encode()]

    def fetch(self, message_set, parts):
        ids = message_set.decode() if isinstance(message_set, bytes) else message_set
        out = []
        for imap_id in ids.split(","):
            raw = self.messages[int(imap_id) - 1]
            payload = raw.split(b"\n\n")[0] + b"\n\n" if "HEADER.FIELDS" in parts else raw
            out += [(f"{imap_id} (BODY[] {{{len(payload)}}}".encode(), payload), b")"]
        return "OK", out

    def logout(self):
        return "BYE", []


class InboxTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "test.db")),
            mock.patch.object(db, "DB_DIR", self.tmp.name),
            mock.patch.object(email_marketing, "load_env_file", lambda: None),
            mock.patch.object(inbox_listener, "load_env_file", lambda: None),
            mock.patch.dict(os.environ, MAIL_ENV),
        ]
        for p in self.patches:
            p.start()
        db.init_db()
        self.profile = load_profile(EXAMPLE_PROFILE_PATH)
        self.profile["sdr"]["alert_email"] = "boss@acme.test"
        self.sent = []
        self.lead_id = self.add_lead("sarah@bloomfilms.test", "contacted")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def add_lead(self, email: str, status: str) -> int:
        conn = db.get_connection()
        cur = conn.execute(
            "INSERT INTO leads (name, company, email, first_name, fit_score, status, sequence_step, next_touch_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("Bloom Films", "Bloom Films", email, "Sarah", 85, status, 1, "2099-01-01T00:00:00+00:00",
             datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()
        return cur.lastrowid

    def lead(self) -> dict:
        conn = db.get_connection()
        row = dict(conn.execute("SELECT * FROM leads WHERE id = ?", (self.lead_id,)).fetchone())
        conn.close()
        return row

    def run_inbox(self, messages: list) -> tuple[dict, FakeIMAP]:
        fake = FakeIMAP(messages)

        def connect(*args, **kwargs):
            fake.connected_with = (args, kwargs)
            return fake

        with mock.patch.object(inbox_listener.imaplib, "IMAP4_SSL", connect), \
             mock.patch.object(inbox_listener.NotificationManager, "notify_all", lambda *a, **k: None):
            engine = InboxListenerEngine(self.profile)
            engine.email_engine.send_real_email = lambda to, subject, html, text, headers=None: self.sent.append(
                {"to": to, "subject": subject, "text": text, "html": html, "headers": headers or {}}) or True
            return engine.check_inbox_and_auto_reply(), fake

    def sent_to(self, address: str) -> list:
        return [m for m in self.sent if m["to"] == address]


class TestInbox(InboxTestCase):
    def test_interested_reply_gets_approved_answer_and_hot_alert(self):
        mail = make_mail("Sarah Lee <sarah@bloomfilms.test>", "Re: question about Bloom Films",
                         "Yes, I'd like to try it!\n\nOn Mon, Jamie wrote:\n> Reply unsubscribe to stop", "<r1@x>")
        result, fake = self.run_inbox([mail])
        self.assertEqual(self.lead()["status"], "interested")
        self.assertIsNone(self.lead()["next_touch_at"])  # sequence stopped
        reply = self.sent_to("sarah@bloomfilms.test")[0]
        self.assertTrue(reply["text"].startswith("Hi Sarah,"))
        self.assertEqual(reply["headers"]["In-Reply-To"], "<r1@x>")
        self.assertIn("HOT LEAD", self.sent_to("boss@acme.test")[0]["subject"])
        self.assertEqual(len(result["hot_leads"]), 1)
        self.assertFalse(fake.flags_changed, "inbox must be opened read-only")

    def test_unsubscribe_is_honoured_silently(self):
        self.run_inbox([make_mail("sarah@bloomfilms.test", "Re: question", "Please remove me", "<u1@x>")])
        self.assertEqual(self.lead()["status"], "unsubscribed")
        self.assertEqual(self.sent, [])

    def test_notifications_and_non_leads_are_ignored(self):
        insta = make_mail("Social <no-reply@mail.social.example>", "acme_studio: 2 unread messages",
                          "You have messages", "<i1@x>", List_Unsubscribe="<https://social.example/u>")
        stranger = make_mail("bob@random.test", "hello", "Are you free?", "<s1@x>")
        self.run_inbox([insta, stranger])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.lead()["status"], "contacted")

    def test_autoresponder_does_not_stop_the_sequence(self):
        auto = make_mail("sarah@bloomfilms.test", "Out of Office: back Monday", "I'm away", "<a1@x>",
                         Auto_Submitted="auto-replied")
        self.run_inbox([auto])
        self.assertEqual(self.lead()["status"], "contacted")
        self.assertEqual(self.sent, [])

    def test_colleague_at_same_studio_counts_as_the_lead(self):
        self.run_inbox([make_mail("Dana Cole <dana@bloomfilms.test>", "Re: question", "How much is it?", "<c1@x>")])
        self.assertEqual(self.lead()["status"], "interested")
        self.assertTrue(self.sent_to("sarah@bloomfilms.test")[0]["text"].startswith("Hi Sarah,"))

    def test_other_reply_alerts_a_human_without_auto_reply(self):
        self.run_inbox([make_mail("sarah@bloomfilms.test", "Re: question", "Who gave you my email.", "<o1@x>")])
        self.assertEqual(self.lead()["status"], "replied")
        self.assertEqual(self.sent_to("sarah@bloomfilms.test"), [])
        self.assertEqual(len(self.sent_to("boss@acme.test")), 1)

    def test_each_message_is_handled_once(self):
        mail = make_mail("sarah@bloomfilms.test", "Re: question", "Sounds good!", "<d1@x>")
        self.run_inbox([mail])
        self.run_inbox([mail])
        self.assertEqual(len(self.sent_to("sarah@bloomfilms.test")), 1)

    def test_bounce_marks_lead(self):
        bounce = make_mail("MAILER-DAEMON@mx.test", "Undelivered Mail Returned to Sender",
                           "Final-Recipient: rfc822; sarah@bloomfilms.test\nStatus: 5.1.1", "<b1@x>")
        self.run_inbox([bounce])
        self.assertEqual(self.lead()["status"], "bounced")

    def test_own_alerts_are_skipped(self):
        self.run_inbox([make_mail(ME, "🔥 HOT LEAD: Bloom Films", "details", "<own@x>")])
        self.assertEqual(self.sent, [])


class TestImapConnection(InboxTestCase):
    def test_imap_verifies_the_server_certificate(self):
        """imaplib's default context is CERT_NONE with no hostname check: anyone on the same
        cafe wifi could present any certificate and receive the mailbox password. The listener
        must verify exactly what the login check verified (same shared context)."""
        _result, fake = self.run_inbox([])
        args, kwargs = fake.connected_with
        self.assertEqual(args, ("imap.acme.test", 993))
        context = kwargs["ssl_context"]
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(kwargs["timeout"], 30)

    def test_imap_uses_the_shared_context_helper(self):
        shared = ssl.create_default_context()
        with mock.patch.object(inbox_listener, "tls_context", return_value=shared):
            _result, fake = self.run_inbox([])
        self.assertIs(fake.connected_with[1]["ssl_context"], shared)

    def test_login_without_an_inbox_server_stops_with_a_fix_it_message(self):
        """No silent default host: the old fallback sent the password to the author's provider."""
        with mock.patch.dict(os.environ, {"IMAP_HOST": ""}), self.assertRaises(ProfileError) as ctx:
            InboxListenerEngine(self.profile)
        self.assertIn("IMAP_HOST", str(ctx.exception))
        self.assertIn("setup --section email", str(ctx.exception))
        self.assertNotIn("hostinger", str(ctx.exception).lower())


class TestQuotedText(unittest.TestCase):
    def test_quoted_history_is_ignored(self):
        body = "Sounds good, how much is it?\n\nOn Mon, Jamie wrote:\n> Reply unsubscribe to stop"
        self.assertEqual(latest_reply_text(body), "Sounds good, how much is it?")


class TestInboxEdgeCases(InboxTestCase):
    def test_suppressed_lead_at_same_domain_does_not_swallow_a_reply(self):
        self.add_lead("billing@bloomfilms.test", "unsubscribed")  # newer row, same company
        self.run_inbox([make_mail("Owner <owner@bloomfilms.test>", "Re: question", "Sounds good!", "<m1@x>")])
        self.assertEqual(self.lead()["status"], "interested")

    def test_company_reply_stops_every_active_lead_there(self):
        other = self.add_lead("info@bloomfilms.test", "contacted")
        self.run_inbox([make_mail("sarah@bloomfilms.test", "Re: question", "Not interested, thanks", "<m2@x>")])
        conn = db.get_connection()
        status, due = conn.execute("SELECT status, next_touch_at FROM leads WHERE id = ?", (other,)).fetchone()
        conn.close()
        self.assertEqual((status, due), ("not_interested", None))

    def test_mail_without_message_id_is_handled_once_across_runs(self):
        msg = make_mail("sarah@bloomfilms.test", "Re: question", "Sounds good!", "<tmp@x>")
        msg = b"\n".join(l for l in msg.split(b"\n") if not l.startswith(b"Message-ID"))
        filler = make_mail("bob@random.test", "hi", "x", "<f@x>")
        self.run_inbox([msg])
        self.run_inbox([filler, msg])  # same mail, different IMAP sequence number
        self.assertEqual(len(self.sent_to("sarah@bloomfilms.test")), 1)


class TestNotNowScheduling(InboxTestCase):
    def test_not_now_reply_schedules_one_check_in(self):
        self.profile["outreach"]["not_now_follow_up"] = {"days": 60, "body": "Checking back."}
        self.run_inbox([make_mail("sarah@bloomfilms.test", "Re: question", "Busy season, maybe later", "<n1@x>")])
        lead = self.lead()
        self.assertEqual(lead["status"], "not_now")
        self.assertIsNotNone(lead["next_touch_at"])

    def test_auto_reply_links_are_tracked(self):
        self.run_inbox([make_mail("sarah@bloomfilms.test", "Re: question", "Yes please!", "<t1@x>")])
        self.assertIn("utm_content=reply-interested", self.sent_to("sarah@bloomfilms.test")[0]["text"])


class TestBrandedWelcomeReply(InboxTestCase):
    def setUp(self):
        super().setUp()
        self.profile["email_design"].update({"style": "personal", "logo_url": "https://acme.test/logo.png"})
        self.profile["auto_reply"]["branded_intents"] = ["interested"]
        self.profile["auto_reply"]["buttons"] = {"interested": {"text": "Start your trial", "url": "{{product_url}}"}}
        self.profile["auto_reply"]["replies"]["interested"] = (
            "Hi {{first_name}},\n\nYou get:\n- Feature one\n- Feature two\n\nSteps:\n1. Sign up\n2. Try it"
            "\n\n{{button}}\n\n{{sign_off}}")

    def test_send_me_the_link_gets_the_branded_welcome(self):
        self.run_inbox([make_mail("sarah@bloomfilms.test", "Re: proposals", "Sounds good, send me the link!", "<w1@x>")])
        reply = self.sent_to("sarah@bloomfilms.test")[0]
        self.assertIn("&#10003;", reply.get("html", ""))                  # checklist
        self.assertIn(">Start your trial &rarr;</a>", reply.get("html", ""))  # branded button
        self.assertIn("Start your trial: https://example.com?utm_source=outreach", reply["text"])
        self.assertIn("utm_content=reply-interested", reply["text"])

    def test_questions_still_get_a_personal_reply(self):
        self.run_inbox([make_mail("sarah@bloomfilms.test", "Re: proposals", "How much is it after the trial?", "<w2@x>")])
        reply = self.sent_to("sarah@bloomfilms.test")[0]
        self.assertNotIn("<table", reply.get("html", ""))
        self.assertNotIn("<img", reply.get("html", ""))
