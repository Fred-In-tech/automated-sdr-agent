"""Offline checks that the public docs (README.md, AGENTS.md, SECURITY.md, CONTRIBUTING.md,
.env.example, CHANGELOG.md) match the real CLI, the real setup questions, .gitignore, the CI
workflow and the dashboard.

README.md and AGENTS.md are where people and AI agents learn the commands, flags and answers-file
keys. These tests read the actual argparse parser (cli.build_parser) and question list
(core.setup_questions.QUESTIONS), so a renamed flag, a new command or a new question fails here
instead of confusing someone at the terminal. The example answers file in AGENTS.md is run through
the real setup with fake dependencies: nothing touches the network, a mailbox, the crontab or the
real config. The promises the docs make about privacy (what is gitignored, what the tool connects
to, what CI runs) are checked against the files that keep them.
"""

import argparse
import fnmatch
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cli
from core.product import REPO_SLUG
from core.search import SETTINGS as SEARCH_SETTINGS
from core.setup_questions import QUESTIONS, SECTIONS
from core.setup_steps import Deps
from core.setup_wizard import run_setup
from core.tui import UI
from core.tui_validation import load_answers

ROOT = Path(__file__).resolve().parent.parent
RAW_URL = f"https://raw.githubusercontent.com/{REPO_SLUG}/main"
CODE_SPAN = re.compile(r"`([^`\n]+)`")
PLACEHOLDER = re.compile(r"^(<.*>|[A-Z_]+|\d+|\S+\.toml|\S+@\S+)$")   # <name>, NAME, N, FILE.toml, 9000, ADDR


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def subcommands() -> dict[str, argparse.ArgumentParser]:
    for action in cli.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("cli.build_parser() has no subcommands")


def commands_in(text: str) -> list[str]:
    """Every `sdr ...` the text tells someone to type: inline code spans and lines in code blocks."""
    spans = [span.replace("\\|", "|").strip() for span in CODE_SPAN.findall(text)]
    lines = [line.strip() for block in re.findall(r"```[a-z]*\n(.*?)```", text, flags=re.S)
             for line in block.splitlines()]
    return [item for item in spans + lines if item == "sdr" or item.startswith("sdr ")]


def row_commands(text: str) -> list[str]:
    """Options named on their own in a command-table row, as full commands: the row
    "| `sdr doctor` | ... `--online` ... |" yields "sdr doctor --online"."""
    found = []
    for line in re.findall(r"^\| `sdr [a-z-]+.*$", text, flags=re.M):
        name = line.split("`")[1].split()[1]
        spans = [span.replace("\\|", "|").strip() for span in CODE_SPAN.findall(line)]
        found += [f"sdr {name} {span}" for span in spans if span.startswith("--")]
    return found


def command_problems(command: str, parsers: dict[str, argparse.ArgumentParser]) -> list[str]:
    """What's wrong with one documented command line ([] = it matches the real CLI)."""
    tokens = command.split()[1:]
    if not tokens or tokens[0].startswith("<"):
        return []
    if tokens[0].startswith("-"):
        top = {opt for action in cli.build_parser()._actions for opt in action.option_strings}
        return [] if tokens[0] in top else [f"{command!r}: unknown option {tokens[0]}"]
    name, rest = tokens[0], tokens[1:]
    if name not in parsers:
        return [f"{command!r}: there is no `sdr {name}` command"]
    parser = parsers[name]
    options = {opt: action for action in parser._actions for opt in action.option_strings}
    positional = {str(choice) for action in parser._actions
                  if not action.option_strings and action.choices for choice in action.choices}
    problems, expects_value = [], None
    for token in rest:
        if expects_value is not None:
            choices = {str(choice) for choice in expects_value.choices or ()}
            if "--section" in expects_value.option_strings:
                choices = set(SECTIONS)
            if choices and not PLACEHOLDER.match(token):
                problems += [f"{command!r}: {alt!r} isn't a valid value" for alt in token.split("|")
                             if alt not in choices]
            expects_value = None
        elif token.startswith("--"):
            action = options.get(token)
            if action is None:
                problems.append(f"{command!r}: `sdr {name}` has no {token} option")
            elif action.nargs != 0:
                expects_value = action
        elif not PLACEHOLDER.match(token):
            problems += [f"{command!r}: {alt!r} isn't accepted by `sdr {name}`" for alt in token.split("|")
                         if alt not in positional]
    return problems


