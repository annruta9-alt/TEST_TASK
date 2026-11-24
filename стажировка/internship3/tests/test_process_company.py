"""Steps 4.1 - 4.6 for one company, with fake HTTP and a fake LLM (no network, no DB)."""
import pytest
import requests

import enrich_companies as enrich
import impressum as imp
from status import Status
from tests.helpers import LEGAL_BLOCK, bloated_page

HOME = "<html><body><nav><a href='/impressum.html'>Impressum</a></nav><p>Willkommen</p></body></html>"
GOOD = {"company_name": "Muster Nutrition GmbH",
        "postal_address": {"street": "Musterstraße", "house_no": "12", "postcode": "10115",
                           "city": "Berlin", "country": "Deutschland"},
        "handelsregister": "HRB 123456 B", "vat_number": "DE123456789", "phone": "+49 30 1234567",
        "email": "Info@Muster-Nutrition.de", "responsible_person": "Max Mustermann"}


class Web:
    """url -> html / exception; records every requested URL."""

    def __init__(self, pages):
        self.pages, self.requested = pages, []

    def __call__(self, url):
        self.requested.append(url)
        page = self.pages.get(url)
        if page is None:
            raise requests.HTTPError("404 Client Error: Not Found for url: " + url)
        if isinstance(page, Exception):
            raise page
        return url, page.encode("utf-8")


class LLM:
    def __init__(self, result=None, error=None, usage=None):
        self.result, self.error, self.usage, self.texts = result, error, usage or {}, []

    def __call__(self, text):
        self.texts.append(text)
        if self.error:
            raise self.error
        return self.result, self.usage


def run(row, web, llm=None, **kw):
    logs = []
    llm = llm or LLM(GOOD)
    outcome = enrich.process_company(row, web, llm, log=logs.append, **kw)
    return outcome, llm, logs


def row(web="https://acme.de", name="ACME GmbH"):
    return {"company_name": name, "web": web}


# ----------------------------------------------------------------- 4.1
@pytest.mark.parametrize("web", ["", "   ", None])
def test_empty_web_is_no_website(web):
    outcome, llm, _ = run(row(web), Web({}))
    assert outcome.status is Status.NO_WEBSITE and llm.texts == []


def test_scheme_is_prepended_before_fetching():
    web = Web({"https://acme.de": HOME, "https://acme.de/impressum.html": bloated_page()})
    run(row("acme.de"), web)
    assert web.requested[0] == "https://acme.de"


def test_http_is_tried_when_https_fails_for_a_url_without_scheme():
    class Flaky(Web):
        def __call__(self, url):
            if url.startswith("https://"):
                self.requested.append(url)
                raise requests.ConnectionError("ssl")
            return super().__call__(url)
    web = Flaky({"http://acme.de": HOME, "http://acme.de/impressum.html": bloated_page()})
    outcome, _, _ = run(row("acme.de"), web)
    assert outcome.status is Status.OK and web.requested[:2] == ["https://acme.de", "http://acme.de"]


def test_no_http_fallback_when_the_scheme_was_given():
    class Down(Web):
        def __call__(self, url):
            self.requested.append(url)
            raise requests.ConnectionError("down")
    web = Down({})
    outcome, _, _ = run(row("https://acme.de"), web)
    assert outcome.status is Status.ERROR and web.requested == ["https://acme.de"]


# ----------------------------------------------------------------- 4.2 transport errors / no link
@pytest.mark.parametrize("error", [requests.ConnectionError("refused"), requests.Timeout("slow"),
                                   requests.HTTPError("500 Server Error")])
def test_unreachable_home_page_is_error(error):
    outcome, llm, _ = run(row(), Web({"https://acme.de": error}))
    assert outcome.status is Status.ERROR and "home page" in outcome.note and llm.texts == []


def test_home_page_http_404_is_error():
    outcome, _, _ = run(row(), Web({}))           # unknown URL -> HTTPError 404
    assert outcome.status is Status.ERROR


