"""Offline tests for core/search.py (Brave / DuckDuckGo / Bing lead search) and its use in
bots/leadgen_pipeline.py. Fake HTTP, fake robots.txt, fake clock: no network, no waiting."""

import base64
import inspect
import json
import os
import tempfile
import types
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import core.db as db
from core import search
from core.robots import SCRAPER_HEADERS, RobotsRules

BRAVE_KEY = "brv-test-key-123"
BING_SEARCH = "https://www.bing.com/search"
DDG_HTML = "https://html.duckduckgo.com/html/"


def make_profile(engine: str | None = None) -> dict:
    targeting = {"ideal_client": "wedding photographer", "professions": ["wedding photographer"],
                 "cities": ["Austin, TX"], "search_extra_words": "contact"}
    if engine is not None:
        targeting["search_engine"] = engine
    return {"targeting": targeting, "sender": {"product_name": "Acme"}}


class FakeHttp:
    """fetch(url, headers) answering from (url_prefix, response) rules. A response is
    (status, text[, headers]) or an exception; a list is served in order (the last one repeats)."""

    def __init__(self, *rules):
        self.rules = [(prefix, list(r) if isinstance(r, list) else [r]) for prefix, r in rules]
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        for prefix, responses in self.rules:
            if url.startswith(prefix):
                response = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(response, BaseException):
                    raise response
                return response[0], response[1], (response[2] if len(response) > 2 else {})
        raise AssertionError("unexpected request: " + url)

    @property
    def urls(self):
        return [url for url, _ in self.calls]


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def brave_json(*results) -> str:
    return json.dumps({"type": "search", "web": {"type": "search", "results": list(results)}})


BRAVE_PAGE = brave_json(
    {"title": "Acme <strong>Studio</strong> &amp; Films", "url": "https://acmestudio.test/", "description": "x"},
    {"title": "Beta Photo", "url": "https://beta.test/about"},
    {"title": "Acme again", "url": "https://acmestudio.test/"},
    {"title": "Not a web page", "url": "ftp://files.test/"},
)

DDG_PAGE = """<html><body><div id="links" class="results">
<div class="result results_links results_links_deep result--ad"><div class="links_main result__body">
  <h2 class="result__title"><a rel="nofollow" class="result__a"
     href="https://duckduckgo.com/y.js?ad_domain=ads.test&amp;u3=https%3A%2F%2Fads.test">Sponsored thing</a></h2>
</div></div>
<div class="result results_links results_links_deep web-result"><div class="links_main result__body">
  <h2 class="result__title"><a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.acmestudio.test%2Fcontact%3Fa%3D1%26b%3D2&amp;rut=abc">Acme <b>Studio</b>
     Contact</a></h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.acmestudio.test%2F">Wedding films</a>
</div></div>
<div class="result results_links web-result"><h2 class="result__title">
  <a class="result__a" href="https://beta.test/">Beta Photo</a></h2></div>
</div></body></html>"""

DDG_ANOMALY = """<html><body><div class="anomaly-modal__modal" data-testid="anomaly-modal">
<div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div>
<form id="challenge-form" action="//duckduckgo.com/anomaly.js?sv=html&amp;cc=botnet" method="POST"></form>
</div></body></html>"""

DDG_NO_RESULTS = '<html><body><div id="links" class="results"><div class="no-results">No results.</div></div></body></html>'


def bing_ck(url: str) -> str:
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return "https://www.bing.com/ck/a?!&amp;&amp;p=abc123&amp;u=a1" + encoded + "&amp;ntb=1"


BING_PAGE = """<html><body><ol id="b_results">
<li class="b_algo"><h2><a href="{ck}">Acme <strong>Studio</strong></a></h2></li>
<li class="b_algo"><h2><a href="https://beta.test/">Beta Photo</a></h2></li>
<li class="b_algo"><h2>No link here</h2></li>
<li class="b_algo"><div>No heading</div></li>
<li class="b_algo"><h2><a href="/search?q=related">Related searches</a></h2></li>
</ol></body></html>""".replace("{ck}", bing_ck("https://www.acmestudio.test/contact"))


class SearchTestCase(unittest.TestCase):
    def setUp(self):
        self.logged = []
        self.robots_checked = []
        self.time = FakeTime()
        for target, value in ((search, "log_event"), (search, "log")):
            replacement = (lambda *args: self.logged.append(args)) if value == "log_event" else mock.Mock()
            patcher = mock.patch.object(target, value, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self, http, env=None, robots=lambda url: True) -> search.SearchClient:
        def robots_allowed(url):
            self.robots_checked.append(url)
            return robots(url)

        return search.SearchClient(fetch=http, robots_allowed=robots_allowed, sleep=self.time.sleep,
                                   clock=self.time.clock, env={} if env is None else env)

    def messages(self) -> list[str]:
        return [entry[3] for entry in self.logged]


