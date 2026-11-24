"""Impressum / Imprint: link detection, visible-text extraction, token optimisation,
DeepSeek client and the status decision. Kept free of database code so it can be
unit-tested on its own.

Token optimisation in one paragraph (see optimize_text): an Impressum page is mostly
noise. Below MAX_TEXT_CHARS the text is sent unchanged. Above it we (1) drop boilerplate
lines, (2) cut everything before the first "Impressum / Angaben gemäß § 5 / Anbieter"
heading (head anchor), (3) find the LAST strong legal signal (phone +49, VAT DE+9 digits,
e-mail, HRB/HRA/Registergericht, Geschäftsführer/Vertreten durch...) and cut
TAIL_MARGIN characters after it - everything behind the last legal data point is footer,
disclaimers and links - (4) check that the cut still contains the anchor and a signal,
otherwise relax it, and (5) as last resorts keep only key lines or a head+tail window.
"""
import json
import os
import re
import time
from typing import Optional, Tuple
from urllib.parse import urldefrag, urljoin

import requests
from bs4 import BeautifulSoup

from status import Status

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ------------------------------------------------------------------ token budget
MAX_TEXT_CHARS = 8000   # ~2000 tokens with the rough rule tokens ~ len(text) // 4
TAIL_MARGIN = 400       # characters kept after the last legal data point
HEAD_MARGIN = 100       # characters kept before the head anchor


def estimate_tokens(text: str) -> int:
    return len(text) // 4


# ------------------------------------------------------------------ URLs and links
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
LINK_TEXTS = ("impressum", "imprint")


def has_scheme(web: Optional[str]) -> bool:
    return bool(web and _SCHEME.match(web.strip()))


def normalize_url(web: Optional[str]) -> Optional[str]:
    """None for an empty value; prepends https:// when the scheme is missing."""
    if web is None:
        return None
    web = web.strip()
    if not web:
        return None
    if not _SCHEME.match(web):
        web = "https://" + web.lstrip("/")
    return web


def make_soup(markup) -> BeautifulSoup:
    try:
        return BeautifulSoup(markup, "lxml")
    except Exception:  # lxml missing -> built-in parser
        return BeautifulSoup(markup, "html.parser")


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _usable_href(href: str) -> bool:
    href = href.strip().lower()
    return bool(href) and not href.startswith(("#", "javascript:", "mailto:", "tel:"))


def find_impressum_link(html, base_url: str) -> Optional[str]:
    """Absolute URL of the Impressum/Imprint link, or None.

    1. an <a> whose visible text (stripped, lower-cased) is "impressum" or "imprint";
    2. otherwise an <a> whose href contains impressum/imprint.
    Relative URLs are resolved with urljoin against the (final) home page URL.
    """
    soup = make_soup(html)
    anchors = [a for a in soup.find_all("a", href=True) if _usable_href(a["href"])]
    for a in anchors:
        if _collapse(a.get_text(" ", strip=True)).lower() in LINK_TEXTS:
            return urldefrag(urljoin(base_url, a["href"].strip()))[0]
    for a in anchors:
        href = a["href"].lower()
        if "impressum" in href or "imprint" in href:
            return urldefrag(urljoin(base_url, a["href"].strip()))[0]
    return None


# ------------------------------------------------------------------ visible text
_REMOVE_ALWAYS = ["head", "title", "script", "style", "noscript", "svg", "template"]
_REMOVE_LAYOUT = ["nav", "header", "footer", "form"]


def normalize_whitespace(text: str) -> str:
    lines = []
    for line in text.replace("\xa0", " ").replace("\u200b", "").split("\n"):
        line = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        if line and (not lines or lines[-1] != line):  # no empty / repeated lines
            lines.append(line)
    return "\n".join(lines)


def extract_visible_text(html, strip_layout: bool = True) -> str:
    """Visible text only (never HTML): script/style/noscript/svg and, by default,
    nav/header/footer/form are removed, whitespace is normalised."""
    soup = make_soup(html)
    for tag in soup(_REMOVE_ALWAYS + (_REMOVE_LAYOUT if strip_layout else [])):
        tag.decompose()
    return normalize_whitespace(soup.get_text("\n"))


