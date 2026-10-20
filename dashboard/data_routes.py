"""Dashboard routes for the pipeline's own data: leads, sent emails, the activity log, CSV
import/export and resuming after a bounce pause. Mixed into DashboardHandler, so login, CSRF
and the local-only checks apply as everywhere else."""

from __future__ import annotations

import json
import os

from bots.leadgen_pipeline import export_leads_to_csv
from core.config import ProfileError, load_profile
from core.db import get_connection, get_db_stats, get_recent_logs, log_event
from core.outreach_rules import resume_sending

IMPORT_PATH = "/api/import-leads"


class DataRoutesMixin:
    DATA_GET_ROUTES = {"/api/stats": "_api_stats", "/api/leads": "_api_leads", "/api/emails": "_api_emails",
                       "/api/logs": "_api_logs", "/api/export-csv": "_api_export_csv",
                       "/api/import-template": "_api_import_template"}
    DATA_POST_ROUTES = {IMPORT_PATH: "_api_import_leads", "/api/resume-sending": "_api_resume_sending"}

    def _api_import_leads(self) -> None:
        """Import leads from CSV text the page read from a file. Saves leads; sends nothing."""
        from core.lead_import import LeadImportError, import_csv_text
        try:
            payload = json.loads(self.body.decode("utf-8"))
            text, dry_run = payload["csv"], bool(payload.get("dry_run"))
            if not isinstance(text, str):
                raise ValueError
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            self._send_json({"success": False, "message": "Choose a CSV file to import."}, 400)
            return
        try:
            summary = import_csv_text(text, load_profile(), dry_run=dry_run)
        except (LeadImportError, ProfileError) as e:
            self._send_json({"success": False, "message": str(e)}, 400)
            return
        result = summary.as_dict()
        if not dry_run:
            log_event("Dashboard", "ImportLeads", "success",
                      f"Imported {result['imported']} leads ({result['skipped']} skipped).")
        self._send_json({"success": True, **result})

    def _api_resume_sending(self) -> None:
        try:
            resumed = resume_sending(load_profile())
        except ProfileError as e:
            self._send_json({"success": False, "message": str(e)}, 400)
            return
        self._send_json({"success": True, "resumed": resumed})

    def _api_stats(self) -> None:
        self._send_json(get_db_stats())

    def _query(self, sql: str) -> list[dict]:
        conn = get_connection()
        try:
            return [dict(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()

    def _api_leads(self) -> None:
        # Real prospects first (best fit on top); rejected sites last
        rows = self._query("""SELECT * FROM leads ORDER BY status IN ('disqualified', 'invalid_email'),
                              COALESCE(fit_score, 0) DESC, id DESC LIMIT 100""")
        for row in rows:
            if row.get("enriched_info"):
                try:
                    row["enriched_info"] = json.loads(row["enriched_info"])
                except ValueError:
                    pass
            try:
                row["fit_reasons_list"] = json.loads(row.get("fit_reasons") or "[]")
            except ValueError:
                row["fit_reasons_list"] = []
        self._send_json({"leads": rows})

    def _api_emails(self) -> None:
        self._send_json({
            "campaigns": self._query("SELECT * FROM email_campaigns ORDER BY id DESC"),
            "logs": self._query("SELECT * FROM email_logs ORDER BY id DESC LIMIT 50"),
        })

    def _api_logs(self) -> None:
        self._send_json({"logs": get_recent_logs(50)})

    def _api_import_template(self) -> None:
        from core.lead_import import TEMPLATE_CSV
        self._send_body(TEMPLATE_CSV.encode("utf-8"), "text/csv", 200,
                        {"Content-Disposition": 'attachment; filename="leads-template.csv"'})

    def _api_export_csv(self) -> None:
        csv_path = export_leads_to_csv()
        if not os.path.exists(csv_path):
            self._send_json({"error": "Export failed"}, 500)
            return
        with open(csv_path, "rb") as f:
            content = f.read()
        self._send_body(content, "text/csv", 200,
                        {"Content-Disposition": 'attachment; filename="leads_export.csv"'})