def test_no_impressum_link_is_status_1_and_the_home_url_is_logged():
    web = Web({"https://acme.de": "<a href='/kontakt'>Kontakt</a>"})
    outcome, llm, logs = run(row(), web)
    assert outcome.status is Status.NO_IMPRESSUM_LINK_FOUND and int(outcome.status) == 1
    assert llm.texts == [] and any("https://acme.de" in line for line in logs)
    assert web.requested == ["https://acme.de"]            # the Impressum page was not fetched


def test_redirected_home_url_is_the_base_for_relative_links():
    class Redirect(Web):
        def __call__(self, url):
            self.requested.append(url)
            if url == "https://acme.de":
                return "https://www.acme.de/de/", HOME.replace("/impressum.html", "legal/impressum.html").encode()
            return super().__call__(url)
    web = Redirect({"https://www.acme.de/de/legal/impressum.html": bloated_page()})
    outcome, _, _ = run(row(), web)
    assert outcome.fields["impressum_url"] == "https://www.acme.de/de/legal/impressum.html"


def test_javascript_fallback_is_used_only_when_the_html_has_no_link():
    web = Web({"https://acme.de": "<p>footer is rendered by JS</p>",
               "https://acme.de/impressum.html": bloated_page()})
    calls = []
    def render(url):
        calls.append(url)
        return url, HOME
    outcome, _, _ = run(row(), web, render=render)
    assert calls == ["https://acme.de"] and outcome.status is Status.OK
    calls.clear()
    web = Web({"https://acme.de": HOME, "https://acme.de/impressum.html": bloated_page()})
    run(row(), web, render=render)
    assert calls == []                                      # link found in the raw HTML


def test_failing_javascript_fallback_just_means_no_link():
    def render(url):
        raise RuntimeError("no chromium")
    outcome, _, logs = run(row(), Web({"https://acme.de": "<p>x</p>"}), render=render)
    assert outcome.status is Status.NO_IMPRESSUM_LINK_FOUND


# ----------------------------------------------------------------- 4.3 impressum fetch
def test_impressum_url_is_stored_and_a_404_there_is_an_error():
    web = Web({"https://acme.de": HOME})                    # /impressum.html -> 404
    outcome, llm, _ = run(row(), web)
    assert outcome.status is Status.ERROR and llm.texts == []
    assert outcome.fields == {"impressum_url": "https://acme.de/impressum.html"}


def test_the_llm_gets_text_not_html():
    web = Web({"https://acme.de": HOME, "https://acme.de/impressum.html": bloated_page()})
    _, llm, _ = run(row(), web)
    sent = llm.texts[0]
    assert "<" not in sent and "script" not in sent.lower().replace("scripts", "")
    assert "Musterstraße 12" in sent


def test_legal_block_inside_a_footer_still_reaches_the_llm():
    page = "<body><main><p>Willkommen</p></main><footer>" + LEGAL_BLOCK + "</footer></body>"
    web = Web({"https://acme.de": HOME, "https://acme.de/impressum.html": page})
    outcome, llm, _ = run(row(), web)
    assert "DE123456789" in llm.texts[0] and outcome.status is Status.OK


# ----------------------------------------------------------------- 4.4 token budget
def test_big_pages_are_optimised_and_the_effect_is_logged():
    page = bloated_page()
    full_text = imp.extract_visible_text(page)
    web = Web({"https://acme.de": HOME, "https://acme.de/impressum.html": page})
    _, llm, logs = run(row(), web)
    assert len(llm.texts[0]) < len(full_text) / 5
    line = next(l for l in logs if "chars" in l)
    assert f"{len(full_text)} -> {len(llm.texts[0])} chars" in line and "tokens" in line


def test_small_pages_are_sent_unchanged():
    page = "<body><h1>Impressum</h1><p>Muster GmbH</p><p>Tel: +49 30 1234567</p></body>"
    web = Web({"https://acme.de": HOME, "https://acme.de/impressum.html": page})
    _, llm, logs = run(row(), web)
    assert llm.texts[0] == imp.extract_visible_text(page) and "[unchanged]" in logs[0]


def test_max_chars_option_lowers_the_budget():
    page = bloated_page(legal=LEGAL_BLOCK)
    web = Web({"https://acme.de": HOME, "https://acme.de/impressum.html": page})
    _, llm_default, _ = run(row(), web)
    _, llm_small, _ = run(row(), web, max_chars=300)
    assert len(llm_small.texts[0]) <= len(llm_default.texts[0])