# ------------------------------------------------------------------ legal signals
PHONE_RE = re.compile(r"(?:\+49|0049)[ \d()/.\-–]{6,}")
VAT_RE = re.compile(r"\bDE\s?\d{9}\b")
EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
REGISTER_RE = re.compile(r"\bHR[AB]\s?\d+|Registergericht|Amtsgericht", re.I)
# Only at the START of a line (a label such as "Vertreten durch", "Geschäftsführer: ..."):
# the same words appear all over the disclaimers ("... sind wir verantwortlich"), and a
# match there would drag the tail cut to the very end of the page.
RESPONSIBLE_RE = re.compile(
    r"^(?:Inhaltlich\s+)?Verantwortlich|^Vertreten durch|^Vertretungsberechtigt|"
    r"^Gesch(?:ä|ae)ftsf(?:ü|ue)hrer|^Inhaber|^V\.\s?i\.\s?S\.\s?d\.\s?P",
    re.I | re.M)

SIGNAL_PATTERNS = (PHONE_RE, VAT_RE, EMAIL_RE, REGISTER_RE)        # verification
TAIL_PATTERNS = SIGNAL_PATTERNS + (RESPONSIBLE_RE,)                # tail anchors

HEAD_RE = re.compile(
    r"impressum|imprint|legal notice|"
    r"angaben\s+gem(?:ä|ae)(?:ß|ss)\s*§\s*5\s*(?:tmg|ddg)|diensteanbieter|anbieter",
    re.I)


def has_signal(text: str) -> bool:
    """At least one strong signal: phone / VAT / e-mail / trade register."""
    return any(p.search(text) for p in SIGNAL_PATTERNS)


# ------------------------------------------------------------------ boilerplate
_BOILER_WORDS = [
    r"datenschutz(?:erkl(?:ä|ae)rung|hinweise?|bestimmungen)?", r"privacy(?: policy)?",
    r"agb", r"allgemeine gesch(?:ä|ae)ftsbedingungen",
    r"terms(?: (?:&|and) conditions| of (?:use|service))?",
    r"cookies?(?:[- ]?(?:einstellungen|settings|richtlinie|policy|hinweis|banner))?",
    r"barrierefreiheit", r"sitemap", r"newsletter(?: anmelden| abonnieren)?",
    r"men(?:ü|ue)|menu", r"startseite|home", r"suche|search", r"warenkorb|cart",
    r"login|anmelden|registrieren", r"zum inhalt springen|skip to (?:main )?content",
    r"nach oben|back to top|top", r"alle akzeptieren|akzeptieren|ablehnen|zustimmen|speichern",
    r"einstellungen|settings",
    r"deutsch|english|fran(?:ç|c)ais|espa(?:ñ|n)ol|italiano|de|en|fr|es|it",
    r"widerrufsbelehrung|widerrufsrecht", r"versand(?:kosten|bedingungen)?|zahlungsarten|lieferung",
    r"kontakt|contact", r"(?:ü|ue)ber uns|about us",
]
_BOILER_FULL = re.compile(r"^(?:" + "|".join(_BOILER_WORDS) + r")[\s:.!|/–\-]*$", re.I)
_BOILER_PREFIX = re.compile(
    r"^(?:©|\(c\)|copyright|alle rechte vorbehalten|all rights reserved)", re.I)
_COOKIE_LINE = re.compile(r"\bcookies?\b", re.I)
_IMPRESSUM_LINE = re.compile(r"^(?:impressum|imprint)[\s:.]*$", re.I)
_POSTCODE_RE = re.compile(r"\b\d{5}\b")
_ADDRESS_WORD = re.compile(r"stra(?:ß|ss)e|str\.|platz|weg\b|allee", re.I)


def _is_protected(line: str) -> bool:
    """Lines that carry legal data are never dropped as boilerplate."""
    return (any(p.search(line) for p in TAIL_PATTERNS)
            or bool(_POSTCODE_RE.search(line)) or bool(_ADDRESS_WORD.search(line)))


# A dropped footer-type line (Datenschutz, AGB, ©...) leaves this marker behind: the line
# itself is noise, but it shows WHERE the footer starts (used as a cut point, then removed).
FOOTER_MARK = "§§FOOTER§§"
_FOOTER_TYPE = re.compile(
    r"^(?:datenschutz|privacy|agb|allgemeine gesch|terms|cookie|©|\(c\)|copyright|"
    r"alle rechte vorbehalten|all rights reserved)", re.I)


def _strip_marks(text: str) -> str:
    return "\n".join(line for line in text.split("\n") if line != FOOTER_MARK)


