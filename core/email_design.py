"""HTML email styles ([email_design] style in config/profile.toml).

style = "personal" (default): looks like an email typed in Gmail — no images, no layout,
    links as plain text. Best inbox placement for 1:1 sales email (Primary, not Promotions).
style = "branded": logo, brand colours, button, feature pills. Looks like marketing, so
    Gmail is more likely to file it under Promotions.

The branded layout:

Email-client-safe on purpose: table layout, inline styles, a "bulletproof" button that
works in Outlook, a text wordmark next to the logo (so the brand still shows when images
are blocked), hidden preview text so the inbox shows your first line instead of the header,
and a plain-text version is always sent alongside.
"""

import html
import re
import zlib

BUTTON_TOKEN = "@@BUTTON@@"
URL_RE = re.compile(r"https?://[^\s<]+")

DEFAULTS = {
    "brand_color": "#3B82F6",
    "text_color": "#1B2B4D",
    "heading_color": "#0F1729",
    "muted_color": "#6B7A90",
    "background": "#F3F6FB",
    "card_border": "#E3EAF5",
    "chip_background": "#EAF2FF",
    "font": "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
}


def email_style(profile: dict) -> str:
    """"personal" or "branded". Older profiles with only `enabled = true` mean branded."""
    design = profile.get("email_design", {})
    return design.get("style") or ("branded" if design.get("enabled") else "personal")


def design_enabled(profile: dict) -> bool:
    return email_style(profile) == "branded"


def _plain_link(match) -> str:
    url, trail = match.group(0), ""
    while url and url[-1] in ".,;:!?)":
        url, trail = url[:-1], url[-1] + trail
    label = re.sub(r"^https?://", "", url).split("?")[0].rstrip("/")
    return f'<a href="{url}">{label}</a>{trail}'


def signature_variant(profile: dict, email: str) -> str:
    """"logo" or "plain" for this lead's signature ([email_design] signature_logo_url).
    With signature_logo_test = true, leads are split 50/50 (stable per address) so the
    report can compare reply rates; otherwise everyone gets the logo."""
    design = profile.get("email_design", {})
    if not design.get("signature_logo_url"):
        return "plain"
    if not design.get("signature_logo_test", True):
        return "logo"
    return "logo" if zlib.crc32(f"signature:{email.lower()}".encode()) % 2 else "plain"


def _signature_with_logo(block: str, logo_url: str, brand: str) -> str:
    """The same sign-off text, with a small logo beside it — like a typical sales rep's signature."""
    lines = "<br>".join(html.escape(line.strip(), quote=False) for line in block.strip().split("\n"))
    return (f'<div><table cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td style="padding-right:10px; vertical-align:middle;">'
            f'<img src="{html.escape(logo_url, quote=True)}" width="36" height="36" alt="{html.escape(brand)}" '
            f'style="display:block; border:0;"></td>'
            f'<td style="vertical-align:middle;">{lines}</td></tr></table></div>')


def render_personal_html(text_body: str, button: dict | None, footer_lines: list,
                         sign_off: str = "", logo_url: str = "", brand: str = "") -> str:
    """Minimal HTML shaped like a message written in Gmail: no styling or layout.
    `text_body` may contain BUTTON_TOKEN, which becomes a plain link. With `logo_url`, the
    sign-off gets a small logo beside it (the signature A/B test)."""
    blocks = []
    for block in text_body.strip().split("\n\n"):
        if block.strip() == BUTTON_TOKEN:
            if button:
                blocks.append(f'<div><a href="{html.escape(button["url"], quote=True)}">'
                              f'{html.escape(button["text"])}</a></div>')
            continue
        if logo_url and sign_off and block.strip() == sign_off.strip():
            blocks.append(_signature_with_logo(block, logo_url, brand))
            continue
        body = URL_RE.sub(_plain_link, html.escape(block, quote=False)).replace("\n", "<br>")
        blocks.append(f"<div>{body}</div>")
    if footer_lines:
        blocks.append("<div>" + "<br>".join(html.escape(line, quote=False) for line in footer_lines) + "</div>")
    return '<div dir="ltr">' + "<div><br></div>".join(blocks) + "</div>"


def _style(profile: dict) -> dict:
    return {**DEFAULTS, **{k: v for k, v in profile.get("email_design", {}).items() if k in DEFAULTS}}


def _link(match, color: str) -> str:
    url, trail = match.group(0), ""
    while url and url[-1] in ".,;:!?)":
        url, trail = url[:-1], url[-1] + trail
    label = re.sub(r"^https?://", "", url).split("?")[0].rstrip("/")
    return (f'<a href="{url}" style="color:{color}; text-decoration:underline; font-weight:600;">'
            f"{label}</a>{trail}")


def _signature(block: str, s: dict) -> str:
    """Sign-off as a signature: name in bold, the rest (company, title) muted."""
    name, *rest = [html.escape(line.strip()) for line in block.strip().split("\n")]
    details = "".join(f'<div style="color:{s["muted_color"]}; font-size:14px;">{line}</div>' for line in rest)
    return (f'<div style="margin:4px 0 18px 0;"><div style="font-weight:700; color:{s["heading_color"]};">'
            f"{name}</div>{details}</div>")


LIST_ITEM_RE = re.compile(r"^\s*(?:[-•]|(\d+)[.)])\s+(.*)$")


def _inline(text: str, s: dict) -> str:
    return URL_RE.sub(lambda m: _link(m, s["brand_color"]), html.escape(text, quote=False))