# ----------------------------------------------------------------- 4.5 / 4.6
def full_web():
    return Web({"https://acme.de": HOME, "https://acme.de/impressum.html": bloated_page()})


def test_ok_flow_fills_every_field():
    outcome, _, _ = run(row(), full_web(), LLM(GOOD, usage={"prompt_tokens": 90, "completion_tokens": 50}))
    assert outcome.status is Status.OK
    f = outcome.fields
    assert f["impressum_url"] == "https://acme.de/impressum.html"
    assert f["legal_name"] == "Muster Nutrition GmbH"
    assert f["postal_address"] == "Musterstraße 12, 10115 Berlin, Deutschland"
    assert (f["handelsregister"], f["vat_number"], f["phone"]) == ("HRB 123456 B", "DE123456789", "+49 30 1234567")
    assert f["email"] == "info@muster-nutrition.de" and f["responsible_person"] == "Max Mustermann"
    assert f["raw_json"] == GOOD                                       # the whole parsed object


def test_deepseek_token_usage_is_logged():
    _, _, logs = run(row(), full_web(), LLM(GOOD, usage={"prompt_tokens": 90, "completion_tokens": 50}))
    assert any("prompt 90" in l and "completion 50" in l for l in logs)


def test_address_incomplete():
    data = {**GOOD, "postal_address": {**GOOD["postal_address"], "house_no": None}}
    outcome, _, _ = run(row(), full_web(), LLM(data))
    assert outcome.status is Status.ADDRESS_INCOMPLETE and int(outcome.status) == 3
    assert outcome.fields["vat_number"] == "DE123456789"                # still stored


def test_empty_json_is_parsing_failed():
    outcome, _, _ = run(row(), full_web(), LLM({}))
    assert outcome.status is Status.PARSING_FAILED and int(outcome.status) == 2
    assert outcome.fields["raw_json"] == {} and outcome.fields["legal_name"] == "ACME GmbH"
    assert outcome.fields["impressum_url"] == "https://acme.de/impressum.html"


def test_deepseek_failure_is_error_and_keeps_the_resolved_url():
    outcome, _, _ = run(row(), full_web(), LLM(error=imp.DeepSeekError("HTTP 500: boom")))
    assert outcome.status is Status.ERROR and int(outcome.status) == 4
    assert outcome.fields == {"impressum_url": "https://acme.de/impressum.html"}
    assert "DeepSeek" in outcome.note


def test_dry_run_never_calls_the_llm():
    outcome, llm, logs = run(row(), full_web(), dry_run=True)
    assert outcome.status is None and llm.texts == []
    assert any("chars" in l for l in logs)


# ----------------------------------------------------------------- fetcher / robots
class FakeSession:
    def __init__(self, robots="User-agent: *\nDisallow: /impressum", pages=None):
        self.robots, self.pages, self.urls = robots, pages or {}, []
        self.headers = {}

    def get(self, url, timeout=None, allow_redirects=True):
        self.urls.append(url)
        class R:
            pass
        r = R()
        r.url = url
        if url.endswith("/robots.txt"):
            r.status_code, r.text, r.content = 200, self.robots, b""
        else:
            r.status_code, r.text, r.content = 200, "", self.pages.get(url, b"<html></html>")
        r.raise_for_status = lambda: None
        return r


def test_robots_txt_is_honoured_when_requested():
    fetcher = enrich.Fetcher(session=FakeSession(), respect_robots=True)
    assert fetcher("https://acme.de/")[0] == "https://acme.de/"
    with pytest.raises(requests.RequestException, match="robots"):
        fetcher("https://acme.de/impressum")


def test_robots_txt_is_ignored_by_default():
    session = FakeSession()
    enrich.Fetcher(session=session)("https://acme.de/impressum")
    assert not any(u.endswith("robots.txt") for u in session.urls)


def test_fetcher_sets_a_browser_like_user_agent():
    headers = enrich.make_session().headers
    assert "Mozilla" in headers["User-Agent"] and "de" in headers["Accept-Language"]