def strip_boilerplate(text: str, mark_footer: bool = False) -> str:
    """Drop lines that are obviously not legal data: cookie banners, Datenschutz/AGB,
    duplicate "Impressum" menu items (the first one is kept as heading), © lines,
    menu words, language names... With mark_footer=True a dropped footer-type line
    leaves FOOTER_MARK behind (cut point for optimize_text)."""
    kept, seen_heading = [], False
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if _is_protected(line):
            kept.append(line)
        elif _IMPRESSUM_LINE.match(line):
            if not seen_heading:
                kept.append(line)
                seen_heading = True
        elif _BOILER_FULL.match(line) or _BOILER_PREFIX.match(line) or _COOKIE_LINE.search(line):
            if mark_footer and _FOOTER_TYPE.match(line) and (not kept or kept[-1] != FOOTER_MARK):
                kept.append(FOOTER_MARK)
        else:
            kept.append(line)
    return "\n".join(kept)


# ------------------------------------------------------------------ cut points
_FOOTER_LINE = re.compile(
    r"^(?:§§FOOTER§§|©|\(c\)|copyright|datenschutz|agb|cookie|alle rechte vorbehalten|"
    r"all rights reserved|powered by|webdesign|realisierung|"
    r"haftung|haftungsausschluss|disclaimer|urheberrecht|streitschlichtung|"
    r"verbraucherstreit|online-streitbeilegung|eu-streitschlichtung)", re.I)

_KEY_LINE = re.compile(
    r"stra(?:ß|ss)e|str\.|tel|fax|e-?mail|\bust|umsatzsteuer|\bhr[ab]\b|register|amtsgericht|"
    r"vertreten|gesch(?:ä|ae)ftsf|verantwortlich|inhaber|anschrift|adresse|"
    r"\b(?:gmbh|gbr|ug|ag|kg|ohg|e\.k\.|mbh)\b", re.I)


HEAD_LOOKBEHIND = 1500  # a heading further than this before the first signal is a menu item


def _head_index(text: str) -> Optional[int]:
    """Start of the heading that opens the legal block.

    The first match of "Impressum / Angaben gemäß § 5 / Anbieter / Diensteanbieter" is
    usually right, but it can be a menu item far away from the data. So: take the
    earliest heading that lies within HEAD_LOOKBEHIND characters before the first strong
    signal (phone/VAT/e-mail/register); if none is that close, the nearest one before it.
    """
    starts = [m.start() for m in HEAD_RE.finditer(text)]
    if not starts:
        return None
    signals = [m.start() for p in SIGNAL_PATTERNS for m in p.finditer(text)]
    if not signals:
        return starts[0]
    first_signal = min(signals)
    before = [i for i in starts if i <= first_signal]
    if not before:
        return starts[0]
    near = [i for i in before if first_signal - i <= HEAD_LOOKBEHIND]
    return near[0] if near else before[-1]


def _tail_end(text: str) -> Optional[int]:
    """End of the LAST strong legal signal in the text."""
    ends = [m.end() for p in TAIL_PATTERNS for m in p.finditer(text)]
    return max(ends) if ends else None


def _line_start(text: str, index: int, limit: int) -> int:
    """Start of the kept block: `index` moved FORWARD to the next line boundary (never
    beyond `limit`, the heading itself). Moving backwards could pull in a huge
    paragraph that sits in a single line right before the heading."""
    nl = text.find("\n", index)
    return nl + 1 if nl != -1 and nl + 1 <= limit else index


def _line_end(text: str, index: int, limit: int = 160) -> int:
    """Finish the current line (so we never cut in the middle of a data line)."""
    nl = text.find("\n", index)
    return nl if nl != -1 and nl - index <= limit else index


def _marker_cut(text: str, lo: int, hi: int) -> Optional[int]:
    """Start of the first footer/disclaimer line between lo and hi (cut right before it)."""
    pos = text.find("\n", lo)
    while pos != -1 and pos < hi:
        start = pos + 1
        end = text.find("\n", start)
        if _FOOTER_LINE.match(text[start:end if end != -1 else len(text)].strip()):
            return start
        pos = end
    return None


def _key_lines(text: str) -> str:
    """Keep only lines with a legal key (street, tel, e-mail, USt, HRB...) +/- 1 line."""
    lines = text.split("\n")
    keep = set()
    for i, line in enumerate(lines):
        if _KEY_LINE.search(line) or _POSTCODE_RE.search(line) or any(
                p.search(line) for p in TAIL_PATTERNS):
            keep.update({i - 1, i, i + 1})
    return "\n".join(lines[i] for i in sorted(keep) if 0 <= i < len(lines))


def _window(text: str, max_chars: int) -> str:
    """Last resort: first N + last N characters (legal data is rarely in the middle)."""
    half = max_chars // 2
    return text[:half] + "\n[...]\n" + text[-half:]