class TestEngineChoice(unittest.TestCase):
    def choose(self, engine, key=""):
        return search.choose_engine(make_profile(engine), {"BRAVE_API_KEY": key} if key else {})

    def test_auto_prefers_brave_when_a_key_is_set(self):
        self.assertEqual(self.choose("auto", BRAVE_KEY), ("brave", ""))
        self.assertEqual(self.choose(None, BRAVE_KEY), ("brave", ""))

    def test_auto_without_a_key_uses_duckduckgo(self):
        for profile in (make_profile("auto"), make_profile(None), make_profile(""), {}, None, {"targeting": None}):
            self.assertEqual(search.choose_engine(profile, {"BRAVE_API_KEY": "  "}), ("duckduckgo", ""), profile)

    def test_explicit_choices_are_honoured(self):
        self.assertEqual(self.choose("duckduckgo", BRAVE_KEY), ("duckduckgo", ""))
        self.assertEqual(self.choose("bing", BRAVE_KEY), ("bing", ""))
        self.assertEqual(self.choose("bing"), ("bing", ""))
        self.assertEqual(self.choose("brave", BRAVE_KEY), ("brave", ""))

    def test_brave_without_a_key_falls_back_to_duckduckgo_and_says_why(self):
        engine, note = self.choose("brave")
        self.assertEqual((engine, note), ("duckduckgo", search.BRAVE_NO_KEY))
        self.assertIn("BRAVE_API_KEY", note)

    def test_unknown_values_use_auto_and_say_so(self):
        engine, note = self.choose("google", BRAVE_KEY)
        self.assertEqual(engine, "brave")
        self.assertIn("'google'", note)

    def test_case_spaces_and_aliases(self):
        self.assertEqual(self.choose(" DuckDuckGo ", BRAVE_KEY)[0], "duckduckgo")
        self.assertEqual(self.choose("DDG", BRAVE_KEY)[0], "duckduckgo")
        self.assertEqual(self.choose("BING")[0], "bing")

    def test_auto_never_picks_bing(self):
        for key in ("", BRAVE_KEY):
            self.assertNotEqual(self.choose("auto", key)[0], "bing")

    def test_describe_search_for_doctor_and_dashboard(self):
        info = search.describe_search(make_profile("auto"), {"BRAVE_API_KEY": BRAVE_KEY})
        self.assertEqual(info, {"setting": "auto", "engine": "brave", "label": "Brave Search",
                                "has_brave_key": True, "note": ""})
        info = search.describe_search(make_profile("brave"), {})
        self.assertEqual((info["engine"], info["has_brave_key"], info["note"]), ("duckduckgo", False, search.BRAVE_NO_KEY))
        self.assertNotIn(BRAVE_KEY, json.dumps(search.describe_search(make_profile(), {"BRAVE_API_KEY": BRAVE_KEY})))


