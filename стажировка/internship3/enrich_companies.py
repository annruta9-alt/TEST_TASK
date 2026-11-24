#!/usr/bin/env python3
"""Enrichment: for every company in PostgreSQL open its website, find the Impressum /
Imprint page, extract the legal data with DeepSeek and write it back.

    python enrich_companies.py [--company "ACME GmbH"] [--limit N] [--status 0]
                               [--dry-run] [--max-chars 8000] [--delay 1.0]
                               [--respect-robots] [--js-fallback]

Default selection: rows that are not OK yet (status IS NULL OR status <> 0), by id.
--company processes exactly that company whatever its status (an explicit request).
--dry-run fetches and optimises the text and logs the token savings, but calls neither
DeepSeek nor writes to the database - handy to check a page without spending tokens.

DB settings: DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD (same as the web service).
The DeepSeek key is read from DEEPSEEK_API_KEY (environment or a .env file), never stored.
"""
import argparse
import os
import sys
import time
import urllib.robotparser
from contextlib import closing
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import requests

import db
import impressum
from status import Status, status_name

MAX_PAGE_BYTES = 5_000_000
WRITABLE_COLUMNS = {
    "status", "impressum_url", "legal_name", "postal_address", "handelsregister",
    "vat_number", "phone", "email", "responsible_person", "raw_json",
}


# ------------------------------------------------------------------ small helpers
def load_dotenv(path: Optional[str] = None) -> None:
    """Minimal .env reader (stdlib only). Real environment variables win."""
    paths = [path] if path else [os.path.join(os.getcwd(), ".env"),
                                 os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")]
    for candidate in paths:
        if not os.path.isfile(candidate):
            continue
        with open(candidate, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key.startswith("export "):
                    key = key[7:].strip()
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": impressum.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
    })
    return session


class Fetcher:
    """GET with a User-Agent and timeouts; optionally honours robots.txt."""

    def __init__(self, session=None, respect_robots=False, timeout=(10, 25)):
        self.session = session or make_session()
        self.respect_robots = respect_robots
        self.timeout = timeout
        self._robots = {}

    def _allowed(self, url: str) -> bool:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                resp = self.session.get(origin + "/robots.txt", timeout=10)
                parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except requests.RequestException:
                parser.parse([])  # robots.txt unreachable -> allowed
            self._robots[origin] = parser
        return self._robots[origin].can_fetch(impressum.USER_AGENT, url)

    def __call__(self, url: str):
        """Returns (final URL after redirects, content bytes); raises RequestException."""
        if self.respect_robots and not self._allowed(url):
            raise requests.RequestException("blocked by robots.txt")
        resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        resp.raise_for_status()  # HTTP 4xx/5xx -> transport failure -> status ERROR
        return resp.url, resp.content[:MAX_PAGE_BYTES]


def render_html(url: str):
    """Optional fallback for JavaScript-rendered pages (needs Playwright + Chromium)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=impressum.USER_AGENT)
            page.goto(url, wait_until="networkidle", timeout=30000)
            return page.url, page.content()
        finally:
            browser.close()


# ------------------------------------------------------------------ one company
@dataclass
class Outcome:
    status: Optional[Status]                 # None only in --dry-run
    fields: dict = field(default_factory=dict)
    note: str = ""


def _fetch_home(http_get, url: str, auto_scheme: bool):
    """Home page; for a URL without scheme we prepended https:// - fall back to http://."""
    try:
        return http_get(url)
    except requests.RequestException:
        if auto_scheme and url.startswith("https://"):
            return http_get("http://" + url[len("https://"):])
        raise


def process_company(row: dict, http_get, llm, *, max_chars: int = impressum.MAX_TEXT_CHARS,
                    render=None, dry_run: bool = False, log=print) -> Outcome:
    """Steps 4.1 - 4.6 for one company. `http_get(url) -> (final_url, content)` and
    `llm(text) -> (json dict, usage)` are injected so the flow can be tested offline."""
    name = row["company_name"]

    # 4.1 normalise the URL
    url = impressum.normalize_url(row.get("web"))
    if url is None:
        return Outcome(Status.NO_WEBSITE, note="web column is empty")

    # 4.2 home page -> Impressum / Imprint link
    try:
        home_url, home_html = _fetch_home(http_get, url, not impressum.has_scheme(row.get("web")))
    except requests.RequestException as exc:
        return Outcome(Status.ERROR, note=f"home page {url}: {exc}")
    link = impressum.find_impressum_link(home_html, home_url)
    if link is None and render is not None:  # footer rendered by JavaScript?
        try:
            rendered_url, rendered_html = render(home_url)
            link = impressum.find_impressum_link(rendered_html, rendered_url)
        except Exception as exc:  # fallback is best effort
            log(f"    JS fallback failed: {exc}")
    if link is None:
        log(f"    no Impressum/Imprint link on {home_url}")
        return Outcome(Status.NO_IMPRESSUM_LINK_FOUND, note=f"no link on {home_url}")
    fields = {"impressum_url": link}

    # 4.3 Impressum page -> visible text (not HTML)
    try:
        imp_url, imp_html = http_get(link)
    except requests.RequestException as exc:
        return Outcome(Status.ERROR, fields, note=f"Impressum page {link}: {exc}")
    text = impressum.extract_visible_text(imp_html)
    if not impressum.has_signal(text):
        # nav/header/footer removal may have eaten the legal block: look at everything
        relaxed = impressum.extract_visible_text(imp_html, strip_layout=False)
        if impressum.has_signal(relaxed):
            text = relaxed

    # 4.4 token budget
    optimized, info = impressum.optimize_text(text, max_chars=max_chars)
    log(f"    {impressum.describe_optimization(info)}")
    if dry_run:
        return Outcome(None, fields, note="dry-run: DeepSeek not called")

    # 4.5 DeepSeek
    try:
        data, usage = llm(optimized)
    except impressum.DeepSeekError as exc:
        return Outcome(Status.ERROR, fields, note=f"DeepSeek: {exc}")
    if usage:
        log(f"    DeepSeek tokens: prompt {usage.get('prompt_tokens')}, "
            f"completion {usage.get('completion_tokens')}")

    # 4.6 status + fields
    norm = impressum.normalize_extraction(data)
    status = impressum.decide_status(norm)
    fields.update(impressum.to_db_fields(norm, name))
    fields["raw_json"] = data
    return Outcome(status, fields)


