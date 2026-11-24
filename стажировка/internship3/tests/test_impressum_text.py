"""URL normalisation, link detection, visible-text extraction, boilerplate removal."""
import pytest

import impressum as imp
from tests.helpers import bloated_page


# ----------------------------------------------------------------- 4.1 URL
@pytest.mark.parametrize("web,expected", [
    ("www.example.de", "https://www.example.de"),
    ("example.de", "https://example.de"),
    ("https://example.de", "https://example.de"),
    ("http://example.de", "http://example.de"),
    ("  https://example.de/path  ", "https://example.de/path"),
    ("//example.de", "https://example.de"),
    ("", None), ("   ", None), (None, None),
])
def test_normalize_url(web, expected):
    assert imp.normalize_url(web) == expected


def test_has_scheme():
    assert imp.has_scheme("https://a.de") and imp.has_scheme("http://a.de")
    assert not imp.has_scheme("a.de") and not imp.has_scheme(None)


# ----------------------------------------------------------------- 4.2 link
BASE = "https://www.acme.de/shop/"


def link(html, base=BASE):
    return imp.find_impressum_link(html, base)


def test_link_by_visible_text_case_insensitive_and_stripped():
    assert link("<a href='/legal'>  IMPRESSUM </a>") == "https://www.acme.de/legal"
    assert link("<a href='/legal'>Imprint</a>") == "https://www.acme.de/legal"
    assert link("<a href='/legal'>Impressum\u00a0</a>") == "https://www.acme.de/legal"


def test_text_match_wins_over_href_match():
    html = "<a href='/impressum-alt'>Rechtliches</a><a href='/legal'>Impressum</a>"
    assert link(html) == "https://www.acme.de/legal"


def test_href_fallback_when_text_differs():
    assert link("<a href='/rechtliches/impressum.html'>Rechtliche Hinweise</a>") == \
        "https://www.acme.de/rechtliches/impressum.html"
    assert link("<a href='/en/Imprint/'>Legal</a>") == "https://www.acme.de/en/Imprint/"


@pytest.mark.parametrize("href,expected", [
    ("/impressum", "https://www.acme.de/impressum"),
    ("impressum.html", "https://www.acme.de/shop/impressum.html"),
    ("../impressum.html", "https://www.acme.de/impressum.html"),
    ("https://other.de/impressum", "https://other.de/impressum"),
    ("//cdn.acme.de/impressum", "https://cdn.acme.de/impressum"),
    ("/impressum#top", "https://www.acme.de/impressum"),
])
def test_relative_links_are_resolved_with_urljoin(href, expected):
    assert link(f"<a href='{href}'>Impressum</a>") == expected


def test_no_link():
    assert link("<a href='/about'>Über uns</a><a href='/kontakt'>Kontakt</a>") is None
    assert link("") is None


@pytest.mark.parametrize("href", ["#", "#impressum", "javascript:void(0)", "mailto:impressum@acme.de", "tel:+49"])
def test_unusable_hrefs_are_ignored(href):
    assert link(f"<a href='{href}'>Impressum</a>") is None


def test_first_matching_link_is_used():
    html = "<a href='/a'>Impressum</a><a href='/b'>Impressum</a>"
    assert link(html) == "https://www.acme.de/a"


# ----------------------------------------------------------------- 4.3 visible text
def test_visible_text_has_no_html_and_no_noise():
    text = imp.extract_visible_text(bloated_page())
    assert "<" not in text and ">" not in text
    assert "var x=" not in text                                # script
    assert "Impressum | Muster" not in text                    # <title>
    assert "Produkt Kategorie" not in text                     # nav
    assert "Footer Link" not in text and "© 2024" not in text  # footer
    assert "Musterstraße 12" in text and "DE123456789" in text


def test_removes_script_style_noscript_svg_form_header():
    html = ("<body><script>a()</script><style>.x{}</style><noscript>enable js</noscript>"
            "<svg><text>icon</text></svg><form><label>Suche</label></form>"
            "<header>HEADER-TEXT</header><p>Kept paragraph</p></body>")
    assert imp.extract_visible_text(html) == "Kept paragraph"


def test_whitespace_is_normalised():
    html = "<p>Muster   GmbH</p>\n\n\n<p>  Musterstraße\u00a012  </p><p>Muster   GmbH</p>"
    assert imp.extract_visible_text(html) == "Muster GmbH\nMusterstraße 12\nMuster GmbH"


def test_layout_tags_can_be_kept():
    html = "<body><footer><p>Tel: +49 30 1234567</p></footer></body>"
    assert imp.extract_visible_text(html) == ""
    assert "+49 30 1234567" in imp.extract_visible_text(html, strip_layout=False)


def test_umlauts_survive_bytes_input():
    html = "<html><head><meta charset='utf-8'></head><body><p>Geschäftsführer Müller</p></body></html>"
    assert imp.extract_visible_text(html.encode("utf-8")) == "Geschäftsführer Müller"


# ----------------------------------------------------------------- boilerplate
def test_boilerplate_lines_are_dropped_and_legal_lines_kept():
    text = "\n".join([
        "Menü", "Startseite", "Deutsch", "English", "Impressum", "Impressum", "Datenschutz", "AGB",
        "Wir verwenden Cookies, um Ihnen das beste Erlebnis zu bieten.", "Alle akzeptieren",
        "© 2024 Muster GmbH", "Alle Rechte vorbehalten",
        "Muster GmbH", "Musterstraße 12", "10115 Berlin", "Tel: +49 30 1234567",
        "© 2024 Muster GmbH · Musterstraße 12 · 10115 Berlin",
    ])
    cleaned = imp.strip_boilerplate(text).split("\n")
    assert cleaned.count("Impressum") == 1                    # duplicate menu item dropped
    for dropped in ("Menü", "Startseite", "Deutsch", "Datenschutz", "AGB", "Alle akzeptieren",
                    "© 2024 Muster GmbH", "Alle Rechte vorbehalten"):
        assert dropped not in cleaned
    assert not any("Cookies" in line for line in cleaned)
    for kept in ("Muster GmbH", "Musterstraße 12", "10115 Berlin", "Tel: +49 30 1234567",
                 "© 2024 Muster GmbH · Musterstraße 12 · 10115 Berlin"):
        assert kept in cleaned                                 # data is never dropped