class TestBrave(SearchTestCase):
    env = {"BRAVE_API_KEY": BRAVE_KEY}

    def test_results_come_from_the_official_api(self):
        http = FakeHttp((search.BRAVE_URL, (200, BRAVE_PAGE)))
        results = self.client(http, self.env).search("wedding photographer austin", 10, make_profile("brave"))
        self.assertEqual(results, [{"title": "Acme Studio & Films", "url": "https://acmestudio.test/"},
                                   {"title": "Beta Photo", "url": "https://beta.test/about"}])
        url, headers = http.calls[0]
        self.assertEqual(parse_qs(urlsplit(url).query), {"q": ["wedding photographer austin"], "count": ["10"]})
        self.assertEqual(headers, {"Accept": "application/json", "X-Subscription-Token": BRAVE_KEY})
        self.assertEqual(self.robots_checked, [url])
        self.assertEqual(self.logged, [])

    def test_count_and_result_limit(self):
        http = FakeHttp((search.BRAVE_URL, (200, BRAVE_PAGE)))
        client = self.client(http, self.env)
        self.assertEqual(len(client.search("q", 1, make_profile())), 1)
        client.search("q", 50, make_profile())
        self.assertEqual(parse_qs(urlsplit(http.urls[1]).query)["count"], ["20"])

    def test_waits_1_1_seconds_between_searches(self):
        http = FakeHttp((search.BRAVE_URL, (200, BRAVE_PAGE)))
        client = self.client(http, self.env)
        client.search("one", 5, make_profile())
        self.time.now += 0.4
        client.search("two", 5, make_profile())
        self.assertEqual(self.time.sleeps, [0.7])

    def test_rate_limit_backs_off_and_retries_once(self):
        limited = (429, "{}", {"x-ratelimit-reset": "1, 1419704", "x-ratelimit-remaining": "0, 1500"})
        http = FakeHttp((search.BRAVE_URL, [limited, (200, BRAVE_PAGE)]))
        client = self.client(http, self.env)
        self.assertEqual(len(client.search("q", 5, make_profile())), 2)
        self.assertEqual(len(http.calls), 2)
        self.assertGreaterEqual(sum(self.time.sleeps), 1.1)
        self.assertEqual(client.stopped_reason(make_profile()), "")

    def test_second_rate_limit_stops_brave_for_the_rest_of_the_run(self):
        http = FakeHttp((search.BRAVE_URL, (429, "{}", {})))
        client = self.client(http, self.env)
        self.assertEqual(client.search("q", 5, make_profile()), [])
        self.assertEqual((len(http.calls), self.time.sleeps[0]), (2, search.RETRY_WAIT_SECONDS))
        self.assertEqual(client.search("again", 5, make_profile()), [])
        self.assertEqual(len(http.calls), 2, "a stopped engine is not asked again this run")
        self.assertEqual(self.messages(), [search.BRAVE_LIMIT])
        self.assertEqual(client.stopped_reason(make_profile()), search.BRAVE_LIMIT)
        client.start_run()
        client.search("next run", 5, make_profile())
        self.assertEqual(len(http.calls), 4)

    def test_used_up_monthly_quota_or_long_reset_stops_without_waiting(self):
        for headers in ({"x-ratelimit-remaining": "1, 0", "x-ratelimit-reset": "1, 999999"},
                        {"Retry-After": "3600"}):
            http = FakeHttp((search.BRAVE_URL, (429, "{}", headers)))
            client = self.client(http, self.env)
            self.time.sleeps.clear()
            self.assertEqual(client.search("q", 5, make_profile()), [])
            self.assertEqual((len(http.calls), self.time.sleeps), (1, []), headers)

    def test_retry_wait_reads_brave_headers(self):
        self.assertEqual(search.retry_wait({"X-RateLimit-Reset": "1, 1419704"}), 1.0)
        self.assertEqual(search.retry_wait({"retry-after": "3"}), 3.0)
        self.assertEqual(search.retry_wait({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}), search.RETRY_WAIT_SECONDS)
        self.assertIsNone(search.retry_wait({"x-ratelimit-remaining": "0, 0"}))
        self.assertIsNone(search.retry_wait({"retry-after": "60"}))
        self.assertEqual(search.retry_wait(None), search.RETRY_WAIT_SECONDS)

    def test_rejected_key_stops_the_run_without_showing_the_key(self):
        http = FakeHttp((search.BRAVE_URL, (401, '{"error": "bad token"}')))
        client = self.client(http, self.env)
        self.assertEqual(client.search("q", 5, make_profile()), [])
        self.assertEqual(client.stopped_reason(make_profile()), search.BRAVE_KEY_REJECTED)
        self.assertNotIn(BRAVE_KEY, " ".join(self.messages()))

    def test_errors_skip_one_search_and_three_in_a_row_stop_the_run(self):
        http = FakeHttp((search.BRAVE_URL, [(500, ""), (200, "not json"), OSError("reset by peer"), (500, "")]))
        client = self.client(http, self.env)
        for query in ("a", "b", "c"):
            self.assertEqual(client.search(query, 5, make_profile()), [])
        self.assertIn("failed 3 times in a row", client.stopped_reason(make_profile()))
        self.assertIn("HTTP 500", self.messages()[0])
        self.assertIn("couldn't read", self.messages()[1])
        self.assertIn("OSError", self.messages()[2])
        self.assertNotIn("reset by peer", " ".join(self.messages()), "raw exception text is not logged")
        client.search("d", 5, make_profile())
        self.assertEqual(len(http.calls), 3)

    def test_a_success_resets_the_failure_count(self):
        http = FakeHttp((search.BRAVE_URL, [(500, ""), (500, ""), (200, BRAVE_PAGE), (500, ""), (500, "")]))
        client = self.client(http, self.env)
        for query in "abcde":
            client.search(query, 5, make_profile())
        self.assertEqual(client.stopped_reason(make_profile()), "")

    def test_check_brave_key_for_doctor_online(self):
        allow = lambda url: True  # noqa: E731
        cases = [((200, BRAVE_PAGE), True, "works"), ((429, "{}"), True, "rate limit"),
                 ((401, "{}"), False, "rejected"), ((503, ""), False, "HTTP 503"),
                 (TimeoutError("slow"), False, "TimeoutError")]
        for response, ok, words in cases:
            http = FakeHttp((search.BRAVE_URL, response))
            result = search.check_brave_key(BRAVE_KEY, fetch=http, robots_allowed=allow)
            self.assertEqual(result[0], ok, response)
            self.assertIn(words, result[1])
            self.assertNotIn(BRAVE_KEY, result[1])
            self.assertEqual(http.calls[0][1]["X-Subscription-Token"], BRAVE_KEY)
        self.assertEqual(search.check_brave_key("  ", fetch=FakeHttp())[0], False)
        with mock.patch.dict(os.environ, {"BRAVE_API_KEY": ""}):
            self.assertFalse(search.check_brave_key(fetch=FakeHttp())[0])

    def test_a_redirect_is_refused_instead_of_followed_with_the_key(self):
        """requests drops only `Authorization` on a cross-host redirect; the key travels in
        X-Subscription-Token, so a 3xx from Brave is an error, never a second request elsewhere."""
        redirect = (302, "", {"location": "https://evil.test/collect"})
        http = FakeHttp((search.BRAVE_URL, redirect))
        client = self.client(http, self.env)
        self.assertEqual(client.search("q", 5, make_profile()), [])
        self.assertEqual(http.urls, [search.brave_url("q", 5)], "the Location target is never requested")
        self.assertIn("redirect", self.messages()[0].lower())
        self.assertNotIn(BRAVE_KEY, " ".join(self.messages()))
        ok, message = search.check_brave_key(BRAVE_KEY, fetch=FakeHttp((search.BRAVE_URL, redirect)),
                                             robots_allowed=lambda url: True)
        self.assertFalse(ok)
        self.assertIn("redirect", message.lower())
        self.assertNotIn(BRAVE_KEY, message)

    def test_http_get_turns_redirects_off_only_when_a_key_is_attached(self):
        response = mock.Mock(status_code=302, text="", headers={"Location": "https://evil.test/"})
        fake_requests = mock.Mock(get=mock.Mock(return_value=response))
        with mock.patch.object(search, "requests", fake_requests):
            status, _, headers = search.http_get(search.brave_url("q", 1), search.brave_headers(BRAVE_KEY))
            self.assertIs(fake_requests.get.call_args.kwargs["allow_redirects"], False)
            self.assertEqual((status, headers["location"]), (302, "https://evil.test/"))
            search.http_get(DDG_HTML + "?q=x", dict(SCRAPER_HEADERS))
            self.assertIs(fake_requests.get.call_args.kwargs["allow_redirects"], True,
                          "DuckDuckGo and Bing carry no secret, and Bing needs its redirects followed")


