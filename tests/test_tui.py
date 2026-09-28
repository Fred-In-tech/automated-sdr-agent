"""Offline tests for the terminal UI (core/tui.py).

Nothing here needs a real terminal: plain mode is driven through patched input()/getpass(),
answers mode through dicts, and the fancy (rich + questionary) mode through a fake
questionary module and a rich Console that writes to a StringIO.
"""

import io
import unittest
from unittest import mock

from core import tui, tui_validation
from core.product import AUTHOR, PRODUCT_NAME, TAGLINE, version
from core.tui import (
    UI,
    Cancelled,
    InvalidAnswer,
    MissingAnswer,
    MissingAnswers,
    normalize_hex_color,
    normalize_hhmm,
    valid_email,
    valid_hex_color,
    valid_hhmm,
    valid_url,
)

try:
    import questionary as real_questionary
    from rich.console import Console
except ImportError:  # pragma: no cover - both are in requirements.txt
    real_questionary = None
    Console = None

HAS_FANCY = real_questionary is not None and Console is not None


def plain_ui(answers=None, interactive=True, environ=None, **kwargs):
    """A plain-mode UI writing to a StringIO. Returns (ui, output buffer)."""
    out = io.StringIO()
    ui = UI(answers=answers, plain=True, interactive=interactive, out=out,
            environ=environ if environ is not None else {}, **kwargs)
    return ui, out


def no_input(*_args, **_kwargs):
    raise AssertionError("the UI prompted although it should not have")


# ── public API ───────────────────────────────────────────────────────────────


class TestPublicApi(unittest.TestCase):
    def test_validation_helpers_are_reexported_from_core_tui(self):
        for name in ["valid_email", "valid_url", "valid_hex_color", "valid_hhmm", "validator", "parse_list",
                     "normalize_hex_color", "normalize_hhmm", "flatten_answers", "load_answers"]:
            self.assertIs(getattr(tui, name), getattr(tui_validation, name), name)
            self.assertIn(name, tui.__all__)


# ── mode detection ──────────────────────────────────────────────────────────


class TestModeDetection(unittest.TestCase):
    def tty(self, value: bool):
        return mock.patch.object(tui, "_isatty", return_value=value)

    def test_not_a_tty_means_plain_and_non_interactive(self):
        with self.tty(False):
            ui = UI(environ={})
        self.assertTrue(ui.plain)
        self.assertFalse(ui.interactive)

    def test_sdr_plain_env_forces_plain_mode_even_on_a_tty(self):
        with self.tty(True):
            ui = UI(environ={"SDR_PLAIN": "1"})
        self.assertTrue(ui.plain)
        self.assertTrue(ui.interactive)

    def test_dumb_terminal_is_plain(self):
        with self.tty(True):
            ui = UI(environ={"TERM": "dumb"})
        self.assertTrue(ui.plain)

    def test_missing_questionary_falls_back_to_plain(self):
        with self.tty(True), mock.patch.object(tui, "_import_questionary", return_value=None):
            ui = UI(environ={})
        self.assertTrue(ui.plain)

    def test_missing_rich_falls_back_to_plain_even_when_asked_for_fancy(self):
        with self.tty(True), mock.patch.object(tui, "_import_rich", return_value=None):
            ui = UI(plain=False, environ={})
        self.assertTrue(ui.plain)
        self.assertIsNone(ui.console)

    @unittest.skipUnless(HAS_FANCY, "rich/questionary not installed")
    def test_real_tty_with_libraries_is_fancy(self):
        with self.tty(True):
            ui = UI(environ={})
        self.assertFalse(ui.plain)
        self.assertIsNotNone(ui.console)

    def test_explicit_plain_wins(self):
        with self.tty(True):
            ui = UI(plain=True, environ={})
        self.assertTrue(ui.plain)


# ── answers mode (used by AI agents: `sdr setup --answers file.toml`) ──────────


