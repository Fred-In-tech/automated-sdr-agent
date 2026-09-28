"""SDR pipeline metrics: the funnel, reply rates and who needs you — for `cli.py report`,
the daily digest email and the dashboard."""

from datetime import datetime, timedelta, timezone

from core.db import get_connection

HUMAN_INTENTS = ("interested", "question", "not_now", "not_interested", "unsubscribe", "wrong_person", "other")
POSITIVE_INTENTS = ("interested", "question")


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _register_intent_functions(conn) -> None:
    """is_human_reply(intent) / is_positive_reply(intent) as SQL functions.

    The intent lists stay single-source Python constants while every query below stays fixed SQL
    text (no IN-list assembled at runtime). NULL (no reply joined) is simply not in either list."""
    conn.create_function("is_human_reply", 1, HUMAN_INTENTS.__contains__)
    conn.create_function("is_positive_reply", 1, POSITIVE_INTENTS.__contains__)


def pipeline_stats(profile: dict | None = None) -> dict:
    conn = get_connection()
    _register_intent_functions(conn)
    q = lambda sql, *args: conn.execute(sql, args).fetchone()[0]  # noqa: E731
    now = datetime.now(timezone.utc)
    local_midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)

    by_status = dict(conn.execute("SELECT status, COUNT(*) FROM leads GROUP BY status").fetchall())
    contacted = q("SELECT COUNT(DISTINCT lead_id) FROM email_logs")
    replied = q("SELECT COUNT(DISTINCT lead_id) FROM inbound_messages WHERE is_human_reply(intent)")
    positive_leads = q("SELECT COUNT(DISTINCT lead_id) FROM inbound_messages WHERE is_positive_reply(intent)")
    steps = dict(conn.execute("SELECT COALESCE(step, 1), COUNT(*) FROM email_logs WHERE COALESCE(step, 1) > 0 "
                              "GROUP BY 1").fetchall())
    variants = [dict(r) for r in conn.execute("""
        SELECT l.variant,
               COUNT(DISTINCT l.id) AS contacted,
               COUNT(DISTINCT CASE WHEN is_human_reply(m.intent) THEN l.id END) AS replied,
               COUNT(DISTINCT CASE WHEN is_positive_reply(m.intent) THEN l.id END) AS positive
        FROM leads l LEFT JOIN inbound_messages m ON m.lead_id = l.id
        WHERE l.variant IS NOT NULL GROUP BY l.variant ORDER BY l.variant
    """).fetchall()]
    signatures = [dict(r) for r in conn.execute("""
        SELECT l.signature_variant AS variant,
               COUNT(DISTINCT l.id) AS contacted,
               COUNT(DISTINCT CASE WHEN is_human_reply(m.intent) THEN l.id END) AS replied,
               COUNT(DISTINCT CASE WHEN is_positive_reply(m.intent) THEN l.id END) AS positive
        FROM leads l LEFT JOIN inbound_messages m ON m.lead_id = l.id
        WHERE l.signature_variant IS NOT NULL GROUP BY l.signature_variant ORDER BY l.signature_variant
    """).fetchall()]
    hot = [dict(r) for r in conn.execute("""
        SELECT l.company, l.email, l.status, m.intent, m.excerpt, m.received_at
        FROM inbound_messages m JOIN leads l ON l.id = m.lead_id
        WHERE is_positive_reply(m.intent) ORDER BY m.id DESC LIMIT 10
    """).fetchall()]
    stats = {
        "leads_found": sum(v for k, v in by_status.items() if k not in ("disqualified", "invalid_email")),
        "disqualified": by_status.get("disqualified", 0),
        "waiting_first_email": by_status.get("new", 0),
        "in_sequence": by_status.get("contacted", 0),
        "finished_no_reply": by_status.get("no_response", 0),
        "interested": by_status.get("interested", 0),
        "not_now": by_status.get("not_now", 0),
        "nurtured": by_status.get("nurtured", 0),
        "opted_out": by_status.get("unsubscribed", 0) + by_status.get("not_interested", 0),
        "bounced": by_status.get("bounced", 0),
        "contacted": contacted,
        "replied": replied,
        "positive": positive_leads,
        "reply_rate": _pct(replied, contacted),
        "positive_rate": _pct(positive_leads, contacted),
        "bounce_rate": _pct(by_status.get("bounced", 0), contacted),
        "emails_sent": q("SELECT COUNT(*) FROM email_logs"),
        "sent_today": q("SELECT COUNT(*) FROM email_logs WHERE sent_at >= ?",
                        local_midnight.astimezone(timezone.utc).isoformat()),
        "sent_7d": q("SELECT COUNT(*) FROM email_logs WHERE sent_at >= ?", (now - timedelta(days=7)).isoformat()),
        "follow_ups_due_24h": q("SELECT COUNT(*) FROM leads WHERE status = 'contacted' AND next_touch_at <= ?",
                                (now + timedelta(days=1)).isoformat()),
        "emails_by_step": {int(k): v for k, v in steps.items()},
        "check_ins_sent": q("SELECT COUNT(*) FROM email_logs WHERE step = 0"),
        "autoresponders_ignored": q("SELECT COUNT(*) FROM inbound_messages WHERE intent = 'auto_reply'"),
        "hot_leads": hot,
        "subject_ab_test": [{**v, "reply_rate": _pct(v["replied"], v["contacted"])} for v in variants],
        "signature_ab_test": [{**v, "reply_rate": _pct(v["replied"], v["contacted"])} for v in signatures],
        "bounce_alarm": None,
    }
    if profile:
        from core.outreach_rules import bounce_alarm
        stats["bounce_alarm"] = bounce_alarm(conn.cursor(), profile)
    conn.close()
    return stats