class TestDuckDuckGo(SearchTestCase):
    def test_parses_redirect_links_and_skips_ads(self):
        self.assertEqual(search.parse_duckduckgo(DDG_PAGE), [
            {"title": "Acme Studio Contact", "url": "https://www.acmestudio.test/contact?a=1&b=2"},
            {"title": "Beta Photo", "url": "https://beta.test/"},
        ])

    def test_decode_ddg_url(self):
        cases = {
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.test%2Fa%20b&rut=1": "https://x.test/a b",
            "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fx.test%2F": "https://x.test/",
            "/l/?uddg=https%3A%2F%2Fy.test": "https://y.test",
            "https://duckduckgo.com/y.js?ad_domain=ads.test": "",
            "https://duckduckgo.com/?q=more": "",
            "https://direct.test/page": "https://direct.test/page",
            "": "",
        }
        for href, expected in cases.items():
            self.assertEqual(search.decode_ddg_url(href), expected, href)

    def test_uses_the_html_endpoint_with_our_browser_identity(self):
        http = FakeHttp((DDG_HTML, (200, DDG_PAGE)))
        results = self.client(http).search('"wedding photographer" "Austin" contact', 8, make_profile())
        self.assertEqual(len(results), 2)
        url, headers = http.calls[0]
        self.assertTrue(url.startswith(DDG_HTML + "?"))
        self.assertEqual(parse_qs(urlsplit(url).query)["q"], ['"wedding photographer" "Austin" contact'])
        self.assertEqual(headers["User-Agent"], SCRAPER_HEADERS["User-Agent"])
        self.assertEqual(self.robots_checked, [url], "DuckDuckGo is always checked against robots.txt")

    def test_polite_two_second_gap(self):
        http = FakeHttp((DDG_HTML, (200, DDG_PAGE)))
        client = self.client(http)
        for query in ("a", "b", "c"):
            client.search(query, 8, make_profile("duckduckgo"))
        self.assertEqual(self.time.sleeps, [2.0, 2.0])

    def test_captcha_page_stops_the_run_with_a_friendly_message(self):
        http = FakeHttp((DDG_HTML, (202, DDG_ANOMALY)))
        client = self.client(http)
        self.assertEqual(client.search("a", 8, make_profile()), [])
        self.assertEqual(client.search("b", 8, make_profile()), [])
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(self.messages(), [search.DDG_BLOCKED])
        self.assertIn("brave.com/search/api", search.DDG_BLOCKED)
        self.assertEqual(client.stopped_reason(make_profile()), search.DDG_BLOCKED)
        client.start_run()
        self.assertEqual(client.stopped_reason(make_profile()), "")

    def test_anomaly_detection(self):
        self.assertTrue(search.is_ddg_anomaly(200, DDG_ANOMALY))
        self.assertTrue(search.is_ddg_anomaly(429, ""))
        self.assertFalse(search.is_ddg_anomaly(200, DDG_NO_RESULTS))
        self.assertFalse(search.is_ddg_anomaly(200, DDG_PAGE))
        http = FakeHttp((DDG_HTML, (200, DDG_ANOMALY)))
        client = self.client(http)
        client.search("a", 8, make_profile())
        self.assertEqual(client.stopped_reason(make_profile()), search.DDG_BLOCKED)

    def test_a_result_mentioning_bots_is_not_a_captcha(self):
        page = DDG_PAGE.replace("Wedding films", "Unfortunately, bots use DuckDuckGo too")
        client = self.client(FakeHttp((DDG_HTML, (200, page))))
        self.assertEqual(len(client.search("a", 8, make_profile())), 2)
        self.assertEqual(client.stopped_reason(make_profile()), "")

    def test_no_results_is_just_empty(self):
        client = self.client(FakeHttp((DDG_HTML, (200, DDG_NO_RESULTS))))
        self.assertEqual(client.search("zzzz", 8, make_profile()), [])
        self.assertEqual((client.stopped_reason(make_profile()), self.logged), ("", []))

    def test_robots_txt_no_means_no_request(self):
        http = FakeHttp()
        client = self.client(http, robots=lambda url: False)
        self.assertEqual(client.search("a", 8, make_profile("duckduckgo")), [])
        self.assertEqual(http.calls, [])
        self.assertIn("robots.txt", client.stopped_reason(make_profile("duckduckgo")))
        self.assertIn("DuckDuckGo", self.messages()[0])


