"""Offline tests for website brand detection. A fake `fetch` serves fixture HTML/CSS, and the
default requests-based fetcher is exercised against a fake `requests` module: no network."""

import colorsys
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from core import brand_detect
from core.brand_detect import MAX_CSS_BYTES, MAX_PAGE_BYTES, default_fetch, detect_brand, normalize_url
from core.email_design import DEFAULTS
from core.product import BRAND_COLOR

SITE = "https://acme-films.com"
HEX_RE = re.compile(r"^#[0-9A-F]{6}$")
FIELDS = ("name", "tagline", "description", "logo_url", "brand_color", "heading_color", "text_color",
          "background")


def page(head: str = "", body: str = "") -> str:
    return f"<!doctype html><html><head><meta charset='utf-8'>{head}</head><body>{body}</body></html>"


def html_ok(text: str) -> tuple:
    return 200, text, "text/html; charset=utf-8"


def css_ok(text: str) -> tuple:
    return 200, text, "text/css"


class FakeWeb:
    """A tiny web: url -> (status, text, content_type) or an Exception to raise. Records calls."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.calls = []

    def __call__(self, url: str) -> tuple:
        self.calls.append(url)
        response = self.pages.get(url)
        if isinstance(response, Exception):
            raise response
        if response is None:
            return 404, "Not found", "text/html"
        return response


def detect(head: str = "", body: str = "", extra: dict | None = None, url: str = SITE) -> dict:
    web = FakeWeb({SITE: html_ok(page(head, body)), **(extra or {})})
    return detect_brand(url, fetch=web)


def rgb(hex_color: str) -> tuple:
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


class TestNormalizeUrl(unittest.TestCase):
    def test_adds_https_to_a_bare_domain(self):
        self.assertEqual(normalize_url("acme.com"), "https://acme.com")

    def test_strips_surrounding_and_inner_spaces(self):
        self.assertEqual(normalize_url("  acme .com \n"), "https://acme.com")

    def test_keeps_an_explicit_http_scheme(self):
        self.assertEqual(normalize_url("http://acme.com/about"), "http://acme.com/about")

    def test_protocol_relative_becomes_https(self):
        self.assertEqual(normalize_url("//acme.com"), "https://acme.com")

    def test_lowercases_scheme_and_host_but_not_path(self):
        self.assertEqual(normalize_url("HTTPS://Acme.COM/About"), "https://acme.com/About")

    def test_host_with_port_is_not_mistaken_for_a_scheme(self):
        self.assertEqual(normalize_url("localhost:8000"), "https://localhost:8000")

    def test_other_schemes_are_left_alone_so_they_can_be_rejected(self):
        self.assertTrue(normalize_url("javascript:alert(1)").startswith("javascript:"))
        self.assertTrue(normalize_url("ftp://acme.com").startswith("ftp://"))

    def test_empty_and_none_give_empty_string(self):
        self.assertEqual(normalize_url(""), "")
        self.assertEqual(normalize_url("   "), "")
        self.assertEqual(normalize_url(None), "")


class TestResultShape(unittest.TestCase):
    def test_result_has_the_contract_keys_and_valid_colors(self):
        result = detect('<meta name="theme-color" content="#e4572e">')
        self.assertEqual(set(result), {"url", *FIELDS, "found", "error"})
        self.assertEqual(set(result["found"]), set(FIELDS))
        for field in ("brand_color", "heading_color", "text_color", "background"):
            self.assertRegex(result[field], HEX_RE)
        self.assertEqual(result["url"], SITE)
        self.assertEqual(result["error"], "")

    def test_empty_page_falls_back_to_defaults(self):
        result = detect()
        self.assertEqual(result["brand_color"], BRAND_COLOR.upper())
        self.assertEqual(result["heading_color"], DEFAULTS["heading_color"])
        self.assertEqual(result["text_color"], DEFAULTS["text_color"])
        self.assertEqual(result["background"], DEFAULTS["background"])
        self.assertEqual((result["tagline"], result["description"], result["logo_url"]), ("", "", ""))
        self.assertFalse(any(result["found"].values()))

    def test_name_defaults_to_the_domain_when_the_site_has_none(self):
        result = detect()
        self.assertEqual(result["name"], "Acme Films")
        self.assertFalse(result["found"]["name"])

    def test_www_is_not_part_of_the_domain_name(self):
        web = FakeWeb({"https://www.acme-films.com": html_ok(page())})
        self.assertEqual(detect_brand("www.acme-films.com", fetch=web)["name"], "Acme Films")


class TestName(unittest.TestCase):
    def test_og_site_name_wins(self):
        result = detect('<meta property="og:site_name" content="Acme Films"><title>Something | Else</title>')
        self.assertEqual(result["name"], "Acme Films")
        self.assertTrue(result["found"]["name"])

    def test_application_name_when_no_og_site_name(self):
        result = detect('<meta name="application-name" content="Acme Studio"><title>Home</title>')
        self.assertEqual(result["name"], "Acme Studio")

    def test_title_is_cleaned(self):
        result = detect("<title>Welcome to Acme Studio | Wedding Films in Austin</title>")
        self.assertEqual(result["name"], "Acme Studio")
        self.assertTrue(result["found"]["name"])

    def test_useless_site_name_falls_through_to_title(self):
        result = detect('<meta property="og:site_name" content="Home"><title>Acme Studio - Films</title>')
        self.assertEqual(result["name"], "Acme Studio")

    def test_svg_titles_are_ignored(self):
        result = detect(body="<svg><title>Menu icon</title></svg>")
        self.assertEqual(result["name"], "Acme Films")
        self.assertFalse(result["found"]["name"])

    def test_html_entities_are_decoded(self):
        result = detect('<meta property="og:site_name" content="Smith &amp; Jones">')
        self.assertEqual(result["name"], "Smith & Jones")


class TestTagline(unittest.TestCase):
    def test_first_sentence_of_og_description(self):
        result = detect('<meta property="og:description" content="Cinematic wedding films. Based in Austin.">'
                        '<meta name="description" content="Fallback text.">')
        self.assertEqual(result["tagline"], "Cinematic wedding films")
        self.assertEqual(result["description"], "Cinematic wedding films. Based in Austin.")
        self.assertTrue(result["found"]["tagline"] and result["found"]["description"])

    def test_meta_description_when_no_og_description(self):
        result = detect('<meta name="description" content="  Proposals that close.\n  Fast. ">')
        self.assertEqual(result["tagline"], "Proposals that close")
        self.assertEqual(result["description"], "Proposals that close. Fast.")

    def test_abbreviations_do_not_end_the_sentence(self):
        result = detect('<meta name="description" content="Acme Inc. makes films for couples. More here.">')
        self.assertEqual(result["tagline"], "Acme Inc. makes films for couples")

    def test_question_and_exclamation_marks_are_kept(self):
        result = detect('<meta name="description" content="Need better proposals? We help.">')
        self.assertEqual(result["tagline"], "Need better proposals?")

    def test_long_first_sentence_is_shortened_to_90_chars(self):
        long_sentence = ("We craft cinematic, story-driven wedding films for adventurous couples across Texas "
                         "and the rest of the world with drones and love.")
        result = detect(f'<meta name="description" content="{long_sentence}">')
        self.assertLessEqual(len(result["tagline"]), 90)
        self.assertTrue(result["tagline"].startswith("We craft cinematic"))

    def test_long_sentence_is_cut_at_a_clause_when_possible(self):
        sentence = ("Cinematic wedding films for couples — story-driven, handcrafted and delivered in eight "
                    "weeks, anywhere in the world")
        result = detect(f'<meta name="description" content="{sentence}">')
        self.assertEqual(result["tagline"], "Cinematic wedding films for couples")


class TestLogo(unittest.TestCase):
    def test_apple_touch_icon_wins_and_is_made_absolute(self):
        result = detect('<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">'
                        '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">')
        self.assertEqual(result["logo_url"], f"{SITE}/apple-touch-icon.png")
        self.assertTrue(result["found"]["logo_url"])

    def test_largest_apple_touch_icon_is_chosen(self):
        result = detect('<link rel="apple-touch-icon" sizes="120x120" href="/a120.png">'
                        '<link rel="apple-touch-icon-precomposed" sizes="180x180" href="/a180.png">')
        self.assertEqual(result["logo_url"], f"{SITE}/a180.png")

    def test_largest_png_icon_when_no_apple_touch_icon(self):
        result = detect('<link rel="icon" type="image/png" sizes="32x32" href="/i32.png">'
                        '<link rel="shortcut icon" type="image/png" sizes="192x192" href="/i192.png">'
                        '<link rel="icon" type="image/svg+xml" href="/logo.svg">')
        self.assertEqual(result["logo_url"], f"{SITE}/i192.png")

    def test_size_is_read_from_the_file_name_when_sizes_is_missing(self):
        result = detect('<link rel="icon" href="/icons/icon-512x512.png"><link rel="icon" href="/icons/icon-48x48.png">')
        self.assertEqual(result["logo_url"], f"{SITE}/icons/icon-512x512.png")

    def test_svg_icon_used_when_there_is_no_png(self):
        result = detect('<link rel="icon" type="image/svg+xml" href="logo.svg">')
        self.assertEqual(result["logo_url"], f"{SITE}/logo.svg")

    def test_safari_mask_icon_is_not_a_logo(self):
        web = FakeWeb({SITE: html_ok(page('<link rel="mask-icon" href="/mask.svg" color="#000">'))})
        self.assertEqual(detect_brand(SITE, fetch=web)["logo_url"], "")

    def test_logo_url_is_upgraded_to_https(self):
        result = detect('<link rel="apple-touch-icon" href="http://cdn.acme-films.com/touch.png">')
        self.assertEqual(result["logo_url"], "https://cdn.acme-films.com/touch.png")

    def test_protocol_relative_and_base_href_are_resolved(self):
        self.assertEqual(detect('<link rel="apple-touch-icon" href="//cdn.acme-films.com/t.png">')["logo_url"],
                         "https://cdn.acme-films.com/t.png")
        self.assertEqual(detect('<base href="/static/"><link rel="apple-touch-icon" href="t.png">')["logo_url"],
                         f"{SITE}/static/t.png")

    def test_data_uri_icons_are_skipped(self):
        result = detect('<link rel="icon" href="data:image/png;base64,iVBORw0KGgo=">'
                        '<link rel="icon" href="/real.ico">')
        self.assertEqual(result["logo_url"], f"{SITE}/real.ico")

    def test_conventional_apple_touch_icon_is_probed(self):
        result = detect(extra={f"{SITE}/apple-touch-icon.png": (200, "\x89PNG", "image/png")})
        self.assertEqual(result["logo_url"], f"{SITE}/apple-touch-icon.png")
        self.assertTrue(result["found"]["logo_url"])

    def test_favicon_ico_is_the_last_resort(self):
        result = detect(extra={f"{SITE}/favicon.ico": (200, "\x00\x00\x01\x00", "image/x-icon")})
        self.assertEqual(result["logo_url"], f"{SITE}/favicon.ico")

    def test_favicon_probe_is_https_even_for_an_http_site(self):
        web = FakeWeb({"http://acme-films.com": html_ok(page()),
                       f"{SITE}/favicon.ico": (200, "ico", "image/vnd.microsoft.icon")})
        self.assertEqual(detect_brand("http://acme-films.com", fetch=web)["logo_url"], f"{SITE}/favicon.ico")

    def test_spa_fallback_html_is_not_a_favicon(self):
        result = detect(extra={f"{SITE}/favicon.ico": html_ok(page()),
                               f"{SITE}/apple-touch-icon.png": html_ok(page())})
        self.assertEqual(result["logo_url"], "")
        self.assertFalse(result["found"]["logo_url"])

    def test_probe_errors_are_swallowed(self):
        result = detect(extra={f"{SITE}/favicon.ico": TimeoutError("slow"),
                               f"{SITE}/apple-touch-icon.png": ConnectionError("reset")})
        self.assertEqual(result["logo_url"], "")


class TestBrandColor(unittest.TestCase):
    def test_theme_color_wins_and_is_uppercased(self):
        result = detect('<meta name="theme-color" content="#e4572e"><style>:root{--primary:#10B981}</style>')
        self.assertEqual(result["brand_color"], "#E4572E")
        self.assertTrue(result["found"]["brand_color"])

    def test_short_hex_and_rgb_theme_colors_are_expanded(self):
        self.assertEqual(detect('<meta name="theme-color" content="#f60">')["brand_color"], "#FF6600")
        self.assertEqual(detect('<meta name="theme-color" content="rgb(228, 87, 46)">')["brand_color"], "#E4572E")

    def test_light_theme_color_preferred_over_dark_mode_one(self):
        result = detect('<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#7C3AED">'
                        '<meta name="theme-color" media="(prefers-color-scheme: light)" content="#E4572E">')
        self.assertEqual(result["brand_color"], "#E4572E")

    def test_white_theme_color_is_not_a_brand_color(self):
        result = detect('<meta name="theme-color" content="#ffffff"><style>:root{--color-primary:#10b981}</style>')
        self.assertEqual(result["brand_color"], "#10B981")

    def test_brand_variable_beats_accent_variable(self):
        result = detect("<style>:root{--accent:#10B981; --brand:#D946EF}</style>")
        self.assertEqual(result["brand_color"], "#D946EF")

    def test_foreground_and_light_shade_variables_are_ignored(self):
        result = detect("<style>:root{--primary-foreground:#FF0066; --color-primary-100:#FFE4E6;"
                        " --accent:#10B981}</style>")
        self.assertEqual(result["brand_color"], "#10B981")

    def test_camel_case_and_wordpress_preset_variables(self):
        self.assertEqual(detect("<style>:root{--primaryColor:#2563EB}</style>")["brand_color"], "#2563EB")
        self.assertEqual(detect("<style>body{--wp--preset--color--primary:#E11D48}</style>")["brand_color"],
                         "#E11D48")

    def test_var_references_are_resolved(self):
        result = detect("<style>:root{--blue-600:#2563EB; --color-primary: var(--blue-600);}</style>")
        self.assertEqual(result["brand_color"], "#2563EB")

    def test_shadcn_hsl_triplets_are_understood(self):
        result = detect("<style>:root{--background:0 0% 100%; --primary:262 83% 58%}</style>")
        self.assertTrue(all(abs(a - b) <= 2 for a, b in zip(rgb(result["brand_color"]), rgb("#7C3AED"))),
                        result["brand_color"])

    def test_most_frequent_saturated_color_from_inline_and_linked_css(self):
        web = FakeWeb({
            SITE: html_ok(page('<link rel="stylesheet" href="/css/site.css">'
                               "<style>.btn{background:#FF6600} .x{border-color:#00AAFF} hr{color:#ccc}</style>",
                               '<a style="color:#ff6600">Book</a>')),
            f"{SITE}/css/site.css": css_ok(".cta{background:#ff6600} .g{color:#cccccc;border:1px solid #CCC}"),
        })
        result = detect_brand(SITE, fetch=web)
        self.assertEqual(result["brand_color"], "#FF6600")
        self.assertTrue(result["found"]["brand_color"])

    def test_id_selectors_that_look_like_hex_are_not_colors(self):
        result = detect("<style>#add{color:#E11D48} #add .x{margin:0} #add .y{padding:0} #add .z{top:0}</style>")
        self.assertEqual(result["brand_color"], "#E11D48")

    def test_translucent_colors_are_ignored(self):
        result = detect("<style>.a{box-shadow:0 0 4px rgba(255,0,0,.2)} .b{background:rgba(255,0,0,0.1)}</style>")
        self.assertFalse(result["found"]["brand_color"])

    def test_dark_declared_primary_used_when_nothing_is_colorful(self):
        result = detect("<style>:root{--primary:222.2 47.4% 11.2%; --primary-foreground:210 40% 98%}</style>")
        self.assertEqual(result["brand_color"], "#0F172A")
        self.assertTrue(result["found"]["brand_color"])

    def test_invalid_colors_are_rejected(self):
        result = detect('<meta name="theme-color" content="#GGHHII"><style>:root{--brand:#12345}</style>')
        self.assertEqual(result["brand_color"], BRAND_COLOR.upper())
        self.assertFalse(result["found"]["brand_color"])

    def test_background_is_a_very_light_tint_of_the_brand(self):
        result = detect('<meta name="theme-color" content="#E4572E">')
        red, green, blue = rgb(result["background"])
        self.assertNotEqual(result["background"], "#FFFFFF")
        self.assertTrue(min(red, green, blue) >= 0xE8, result["background"])
        self.assertTrue(red > green and red > blue, "tint keeps the brand's hue")
        hue_brand = colorsys.rgb_to_hls(*(c / 255 for c in rgb("#E4572E")))[0]
        hue_tint = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)[0]
        self.assertAlmostEqual(hue_brand, hue_tint, delta=0.03)
        self.assertTrue(result["found"]["background"])


class TestLinkedStylesheets(unittest.TestCase):
    def test_only_same_site_stylesheets_are_fetched_and_at_most_two(self):
        links = "".join(f'<link rel="stylesheet" href="{href}">' for href in (
            "https://cdn.other.com/lib.css", "https://static.acme-films.com/app.css", "/b.css", "/c.css"))
        web = FakeWeb({SITE: html_ok(page(links))})
        detect_brand(SITE, fetch=web)
        css_calls = [url for url in web.calls if url.endswith(".css")]
        self.assertEqual(css_calls, ["https://static.acme-films.com/app.css", f"{SITE}/b.css"])

    def test_theme_css_preferred_over_plugin_css(self):
        links = ('<link rel="stylesheet" href="/wp-includes/css/dist/block-library/style.min.css">'
                 '<link rel="stylesheet" href="/wp-content/plugins/x/x.css">'
                 '<link rel="stylesheet" href="/wp-content/themes/acme/style.css">')
        web = FakeWeb({SITE: html_ok(page(links))})
        detect_brand(SITE, fetch=web)
        css_calls = [url for url in web.calls if url.endswith(".css")]
        self.assertEqual(css_calls[0], f"{SITE}/wp-content/themes/acme/style.css")
        self.assertEqual(len(css_calls), 2)

    def test_alternate_and_preload_links_are_not_stylesheets(self):
        web = FakeWeb({SITE: html_ok(page('<link rel="alternate stylesheet" href="/alt.css">'
                                          '<link rel="preload" as="style" href="/p.css">'))})
        detect_brand(SITE, fetch=web)
        self.assertFalse([url for url in web.calls if url.endswith(".css")])

    def test_css_is_read_only_up_to_500_kb(self):
        filler = ".a{margin:0}" * (MAX_CSS_BYTES // 12 + 10)
        web = FakeWeb({SITE: html_ok(page('<link rel="stylesheet" href="/big.css">')),
                       f"{SITE}/big.css": css_ok(filler + ".late{background:#00FF88}")})
        self.assertFalse(detect_brand(SITE, fetch=web)["found"]["brand_color"])

    def test_html_served_as_css_is_ignored(self):
        web = FakeWeb({SITE: html_ok(page('<link rel="stylesheet" href="/missing.css">')),
                       f"{SITE}/missing.css": html_ok("<style>.x{background:#00FF88}</style>")})
        self.assertFalse(detect_brand(SITE, fetch=web)["found"]["brand_color"])

    def test_stylesheet_errors_do_not_lose_page_results(self):
        web = FakeWeb({SITE: html_ok(page('<meta name="theme-color" content="#E4572E">'
                                          '<link rel="stylesheet" href="/s.css">')),
                       f"{SITE}/s.css": OSError("connection reset")})
        result = detect_brand(SITE, fetch=web)
        self.assertEqual(result["brand_color"], "#E4572E")
        self.assertEqual(result["error"], "")


class TestTextColors(unittest.TestCase):
    CSS = ("h1,h2{color:#0B0B0B} body{color:#2E2E2E} p{color:#2E2E2E} .m{color:#2E2E2E} li{color:#2e2e2e}"
           " .note{color:#999} .n2{color:#999} .n3{color:#999} .n4{color:#999} .n5{color:#999}"
           " a{color:#E4572E} .btn{background:#0B0B0B}")

    def test_heading_is_darkest_frequent_and_text_is_most_frequent_dark(self):
        result = detect(f"<style>{self.CSS}</style>")
        self.assertEqual(result["heading_color"], "#0B0B0B")
        self.assertEqual(result["text_color"], "#2E2E2E")
        self.assertTrue(result["found"]["heading_color"] and result["found"]["text_color"])

    def test_light_grey_is_never_used_for_text(self):
        result = detect("<style>.a{color:#999} .b{color:#999} .c{color:#aaa}</style>")
        self.assertEqual(result["text_color"], DEFAULTS["text_color"])
        self.assertFalse(result["found"]["text_color"])

    def test_single_dark_color_is_used_for_both(self):
        result = detect("<style>body{color:#1a1a1a}</style>")
        self.assertEqual((result["heading_color"], result["text_color"]), ("#1A1A1A", "#1A1A1A"))


class TestNeverRaises(unittest.TestCase):
    def test_network_error_returns_defaults_with_a_friendly_error(self):
        web = FakeWeb({SITE: ConnectionError("Name or service not known")})
        result = detect_brand("acme-films.com", fetch=web)
        self.assertEqual(result["url"], SITE)
        self.assertEqual(result["name"], "Acme Films")
        self.assertFalse(any(result["found"].values()))
        self.assertIn("acme-films.com", result["error"])

    def test_timeout_is_described(self):
        class ReadTimeout(Exception):
            pass
        result = detect_brand(SITE, fetch=FakeWeb({SITE: ReadTimeout("read timed out")}))
        self.assertIn("too long", result["error"])

    def test_http_error_status_returns_defaults(self):
        result = detect_brand(SITE, fetch=FakeWeb({SITE: (503, "<title>Down</title>", "text/html")}))
        self.assertFalse(result["found"]["name"])
        self.assertIn("503", result["error"])

    def test_non_html_page_returns_defaults(self):
        result = detect_brand(SITE, fetch=FakeWeb({SITE: (200, "%PDF-1.7", "application/pdf")}))
        self.assertFalse(any(result["found"].values()))
        self.assertTrue(result["error"])

    def test_garbage_html_does_not_raise(self):
        junk = "<<<>>><meta content=><link rel=icon href=>\x00\xff<style>{{{}}}:;#</style<title>" + "<" * 50
        result = detect_brand(SITE, fetch=FakeWeb({SITE: html_ok(junk)}))
        self.assertEqual(set(result["found"]), set(FIELDS))

    def test_fetch_returning_nonsense_does_not_raise(self):
        result = detect_brand(SITE, fetch=lambda url: None)
        self.assertFalse(any(result["found"].values()))
        self.assertTrue(result["error"])

    def test_non_http_urls_are_rejected_without_fetching(self):
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://acme.com", ""):
            web = FakeWeb({})
            result = detect_brand(bad, fetch=web)
            self.assertEqual(web.calls, [], bad)
            self.assertEqual(result["url"], "")
            self.assertTrue(result["error"])

    def test_none_url_does_not_raise(self):
        self.assertEqual(detect_brand(None, fetch=FakeWeb({}))["url"], "")


class FakeResponse:
    def __init__(self, body: bytes = b"", status: int = 200, content_type: str = "text/html; charset=utf-8"):
        self.body = body
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.chunk_sizes = []

    def iter_content(self, chunk_size: int = 1):
        self.chunk_sizes.append(chunk_size)
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestDefaultFetch(unittest.TestCase):
    def fake_requests(self, response: FakeResponse):
        calls = []

        def get(url, **kwargs):
            calls.append((url, kwargs))
            return response
        return SimpleNamespace(get=get), calls

    def test_uses_desktop_user_agent_timeout_and_streaming(self):
        fake, calls = self.fake_requests(FakeResponse(b"<title>Acme</title>"))
        with mock.patch.object(brand_detect, "requests", fake):
            status, text, content_type = default_fetch(SITE)
        self.assertEqual((status, text, content_type), (200, "<title>Acme</title>", "text/html; charset=utf-8"))
        url, kwargs = calls[0]
        self.assertEqual(url, SITE)
        self.assertIn("Mozilla/5.0", kwargs["headers"]["User-Agent"])
        self.assertEqual(kwargs["timeout"], 10)
        self.assertTrue(kwargs["stream"])

    def test_body_is_capped_at_1_5_mb(self):
        fake, _ = self.fake_requests(FakeResponse(b"a" * (MAX_PAGE_BYTES + 400_000)))
        with mock.patch.object(brand_detect, "requests", fake):
            _, text, _ = default_fetch(SITE)
        self.assertEqual(MAX_PAGE_BYTES, 1_500_000)
        self.assertEqual(len(text), MAX_PAGE_BYTES)

    def test_declared_charset_is_used(self):
        fake, _ = self.fake_requests(FakeResponse("Café".encode("latin-1"), content_type="text/html; charset=ISO-8859-1"))
        with mock.patch.object(brand_detect, "requests", fake):
            self.assertEqual(default_fetch(SITE)[1], "Café")

    def test_unknown_charset_falls_back_to_utf8(self):
        fake, _ = self.fake_requests(FakeResponse("Café".encode(), content_type="text/html; charset=klingon"))
        with mock.patch.object(brand_detect, "requests", fake):
            self.assertEqual(default_fetch(SITE)[1], "Café")

    def test_non_http_scheme_is_refused_before_any_request(self):
        fake, calls = self.fake_requests(FakeResponse())
        with mock.patch.object(brand_detect, "requests", fake):
            with self.assertRaises(ValueError):
                default_fetch("file:///etc/passwd")
        self.assertEqual(calls, [])

    def test_missing_requests_library_is_a_clear_error(self):
        with mock.patch.object(brand_detect, "requests", None):
            with self.assertRaises(RuntimeError):
                default_fetch(SITE)

    def test_detect_brand_uses_default_fetch_when_none_given(self):
        body = page('<meta property="og:site_name" content="Acme Films">').encode()
        fake, calls = self.fake_requests(FakeResponse(body))
        with mock.patch.object(brand_detect, "requests", fake):
            result = detect_brand("acme-films.com")
        self.assertEqual(result["name"], "Acme Films")
        self.assertTrue(result["found"]["name"])
        self.assertEqual(calls[0][0], SITE)


if __name__ == "__main__":
    unittest.main()