class TestAnswersMode(unittest.TestCase):
    def test_text_answer_is_returned_without_prompting(self):
        ui, _ = plain_ui({"business.website": "https://acme.com"}, interactive=True)
        with mock.patch("builtins.input", side_effect=no_input):
            self.assertEqual(ui.text("business.website", "Your website", validate=valid_url), "https://acme.com")

    def test_numbers_in_answers_become_text(self):
        ui, _ = plain_ui({"audience.leads_per_run": 4}, interactive=False)
        self.assertEqual(ui.text("audience.leads_per_run", "Leads per run"), "4")

    def test_normalize_is_applied_to_answers(self):
        ui, _ = plain_ui({"style.brand_color": "3b82f6"}, interactive=False)
        value = ui.text("style.brand_color", "Brand colour", normalize=normalize_hex_color, validate=valid_hex_color)
        self.assertEqual(value, "#3B82F6")

    def test_invalid_answer_without_a_tty_raises_invalid_answer(self):
        ui, _ = plain_ui({"email.address": "not-an-email"}, interactive=False)
        with self.assertRaises(InvalidAnswer) as ctx:
            ui.text("email.address", "Your email address", validate=valid_email)
        self.assertEqual(ctx.exception.key, "email.address")
        self.assertIsInstance(ctx.exception, MissingAnswer)  # one except clause catches both
        self.assertIn("email", str(ctx.exception).lower())

    def test_invalid_answer_on_a_tty_warns_and_asks_again(self):
        ui, out = plain_ui({"email.address": "not-an-email"}, interactive=True)
        with mock.patch("builtins.input", return_value="ana@acme.com"):
            self.assertEqual(ui.text("email.address", "Your email address", validate=valid_email), "ana@acme.com")
        self.assertIn("email.address", out.getvalue())

    def test_missing_required_text_without_a_tty_raises_missing_answer(self):
        ui, _ = plain_ui({}, interactive=False)
        with self.assertRaises(MissingAnswer) as ctx:
            ui.text("business.offer", "What you offer")
        self.assertEqual((ctx.exception.key, ctx.exception.label), ("business.offer", "What you offer"))
        self.assertNotIsInstance(ctx.exception, InvalidAnswer)

    def test_no_answers_at_all_without_a_tty_also_raises(self):
        ui, _ = plain_ui(None, interactive=False)
        with self.assertRaises(MissingAnswer):
            ui.text("business.offer", "What you offer")

    def test_missing_text_with_default_uses_the_default(self):
        ui, _ = plain_ui({}, interactive=False)
        self.assertEqual(ui.text("email.sign_off", "Sign-off", default="Best,\nAna"), "Best,\nAna")

    def test_missing_optional_text_is_empty(self):
        ui, _ = plain_ui({}, interactive=False)
        self.assertEqual(ui.text("business.name", "Business name", required=False), "")

    def test_empty_required_answer_is_invalid(self):
        ui, _ = plain_ui({"business.offer": "  "}, interactive=False)
        with self.assertRaises(InvalidAnswer):
            ui.text("business.offer", "What you offer")

    def test_list_value_for_a_text_prompt_is_invalid(self):
        ui, _ = plain_ui({"business.offer": ["a"]}, interactive=False)
        with self.assertRaises(InvalidAnswer):
            ui.text("business.offer", "What you offer")

    def test_confirm_accepts_booleans_and_words(self):
        ui, _ = plain_ui({"a.x": True, "a.y": "no", "a.z": "Yes", "a.w": 0}, interactive=False)
        self.assertIs(ui.confirm("a.x", "X?", default=False), True)
        self.assertIs(ui.confirm("a.y", "Y?"), False)
        self.assertIs(ui.confirm("a.z", "Z?", default=False), True)
        self.assertIs(ui.confirm("a.w", "W?"), False)

    def test_confirm_rejects_nonsense(self):
        ui, _ = plain_ui({"replies.enabled": "maybe"}, interactive=False)
        with self.assertRaises(InvalidAnswer):
            ui.confirm("replies.enabled", "Answer replies?")

    def test_missing_confirm_uses_its_default(self):
        ui, _ = plain_ui({}, interactive=False)
        self.assertIs(ui.confirm("replies.enabled", "Answer replies?", default=True), True)
        self.assertIs(ui.confirm("schedule.install", "Install?", default=False), False)

    def test_required_confirm_must_be_answered(self):
        ui, _ = plain_ui({}, interactive=False)
        with self.assertRaises(MissingAnswer):
            ui.confirm("business.allow_no_address", "Send without an address?", required=True)

    def test_select_matches_values_case_insensitively(self):
        choices = [("gmail", "Gmail", "App password needed"), ("outlook", "Outlook", "")]
        ui, _ = plain_ui({"email.provider": "Outlook"}, interactive=False)
        self.assertEqual(ui.select("email.provider", "Email provider", choices), "outlook")

    def test_select_rejects_unknown_values_and_lists_the_options(self):
        choices = [("gmail", "Gmail", ""), ("outlook", "Outlook", "")]
        ui, _ = plain_ui({"email.provider": "aol"}, interactive=False)
        with self.assertRaises(InvalidAnswer) as ctx:
            ui.select("email.provider", "Email provider", choices)
        self.assertIn("gmail", str(ctx.exception))
        self.assertIn("outlook", str(ctx.exception))

    def test_select_missing_uses_default_or_raises(self):
        choices = [("sales", "Sales", ""), ("marketing", "Marketing", "")]
        ui, _ = plain_ui({}, interactive=False)
        self.assertEqual(ui.select("style.kind", "Email style", choices, default="marketing"), "marketing")
        with self.assertRaises(MissingAnswer):
            ui.select("style.kind", "Email style", choices)

    def test_list_answer_accepts_a_real_list(self):
        ui, _ = plain_ui({"audience.cities": [" Austin, TX ", "Dallas, TX", ""]}, interactive=False)
        self.assertEqual(ui.list("audience.cities", "Cities", "Austin, TX"), ["Austin, TX", "Dallas, TX"])

    def test_list_answer_accepts_a_semicolon_string(self):
        ui, _ = plain_ui({"audience.cities": "Austin, TX; Dallas, TX"}, interactive=False)
        self.assertEqual(ui.list("audience.cities", "Cities", "Austin, TX"), ["Austin, TX", "Dallas, TX"])

    def test_list_items_are_normalized_and_validated(self):
        ui, _ = plain_ui({"schedule.run_times": ["9:00", "14:00"]}, interactive=False)
        self.assertEqual(ui.list("schedule.run_times", "Run times", "09:00", normalize=normalize_hhmm,
                                 validate=valid_hhmm), ["09:00", "14:00"])
        ui, _ = plain_ui({"schedule.run_times": ["9:00", "25:00"]}, interactive=False)
        with self.assertRaises(InvalidAnswer) as ctx:
            ui.list("schedule.run_times", "Run times", "09:00", validate=valid_hhmm)
        self.assertIn("25:00", str(ctx.exception))

    def test_empty_required_list_is_invalid_and_missing_list_uses_default(self):
        ui, _ = plain_ui({"audience.cities": []}, interactive=False)
        with self.assertRaises(InvalidAnswer):
            ui.list("audience.cities", "Cities", "Austin, TX")
        ui, _ = plain_ui({}, interactive=False)
        self.assertEqual(ui.list("schedule.send_days", "Days", "Mon", default=["Mon", "Tue"]), ["Mon", "Tue"])
        with self.assertRaises(MissingAnswer):
            ui.list("audience.cities", "Cities", "Austin, TX")

    def test_checkbox_answers(self):
        days = [(d, d, "") for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]]
        ui, _ = plain_ui({"schedule.send_days": ["mon", "Wed"]}, interactive=False)
        self.assertEqual(ui.checkbox("schedule.send_days", "Send days", days), ["Mon", "Wed"])
        ui, _ = plain_ui({"schedule.send_days": ["Funday"]}, interactive=False)
        with self.assertRaises(InvalidAnswer):
            ui.checkbox("schedule.send_days", "Send days", days)
        ui, _ = plain_ui({}, interactive=False)
        self.assertEqual(ui.checkbox("schedule.send_days", "Send days", days, default=["Mon"]), ["Mon"])

    def test_answered_values_are_echoed_so_transcripts_make_sense(self):
        ui, out = plain_ui({"business.website": "https://acme.com"}, interactive=False)
        ui.text("business.website", "Your website")
        self.assertIn("Your website", out.getvalue())
        self.assertIn("https://acme.com", out.getvalue())