class TestBing(SearchTestCase):
    def test_parses_results_and_unwraps_ck_links(self):
        client = self.client(FakeHttp((BING_SEARCH, (200, BING_PAGE))))
        self.assertEqual(client.search("acme", 8, make_profile("bing")), [
            {"title": "Acme Studio", "url": "https://www.acmestudio.test/contact"},
            {"title": "Beta Photo", "url": "https://beta.test/"},
        ])

    def test_decode_bing_url(self):
        target = "https://a.test/?q=>>>"            # its base64 contains "+" (standard) / "-" (url-safe)
        standard = base64.b64encode(target.encode()).decode()
        urlsafe = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        self.assertIn("+", standard)
        for encoded in (standard, urlsafe):
            href = "https://www.bing.com/ck/a?!&&p=1&u=a1" + encoded + "&ntb=1"
            self.assertEqual(search.decode_bing_url(href), target)
        for href in ("https://www.bing.com/ck/a?p=1", "https://www.bing.com/ck/a?u=a1%%%", "not a url"):
            self.assertEqual(search.decode_bing_url(href), href)

    def test_bing_search_page_skips_robots_txt_only_when_bing_is_chosen(self):
        http = FakeHttp((BING_SEARCH, (200, BING_PAGE)))
        client = self.client(http, robots=lambda url: False)
        self.assertEqual(len(client.search("acme", 8, make_profile("bing"))), 2)
        self.assertEqual(self.robots_checked, [])
        self.assertTrue(http.urls[0].startswith(BING_SEARCH + "?q="))
        self.assertEqual(http.calls[0][1]["User-Agent"], SCRAPER_HEADERS["User-Agent"])

    def test_robots_exemption_is_narrow(self):
        cases = [
            ("bing", "https://www.bing.com/search?q=x", True),
            ("bing", "https://bing.com/search?q=x", True),
            ("bing", "https://WWW.BING.COM/search?q=x", True),
            ("bing", "https://www.bing.com/ck/a?u=a1xyz", False),
            ("bing", "https://www.bing.com/search/more", False),
            ("bing", "http://www.bing.com/search?q=x", False),
            ("bing", "https://www.bing.com.evil.test/search?q=x", False),
            ("bing", "https://acmestudio.test/search", False),
            ("bing", "https://html.duckduckgo.com/html/?q=x", False),
            ("duckduckgo", "https://www.bing.com/search?q=x", False),
            ("brave", "https://www.bing.com/search?q=x", False),
            ("auto", "https://www.bing.com/search?q=x", False),
        ]
        for engine, url, exempt in cases:
            self.assertIs(search.robots_exempt(engine, url), exempt, (engine, url))

    def test_other_engines_always_ask_robots_txt(self):
        http = FakeHttp((search.BRAVE_URL, (200, BRAVE_PAGE)), (DDG_HTML, (200, DDG_PAGE)))
        client = self.client(http, env={"BRAVE_API_KEY": BRAVE_KEY})
        client.search("a", 8, make_profile("brave"))
        client.search("a", 8, make_profile("duckduckgo"))
        self.assertEqual(self.robots_checked, http.urls)

    def test_warns_once_per_run_that_bing_breaks_its_rules(self):
        client = self.client(FakeHttp((BING_SEARCH, (200, BING_PAGE))))
        for query in ("a", "b"):
            client.search(query, 8, make_profile("bing"))
        self.assertEqual(self.messages(), [search.BING_WARNING])
        self.assertIn("robots.txt", search.BING_WARNING)
        client.start_run()
        client.search("c", 8, make_profile("bing"))
        self.assertEqual(self.messages().count(search.BING_WARNING), 2)

    def test_rate_limit_stops_bing(self):
        http = FakeHttp((BING_SEARCH, (429, "")))
        client = self.client(http)
        client.search("a", 8, make_profile("bing"))
        client.search("b", 8, make_profile("bing"))
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(client.stopped_reason(make_profile("bing")), search.BING_BLOCKED)


