"""The dashboard's sign-in page, in the business's own colours and logo (served by dashboard/server.py).

It is shown before anyone has signed in, so it is entirely self-contained: no fonts, scripts or
styles are fetched from other sites (SECURITY.md lists everything the tool connects to, and a
third-party font host isn't on it). The only outside resource is the business's own logo, which
is on the business's own website.
"""

import html
import re
import string

from core.email_design import DEFAULTS as DESIGN_DEFAULTS
from core.product import BRAND_COLOR, DISPLAY_NAME, PRODUCT_NAME, version

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _hex(value, fallback: str) -> str:
    return value if isinstance(value, str) and HEX_COLOR_RE.match(value) else fallback


def _text_on(hex_color: str) -> str:
    """White or near-black text, whichever reads better on `hex_color` (light brand colours
    like yellow would make white button text unreadable)."""
    def channel(i: int) -> float:
        c = int(hex_color[i:i + 2], 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    luminance = 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)
    return "#0F1729" if luminance > 0.45 else "#FFFFFF"


def login_brand(profile: dict | None) -> dict:
    """Name, logo and colours for the login page from [sender]/[email_design], with safe
    fallbacks so a half-finished profile still renders a proper page."""
    sender = (profile or {}).get("sender", {})
    design = (profile or {}).get("email_design", {})
    logo = design.get("logo_url") or design.get("signature_logo_url") or ""
    return {
        "name": sender.get("product_name") or PRODUCT_NAME,
        "logo_url": logo if isinstance(logo, str) and logo.startswith("https://") else "",
        "brand": _hex(design.get("brand_color"), BRAND_COLOR),
        "heading": _hex(design.get("heading_color"), DESIGN_DEFAULTS["heading_color"]),
        "text": _hex(design.get("text_color"), DESIGN_DEFAULTS["text_color"]),
        "background": _hex(design.get("background"), DESIGN_DEFAULTS["background"]),
        "tagline": str(design.get("tagline") or ""),
    }


LOGIN_TEMPLATE = string.Template("""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="robots" content="noindex">
  <title>Sign in · $name</title>
  $favicon
  <style>
    :root { --brand: $brand; --on-brand: $on_brand; --ink: $heading; --text: $text; --bg: $background;
            --muted: #64748B; --border: #E3EAF5; --danger: #B91C1C; }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { min-height: 100vh; display: grid; place-items: center; padding: 1.5rem; color: var(--text);
           font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
           background-color: var(--bg);
           background-image: radial-gradient(900px 500px at 0% -10%, color-mix(in srgb, var(--brand) 14%, transparent), transparent 60%),
                             radial-gradient(700px 420px at 110% 110%, color-mix(in srgb, var(--brand) 9%, transparent), transparent 60%); }
    main { width: 100%; max-width: 384px; }
    .card { background: #fff; border: 1px solid var(--border); border-radius: 20px; padding: 2.25rem 2rem 1.75rem;
            box-shadow: 0 1px 2px rgba(15, 23, 41, 0.04), 0 18px 48px rgba(15, 23, 41, 0.10); }
    .mark { width: 56px; height: 56px; border-radius: 15px; overflow: hidden; background: var(--brand);
            display: grid; place-items: center; margin-bottom: 1.35rem;
            box-shadow: 0 8px 20px color-mix(in srgb, var(--brand) 32%, transparent); }
    .mark img { width: 100%; height: 100%; object-fit: cover; display: block; background: #fff; }
    .mark span { color: var(--on-brand); font-weight: 800; font-size: 1.5rem; }
    h1 { font-size: 1.4rem; font-weight: 800; color: var(--ink); letter-spacing: -0.02em; line-height: 1.2; }
    .sub { color: var(--muted); font-size: 0.9rem; margin: 0.35rem 0 1.6rem; line-height: 1.45; }
    label { display: block; font-size: 0.8rem; font-weight: 600; color: var(--ink); margin-bottom: 0.45rem; }
    input { width: 100%; padding: 0.78rem 0.9rem; font: inherit; font-size: 1rem; color: var(--ink); background: #fff;
            border: 1px solid var(--border); border-radius: 11px; transition: border-color 0.15s, box-shadow 0.15s; }
    input:focus { outline: none; border-color: var(--brand);
                  box-shadow: 0 0 0 4px color-mix(in srgb, var(--brand) 18%, transparent); }
    button { width: 100%; margin-top: 1rem; padding: 0.82rem 1rem; border: 0; border-radius: 11px; cursor: pointer;
             background: var(--brand); color: var(--on-brand); font: inherit; font-weight: 700; font-size: 0.95rem;
             box-shadow: 0 6px 16px color-mix(in srgb, var(--brand) 28%, transparent); transition: filter 0.15s, transform 0.15s; }
    button:hover { filter: brightness(1.06); transform: translateY(-1px); }
    button:active { filter: brightness(0.96); transform: translateY(0); }
    button:focus-visible { outline: 2px solid var(--brand); outline-offset: 3px; }
    .error { margin-bottom: 1.1rem; padding: 0.7rem 0.85rem; border-radius: 10px; font-size: 0.85rem; font-weight: 500;
             color: var(--danger); background: #FEF2F2; border: 1px solid #FECACA; }
    .hint { margin-top: 1.35rem; font-size: 0.78rem; color: var(--muted); line-height: 1.55; }
    code { font-family: 'SFMono-Regular', Menlo, Consolas, monospace; font-size: 0.76rem; color: var(--ink);
           background: color-mix(in srgb, var(--brand) 8%, #fff); padding: 0.1rem 0.35rem; border-radius: 5px; }
    footer { margin-top: 1.25rem; text-align: center; font-size: 0.75rem; color: var(--muted); }
    @media (prefers-reduced-motion: reduce) { input, button { transition: none; } button:hover { transform: none; } }
  </style>
</head>
<body>
  <main>
    <div class="card">
      <div class="mark">$mark</div>
      <h1>$name</h1>
      <p class="sub">$subtitle</p>
      $error
      <form method="post" action="/login">
        <label for="password">Dashboard password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
        <button type="submit">Sign in</button>
      </form>
      <p class="hint">Forgot it? Run <code>sdr setup --section security</code> in your terminal to set a new one.</p>
    </div>
    <footer>$footer</footer>
  </main>
</body>
</html>
""")


def render_login_page(profile: dict | None, error: str = "") -> str:
    """The sign-in page, in the business's own colours and logo. Every value is escaped."""
    brand = login_brand(profile)
    esc = html.escape
    name = esc(brand["name"])
    logo = esc(brand["logo_url"], quote=True)
    initial = esc(brand["name"][:1] or "?")
    mark = f'<img src="{logo}" alt="" width="56" height="56">' if logo else f"<span>{initial}</span>"
    return LOGIN_TEMPLATE.substitute(
        name=name,
        favicon=f'<link rel="icon" href="{logo}">' if logo else "",
        brand=brand["brand"], on_brand=_text_on(brand["brand"]), heading=brand["heading"],
        text=brand["text"], background=brand["background"],
        mark=mark,
        subtitle=esc(brand["tagline"] or "SDR Control Center · sign in to continue"),
        error=f'<p class="error" role="alert">{esc(error)}</p>' if error else "",
        footer=esc(f"{DISPLAY_NAME} · v{version()}"),
    )
