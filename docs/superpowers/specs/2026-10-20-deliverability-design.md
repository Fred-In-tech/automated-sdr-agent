# Deliverability & lead quality (v1.5.0): design

Status: approved by the owner on 2026-10-20 (approaches A + B; borderline-only lead review;
warn + slow down on serious problems).

## Why
Research ranked "cold email lands in spam even when set up right" first. Since 2024 Gmail and
Yahoo (and Microsoft from May 2025 for large senders) require SPF/DKIM, aligned DMARC for bulk
senders, and low complaint rates. Owners rarely know their DNS state. Our own baseline: ~80% of
found leads were off-target, which hurts replies and reputation.

## 1. Deliverability check (`core/deliverability.py`)
- Domain = the sending address's domain (alias `sender.from_email` if set, else `SMTP_USER`).
- Provider (google_workspace/gmail, outlook, hostinger, zoho, other) from the saved SMTP host.
- Checks, each OK / Warning / Problem with a plain fix and, where relevant, the record to add:
  - **MX**: present (replies and bounces need it). Missing = Problem.
  - **SPF**: exactly one `v=spf1` TXT; includes the provider's include (Google `_spf.google.com`,
    Microsoft `spf.protection.outlook.com`, Hostinger `_spf.mail.hostinger.com`, Zoho `zoho.`);
    `+all` = Problem; missing = Problem; two records = Problem; missing provider include = Warning.
  - **DKIM**: TXT at `<selector>._domainkey.<domain>` with a non-empty `p=`, trying the provider's
    selectors first (Google `google`; Microsoft `selector1`, `selector2`; Hostinger
    `hostingermail-a`, `hostingermail-b`; Zoho `zmail`, `zoho`) then common ones (`default`, `dkim`,
    `mail`, `k1`, `s1`, `s2`). None found = Problem, unless a sample email proved `dkim=pass`.
  - **DMARC**: `_dmarc.<domain>` `v=DMARC1`. Missing = Warning; `p=none` = Warning (works, but
    gives no protection); quarantine/reject = OK.
  - Consumer Gmail/Outlook addresses (gmail.com, outlook.com...) skip DNS checks (the provider owns
    the domain) and get an info note about daily limits.
- Result cached in `data/deliverability.json` (owner-only): checked_at, domain, checks, status
  (`ok` | `warning` | `problem`), plus the latest sample verdict. Refreshed at most once a day,
  at the start of the email step; `Check again` refreshes on demand. DNS failures never block
  sending: an unreachable resolver = "not checked" (status unchanged).
- **B. Sample proof:** after `sdr test-email` to the owner's own address, read the delivered
  copy's `Authentication-Results` (IMAP, by Message-ID, a few short retries) and store
  `{spf, dkim, dmarc, seen_by, checked_at}`. A sample `dkim=pass` overrides "no DKIM selector
  found" (custom selector). Parsing is a pure function, tested on real header shapes.

## 2. Warn + slow down
- Status `problem` caps the daily send limit at 5 (`DELIVERABILITY_CAP`) until a later check is
  not `problem`. Shown in `sdr doctor`, an Overview banner + Deliverability card, the daily
  digest, and the email step's log. Warnings never cap.

## 3. Sending-domain note
- If the sending domain equals the website's domain, an info item explains the risk and the
  common fix (a second domain). Info only.

## 4. Borderline lead review
- `targeting.auto_send_score` (default 70). Qualified leads with fit >= auto_send_score are
  `new` (emailed as today); between `min_fit_score` and it get status `review` and wait.
- Needs-you card lists them with "why this lead" (score reasons + AI fit line). Approve -> `new`;
  Reject -> `disqualified` ("rejected by you"). `review` leads are never picked for sending.
  Unreviewed for 7 days -> `disqualified` ("not reviewed in 7 days"). Imported leads (fit 70)
  are unaffected. `sdr review` does the same in the terminal.

## 5. Results funnel (`core/funnel.py`)
- first emails sent -> delivered (not bounced) -> replied (human) -> interested -> meeting
  (Follow-ups stage meeting/proposal/won) -> won; counts + step rates. In `sdr report`, the
  daily digest, and an Overview card. Never uses opens.

## Non-goals
Inbox warm-up, blacklist APIs, third-party spam testers, automatic DNS changes.

## Testing
All DNS via an injected resolver fake; IMAP via the existing fakes; header parser tests; cap,
review band, expiry, funnel math, API auth/CSRF, docs tables. No real network in tests.
