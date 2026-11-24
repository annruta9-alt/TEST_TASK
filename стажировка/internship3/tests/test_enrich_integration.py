"""End to end: enrich_companies.py + real PostgreSQL + a local fake website and a local fake
DeepSeek API (OpenAI-compatible). Skipped without DB_*. No real API key is ever used."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import enrich_companies as enrich
from status import Status
from tests.helpers import LEGAL_BLOCK, bloated_page

API_KEY = "sk-test-not-a-real-key"

FULL = {"company_name": "Muster Nutrition GmbH",
        "postal_address": {"street": "Musterstraße", "house_no": "12", "postcode": "10115",
                           "city": "Berlin", "country": "Deutschland"},
        "handelsregister": "HRB 123456 B", "vat_number": "DE123456789", "phone": "+49 30 1234567",
        "email": "Info@Muster-Nutrition.de", "responsible_person": "Max Mustermann"}
INCOMPLETE = {**FULL, "postal_address": {**FULL["postal_address"], "house_no": None, "street": None}}


def small_page(marker):
    return f"<body><h1>Impressum</h1><p>{marker} GmbH</p><p>Musterstraße 12</p><p>10115 Berlin</p>" \
           f"<p>Tel: +49 30 1234567</p></body>"


# marker -> what the fake DeepSeek answers
ANSWERS = {"MARK_OK": ("json", FULL), "MARK_INC": ("json", INCOMPLETE), "MARK_EMPTY": ("json", {}),
           "MARK_BLANK": ("blank", None), "MARK_API": ("http", 500)}


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        home = "<body><footer><a href='impressum.html'>Impressum</a></footer></body>"
        pages = {
            "/ok/": home, "/ok/impressum.html": bloated_page(LEGAL_BLOCK.replace("Muster Nutrition GmbH", "MARK_OK Muster Nutrition GmbH")),
            "/inc/": home, "/inc/impressum.html": small_page("MARK_INC"),
            "/empty/": home, "/empty/impressum.html": small_page("MARK_EMPTY"),
            "/blank/": home, "/blank/impressum.html": small_page("MARK_BLANK"),
            "/api/": home, "/api/impressum.html": small_page("MARK_API"),
            "/nolink/": "<body><a href='/kontakt'>Kontakt</a></body>",
            "/err/": home,                                          # its Impressum page is a 404
        }
        body = pages.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeDeepSeek(BaseHTTPRequestHandler):
    requests_seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeDeepSeek.requests_seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                           "body": body})
        user = body["messages"][1]["content"]
        kind, value = next((v for k, v in ANSWERS.items() if k in user), ("json", {}))
        if self.headers.get("Authorization") != f"Bearer {API_KEY}":
            return self.reply(401, {"error": "Authentication Fails"})
        if kind == "http":
            return self.reply(value, {"error": "server error"})
        content = "   " if kind == "blank" else json.dumps(value)
        self.reply(200, {"choices": [{"message": {"content": content}}],
                         "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": 30}})

    def reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture(scope="module")
def servers():
    site, api = serve(Site), serve(FakeDeepSeek)
    yield site.server_address[1], api.server_address[1]
    site.shutdown()
    api.shutdown()


@pytest.fixture
def world(servers, clean_companies, monkeypatch):
    site_port, api_port = servers
    monkeypatch.setenv("DEEPSEEK_API_KEY", API_KEY)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", f"http://127.0.0.1:{api_port}")
    monkeypatch.setenv("DEEPSEEK_RETRY_BACKOFF", "0.01")
    FakeDeepSeek.requests_seen.clear()
    db = clean_companies
    # (name, web) in id order; 'ok' has no scheme -> https fails -> http fallback
    companies = [
        ("OK GmbH", f"127.0.0.1:{site_port}/ok/"),
        ("Incomplete AG", f"http://127.0.0.1:{site_port}/inc/"),
        ("Empty KG", f"http://127.0.0.1:{site_port}/empty/"),
        ("Blank KG", f"http://127.0.0.1:{site_port}/blank/"),
        ("Api GmbH", f"http://127.0.0.1:{site_port}/api/"),
        ("NoLink Ltd", f"http://127.0.0.1:{site_port}/nolink/"),
        ("Err GmbH", f"http://127.0.0.1:{site_port}/err/"),
        ("Down GmbH", "http://127.0.0.1:1/"),
        ("NoWeb AG", ""),
    ]
    conn = db.connect()
    with conn, conn.cursor() as cur:
        for name, web in companies:
            cur.execute("INSERT INTO companies (company_name, web) VALUES (%s, %s)", (name, web))
    conn.close()

    def rows():
        c = db.connect()
        try:
            with c, c.cursor() as cur:
                cur.execute("SELECT * FROM companies ORDER BY id")
                return {r["company_name"]: r for r in cur.fetchall()}
        finally:
            c.close()
    return rows


def main(*args):
    return enrich.main(["--delay", "0", *args])


def test_full_run_sets_every_status(world, capsys):
    assert main() == 0
    rows = world()
    got = {name: r["status"] for name, r in rows.items()}
    assert got == {
        "OK GmbH": Status.OK, "Incomplete AG": Status.ADDRESS_INCOMPLETE, "Empty KG": Status.PARSING_FAILED,
        "Blank KG": Status.PARSING_FAILED, "Api GmbH": Status.ERROR, "NoLink Ltd": Status.NO_IMPRESSUM_LINK_FOUND,
        "Err GmbH": Status.ERROR, "Down GmbH": Status.ERROR, "NoWeb AG": Status.NO_WEBSITE}
    out = capsys.readouterr().out
    assert "summary:" in out and API_KEY not in out                   # the key is never printed


def test_ok_company_is_fully_enriched(world):
    main()
    ok = world()["OK GmbH"]
    assert ok["impressum_url"].endswith("/ok/impressum.html")
    assert ok["legal_name"] == "Muster Nutrition GmbH"
    assert ok["postal_address"] == "Musterstraße 12, 10115 Berlin, Deutschland"
    assert (ok["handelsregister"], ok["vat_number"], ok["phone"]) == ("HRB 123456 B", "DE123456789", "+49 30 1234567")
    assert ok["email"] == "info@muster-nutrition.de" and ok["responsible_person"] == "Max Mustermann"
    assert ok["raw_json"] == FULL and ok["updated_at"] > ok["created_at"]


def test_failure_rows_keep_the_right_fields(world):
    main()
    rows = world()
    assert rows["NoLink Ltd"]["impressum_url"] is None and rows["NoLink Ltd"]["raw_json"] is None
    assert rows["Err GmbH"]["impressum_url"].endswith("/err/impressum.html")     # resolved, then 404
    assert rows["Api GmbH"]["impressum_url"].endswith("/api/impressum.html") and rows["Api GmbH"]["raw_json"] is None
    assert rows["Empty KG"]["raw_json"] == {} and rows["Empty KG"]["legal_name"] == "Empty KG"
    assert rows["Incomplete AG"]["phone"] == "+49 30 1234567"
    assert rows["NoWeb AG"]["impressum_url"] is None


def test_transport_errors_do_not_stop_the_batch(world, capsys):
    main()
    out = capsys.readouterr().out
    assert "[9/9] NoWeb AG" in out and "ERROR" in out and out.count("[") >= 9


def test_only_the_legal_block_is_sent_to_deepseek(world, capsys):
    main("--company", "OK GmbH")
    sent = [r for r in FakeDeepSeek.requests_seen if "MARK_OK" in r["body"]["messages"][1]["content"]]
    assert len(sent) == 1
    user = sent[0]["body"]["messages"][1]["content"]
    assert "Musterstraße 12" in user and "DE123456789" in user
    assert "Produkt Kategorie" not in user and "Footer Link" not in user and "Haftung für Inhalte" not in user
    assert "<" not in user and len(user) < 1500                       # not the 17 kB page
    out = capsys.readouterr().out
    assert "chars" in out and "tokens" in out and "[boilerplate+sandwich]" in out


def test_requests_follow_the_documented_api(world):
    main("--company", "OK GmbH")
    req = FakeDeepSeek.requests_seen[0]
    assert req["path"] == "/chat/completions" and req["auth"] == f"Bearer {API_KEY}"
    assert req["body"]["response_format"] == {"type": "json_object"} and req["body"]["temperature"] == 0


def test_blank_and_http_errors_are_retried_three_times(world):
    main("--company", "Blank KG")
    assert len(FakeDeepSeek.requests_seen) == 3
    FakeDeepSeek.requests_seen.clear()
    main("--company", "Api GmbH")
    assert len(FakeDeepSeek.requests_seen) == 3


def test_second_run_skips_ok_rows_and_retries_the_rest(world):
    main()
    first = len(FakeDeepSeek.requests_seen)
    before = world()
    main()
    second = FakeDeepSeek.requests_seen[first:]
    assert not any("MARK_OK" in r["body"]["messages"][1]["content"] for r in second)   # OK skipped
    assert world()["OK GmbH"]["updated_at"] == before["OK GmbH"]["updated_at"]          # untouched
    # not-OK rows that reach DeepSeek again: Incomplete (1) + Empty (1) + Blank (3) + Api (3)
    assert len(second) == 8


def test_company_option_reprocesses_an_ok_company_and_limit_works(world):
    main()
    n = len(FakeDeepSeek.requests_seen)
    main("--company", "OK GmbH")
    assert len(FakeDeepSeek.requests_seen) == n + 1
    FakeDeepSeek.requests_seen.clear()
    main("--status", "4", "--limit", "1")          # first ERROR row by id is "Api GmbH"
    seen = FakeDeepSeek.requests_seen
    assert len(seen) == 3 and all("MARK_API" in r["body"]["messages"][1]["content"] for r in seen)


def test_status_filter_selects_only_that_status(world, capsys):
    main()
    capsys.readouterr()
    main("--status", "5")
    out = capsys.readouterr().out
    assert "NoWeb AG" in out and "OK GmbH" not in out and "processing 1 company" in out


def test_pending_rows_are_the_default_selection(world, capsys):
    main("--limit", "2")
    rows = world()
    assert [n for n, r in rows.items() if r["status"] is not None] == ["OK GmbH", "Incomplete AG"]


def test_dry_run_neither_calls_deepseek_nor_writes(world, capsys):
    assert main("--dry-run") == 0
    assert FakeDeepSeek.requests_seen == []
    assert all(r["status"] is None for r in world().values())
    out = capsys.readouterr().out
    assert "chars" in out and "DRY-RUN" in out


def test_dry_run_works_without_an_api_key(world, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert main("--dry-run") == 0


def test_missing_api_key_stops_before_touching_anything(world, monkeypatch, capsys):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert main() == 2
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err
    assert all(r["status"] is None for r in world().values())


def test_wrong_api_key_marks_companies_as_error(world, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-wrong")
    main("--company", "OK GmbH")
    assert world()["OK GmbH"]["status"] == Status.ERROR
    assert len(FakeDeepSeek.requests_seen) == 1                         # 401 is not retried


def test_nothing_to_process(world, capsys):
    main()
    main("--status", "0", "--company", "NoWeb AG")                      # filter that matches nothing
    assert "nothing to process" in capsys.readouterr().out


def test_reimporting_the_csv_keeps_enrichment_results(world):
    import io
    import web_service
    main()
    client = web_service.app.test_client()
    csv_text = "Company Name;Web\nOK GmbH;www.changed-example.de\n"
    client.post("/upload_companies", data={"file": (io.BytesIO(csv_text.encode()), "c.csv")},
                content_type="multipart/form-data")
    ok = world()["OK GmbH"]
    assert ok["web"] == "www.changed-example.de" and ok["status"] == Status.OK and ok["vat_number"] == "DE123456789"