# ------------------------------------------------------------------ database
def select_companies(company=None, status=None, limit=None):
    """Rows to process, in order of id. `status`: None, an int, or 'pending'."""
    sql, params, where = "SELECT id, company_name, web, status FROM companies", [], []
    if company:
        where.append("company_name = %s")
        params.append(company)
    if status == "pending":
        where.append("status IS NULL")
    elif status is not None:
        where.append("status = %s")
        params.append(status)
    elif not company:  # default: everything that is not OK yet
        where.append("(status IS NULL OR status <> %s)")
        params.append(int(Status.OK))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with closing(db.connect()) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def save_outcome(company_id: int, outcome: Outcome) -> None:
    """New connection per operation; column names come from a fixed whitelist, values
    are always passed as parameters."""
    columns = {"status": int(outcome.status), **outcome.fields}
    unknown = set(columns) - WRITABLE_COLUMNS
    if unknown:
        raise ValueError(f"unexpected columns: {unknown}")
    sets, params = [], []
    for key, value in columns.items():
        sets.append(f"{key} = %s")
        params.append(psycopg2.extras.Json(value) if key == "raw_json" else value)
    sets.append("updated_at = NOW()")
    with closing(db.connect()) as conn, conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE companies SET {', '.join(sets)} WHERE id = %s",
                        params + [company_id])


# ------------------------------------------------------------------ command line
def parse_status(value: str):
    if value.lower() in ("pending", "null", "none"):
        return "pending"
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use a status code (0-5) or 'pending'")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--company", help="process only this company (any status)")
    ap.add_argument("--limit", type=int, help="process at most N rows")
    ap.add_argument("--status", type=parse_status,
                    help="only rows with this status (default: not yet OK)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + optimise only: no DeepSeek call, no database writes")
    ap.add_argument("--max-chars", type=int, default=impressum.MAX_TEXT_CHARS,
                    help=f"token budget in characters (default {impressum.MAX_TEXT_CHARS})")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds to wait between companies (politeness, default 1)")
    ap.add_argument("--respect-robots", action="store_true", help="honour robots.txt")
    ap.add_argument("--js-fallback", action="store_true",
                    help="render the home page with Playwright if no link is found in the HTML")
    return ap


def main(argv=None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key and not args.dry_run:
        print("error: DEEPSEEK_API_KEY is not set (put it into .env or export it; "
              "or use --dry-run)", file=sys.stderr)
        return 2

    db.init_db()  # idempotent: the table exists even if the web service never started
    rows = select_companies(args.company, args.status, args.limit)
    if not rows:
        print("nothing to process")
        return 0

    fetcher = Fetcher(respect_robots=args.respect_robots)
    llm = lambda text: impressum.call_deepseek(text, api_key=api_key)  # noqa: E731
    render = render_html if args.js_fallback else None
    counts = {}
    print(f"processing {len(rows)} compan{'y' if len(rows) == 1 else 'ies'}"
          f"{' (dry run)' if args.dry_run else ''}", flush=True)

    try:
        for i, row in enumerate(rows, 1):
            print(f"[{i}/{len(rows)}] {row['company_name']}  (web: {row['web'] or '-'})", flush=True)
            try:
                outcome = process_company(row, fetcher, llm, max_chars=args.max_chars,
                                          render=render, dry_run=args.dry_run,
                                          log=lambda m: print(m, flush=True))
            except Exception as exc:  # never let one company stop the batch
                outcome = Outcome(Status.ERROR, note=f"unexpected error: {exc!r}")
            label = "DRY-RUN" if outcome.status is None else status_name(int(outcome.status))
            print(f"    -> {label}{': ' + outcome.note if outcome.note else ''}", flush=True)
            counts[label] = counts.get(label, 0) + 1
            if outcome.status is not None and not args.dry_run:
                save_outcome(row["id"], outcome)   # --dry-run never writes to the database
            if i < len(rows) and args.delay > 0:
                time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)

    print("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
