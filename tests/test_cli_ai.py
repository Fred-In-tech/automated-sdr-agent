"""`sdr ai test`, `sdr drafts` and the AI line in `sdr doctor` (all with a faked AI)."""

import io
import unittest

from core.ai.connectors import AIError, Reply
from core.ai.providers import load_ai_config
from core.cli_ai import ai_doctor_checks, check_connection, cmd_ai_test, cmd_drafts
from core.tui import UI
from test_ai_drafts import DraftsTest

LOCAL = {"ai": {"mode": "local_only", "provider": "ollama", "model": "llama3.2"}}


def cfg(profile=LOCAL, env=None):
    return load_ai_config(profile, environ=env or {})


def plain_ui(answers=None, interactive=False):
    out = io.StringIO()
    return UI(answers=answers or {}, plain=True, interactive=interactive, out=out, environ={}), out


class TestConnection(unittest.TestCase):
    def test_working_local_ai(self):
        result = check_connection(cfg(), list_fn=lambda c: ["llama3.2:latest", "qwen3"],
                                 chat_fn=lambda *a, **k: Reply('{"intent": "question"}'), clock=iter([1.0, 1.25]).__next__)
        self.assertTrue(result["ok"])
        self.assertTrue(result["json_ok"])
        self.assertEqual(result["latency_ms"], 250)
        self.assertEqual(result["models"], ["llama3.2:latest", "qwen3"])
        self.assertIn("nothing leaves this computer", result["notice"])

    def test_not_running(self):
        result = check_connection(cfg(), list_fn=lambda c: (_ for _ in ()).throw(AIError("Ollama isn't running at x.")),
                                 chat_fn=lambda *a, **k: (_ for _ in ()).throw(AIError("Ollama isn't running at x.")))
        self.assertFalse(result["ok"])
        self.assertIn("isn't running", result["message"])

    def test_model_not_installed(self):
        result = check_connection(cfg(), list_fn=lambda c: ["qwen3"], chat_fn=lambda *a, **k: Reply('{"intent": "other"}'))
        self.assertIn("ollama pull llama3.2", result["message"])

    def test_answers_not_json_is_a_warning(self):
        result = check_connection(cfg(), list_fn=lambda c: ["llama3.2"], chat_fn=lambda *a, **k: Reply("it's a question"))
        self.assertTrue(result["ok"])
        self.assertFalse(result["json_ok"])
        self.assertIn("built-in rules", result["message"])

    def test_off(self):
        result = check_connection(load_ai_config({}, environ={}))
        self.assertFalse(result["ok"])
        self.assertIn("setup --section ai", result["message"])

    def test_key_never_in_result(self):
        hosted = load_ai_config({"ai": {"mode": "hosted", "provider": "openai", "model": "m"}}, environ={"AI_API_KEY": "sk-zz"})
        result = check_connection(hosted, list_fn=lambda c: ["m"], chat_fn=lambda *a, **k: Reply('{"intent": "other"}'))
        self.assertNotIn("sk-zz", str(result))

    def test_cli_exit_codes(self):
        ui, out = plain_ui()
        ok = cmd_ai_test(ui, LOCAL, env={}, list_fn=lambda c: ["llama3.2"], chat_fn=lambda *a, **k: Reply('{"intent": "other"}'))
        self.assertEqual(ok, 0)
        self.assertIn("working", out.getvalue().lower())
        ui, out = plain_ui()
        self.assertEqual(cmd_ai_test(ui, {}, env={}), 1)


class TestDoctor(unittest.TestCase):
    def test_off_is_info(self):
        checks = ai_doctor_checks({}, {}, online=False)
        self.assertEqual((checks[0].label, checks[0].status), ("AI", "info"))

    def test_local_offline_is_ok_with_notice(self):
        check = ai_doctor_checks(LOCAL, {}, online=False)[0]
        self.assertEqual(check.status, "ok")
        self.assertIn("nothing leaves this computer", check.detail)

    def test_bad_url_fails(self):
        check = ai_doctor_checks({"ai": {"mode": "local_only", "provider": "openai", "model": "m"}}, {}, online=False)[0]
        self.assertEqual(check.status, "fail")

    def test_online_runs_the_test(self):
        check = ai_doctor_checks(LOCAL, {}, online=True, tester=lambda c: {"ok": False, "message": "Ollama isn't running"})[0]
        self.assertEqual(check.status, "fail")
        self.assertIn("isn't running", check.detail)


class TestDraftsCommand(DraftsTest):
    def test_no_terminal_lists_and_points_to_the_dashboard(self):
        from core.ai import drafts
        drafts.add_draft(self.lead_id, "answer", "Plans start at $29 a month.")
        ui, out = plain_ui()
        self.assertEqual(cmd_drafts(ui, {}), 0)
        self.assertIn("Lantern Hall", out.getvalue())
        self.assertIn("dashboard", out.getvalue())
        self.assertEqual(len(drafts.pending()), 1)

    def test_nothing_waiting(self):
        ui, out = plain_ui()
        self.assertEqual(cmd_drafts(ui, {}), 0)
        self.assertIn("Nothing is waiting", out.getvalue())

    def test_interactive_approve_and_edit(self):
        from core.ai import drafts
        a = drafts.add_draft(self.lead_id, "answer", "Plans start at $29 a month.")
        ui, _ = plain_ui({"drafts.action": "approve"}, interactive=False)
        ui.interactive = True                     # answers stand in for the keypresses
        cmd_drafts(ui, {"ai": {"facts": "$29"}})
        self.assertEqual(drafts.approved_for(self.lead_id, "answer")["id"], a)


if __name__ == "__main__":
    unittest.main()