class TestSearchWeb(SearchTestCase):
    def use_client(self, http, env=None):
        patcher = mock.patch.object(search, "_CLIENT", self.client(http, env))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_search_web_uses_the_owners_engine(self):
        http = FakeHttp((DDG_HTML, (200, DDG_PAGE)))
        self.use_client(http)
        self.assertEqual(search.search_web("acme", 1, make_profile("duckduckgo")),
                         [{"title": "Acme Studio Contact", "url": "https://www.acmestudio.test/contact?a=1&b=2"}])

    def test_never_raises_and_skips_empty_queries(self):
        http = FakeHttp()   # any request would fail the test
        self.use_client(http)
        for query, limit in ((None, 8), ("   ", 8), ("ok", 0), ("ok", -3)):
            self.assertEqual(search.search_web(query, limit, make_profile()), [])
        self.assertEqual(http.calls, [])
        http.rules = [(DDG_HTML, [(200, DDG_PAGE)])]
        self.assertEqual(len(search.search_web("ok", "lots", make_profile())), 2)   # bad limit -> default
        with mock.patch.object(search.SearchClient, "_search_duckduckgo", side_effect=RuntimeError("boom")):
            self.assertEqual(search.search_web("ok", 8, make_profile()), [])

    def test_without_a_profile_it_reads_the_saved_one(self):
        http = FakeHttp((BING_SEARCH, (200, BING_PAGE)), (DDG_HTML, (200, DDG_PAGE)))
        self.use_client(http)
        with mock.patch.object(search, "load_profile", return_value=make_profile("bing")):
            search.search_web("acme")
        with mock.patch.object(search, "load_profile", side_effect=search.ProfileError("no profile")):
            search.search_web("acme")
        self.assertTrue(http.urls[0].startswith(BING_SEARCH))
        self.assertTrue(http.urls[1].startswith(DDG_HTML))

    def test_start_run_and_stopped_reason(self):
        self.use_client(FakeHttp((DDG_HTML, (202, DDG_ANOMALY))))
        search.search_web("a", 8, make_profile())
        self.assertEqual(search.search_stopped(make_profile()), search.DDG_BLOCKED)
        self.assertEqual(search.search_stopped(make_profile("bing")), "", "only the stopped engine")
        search.start_search_run()
        self.assertEqual(search.search_stopped(make_profile()), "")

    def test_clean_results(self):
        raw = [{"title": " A \n b ", "url": " https://a.test/ "}, {"title": "dupe", "url": "https://a.test/"},
               {"title": "rel", "url": "/relative"}, {"title": "js", "url": "javascript:alert(1)"},
               {"title": None, "url": "http://c.test"}, {"url": "https://[bad"}, {"title": "d", "url": "https://d.test"}]
        self.assertEqual(search.clean_results(raw, 2), [{"title": "A b", "url": "https://a.test/"},
                                                        {"title": "", "url": "http://c.test"}])

    def test_warnings_reach_the_activity_log_and_never_break_search(self):
        http = FakeHttp((DDG_HTML, (500, "")))
        client = self.client(http)
        with mock.patch.object(search, "log_event", side_effect=OSError("database is locked")):
            self.assertEqual(client.search("a", 8, make_profile()), [])