def render_report(stats: dict, product_name: str = "", profile: dict | None = None) -> str:
    step_line = ", ".join(f"#{k}: {v}" for k, v in sorted(stats["emails_by_step"].items())) or "none yet"
    lines = [
        f"{product_name + ' ' if product_name else ''}SDR pipeline".strip(),
        "",
        "FUNNEL",
        f"  Leads in pipeline         {stats['leads_found']}   (+{stats['disqualified']} rejected as poor fit)",
        f"  Contacted                 {stats['contacted']}",
        f"  Replied (real people)     {stats['replied']}   ({stats['reply_rate']}%)",
        f"  Interested / questions    {stats['positive']}   ({stats['positive_rate']}%)",
        "",
        "RIGHT NOW",
        f"  Waiting for first email   {stats['waiting_first_email']}",
        f"  In follow-up sequence     {stats['in_sequence']}   ({stats['follow_ups_due_24h']} follow-ups due in 24h)",
        f"  Finished, no reply        {stats['finished_no_reply']}",
        f"  Not now / checked in      {stats['not_now']} / {stats['nurtured']}",
        f"  Opted out                 {stats['opted_out']}",
        f"  Bounced                   {stats['bounced']}   ({stats['bounce_rate']}% — keep under 3%)",
        "",
        "SENDING",
        f"  Today {stats['sent_today']} · last 7 days {stats['sent_7d']} · all time {stats['emails_sent']}",
        f"  By sequence step: {step_line} · \"not now\" check-ins: {stats['check_ins_sent']}",
        f"  Autoresponders ignored: {stats['autoresponders_ignored']}",
    ]
    if stats["subject_ab_test"]:
        subjects = ((profile or {}).get("outreach", {}) or {}).get("subject_variants") or []
        lines += ["", "SUBJECT A/B TEST (reply rate per subject line)"]
        for v in stats["subject_ab_test"]:
            index = "ABCDEFGH".find(v["variant"])
            label = f'"{subjects[index]}"' if 0 <= index < len(subjects) else ""
            lines.append(f"  {v['variant']}: {v['replied']}/{v['contacted']} replied ({v['reply_rate']}%), "
                         f"{v['positive']} positive  {label}")
    if stats["signature_ab_test"]:
        lines += ["", "SIGNATURE A/B TEST (plain text vs. small logo in the signature)"]
        for v in stats["signature_ab_test"]:
            lines.append(f"  {v['variant']:<6} {v['replied']}/{v['contacted']} replied ({v['reply_rate']}%), "
                         f"{v['positive']} positive")
    if stats["bounce_alarm"]:
        lines = [f"⚠️  {stats['bounce_alarm']}", ""] + lines
    if stats["hot_leads"]:
        lines += ["", "HOT LEADS (reply to these personally)"]
        for lead in stats["hot_leads"]:
            excerpt = " ".join((lead["excerpt"] or "").split())[:120]
            lines.append(f"  • {lead['company']} <{lead['email']}> [{lead['intent']}] {lead['received_at'][:10]}")
            if excerpt:
                lines.append(f"    \"{excerpt}\"")
    return "\n".join(lines)