def github_anchor(heading: str) -> str:
    """The #fragment GitHub gives a heading: lower case, punctuation dropped, spaces -> hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def is_gitignored(path: str) -> bool:
    """Whether this repo's .gitignore ignores `path` (relative, forward slashes).

    A deliberately small reading of the gitignore rules, enough for what this repo uses: bare
    names and globs match any path component (`*.log`, `*answers*.toml`, `data/`), patterns with
    a slash match the whole path (`config/profile.toml`), `!` un-ignores, and the last matching
    line wins. Pure Python so the check is the same on CI, in a tarball and without git."""
    ignored = False
    for line in read(".gitignore").splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negated = pattern.startswith("!")
        body = pattern.lstrip("!").rstrip("/")
        if "/" in body:
            body = body.lstrip("/")
            matched = fnmatch.fnmatchcase(path, body) or path.startswith(body + "/")
        else:
            matched = any(fnmatch.fnmatchcase(part, body) for part in path.split("/"))
        if matched:
            ignored = not negated
    return ignored


def example_answers() -> str:
    """The TOML agents copy from AGENTS.md: the first ```toml block on the page."""
    return read("AGENTS.md").split("```toml", 1)[1].split("```", 1)[0]


def run_example_setup(block: str) -> tuple[int, list[str], str, str, str]:
    """Run `sdr setup --answers` on `block` with fake dependencies, in a temp folder: nothing
    touches the network, a mailbox, the crontab or the real config.
    Returns (exit code, names of the dependencies that were called, output, .env text, profile text)."""
    calls: list[str] = []

    def fake(name, result):
        def fn(*_args, **_kwargs):
            calls.append(name)
            return result
        return fn

    brand = {"url": "https://acme.com", "name": "Acme", "tagline": "", "description": "", "logo_url": "",
             "brand_color": "#FF5500", "heading_color": "#111111", "text_color": "#222222",
             "background": "#FFF5EE", "found": {"name": True}, "error": ""}
    deps = Deps(detect_brand=fake("detect_brand", brand), guess_provider=fake("guess_provider", "gmail"),
                check_login=fake("check_login", {"smtp": True, "imap": True, "errors": []}),
                send_code=fake("send_code", "123456"),
                hash_password=fake("hash_password", "pbkdf2_sha256$1000$c2FsdA==$aGFzaA=="),
                install_schedule=fake("install_schedule", []),
                schedule_status=fake("schedule_status", {"installed": False}),
                send_sample=fake("send_sample", True), start_dashboard=fake("start_dashboard", None),
                find_leads=fake("find_leads", {}))
    environ = {"SDR_EMAIL_PASSWORD": "abcd efgh ijkl mnop", "SDR_DASHBOARD_PASSWORD": "correct horse battery",
               "SDR_BRAVE_API_KEY": "brave-test-key"}
    env_text = profile_text = ""
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
        answers_path = Path(tmp, "setup-answers.toml")
        answers_path.write_text(block, encoding="utf-8")
        out = io.StringIO()
        ui = UI(answers=load_answers(str(answers_path)), plain=True, interactive=False, collect_missing=True,
                out=out, environ=environ)
        code = run_setup(ui=ui, deps=deps, profile_path=os.path.join(tmp, "config", "profile.toml"),
                         env_path=os.path.join(tmp, ".env"), backup_dir=os.path.join(tmp, "backups"))
        if code == 0:
            env_text = Path(tmp, ".env").read_text(encoding="utf-8")
            profile_text = Path(tmp, "config", "profile.toml").read_text(encoding="utf-8")
    return code, calls, out.getvalue(), env_text, profile_text


class CommandsMatchTheCliTest(unittest.TestCase):
    def setUp(self):
        self.parsers = subcommands()

    def test_readme_and_agents_only_use_real_commands_options_and_values(self):
        for doc in ("README.md", "AGENTS.md"):
            text = read(doc)
            found = commands_in(text) + row_commands(text)
            self.assertGreater(len(found), 20, f"{doc}: expected a command table")
            problems = [problem for command in found for problem in command_problems(command, self.parsers)]
            self.assertEqual(problems, [], doc)

    def test_every_command_is_documented(self):
        for doc in ("README.md", "AGENTS.md"):
            text = read(doc)
            self.assertIn("| `sdr` |", text, f"{doc}: the home menu row is missing")
            missing = [name for name in self.parsers if not re.search(rf"`sdr {re.escape(name)}\b", text)]
            self.assertEqual(missing, [], f"{doc} doesn't mention these commands")

    def test_every_setup_section_is_named(self):
        for doc in ("README.md", "AGENTS.md"):
            text = read(doc)
            self.assertEqual([name for name in SECTIONS if f"`{name}`" not in text], [], doc)

    def test_the_checker_itself_catches_mistakes(self):
        self.assertTrue(command_problems("sdr frobnicate", self.parsers))
        self.assertTrue(command_problems("sdr doctor --verbose", self.parsers))
        self.assertTrue(command_problems("sdr schedule on|pause", self.parsers))
        self.assertTrue(command_problems("sdr test-email --reply maybe", self.parsers))
        self.assertTrue(command_problems("sdr setup --section billing", self.parsers))
        self.assertEqual(row_commands("| `sdr doctor` | Checks. `--online` tests the key |"), ["sdr doctor --online"])
        self.assertEqual(command_problems("sdr setup --section <name>", self.parsers), [])
        self.assertEqual(command_problems("sdr test-email --reply interested|question|not_now", self.parsers), [])