class TestRobotsRules(unittest.TestCase):
    def test_one_download_per_site_per_day_and_failures_allow(self):
        now = [0.0]
        calls = []
        pages = {"https://a.test/robots.txt": (200, "User-agent: *\nDisallow: /search\n"),
                 "https://down.test/robots.txt": OSError("down")}

        def fetch(url):
            calls.append(url)
            page = pages.get(url, (404, ""))
            if isinstance(page, BaseException):
                raise page
            return page

        rules = RobotsRules(fetch=fetch, clock=lambda: now[0])
        self.assertFalse(rules.allowed("https://a.test/search?q=x"))
        self.assertTrue(rules.allowed("https://A.test/about"))
        self.assertTrue(rules.allowed("https://down.test/search"))
        self.assertTrue(rules.allowed("mailto:x@a.test"))
        self.assertEqual(calls, ["https://a.test/robots.txt", "https://down.test/robots.txt"])
        now[0] += 24 * 60 * 60 + 1
        rules.allowed("https://a.test/")
        self.assertEqual(len(calls), 3)


class TestLeadgenUsesSearchWeb(unittest.TestCase):
    def setUp(self):
        from bots import leadgen_pipeline
        self.lp = leadgen_pipeline
        if not leadgen_pipeline.REQUESTS_AVAILABLE:
            self.skipTest("requests/bs4 are not installed")
        self.profile = make_profile("duckduckgo")
        self.results = []
        self.searches = []
        self.stopped = ""
        self.scraped = []
        self.sites = {}

        def fake_search(query, max_results=8, profile=None):
            self.searches.append((query, max_results, profile))
            return list(self.results)

        def fake_scrape(url, skip_domains=None):
            self.scraped.append(url)
            return self.sites.get(url, {})

        self.patches = [
            mock.patch.object(leadgen_pipeline, "search_web", fake_search),
            mock.patch.object(leadgen_pipeline, "search_stopped", lambda profile=None: self.stopped),
            mock.patch.object(leadgen_pipeline, "scrape_site", fake_scrape),
            mock.patch.object(leadgen_pipeline, "log_event", lambda *args: None),
            mock.patch.object(leadgen_pipeline, "time", types.SimpleNamespace(sleep=lambda s: None,
                                                                              monotonic=lambda: 0.0)),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def result(self, title, url):
        return {"title": title, "url": url}

    def test_option1_searches_each_profession_and_city_and_keeps_its_filters(self):
        self.results = [
            self.result("Top 10 Wedding Photographers in Austin", "https://listicle.test/best"),
            self.result("Acme on Yelp", "https://www.yelp.com/biz/acme"),
            self.result("Wedding blog", "https://blog.weddings.test/"),
            self.result("Known Studio", "https://known.test/"),
            self.result("Is this a studio?", "https://question.test/"),
            self.result("Acme Studio | Austin", "https://www.acmestudio.test/"),
        ]
        qualified = []

        def fake_qualify(profile, site, **kwargs):
            qualified.append(kwargs)
            return {"email": "hi@acmestudio.test", "status": "new"}

        option1 = self.lp.Option1_SearchPortfolioScraper(self.profile, self.lp.SKIP_DOMAINS_GLOBAL, {"known.test"})
        with mock.patch.object(self.lp, "qualify_site", fake_qualify):
            leads = option1.scrape(count=1)
        self.assertEqual(self.searches, [('"wedding photographer" "Austin" contact',
                                          self.lp.SEARCH_RESULTS_PER_QUERY, self.profile)])
        self.assertEqual(self.scraped, ["https://www.acmestudio.test/"])
        self.assertEqual((qualified[0]["source"], qualified[0]["page_title"]), ("Option1_WebSearch", "Acme Studio | Austin"))
        self.assertEqual(len(leads), 1)
        self.assertIn("acmestudio.test", option1.known_domains)

    def test_option1_caps_sites_per_query_at_eight(self):
        self.results = [self.result(f"Studio {i}", f"https://studio{i}.test/") for i in range(10)]
        option1 = self.lp.Option1_SearchPortfolioScraper(self.profile, set(), set())
        self.assertEqual(len(option1._search_results("q")), 8)

    def test_a_stopped_engine_ends_the_search_early(self):
        self.stopped = "DuckDuckGo asked us to prove we're human"
        option1 = self.lp.Option1_SearchPortfolioScraper(self.profile, set(), set())
        self.assertEqual(option1.scrape(count=2), [])
        self.assertEqual(self.searches, [])

    def test_option2_finds_the_first_website_that_is_not_a_directory(self):
        self.results = [self.result("Acme on Thumbtack", "https://www.thumbtack.com/tx/austin/acme"),
                        self.result("Acme on Yelp", "https://m.yelp.com/biz/acme"),
                        self.result("Acme on Facebook", "https://facebook.com/acme"),
                        self.result("Acme Studio", "https://www.acmestudio.test/"),
                        self.result("Other", "https://other.test/")]
        option2 = self.lp.Option2_ThumbtackCategoryScraper(self.profile, set(self.lp.SKIP_DOMAINS_GLOBAL) - {"thumbtack.com", "yelp.com"}, set())
        self.assertEqual(option2._find_studio_website("Acme Studio"), "https://www.acmestudio.test/")
        query = self.searches[0][0]
        self.assertTrue(query.startswith('"Acme Studio" '))
        self.assertTrue(query.endswith(" contact site:.com"))
        self.results = []
        self.assertEqual(option2._find_studio_website("Nobody"), "")

    def test_option3_reads_the_first_matching_site_it_has_not_seen(self):
        self.results = [self.result("Known", "https://known.test/"),
                        self.result("Wrong business", "https://wrong.test/"),
                        self.result("No emails", "https://noemail.test/"),
                        self.result("Golden Hour Films", "https://www.goldenhour.test/")]
        self.sites = {
            "https://wrong.test/": {"emails": ["a@wrong.test"], "page_text": "plumbing", "domain": "wrong.test"},
            "https://noemail.test/": {"emails": [], "page_text": "golden hour", "domain": "noemail.test"},
            "https://www.goldenhour.test/": {"emails": ["hi@goldenhour.test"], "page_text": "Golden Hour Films",
                                             "domain": "goldenhour.test"},
        }
        option3 = self.lp.Option3_ThumbtackCityScraper(self.profile, set(), {"known.test"})
        site = option3._find_studio_site("Golden Hour Films", "Austin, TX")
        self.assertEqual(site["domain"], "goldenhour.test")
        self.assertEqual(self.scraped, ["https://wrong.test/", "https://noemail.test/", "https://www.goldenhour.test/"])
        self.assertIn("goldenhour.test", option3.known_domains)
        self.assertTrue(self.searches[0][0].startswith('"Golden Hour Films" Austin '))

    def test_no_bing_scraping_left_in_the_lead_finder(self):
        source = inspect.getsource(self.lp)
        for leftover in ("bing.com/search", "b_algo", "decode_bing_url", "_via_bing"):
            self.assertNotIn(leftover, source)

    def test_each_run_starts_a_fresh_search_run_before_searching(self):
        order = []
        pipeline = self.lp.LeadGenPipeline.__new__(self.lp.LeadGenPipeline)
        pipeline.profile = self.profile
        pipeline.notifier = mock.Mock()
        pipeline.option1 = mock.Mock(scrape=lambda count: order.append("option1") or [])
        pipeline.option2 = mock.Mock(categories=[])
        pipeline.option3 = mock.Mock(city_urls=[])
        pipeline._save_leads = lambda candidates: []
        pipeline.export_to_csv = lambda: "leads.csv"
        with mock.patch.object(self.lp, "start_search_run", lambda: order.append("start")):
            result = pipeline.run_pipeline(count=2)
        self.assertEqual(order, ["start", "option1"])
        self.assertEqual(result["sources"]["option1_web_search"], 0)


class TestLeadStore(unittest.TestCase):
    """bots/lead_store.py was split out of the lead finder; the finder still re-exports it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", os.path.join(self.tmp.name, "t.db")), ("DB_DIR", self.tmp.name)):
            patcher = mock.patch.object(db, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()

    def test_save_skips_known_emails_and_known_domains_are_remembered(self):
        from bots import lead_store, leadgen_pipeline
        self.assertIs(leadgen_pipeline.export_leads_to_csv, lead_store.export_leads_to_csv)
        leads = [{"email": "Hi@AcmeStudio.test", "name": "Acme", "website": "https://www.acmestudio.test",
                  "status": "new", "fit_score": 80, "source": "Option1_WebSearch"},
                 {"email": "hi@acmestudio.test", "name": "Acme dupe"},
                 {"email": "jane@gmail.com", "name": "Jane", "status": "disqualified"}]
        saved = lead_store.save_leads(leads, "wedding photographer")
        self.assertEqual([lead["email"] for lead in saved], ["hi@acmestudio.test", "jane@gmail.com"])
        self.assertEqual(saved[0]["category"], "wedding photographer")
        self.assertEqual(lead_store.save_leads(leads[:1], "x"), [])
        self.assertEqual(lead_store.known_domains(), {"acmestudio.test"})


if __name__ == "__main__":
    unittest.main()
