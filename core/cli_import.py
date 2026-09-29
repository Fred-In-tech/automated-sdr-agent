"""`sdr import leads.csv`: bring your own leads into the pipeline. Sends nothing."""

from __future__ import annotations

import os

from core.lead_import import MAX_IMPORT_BYTES, TEMPLATE_CSV, ImportSummary, LeadImportError, import_csv_text
from core.product import CLI_NAME
from core.tui import UI


def read_csv_file(path: str) -> str:
    """The file as text. Excel on Windows saves "CSV" in the ANSI code page, not UTF-8."""
    if not os.path.isfile(path):
        raise LeadImportError(f"No file at {path}. Check the path, or drag the file into the terminal window.")
    if os.path.getsize(path) > MAX_IMPORT_BYTES:
        raise LeadImportError("That file is larger than 1 MB. Split it into smaller files and import them one by one.")
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def summary_lines(summary: ImportSummary) -> list[str]:
    """Plain-English result, shared by the terminal and the tests."""
    verb = "would be imported" if summary.dry_run else "imported"
    noun = "lead" if summary.imported == 1 else "leads"
    lines = [f"{summary.imported} {noun} {verb} (of {summary.total} in the file)."]
    if summary.duplicates:
        lines.append(f"{summary.duplicates} skipped: already in your leads, or listed twice.")
    if summary.blocked:
        lines.append(f"{summary.blocked} skipped: on your do-not-contact list.")
    if summary.invalid:
        lines.append(f"{summary.invalid} skipped: the email address can't be used.")
    return lines


def cmd_import(ui: UI, path: str | None, dry_run: bool, no_verify: bool, template: bool, profile: dict) -> int:
    if template:
        print(TEMPLATE_CSV, end="")
        return 0
    if not path:
        ui.error(f"Which file? Try `{CLI_NAME} import leads.csv`, or `{CLI_NAME} import --template` for an example.")
        return 2
    try:
        text = read_csv_file(path)
        if no_verify:
            summary = import_csv_text(text, profile, dry_run=dry_run, check_mail_server=False)
        else:
            with ui.spinner("Checking each address can receive email"):
                summary = import_csv_text(text, profile, dry_run=dry_run)
    except LeadImportError as exc:
        ui.error(str(exc))
        return 1
    lines = summary_lines(summary)
    (ui.success if summary.imported else ui.warn)(lines[0])
    for line in lines[1:]:
        ui.info(line)
    for problem in summary.problems:
        ui.info("  " + problem)
    more = summary.as_dict()["more_problems"]
    if more:
        ui.info(f"  ...and {more} more.")
    if summary.imported and not summary.dry_run:
        ui.info(f"Nothing was emailed. They go out with your next run, best first: see them with `{CLI_NAME} report` "
                f"or in your dashboard.")
    return 0