class AgentAnswersTest(unittest.TestCase):
    def test_every_setup_question_is_explained(self):
        text = read("AGENTS.md")
        for question in QUESTIONS:
            table, key = question.key.split(".", 1)
            self.assertIn(f"`[{table}]`", text, question.key)
            self.assertRegex(text, rf"`{re.escape(key)}(`| =)", f"{question.key} is not explained in AGENTS.md")

    def test_every_search_engine_setting_is_explained(self):
        for doc in ("README.md", "AGENTS.md"):
            text = read(doc)
            self.assertEqual([s for s in SEARCH_SETTINGS if f'"{s}"' not in text], [], doc)

    def test_example_answers_file_sets_up_offline(self):
        """The TOML in AGENTS.md is what agents copy: it must save a profile as written, keep the
        schedule off, and put the Brave key in .env (never in the answers file)."""
        code, calls, out, env_text, profile_text = run_example_setup(example_answers())
        self.assertEqual(code, 0, out)
        self.assertNotIn("install_schedule", calls)
        self.assertNotIn("start_dashboard", calls)
        self.assertIn("BRAVE_API_KEY=brave-test-key", env_text)
        self.assertIn('search_engine = "brave"', profile_text)
        self.assertNotIn("brave-test-key", out + profile_text)

    def test_leaving_install_out_of_an_answers_file_keeps_the_schedule_off(self):
        """AGENTS.md and `--print-questions` must both give the answers-file truth: `install`
        defaults to false, and only an explicit `install = true` starts a schedule that emails real
        people. An agent that trusts a documented "defaults to true" would omit the key and its
        user would wait for runs that never come."""
        block = example_answers().replace("install = false\n", "")
        self.assertNotIn("install", block, "the example must otherwise stay as agents copy it")
        code, calls, out, _env, _profile = run_example_setup(block)
        self.assertEqual(code, 0, out)
        self.assertNotIn("install_schedule", calls)
        text = read("AGENTS.md")
        self.assertNotRegex(text, r"`install`[^\n]*defaults? to `?true")
        self.assertRegex(text, r"`install`[^\n]*default `?false")
        install = next(question for question in QUESTIONS if question.key == "schedule.install")
        self.assertFalse(install.as_json()["default"], "--print-questions must report the answers-file default")

    def test_every_answers_file_the_docs_mention_is_gitignored(self):
        """Answers files hold a postal address, cities and the sender address. Whatever name the
        docs suggest for one (and the obvious variations) must never be committable, while the
        documented templates must stay tracked."""
        names = {"answers.toml", "my-answers.toml", "brand-answers.toml"}
        for doc in ("README.md", "AGENTS.md", "SECURITY.md", "CONTRIBUTING.md"):
            text = read(doc)
            names.update(re.findall(r"--answers\s+(\S+\.toml)", text))
            names.update(re.findall(r"[\w-]*answers[\w-]*\.toml", text))
        self.assertIn("setup-answers.toml", names)
        self.assertEqual([name for name in sorted(names) if not is_gitignored(name)], [])
        for template in ("config/profile.example.toml", ".env.example"):
            self.assertFalse(is_gitignored(template), template)

    def test_the_gitignore_reader_agrees_with_git_on_this_repos_rules(self):
        self.assertTrue(is_gitignored(".env"))
        self.assertFalse(is_gitignored(".env.example"))
        self.assertTrue(is_gitignored("config/profile.toml"))
        self.assertTrue(is_gitignored("config/do_not_contact.txt"))
        self.assertTrue(is_gitignored("data/automations.db"))
        self.assertTrue(is_gitignored("data/backups/20260101/profile.toml"))
        self.assertFalse(is_gitignored("config/profile.example.toml"))
        self.assertFalse(is_gitignored("core/setup_toml.py"))