class TestPasswordAnswers(unittest.TestCase):
    def test_password_is_read_from_the_named_environment_variable(self):
        ui, out = plain_ui({"email.password_env": "SDR_EMAIL_PASSWORD"}, interactive=False,
                           environ={"SDR_EMAIL_PASSWORD": "s3cret-app-pw"})
        with mock.patch("getpass.getpass", side_effect=no_input):
            self.assertEqual(ui.password("email.password", "Email app password"), "s3cret-app-pw")
        self.assertNotIn("s3cret-app-pw", out.getvalue())
        self.assertIn("SDR_EMAIL_PASSWORD", out.getvalue())

    def test_raw_password_in_answers_is_refused(self):
        ui, out = plain_ui({"email.password": "hunter2"}, interactive=False)
        with self.assertRaises(InvalidAnswer) as ctx:
            ui.password("email.password", "Email app password")
        self.assertIn("email.password_env", str(ctx.exception))
        self.assertNotIn("hunter2", str(ctx.exception))
        self.assertNotIn("hunter2", out.getvalue())

    def test_unset_environment_variable_is_reported_against_the_env_key(self):
        ui, _ = plain_ui({"email.password_env": "SDR_EMAIL_PASSWORD"}, interactive=False, environ={})
        with self.assertRaises(MissingAnswer) as ctx:
            ui.password("email.password", "Email app password")
        self.assertEqual(ctx.exception.key, "email.password_env")
        self.assertIn("SDR_EMAIL_PASSWORD", str(ctx.exception))

    def test_missing_env_key_without_a_tty_raises(self):
        ui, _ = plain_ui({}, interactive=False)
        with self.assertRaises(MissingAnswer) as ctx:
            ui.password("email.password", "Email app password")
        self.assertEqual(ctx.exception.key, "email.password_env")

    def test_optional_password_can_be_skipped(self):
        ui, _ = plain_ui({}, interactive=False)
        self.assertEqual(ui.password("security.dashboard_password", "Dashboard password", required=False), "")

    def test_bad_environment_variable_name_is_invalid(self):
        ui, _ = plain_ui({"email.password_env": "not a name"}, interactive=False)
        with self.assertRaises(InvalidAnswer):
            ui.password("email.password", "Email app password")

    def test_unset_env_on_a_tty_falls_back_to_a_hidden_prompt(self):
        ui, _ = plain_ui({"email.password_env": "SDR_EMAIL_PASSWORD"}, interactive=True, environ={})
        with mock.patch("getpass.getpass", return_value="typed-pw") as gp:
            self.assertEqual(ui.password("email.password", "Email app password"), "typed-pw")
        gp.assert_called_once()