def _finish(original: str, result: str, info: dict, strategy) -> Tuple[str, dict]:
    info["strategy"] = "+".join(strategy)
    info["after_chars"] = len(result)
    info["after_tokens"] = estimate_tokens(result)
    info["saved_pct"] = (round(100 * (1 - len(result) / len(original)))
                         if original else 0)
    return result, info


def optimize_text(text: str, max_chars: int = MAX_TEXT_CHARS, tail_margin: int = TAIL_MARGIN,
                  head_margin: int = HEAD_MARGIN) -> Tuple[str, dict]:
    """Return (text to send to the LLM, statistics).

    <= max_chars: unchanged. Otherwise: boilerplate -> head anchor -> last tail anchor
    + margin ("sandwich") -> verification with relaxing -> key lines -> head+tail window.
    """
    info = {"before_chars": len(text), "before_tokens": estimate_tokens(text),
            "head_found": None, "tail_found": None}
    if len(text) <= max_chars:
        return _finish(text, text, info, ["unchanged"])

    strategy = ["boilerplate"]
    cleaned = strip_boilerplate(text, mark_footer=True)
    head = _head_index(cleaned)
    tail_end = _tail_end(cleaned)
    info["head_found"], info["tail_found"] = head is not None, tail_end is not None

    cut = cleaned
    if head is not None or tail_end is not None:
        start = _line_start(cleaned, max(0, head - head_margin), head) if head is not None else 0
        if tail_end is not None:
            end = _line_end(cleaned, min(len(cleaned), tail_end + tail_margin))
            footer = _marker_cut(cleaned, tail_end, end)
            if footer is not None:
                end = footer          # cut right before the footer / disclaimers
        else:
            end = len(cleaned)
        candidate = _strip_marks(cleaned[start:end]).strip()
        # verification: the cut must still hold the head anchor and a strong signal
        if (candidate and (head is None or HEAD_RE.search(candidate))
                and (tail_end is None or has_signal(candidate))):
            cut = candidate
            strategy.append("sandwich" if head is not None and tail_end is not None
                            else "head-cut" if head is not None else "tail-cut")
        else:
            strategy.append("relaxed-full")  # relax the cut: use the whole (cleaned) text

    cut = _strip_marks(cut)
    if len(cut) > max_chars:
        key = _key_lines(cut)
        if key and len(key) < len(cut):
            cut = key
            strategy.append("keylines")
    if len(cut) > max_chars:
        cut = _window(cut, max_chars)
        strategy.append("window")
    return _finish(text, cut, info, strategy)


def describe_optimization(info: dict) -> str:
    return (f"text {info['before_chars']} -> {info['after_chars']} chars "
            f"(~{info['before_tokens']} -> ~{info['after_tokens']} tokens, "
            f"-{info['saved_pct']}%) [{info['strategy']}]")


# ------------------------------------------------------------------ DeepSeek
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

SYSTEM_PROMPT = """\
You extract legally required company information ("Impressum" / "Imprint") from
German-language website text.
Return ONLY a valid JSON object (no markdown fences, no commentary) that parses
with json.loads. The JSON must have exactly these keys:
{
  "company_name": "<string or null>",
  "postal_address": {
    "street": "<string or null>",
    "house_no": "<string or null>",
    "postcode": "<string or null>",
    "city": "<string or null>",
    "country": "<string or null>"
  },
  "handelsregister": "<string or null>",
  "vat_number": "<string or null>",
  "phone": "<string or null>",
  "email": "<string or null>",
  "responsible_person": "<string or null>"
}
Rules:
- Extract ONLY information actually present in the text; use null when absent.
- Do not invent or guess values.
- phone: keep the international format when possible (e.g. +49 ...).
- vat_number: German VAT ids look like DE followed by 9 digits.
- email: lowercase.
- responsible_person: the named person or role legally responsible for the
  content (e.g. Geschaeftsfuehrer, Vorstand, "inhaltlich verantwortlich").
- Output valid JSON only.
"""


class DeepSeekError(Exception):
    """Transport / API failure (network error, HTTP 4xx/5xx, missing key) -> Status.ERROR."""


def build_messages(text: str) -> list:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",
         "content": f"Extract the Impressum data from the text below and return JSON:\n\n{text}"},
    ]