class ReadmeTest(unittest.TestCase):
    def setUp(self):
        self.text = read("README.md")

    def test_hero_badges_and_install_lines(self):
        self.assertTrue(self.text.startswith("# Automated SDR by Fred\n"))
        self.assertIn(f"https://github.com/{REPO_SLUG}/actions/workflows/tests.yml/badge.svg", self.text)
        self.assertIn("license-MIT", self.text)
        self.assertIn("python-3.11", self.text)
        self.assertIn(f"curl -fsSL {RAW_URL}/install.sh | bash", self.text)
        self.assertIn(f"irm {RAW_URL}/install.ps1 | iex", self.text)
        self.assertIn("Built by", self.text.rstrip().splitlines()[-1])

    def test_relative_links_and_anchors_resolve(self):
        anchors = {github_anchor(h) for h in re.findall(r"^#{1,6} (.+)$", self.text, flags=re.M)}
        for target in re.findall(r"\]\(([^)\s]+)\)", self.text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if target.startswith("#"):
                self.assertIn(target[1:], anchors, f"README links to a missing heading: {target}")
            else:
                self.assertTrue((ROOT / target).exists(), f"README links to a missing file: {target}")

    def test_covers_what_people_need_to_know(self):
        for needle in ("Primary", "Promotions", "Brave", "DuckDuckGo", "Bing", "BRAVE_API_KEY", "rolls back",
                       "data/backups/", "CAN-SPAM", "GDPR", "robots.txt", "App Password", "basic authentication",
                       "Tls12", "command not found", "SDR_NO_SETUP=1", "AGENTS.md", "MIT"):
            self.assertIn(needle, self.text)

    def test_setup_section_does_not_promise_a_suggestion_for_every_question(self):
        """The website, mailing address, ideal client, pitch, cities and mailbox login are required
        and have no default, so "press Enter to accept" fails on the very first prompt. The README
        may only promise a suggestion for *most* questions while such questions exist."""
        open_questions = [q.key for q in QUESTIONS if q.required and q.default in (None, "")]
        self.assertIn("business.website", open_questions)
        self.assertNotIn("Every question has a suggestion", self.text)
        self.assertRegex(self.text, r"Most questions (have|come with) a suggestion")


class PrivacyPromisesTest(unittest.TestCase):
    """SECURITY.md and README list what the tool connects to and what CI checks; the files that
    keep those promises are checked here so the lists can't drift."""

    def test_dashboard_loads_nothing_from_third_parties_as_the_docs_say(self):
        """The 'only connects to' lists name no font or script CDN, and the dashboard (login page
        included, which renders before any password) must not add one: every page view would
        otherwise send the user's IP to a third party the docs never mention."""
        for doc in ("README.md", "SECURITY.md"):
            self.assertIn("no third-party fonts, scripts or styles", read(doc), doc)
        for page in ("dashboard/index.html", "dashboard/server.py"):
            source = read(page)
            for host in ("googleapis.com", "gstatic.com", "jsdelivr.net", "unpkg.com", "cdnjs.", "cloudflare.com"):
                self.assertNotIn(host, source, f"{page} loads from {host}")

    def test_ci_runs_on_every_push_and_pull_request_as_security_md_says(self):
        """SECURITY.md promises the offline suite, bandit and pip-audit on every push and pull
        request, on macOS, Linux and Windows. A `branches: [main]` filter would silently leave
        feature branches and tags without CI. PyYAML isn't a dependency, so the two-space-indented
        `on:` block is read as text."""
        workflow = read(".github/workflows/tests.yml")
        triggers = re.search(r"^on:\n((?:[ \t]+\S.*\n)+)", workflow, flags=re.M)
        self.assertIsNotNone(triggers, "tests.yml has no `on:` block")
        push = re.search(r"^  push:\n((?:    .*\n)*)", triggers.group(1), flags=re.M)
        self.assertIsNotNone(push, "tests.yml does not run on push")
        self.assertIn('branches: ["**"]', push.group(1))
        self.assertIn('tags: ["v*"]', push.group(1))
        self.assertRegex(triggers.group(1), r"(?m)^  pull_request:", "tests.yml does not run on pull requests")
        for needle in ("ubuntu-latest", "macos-latest", "windows-latest", "pip-audit -r requirements.txt",
                       "bandit -q -r"):
            self.assertIn(needle, workflow)
        security = read("SECURITY.md")
        for needle in ("Every push", "pull request", "macOS, Linux and Windows", "`bandit`", "`pip-audit`"):
            self.assertIn(needle, security)


class EnvAndChangelogTest(unittest.TestCase):
    def test_env_example_documents_the_brave_key_without_a_value(self):
        text = read(".env.example")
        self.assertIn("\nBRAVE_API_KEY=\n", text)
        self.assertIn("https://brave.com/search/api/", text)

    def test_changelog_release_mentions_the_search_engines(self):
        release = read("CHANGELOG.md").split("## [1.0.0]", 1)[1].split("\n## [", 1)[0]
        for needle in ("Brave Search", "DuckDuckGo", "Bing", "BRAVE_API_KEY", "search_engine"):
            self.assertIn(needle, release)


if __name__ == "__main__":
    unittest.main()
