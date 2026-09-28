import unittest
import os
import sqlite3
import json
import tempfile
from unittest import mock

import core.db
from core.db import init_db, get_connection, get_db_stats, log_event
from bots.leadgen_pipeline import LeadGenPipeline
from bots.email_marketing import EmailMarketingEngine
from bots.social_bot import SocialBot
from runner import run_task

# These tests hit the real web, your real database and your real email account.
# They are skipped unless you opt in:  RUN_LIVE_TESTS=1 python3 -m pytest tests/
live_only = unittest.skipUnless(
    os.getenv("RUN_LIVE_TESTS") == "1",
    "Live test (scrapes the web, may send real emails). Set RUN_LIVE_TESTS=1 to run.",
)


class TestAutomatedSdr(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Offline runs use a throwaway database; only RUN_LIVE_TESTS=1 touches the real one.
        cls._patches = []
        if os.getenv("RUN_LIVE_TESTS") != "1":
            cls._tmp = tempfile.TemporaryDirectory()
            db_path = os.path.join(cls._tmp.name, "automations.db")
            cls._patches = [mock.patch.object(core.db, "DB_PATH", db_path),
                            mock.patch.object(core.db, "DB_DIR", cls._tmp.name)]
            for patch in cls._patches:
                patch.start()
        init_db()

    @classmethod
    def tearDownClass(cls):
        for patch in cls._patches:
            patch.stop()
        if getattr(cls, "_tmp", None):
            cls._tmp.cleanup()

    def test_01_db_initialization(self):
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cursor.fetchall()]
        conn.close()

        self.assertIn("leads", tables)
        self.assertIn("email_campaigns", tables)
        self.assertIn("email_logs", tables)
        self.assertIn("social_posts", tables)
        self.assertIn("bot_logs", tables)

    @live_only
    def test_02_leadgen_pipeline(self):
        pipeline = LeadGenPipeline()
        res = pipeline.run_pipeline(count=2)
        self.assertIn("new_leads_count", res)
        self.assertTrue(os.path.exists(res["csv_path"]))

    @live_only
    def test_03_email_marketing_engine(self):
        engine = EmailMarketingEngine()
        res = engine.run_outreach_campaign(limit=2)
        self.assertIn("sent_count", res)

    @live_only
    def test_04_social_bot(self):
        bot = SocialBot()
        scheduled = bot.generate_and_schedule_thread("AI Test Topic")
        self.assertEqual(scheduled["status"], "scheduled")

        published = bot.publish_pending_posts()
        self.assertGreaterEqual(published["published_count"], 1)

    @live_only
    def test_05_runner_pipeline(self):
        res = run_task("pipeline")
        self.assertIn("leadgen", res)
        self.assertIn("email", res)
        self.assertIn("inbox", res)

    def test_06_db_stats(self):
        stats = get_db_stats()
        self.assertGreaterEqual(stats["total_leads"], 0)

if __name__ == "__main__":
    unittest.main()