def parse_json_content(content) -> Optional[dict]:
    """json.loads with a tolerant fallback; None for blank / unparseable / non-object."""
    if not content or not content.strip():
        return None
    s = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.S)
    if fenced:
        s = fenced.group(1)
    candidates = [s]
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        candidates.append(s[i:j + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def call_deepseek(text: str, api_key: Optional[str] = None, model: Optional[str] = None,
                  base_url: Optional[str] = None, session=None, max_attempts: int = 3,
                  backoff: Optional[float] = None, sleep=time.sleep, timeout: int = 60,
                  max_tokens: int = 1024) -> Tuple[dict, dict]:
    """POST /chat/completions in JSON mode. Returns (parsed JSON object, token usage).

    * network error, HTTP 429/5xx: retried with backoff, then DeepSeekError;
    * other HTTP 4xx (e.g. 401 bad key): DeepSeekError at once;
    * blank / unparseable content (JSON mode may return spaces): retried with backoff,
      afterwards an EMPTY dict is returned -> the caller sets PARSING_FAILED.
    """
    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise DeepSeekError("DEEPSEEK_API_KEY is not set")
    model = model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL
    base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    http = session or requests
    if backoff is None:  # pause before a retry: backoff * 1, 2, 4 ... seconds
        backoff = float(os.environ.get("DEEPSEEK_RETRY_BACKOFF", "1.0"))
    payload = {
        "model": model,
        "messages": build_messages(text),
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    usage = {}
    for attempt in range(1, max_attempts + 1):
        last = attempt == max_attempts
        try:
            resp = http.post(f"{base_url}/chat/completions", headers=headers,
                             json=payload, timeout=timeout)
        except requests.RequestException as exc:
            if last:
                raise DeepSeekError(f"network error: {exc}") from exc
            sleep(backoff * 2 ** (attempt - 1))
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and not last:
            sleep(backoff * 2 ** (attempt - 1))
            continue
        if resp.status_code >= 400:
            raise DeepSeekError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            body = resp.json()
            usage = body.get("usage") or usage
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            content = None
        data = parse_json_content(content)
        if data is not None:
            return data, usage
        if not last:
            sleep(backoff * 2 ** (attempt - 1))
    return {}, usage


# ------------------------------------------------------------------ mapping + status
_NULLISH = {"", "null", "none", "n/a", "na", "-", "—", "nicht angegeben"}


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return None if s.lower() in _NULLISH else s


def normalize_extraction(data) -> dict:
    """Tolerant view of the model's JSON: every key present, empty values -> None."""
    data = data if isinstance(data, dict) else {}
    addr = data.get("postal_address")
    addr = addr if isinstance(addr, dict) else {}
    email = _clean(data.get("email"))
    return {
        "company_name": _clean(data.get("company_name")),
        "postal_address": {k: _clean(addr.get(k))
                           for k in ("street", "house_no", "postcode", "city", "country")},
        "handelsregister": _clean(data.get("handelsregister")),
        "vat_number": _clean(data.get("vat_number")),
        "phone": _clean(data.get("phone")),
        "email": email.lower() if email else None,
        "responsible_person": _clean(data.get("responsible_person")),
    }


def has_detected_fields(norm: dict) -> bool:
    flat = [v for k, v in norm.items() if k != "postal_address"]
    flat += list(norm["postal_address"].values())
    return any(flat)


def address_complete(norm: dict) -> bool:
    """postcode, city, street and house_no all non-empty (country is not required)."""
    a = norm["postal_address"]
    return all(a.get(k) for k in ("postcode", "city", "street", "house_no"))


def decide_status(norm: dict) -> Status:
    """4.6: PARSING_FAILED (nothing detected) -> ADDRESS_INCOMPLETE -> OK."""
    if not has_detected_fields(norm):
        return Status.PARSING_FAILED
    if not address_complete(norm):
        return Status.ADDRESS_INCOMPLETE
    return Status.OK


def format_postal_address(addr: dict) -> Optional[str]:
    """'Street 12, 10115 Berlin, Germany'"""
    street = " ".join(p for p in (addr.get("street"), addr.get("house_no")) if p)
    town = " ".join(p for p in (addr.get("postcode"), addr.get("city")) if p)
    joined = ", ".join(p for p in (street, town, addr.get("country")) if p)
    return joined or None


def to_db_fields(norm: dict, csv_company_name: str) -> dict:
    """Columns written back to companies (legal_name falls back to the CSV name)."""
    return {
        "legal_name": norm["company_name"] or csv_company_name,
        "postal_address": format_postal_address(norm["postal_address"]),
        "handelsregister": norm["handelsregister"],
        "vat_number": norm["vat_number"],
        "phone": norm["phone"],
        "email": norm["email"],
        "responsible_person": norm["responsible_person"],
    }