class TestCollectMissing(unittest.TestCase):
    def test_collect_mode_records_every_problem_then_raises_them_together(self):
        ui, _ = plain_ui({"email.address": "nope"}, interactive=False, collect_missing=True)
        self.assertEqual(ui.text("business.website", "Your website"), "")
        self.assertEqual(ui.text("email.address", "Your email", validate=valid_email), "")
        self.assertEqual(ui.select("email.provider", "Provider", [("gmail", "Gmail", "")]), "gmail")
        self.assertEqual(ui.list("audience.cities", "Cities", "Austin, TX"), [])
        self.assertEqual(ui.password("email.password", "Password"), "")
        self.assertEqual(ui.text("business.website", "Your website"), "")  # asked twice, reported once
        with self.assertRaises(MissingAnswers) as ctx:
            ui.raise_missing()
        keys = [e.key for e in ctx.exception.errors]
        self.assertEqual(keys, ["business.website", "email.address", "email.provider",
                                "audience.cities", "email.password_env"])
        message = str(ctx.exception)
        for key in keys:
            self.assertIn(key, message)

    def test_text_placeholder_is_always_a_string(self):
        ui, _ = plain_ui({"audience.leads_per_run": "lots"}, interactive=False, collect_missing=True)
        value = ui.text("audience.leads_per_run", "Leads per run", default=4, validate=str.isdigit)
        self.assertEqual(value, "4")
        self.assertEqual([e.key for e in ui.missing], ["audience.leads_per_run"])

    def test_raise_missing_is_quiet_when_everything_was_answered(self):
        ui, _ = plain_ui({"business.website": "acme.com"}, interactive=False, collect_missing=True)
        ui.text("business.website", "Your website")
        ui.raise_missing()  # no exception
        self.assertEqual(ui.missing, [])


# ── plain interactive prompts ────────────────────────────────────────────────


