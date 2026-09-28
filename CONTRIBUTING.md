# Contributing to Automated SDR

Thanks for helping! This is a small, friendly project. Bug reports, fixes, docs and ideas are all
welcome.

## Before you start

- **Found a security problem?** Don't open an issue. Follow [SECURITY.md](SECURITY.md).
- **Want a bigger change?** Open an issue first and describe the problem you want to solve, so
  we can agree on the approach before you spend time on it.
- **Using an AI coding agent?** Point it at [AGENTS.md](AGENTS.md), which has the project map and
  the safety rules.

## Development setup

```bash
git clone https://github.com/Fred-In-tech/automated-sdr-agent.git
cd automated-sdr-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest       # Windows: .venv\Scripts\pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/ -q                   # Windows: .venv\Scripts\python -m pytest tests/ -q
bin/sdr --help                                         # Windows: bin\sdr.cmd --help
```

You need Python 3.11 or newer. You don't need a mailbox or a profile to run the tests.

## Ground rules

- **The tests never touch the outside world.** No network, no real SMTP/IMAP, no scraping, and
  no changes to the real crontab or Task Scheduler. Mock them, and inject dependencies where you
  can. Look at `tests/test_sequence.py` for the style.
- **Never commit personal data or secrets**: `.env`, `config/profile.toml`,
  `config/do_not_contact.txt`, any `*answers*.toml` (such as `setup-answers.toml`) or anything in
  `data/`.
- **Test fixtures are made up.** Use reserved domains (`.test`, `.example`, `example.com`) and
  invented businesses, people and replies. Never paste a real lead, a real person's name or a real
  email from your inbox: once it's in a public test file it stays in git history.
- **Keep the compliance features intact**: the postal address, unsubscribe line, do-not-contact
  list, send limits, bounce pause and robots.txt.
- **Standard library first.** Adding a dependency needs a good reason, a version floor in
  `requirements.txt`, and a clean `pip-audit`.
- **It has to work everywhere**: Python 3.11, 3.12 and 3.13 on macOS, Linux and Windows.
  - Don't put backslashes or the enclosing quote character inside f-string expressions (that's
    only legal from 3.12).
  - Don't import `fcntl` or `termios` at module top level. Use `core/locking.py`.
  - Use `pathlib`/`os.path`, open text files with `encoding="utf-8"`, and never use `shell=True`.
- **Style**: small functions, type hints in the `str | None` style, and docstrings that explain
  *why*. Library code doesn't `print`; only the CLI and terminal UI talk to the user.
- **Shell scripts** (`install.sh`, `bin/sdr`, `run_cron_pipeline.sh`) must run on the bash 3.2
  that macOS ships. Quote every variable and keep `set -euo pipefail`. `install.ps1` must work on
  Windows PowerShell 5.1 when piped into `iex`, so never call `exit` in it.

## Before you open a pull request

```bash
python3 -m pytest tests/ -q
pip install bandit pip-audit
bandit -q -r core bots dashboard cli.py runner.py -ll
pip-audit -r requirements.txt
```

CI runs the same checks on every push and pull request, on macOS, Linux and Windows. If `bandit`
flags a false positive, add
`# nosec BXXX` with a short reason on the same line; never add it without one.

- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
  `fix:`, `docs:`, `test:`, `refactor:`, `chore:` and so on.
- Keep pull requests focused, and add or update tests with every behaviour change.
- Docs are part of the change. A new command, option or setup question goes in the tables in
  `README.md` and `AGENTS.md` (setup questions live in `core/setup_questions.py`).
  `tests/test_packaging.py` and `tests/test_docs.py` fail when the docs and the CLI disagree.
- User-visible changes go under an `## [Unreleased]` heading in `CHANGELOG.md`.

## Releases (maintainers)

1. Move the `Unreleased` notes into a new `## [X.Y.Z] - YYYY-MM-DD` section of `CHANGELOG.md`.
2. Bump `VERSION` to `X.Y.Z`.
3. Merge to `main`, then tag it: `git tag vX.Y.Z && git push origin vX.Y.Z`.

Installed copies only ever fast-forward to release tags, so a tag is a promise that `main` passes
CI at that commit.

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