def _list_items(items: list, s: dict) -> str:
    """'- item' lines become a ✓ checklist; '1. step' lines become numbered steps."""
    rows = []
    for number, content in items:
        marker = (f'<span style="display:inline-block; width:22px; height:22px; line-height:22px; border-radius:50%; '
                  f'background:{s["brand_color"]}; color:#ffffff; font-size:12px; font-weight:700; text-align:center;">'
                  f"{number}</span>") if number else f'<span style="color:{s["brand_color"]}; font-weight:800;">&#10003;</span>'
        rows.append(f'<tr><td style="width:30px; vertical-align:top; padding:2px 0 8px 0;">{marker}</td>'
                    f'<td style="vertical-align:top; padding:2px 0 8px 0;">{_inline(content, s)}</td></tr>')
    return (f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:0 0 16px 0;">'
            f'{"".join(rows)}</table>')


def _block(block: str, s: dict) -> str:
    """A paragraph, possibly with an intro line followed by list items."""
    out, text_lines, items = [], [], []
    for line in block.split("\n"):
        match = LIST_ITEM_RE.match(line)
        if match:
            if text_lines:
                out.append(f'<p style="margin:0 0 10px 0;">{"<br>".join(_inline(t, s) for t in text_lines)}</p>')
                text_lines = []
            items.append((match.group(1), match.group(2)))
        else:
            if items:
                out.append(_list_items(items, s))
                items = []
            text_lines.append(line)
    if text_lines:
        out.append(f'<p style="margin:0 0 16px 0;">{"<br>".join(_inline(t, s) for t in text_lines)}</p>')
    if items:
        out.append(_list_items(items, s))
    return "".join(out)


def _paragraphs(text: str, s: dict, sign_off: str = "") -> str:
    blocks = []
    for block in text.strip().split("\n\n"):
        if block.strip() == BUTTON_TOKEN:
            blocks.append(BUTTON_TOKEN)
            continue
        if sign_off and block.strip() == sign_off.strip():
            blocks.append(_signature(block, s))
            continue
        blocks.append(_block(block, s))
    return "\n".join(blocks)


def _button(text: str, url: str, s: dict) -> str:
    safe_url, safe_text = html.escape(url, quote=True), html.escape(text)
    return f"""<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:8px 0 24px 0;">
  <tr><td align="center" bgcolor="{s['brand_color']}" style="border-radius:8px;">
    <a href="{safe_url}" style="display:inline-block; padding:13px 24px; font-family:{s['font']}; font-size:15px; font-weight:700; color:#ffffff; text-decoration:none; border-radius:8px;">{safe_text} &rarr;</a>
  </td></tr>
</table>"""


def _chips(highlights: list, s: dict) -> str:
    if not highlights:
        return ""
    chips = "".join(
        f'<span style="display:inline-block; margin:0 6px 6px 0; padding:5px 11px; border-radius:999px; '
        f'background:{s["chip_background"]}; color:{s["brand_color"]}; font-size:12px; font-weight:600;">'
        f"{html.escape(h)}</span>" for h in highlights)
    return f'<div style="margin:4px 0 20px 0;">{chips}</div>'


def render_branded_html(profile: dict, text_body: str, button: dict | None, footer_lines: list,
                        highlights: list | None = None) -> str:
    """`text_body` may contain BUTTON_TOKEN on its own paragraph where the button goes."""
    s = _style(profile)
    design = profile.get("email_design", {})
    sender = profile["sender"]
    brand = html.escape(sender["product_name"])
    logo_url = design.get("logo_url", "")
    tagline = html.escape(design.get("tagline", ""))

    content = _paragraphs(text_body, s, sender.get("sign_off", ""))
    # the button (with the feature pills right under it) goes where {{button}} is, else at the end
    call_to_action = (_button(button["text"], button["url"], s) if button else "") + _chips(highlights or [], s)
    content = (content.replace(BUTTON_TOKEN, call_to_action) if BUTTON_TOKEN in content
               else content + call_to_action)

    first_line = next((b for b in text_body.split("\n\n")[1:] if b.strip() and BUTTON_TOKEN not in b), "")
    preheader = html.escape(" ".join(first_line.split())[:140])
    logo = (f'<img src="{html.escape(logo_url, quote=True)}" width="32" height="32" alt="{brand}" '
            f'style="display:block; border:0; border-radius:8px;">') if logo_url else ""
    footer = "<br>".join(html.escape(line) for line in footer_lines)
    tagline_html = (f'<div style="margin-bottom:6px; font-weight:600;">{brand} · {tagline}</div>'
                    if tagline else "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{brand}</title>
</head>
<body style="margin:0; padding:0; background:{s['background']};">
<div style="display:none; max-height:0; overflow:hidden; opacity:0; color:transparent;">{preheader}&#8199;&#65279;&#847;&#8199;&#65279;&#847;</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:{s['background']};">
  <tr><td align="center" style="padding:28px 12px;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;">
      <tr><td style="padding:0 4px 16px 4px;">
        <table role="presentation" cellspacing="0" cellpadding="0" border="0"><tr>
          <td style="padding-right:10px;">{logo}</td>
          <td style="font-family:{s['font']}; font-size:18px; font-weight:800; color:{s['heading_color']}; letter-spacing:-0.2px;">{brand}</td>
        </tr></table>
      </td></tr>
      <tr><td style="background:#ffffff; border:1px solid {s['card_border']}; border-top:4px solid {s['brand_color']}; border-radius:12px; padding:30px 32px 14px 32px; font-family:{s['font']}; font-size:16px; line-height:1.6; color:{s['text_color']};">
{content}
      </td></tr>
      <tr><td style="padding:18px 8px 0 8px; font-family:{s['font']}; font-size:12px; line-height:1.6; color:{s['muted_color']}; text-align:center;">
        {tagline_html}{footer}
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""