class TestPlainPrompts(unittest.TestCase):
    def test_text_enter_keeps_the_default_shown_in_brackets(self):
        ui, _ = plain_ui()
        with mock.patch("builtins.input", return_value="") as inp:
            self.assertEqual(ui.text("email.from_name", "Your name", default="Ana"), "Ana")
        self.assertIn("[Ana]", inp.call_args.args[0])

    def test_text_required_asks_again_when_empty(self):
        ui, out = plain_ui()
        with mock.patch("builtins.input", side_effect=["", "  Free trial  "]):
            self.assertEqual(ui.text("business.offer", "What you offer"), "Free trial")
        self.assertIn("required", out.getvalue().lower())

    def test_text_validation_message_is_shown_before_asking_again(self):
        ui, out = plain_ui()
        with mock.patch("builtins.input", side_effect=["nope", "ana@acme.com"]):
            self.assertEqual(ui.text("email.address", "Email", validate=valid_email), "ana@acme.com")
        self.assertIn("email address", out.getvalue())

    def test_custom_validator_messages_and_exceptions(self):
        def must_be_short(value):
            if len(value) > 5:
                raise ValueError("Keep it under 6 characters")
            return True
        ui, out = plain_ui()
        with mock.patch("builtins.input", side_effect=["toolong", "ok"]):
            self.assertEqual(ui.text("x.y", "Short", validate=must_be_short), "ok")
        self.assertIn("Keep it under 6 characters", out.getvalue())

    def test_optional_text_can_be_skipped_and_hint_is_printed(self):
        ui, out = plain_ui()
        with mock.patch("builtins.input", return_value="") as inp:
            self.assertEqual(ui.text("business.name", "Business name", required=False, hint="We guess it"), "")
        self.assertIn("optional", inp.call_args.args[0])
        self.assertIn("We guess it", out.getvalue())

    def test_text_normalize_runs_on_typed_input(self):
        ui, _ = plain_ui()
        with mock.patch("builtins.input", return_value="3b82f6"):
            self.assertEqual(ui.text("style.brand_color", "Colour", normalize=normalize_hex_color,
                                     validate=valid_hex_color), "#3B82F6")

    def test_confirm(self):
        ui, _ = plain_ui()
        with mock.patch("builtins.input", return_value="") as inp:
            self.assertIs(ui.confirm("a.b", "Continue?", default=True), True)
        self.assertIn("[Y/n]", inp.call_args.args[0])
        with mock.patch("builtins.input", side_effect=["maybe", "n"]):
            self.assertIs(ui.confirm("a.b", "Continue?"), False)
        with mock.patch("builtins.input", return_value="") as inp:
            self.assertIs(ui.confirm("a.b", "Continue?", default=False), False)
        self.assertIn("[y/N]", inp.call_args.args[0])

    def test_select_by_number_value_or_default(self):
        choices = [("gmail", "Gmail", "Needs an app password"), ("outlook", "Outlook", ""),
                   ("other", "Other", "")]
        ui, out = plain_ui()
        with mock.patch("builtins.input", return_value="2"):
            self.assertEqual(ui.select("email.provider", "Provider", choices), "outlook")
        self.assertIn("Needs an app password", out.getvalue())
        with mock.patch("builtins.input", return_value="Other"):
            self.assertEqual(ui.select("email.provider", "Provider", choices), "other")
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(ui.select("email.provider", "Provider", choices, default="gmail"), "gmail")
        with mock.patch("builtins.input", side_effect=["9", "", "1"]):
            self.assertEqual(ui.select("email.provider", "Provider", choices), "gmail")

    def test_select_accepts_two_item_choices(self):
        ui, _ = plain_ui()
        with mock.patch("builtins.input", return_value="1"):
            self.assertEqual(ui.select("a.b", "Pick", [("x", "Ex"), ("y", "Why")]), "x")

    def test_select_rejects_bad_programmer_input(self):
        ui, _ = plain_ui()
        with self.assertRaises(ValueError):
            ui.select("a.b", "Pick", [])
        with self.assertRaises(ValueError):
            ui.select("a.b", "Pick", [("x", "Ex", "")], default="zzz")

    def test_checkbox_plain(self):
        days = [(d, d, "") for d in ["Mon", "Tue", "Wed"]]
        ui, _ = plain_ui()
        with mock.patch("builtins.input", return_value="1, 3"):
            self.assertEqual(ui.checkbox("schedule.send_days", "Days", days), ["Mon", "Wed"])
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(ui.checkbox("schedule.send_days", "Days", days, default=["Tue"]), ["Tue"])
        with mock.patch("builtins.input", side_effect=["7", "tue"]):
            self.assertEqual(ui.checkbox("schedule.send_days", "Days", days), ["Tue"])

    def test_password_never_echoes_and_is_required(self):
        ui, out = plain_ui()
        with mock.patch("getpass.getpass", side_effect=["", "pw-123"]) as gp, \
                mock.patch("builtins.input", side_effect=no_input):
            self.assertEqual(ui.password("email.password", "Email password"), "pw-123")
        self.assertEqual(gp.call_count, 2)
        self.assertNotIn("pw-123", out.getvalue())

    def test_password_confirmation_must_match(self):
        ui, out = plain_ui()
        with mock.patch("getpass.getpass", side_effect=["one", "two", "same", "same"]):
            self.assertEqual(ui.password("security.dashboard_password", "Dashboard password", confirm=True), "same")
        self.assertIn("match", out.getvalue())

    def test_list_one_per_line_until_an_empty_line(self):
        ui, out = plain_ui()
        with mock.patch("builtins.input", side_effect=["Austin, TX", "Dallas, TX; Houston, TX", ""]):
            self.assertEqual(ui.list("audience.cities", "Cities", "Austin, TX", split_commas=False),
                             ["Austin, TX", "Dallas, TX", "Houston, TX"])
        self.assertIn("Austin, TX", out.getvalue())

    def test_list_empty_first_line_keeps_the_default(self):
        ui, _ = plain_ui()
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(ui.list("audience.job_titles", "Titles", "Owner", default=["Owner", "Founder"]),
                             ["Owner", "Founder"])

    def test_list_requires_at_least_one_item(self):
        ui, out = plain_ui()
        with mock.patch("builtins.input", side_effect=["", "Owner", ""]):
            self.assertEqual(ui.list("audience.job_titles", "Titles", "Owner"), ["Owner"])
        self.assertIn("at least one", out.getvalue())

    def test_list_invalid_items_are_skipped_with_a_message(self):
        ui, out = plain_ui()
        with mock.patch("builtins.input", side_effect=["25:00", "9:00", ""]):
            self.assertEqual(ui.list("schedule.run_times", "Times", "09:00", validate=valid_hhmm,
                                     normalize=normalize_hhmm), ["09:00"])
        self.assertIn("25:00", out.getvalue())

    def test_pause_for_waits_for_enter_only_when_interactive(self):
        ui, out = plain_ui()
        with mock.patch("builtins.input", return_value="") as inp:
            ui.pause_for("Create an app password")
        inp.assert_called_once()
        ui, out = plain_ui(interactive=False)
        with mock.patch("builtins.input", side_effect=no_input):
            ui.pause_for("Create an app password")
        self.assertIn("Create an app password", out.getvalue())

    def test_ctrl_d_and_ctrl_c_become_cancelled(self):
        ui, _ = plain_ui()
        with mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(Cancelled):
                ui.text("a.b", "Anything")
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):  # Cancelled is a KeyboardInterrupt
                ui.confirm("a.b", "Anything?")

    def test_endless_invalid_input_eventually_gives_up(self):
        ui, _ = plain_ui()
        with mock.patch("builtins.input", return_value="nope"):
            with self.assertRaises(InvalidAnswer):
                ui.text("email.address", "Email", validate=valid_email)


