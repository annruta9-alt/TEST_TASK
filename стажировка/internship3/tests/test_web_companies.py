"""Company endpoints and the CSV import against a real PostgreSQL (skipped without DB_*)."""
import io

import pytest

HEADER = "URL;Company Name;Language;Country;Postcode;City;Street;HouseNo;Web"


@pytest.fixture
def client(clean_companies):
    import web_service
    web_service.app.config["TESTING"] = True
    return web_service.app.test_client()


def upload(client, data, name="companies.csv"):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return client.post("/upload_companies", data={"file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def sql(database, query, params=None):
    conn = database.connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


CSV = (f"{HEADER}\n"
       '"https://duckduckgo.com";"ACME GmbH";"German";"Germany";"10115";"Berlin";"Main Street";"12";"https://www.acme-gmbh.de"\n'
       '"https://duckduckgo.com";"nutrimmun GmbH";;;;;;;"nutrimmun.de"\n')


# ----------------------------------------------------------------- the form
def test_upload_form_is_a_working_html_page(client):
    r = client.get("/upload_companies")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and r.mimetype == "text/html"
    assert '<form method="post" action="/upload_companies" enctype="multipart/form-data">' in html
    assert '<input type="file" name="file"' in html and 'type="submit"' in html
    assert 'href="/companies"' in html and "<script" not in html


# ----------------------------------------------------------------- import
def test_import_counts_and_stores_the_columns_as_provided(client, database):
    r = upload(client, CSV)
    assert r.status_code == 200
    assert r.get_json() == {"imported": 2, "updated": 0, "total_rows": 2, "skipped": 0}
    rows = sql(database, "SELECT * FROM companies ORDER BY id")
    acme = rows[0]
    assert (acme["company_name"], acme["language"], acme["country"], acme["postcode"], acme["city"],
            acme["street"], acme["house_no"], acme["web"]) == (
        "ACME GmbH", "German", "Germany", "10115", "Berlin", "Main Street", "12", "https://www.acme-gmbh.de")
    assert rows[1]["web"] == "nutrimmun.de" and rows[1]["city"] is None      # stored as-is, empty -> NULL
    assert acme["status"] is None and acme["raw_json"] is None                # fresh import = pending


def test_reimport_is_idempotent(client, database):
    upload(client, CSV)
    r = upload(client, CSV)
    assert r.get_json() == {"imported": 0, "updated": 2, "total_rows": 2, "skipped": 0}
    assert sql(database, "SELECT COUNT(*) AS n FROM companies")[0]["n"] == 2


def test_reimport_updates_csv_columns_but_never_the_enrichment_results(client, database):
    upload(client, CSV)
    sql(database, """UPDATE companies SET status = 0, impressum_url = 'https://x/impressum',
        legal_name = 'ACME Legal GmbH', postal_address = 'Main Street 12, 10115 Berlin',
        handelsregister = 'HRB 1', vat_number = 'DE123456789', phone = '+49 1', email = 'a@b.de',
        responsible_person = 'Max', raw_json = '{"k": 1}' WHERE company_name = 'ACME GmbH'""")
    changed = CSV.replace("https://www.acme-gmbh.de", "https://new.acme.de").replace("Berlin", "Hamburg")
    assert upload(client, changed).get_json()["updated"] == 2
    row = sql(database, "SELECT * FROM companies WHERE company_name = 'ACME GmbH'")[0]
    assert row["web"] == "https://new.acme.de" and row["city"] == "Hamburg"          # CSV columns updated
    assert row["status"] == 0 and row["impressum_url"] == "https://x/impressum"      # results untouched
    assert (row["legal_name"], row["handelsregister"], row["vat_number"], row["phone"],
            row["email"], row["responsible_person"]) == ("ACME Legal GmbH", "HRB 1", "DE123456789",
                                                         "+49 1", "a@b.de", "Max")
    assert row["raw_json"] == {"k": 1} and row["postal_address"] == "Main Street 12, 10115 Berlin"


def test_new_rows_are_added_to_existing_ones(client):
    upload(client, CSV)
    more = CSV + '"https://duckduckgo.com";"New AG";;;;;;;"new.de"\n'
    assert upload(client, more).get_json() == {"imported": 1, "updated": 2, "total_rows": 3, "skipped": 0}


def test_csv_parsing_follows_the_format(client, database):
    tricky = (f"\ufeff{HEADER}\r\n"                                   # BOM + Windows line endings
              '"https://duckduckgo.com";"Müller; Söhne ""KG""";"German";"Germany";"80331";"München";"Ludwigstraße";"1";"www.mueller.de"\r\n'
              ';;;;;;;;\r\n'                                           # row without a company name
              '"u";"Spaced AG  ";;;;;;;"  spaced.de  "\r\n')
    r = upload(client, tricky).get_json()
    assert r == {"imported": 2, "updated": 0, "total_rows": 3, "skipped": 1}
    names = [x["company_name"] for x in sql(database, "SELECT company_name FROM companies ORDER BY id")]
    assert names == ['Müller; Söhne "KG"', "Spaced AG"]               # quotes, ';' inside, trimmed
    assert sql(database, "SELECT web FROM companies WHERE company_name = 'Spaced AG'")[0]["web"] == "spaced.de"


def test_only_company_name_and_web_are_strictly_required(client, database):
    r = upload(client, "Company Name;Web\nMinimal GmbH;minimal.de\n")
    assert r.status_code == 200 and r.get_json()["imported"] == 1
    r = upload(client, "company name;WEB;house no\nLoose GmbH;loose.de;5\n")   # header case / spacing
    assert r.status_code == 200
    assert sql(database, "SELECT house_no FROM companies WHERE company_name = 'Loose GmbH'")[0]["house_no"] == "5"


@pytest.mark.parametrize("data,fragment", [
    ("Company Name,Web\nACME,acme.de\n", "delimiter"),      # comma instead of ';'
    ("Name;Web\nACME;acme.de\n", "Company Name"),
    ("", "Company Name"),
])
def test_bad_csv_is_rejected_with_400(client, data, fragment):
    r = upload(client, data)
    assert r.status_code == 400 and fragment in r.get_json()["error"]


def test_missing_file_and_wrong_encoding_are_400(client):
    assert client.post("/upload_companies", data={}).status_code == 400
    r = upload(client, "Company Name;Web\nMüller;x.de\n".encode("latin-1"))
    assert r.status_code == 400 and "UTF-8" in r.get_json()["error"]


def test_binary_file_is_rejected(client):
    r = upload(client, "Company Name;Web\nBad\x00Name;x.de\n")
    assert r.status_code == 400 and "NUL" in r.get_json()["error"]


def test_a_database_failure_in_the_middle_leaves_no_half_import(client, database):
    import web_service
    upload(client, "Company Name;Web\nKeep GmbH;keep.de\n")
    sql(database, "ALTER TABLE companies ADD CONSTRAINT no_bad CHECK (company_name <> 'Bad AG')")
    web_service.app.config["TESTING"] = False        # production behaviour: JSON error, no traceback page
    try:
        r = upload(client, "Company Name;Web\nGood AG;good.de\nBad AG;bad.de\nLater AG;later.de\n")
    finally:
        sql(database, "ALTER TABLE companies DROP CONSTRAINT no_bad")
        web_service.app.config["TESTING"] = True
    assert r.status_code == 500 and r.get_json() == {"error": "database error"}
    names = {x["company_name"] for x in sql(database, "SELECT company_name FROM companies")}
    assert names == {"Keep GmbH"}                    # Good AG was rolled back: all-or-nothing


# ----------------------------------------------------------------- read endpoints
def test_list_and_read_one_company(client, database):
    upload(client, CSV)
    sql(database, "UPDATE companies SET status = 3, legal_name = 'ACME Legal', raw_json = '{\"a\": 1}' "
                  "WHERE company_name = 'ACME GmbH'")
    rows = client.get("/companies").get_json()
    assert [r["company_name"] for r in rows] == ["ACME GmbH", "nutrimmun GmbH"]
    assert rows[0]["status"] == 3 and rows[0]["status_name"] == "ADDRESS_INCOMPLETE"
    assert rows[1]["status"] is None and rows[1]["status_name"] == "PENDING"
    one = client.get("/companies/ACME%20GmbH").get_json()
    assert one["legal_name"] == "ACME Legal" and one["raw_json"] == {"a": 1}
    assert {"impressum_url", "postal_address", "handelsregister", "vat_number", "phone", "email",
            "responsible_person", "created_at", "updated_at"} <= set(one)
    assert client.get("/companies/does-not-exist").status_code == 404


def test_company_names_with_special_characters_can_be_read(client):
    upload(client, "Company Name;Web\nBarolo & Champagne GbR;b.com\nA/B Handel;ab.de\n")
    assert client.get("/companies/Barolo%20%26%20Champagne%20GbR").get_json()["web"] == "b.com"
    assert client.get("/companies/A/B%20Handel").get_json()["web"] == "ab.de"


def test_status_filter(client, database):
    upload(client, CSV + '"u";"Third AG";;;;;;;"third.de"\n')
    sql(database, "UPDATE companies SET status = 0 WHERE company_name = 'ACME GmbH'")
    sql(database, "UPDATE companies SET status = 5 WHERE company_name = 'Third AG'")
    names = lambda q: [r["company_name"] for r in client.get(q).get_json()]
    assert names("/companies?status=0") == ["ACME GmbH"]
    assert names("/companies?status=5") == ["Third AG"]
    assert names("/companies?status=pending") == ["nutrimmun GmbH"]
    assert names("/companies?status=3") == []
    assert len(client.get("/companies").get_json()) == 3
    assert client.get("/companies?status=abc").status_code == 400


# ----------------------------------------------------------------- Task 1 endpoints still work
def test_video_endpoints_are_kept(client, database, tmp_path, monkeypatch):
    import web_service
    monkeypatch.setattr(web_service, "UPLOAD_DIR", str(tmp_path))     # do not litter the project
    try:
        r = client.post("/upload", data={"file": (io.BytesIO(b"\x1aE\xdf\xa3 webm"), "x.webm"),
                                         "company_name": "Video GmbH"}, content_type="multipart/form-data")
        assert r.status_code == 200
        url = client.get("/get_video_url?company_name=Video%20GmbH").get_json()["video_url"]
        assert url == r.get_json()["video_url"] and url.endswith("/media/Video_GmbH.webm")
        assert client.get("/media/Video_GmbH.webm").status_code == 200
        assert client.get("/get_video_url?company_name=nobody").status_code == 404
        assert client.get("/health").get_json() == {"status": "ok"}
    finally:
        sql(database, "DELETE FROM videos WHERE company_name = 'Video GmbH'")


def test_schema_is_idempotent(database):
    database.init_db(wait_seconds=3)
    database.init_db(wait_seconds=3)
    cols = {r["column_name"] for r in sql(database, "SELECT column_name FROM information_schema.columns "
                                                   "WHERE table_name = 'companies'")}
    assert {"id", "company_name", "language", "country", "postcode", "city", "street", "house_no", "web",
            "status", "impressum_url", "legal_name", "postal_address", "handelsregister", "vat_number",
            "phone", "email", "responsible_person", "raw_json", "created_at", "updated_at"} <= cols