# ── output helpers ───────────────────────────────────────────────────────────


class TestPlainOutput(unittest.TestCase):
    def test_banner_is_a_simple_two_line_ascii_header(self):
        ui, out = plain_ui()
        ui.banner()
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertIn(f"{PRODUCT_NAME} by {AUTHOR}", lines[0])
        self.assertIn(version(), lines[0])
        self.assertIn(TAGLINE, lines[1])
        out.getvalue().encode("ascii")  # no fancy characters in plain mode

    def test_step_messages_and_summary(self):
        ui, out = plain_ui()
        ui.step(2, 8, "Your audience", hint="Who should we find?")
        ui.info("Reading your website")
        ui.success("Saved")
        ui.warn("No postal address")
        ui.error("Login failed")
        ui.summary("Your setup", [("Website", "https://acme.com"), ("Email", "ana@acme.com")])
        text = out.getvalue()
        for expected in ["2/8", "Your audience", "Who should we find?", "Reading your website", "Saved",
                         "Warning: No postal address", "Error: Login failed", "Your setup", "https://acme.com",
                         "ana@acme.com"]:
            self.assertIn(expected, text)
        text.encode("ascii")

    def test_summary_aligns_values(self):
        ui, out = plain_ui()
        ui.summary("Setup", [("A", "1"), ("Longer key", "2")])
        rows = [line for line in out.getvalue().splitlines() if line.strip().startswith(("A", "Longer"))]
        self.assertEqual(rows[0].index("1"), rows[1].index("2"))

    def test_spinner_prints_the_message_and_propagates_errors(self):
        ui, out = plain_ui()
        with ui.spinner("Checking your login") as status:
            status.update("Still checking")
        self.assertIn("Checking your login", out.getvalue())
        with self.assertRaises(RuntimeError):
            with ui.spinner("Boom"):
                raise RuntimeError("x")

    def test_unencodable_text_does_not_crash_plain_output(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        ui = UI(plain=True, interactive=False, out=stream, environ={})
        ui.info("Café in São Paulo")
        ui.summary("Setup", [("City", "Zürich")])
        stream.flush()
        self.assertIn(b"Caf", stream.buffer.getvalue())

    def test_echo_and_panel(self):
        ui, out = plain_ui()
        ui.echo("Subject: hello")
        ui.panel("Hi Ana,\nBody", title="Email 1")
        text = out.getvalue()
        self.assertIn("Subject: hello", text)
        self.assertIn("Email 1", text)
        self.assertIn("Hi Ana,", text)


@unittest.skipUnless(HAS_FANCY, "rich/questionary not installed")
class TestFancyOutput(unittest.TestCase):
    def fancy(self, width=100, file=None):
        buf = file or io.StringIO()
        console = Console(file=buf, width=width, color_system=None, force_terminal=False, legacy_windows=False)
        ui = UI(plain=False, interactive=False, console=console, environ={})
        return ui, buf

    def test_banner_art_is_compact(self):
        self.assertLessEqual(len(tui.BANNER_ART), 6)
        self.assertTrue(all(len(line) <= 60 for line in tui.BANNER_ART))

    def test_banner_shows_art_byline_tagline_and_version(self):
        ui, buf = self.fancy()
        ui.banner()
        text = buf.getvalue()
        self.assertIn(tui.BANNER_ART[0].strip(), text)
        self.assertIn(f"by {AUTHOR}", text)
        self.assertIn(version(), text)
        self.assertIn(TAGLINE.split(":")[0], text)

    def test_narrow_terminal_gets_a_text_title_instead_of_art(self):
        ui, buf = self.fancy(width=40)
        ui.banner()
        text = buf.getvalue()
        self.assertNotIn(tui.BANNER_ART[0].strip(), text)
        self.assertIn("AUTOMATED SDR", text)

    def test_non_utf_terminal_gets_ascii_only(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        ui, _ = self.fancy(file=stream)
        ui.banner()
        ui.success("Saved")
        ui.step(1, 3, "Business")
        stream.flush()
        self.assertIn(b"AUTOMATED SDR", stream.buffer.getvalue())

    def test_markup_like_text_is_printed_literally(self):
        ui, buf = self.fancy()
        ui.warn("[sender] postal_address is empty")
        ui.summary("Setup [draft]", [("[email]", "[bold]x[/bold]")])
        text = buf.getvalue()
        self.assertIn("[sender] postal_address is empty", text)
        self.assertIn("[bold]x[/bold]", text)

    def test_step_summary_spinner_and_panel_render(self):
        ui, buf = self.fancy()
        ui.step(3, 8, "Email style", hint="Sales or marketing?")
        ui.summary("Your setup", [("Website", "https://acme.com")])
        with ui.spinner("Reading your website") as status:
            status.update("Almost done")
        ui.panel("Hi Ana", title="Email 1")
        ui.echo("plain line")
        text = buf.getvalue()
        for expected in ["Step 3 of 8", "Email style", "Sales or marketing?", "Your setup", "https://acme.com",
                         "Hi Ana", "Email 1", "plain line"]:
            self.assertIn(expected, text)


# ── fancy prompts through a fake questionary ─────────────────────────────────


class FakeQuestion:
    def __init__(self, answer):
        self.answer = answer

    def unsafe_ask(self):
        if isinstance(self.answer, BaseException) or (isinstance(self.answer, type)
                                                       and issubclass(self.answer, BaseException)):
            raise self.answer
        return self.answer


class FakeQuestionary:
    """Stands in for the questionary module: records calls, returns canned answers."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        if real_questionary is not None:
            self.Choice = real_questionary.Choice
            self.Style = real_questionary.Style

    def _next(self, kind, args, kwargs):
        self.calls.append((kind, args, kwargs))
        return FakeQuestion(self.answers.pop(0))

    def text(self, *args, **kwargs):
        return self._next("text", args, kwargs)

    def confirm(self, *args, **kwargs):
        return self._next("confirm", args, kwargs)

    def select(self, *args, **kwargs):
        return self._next("select", args, kwargs)

    def checkbox(self, *args, **kwargs):
        return self._next("checkbox", args, kwargs)

    def password(self, *args, **kwargs):
        return self._next("password", args, kwargs)


@unittest.skipUnless(HAS_FANCY, "rich/questionary not installed")
class TestFancyPrompts(unittest.TestCase):
    def fancy(self, fake, answers=None):
        buf = io.StringIO()
        console = Console(file=buf, width=100, color_system=None, force_terminal=False, legacy_windows=False)
        patcher = mock.patch.object(tui, "_import_questionary", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return UI(answers=answers, plain=False, interactive=True, console=console, environ={}), buf

    def test_text_uses_questionary_with_default_and_validation(self):
        fake = FakeQuestionary("ana@acme.com")
        ui, _ = self.fancy(fake)
        self.assertEqual(ui.text("email.address", "Email", default="me@x.com", validate=valid_email), "ana@acme.com")
        kind, args, kwargs = fake.calls[0]
        self.assertEqual(kind, "text")
        self.assertEqual(kwargs["default"], "me@x.com")
        self.assertIn("email address", kwargs["validate"]("nope"))
        self.assertIs(kwargs["validate"]("ok@acme.com"), True)

    def test_select_passes_choices_with_descriptions_and_returns_the_value(self):
        fake = FakeQuestionary("outlook")
        ui, _ = self.fancy(fake)
        choices = [("gmail", "Gmail", "App password"), ("outlook", "Outlook", "Microsoft 365")]
        self.assertEqual(ui.select("email.provider", "Provider", choices, default="gmail"), "outlook")
        _kind, _args, kwargs = fake.calls[0]
        self.assertEqual([c.value for c in kwargs["choices"]], ["gmail", "outlook"])
        self.assertEqual(kwargs["default"].value, "gmail")

    def test_confirm_password_checkbox_and_list(self):
        fake = FakeQuestionary(False, "pw", ["Mon", "Fri"], "Austin, TX", "Dallas, TX", "")
        ui, buf = self.fancy(fake)
        self.assertIs(ui.confirm("a.b", "OK?"), False)
        self.assertEqual(ui.password("email.password", "Password"), "pw")
        self.assertEqual(ui.checkbox("schedule.send_days", "Days", [(d, d, "") for d in ["Mon", "Fri"]]),
                         ["Mon", "Fri"])
        self.assertEqual(ui.list("audience.cities", "Cities", "Austin, TX", split_commas=False),
                         ["Austin, TX", "Dallas, TX"])
        self.assertEqual([c[0] for c in fake.calls], ["confirm", "password", "checkbox", "text", "text", "text"])
        self.assertNotIn("pw", buf.getvalue().replace("password", "").replace("Password", ""))

    def test_ctrl_c_in_questionary_is_cancelled(self):
        fake = FakeQuestionary(KeyboardInterrupt())
        ui, _ = self.fancy(fake)
        with self.assertRaises(Cancelled):
            ui.text("a.b", "Anything")

    def test_broken_terminal_falls_back_to_plain_input(self):
        fake = FakeQuestionary(OSError("No console screen buffer"))
        ui, _ = self.fancy(fake)
        with mock.patch("builtins.input", return_value="typed"):
            self.assertEqual(ui.text("a.b", "Anything"), "typed")
        with mock.patch("builtins.input", return_value="y"):
            self.assertIs(ui.confirm("a.c", "Sure?", default=False), True)
        self.assertEqual(len(fake.calls), 1)  # questionary is not tried again after it failed

    def test_answers_skip_questionary(self):
        fake = FakeQuestionary()
        ui, _ = self.fancy(fake, answers={"business.website": "acme.com"})
        self.assertEqual(ui.text("business.website", "Website"), "acme.com")
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
