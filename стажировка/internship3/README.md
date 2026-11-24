# Company import and Impressum extraction with DeepSeek (Internship Task 3)

Extends the Dockerized stack of Internship Task 1 (`web`: Apache + mod_wsgi + Flask, `db`: PostgreSQL 16;
the video endpoints are kept) with

1. a `companies` table, a CSV import form and read endpoints;
2. `enrich_companies.py`: opens every company's website, finds the Impressum / Imprint page and
   extracts the legally required data with the DeepSeek API, **sending as few tokens as possible**.

## Run

```
copy .env.example .env          # then put your key after DEEPSEEK_API_KEY=   (cp on Linux/macOS)
docker compose up -d --build
```

* Open <http://localhost:8000/upload_companies>, choose `companies.csv`, press **Upload**.
* The data is at <http://localhost:8000/companies> and `/companies/<company name>`.
* Enrichment (a one-off container with the same image; the key comes from `.env`):

```
docker compose run --rm enrich --dry-run     # fetch + optimise only: no DeepSeek call, no DB writes
docker compose run --rm enrich --limit 1     # one real company first
docker compose run --rm enrich               # everything that is not OK yet
```

The key is read from `DEEPSEEK_API_KEY` (environment or `.env`). It is never in the code, never in the
image (`.env` is in `.dockerignore`) and never in git (`.env` is in `.gitignore`).

## CSV

UTF-8, delimiter `;`, qualifier `"`. Header: `URL;Company Name;Language;Country;Postcode;City;Street;HouseNo;Web`.
`Company Name` (unique key) and `Web` are required; `URL` belongs to the recorder and is ignored here.
`Web` may omit the scheme (`nutrimmun.de`): it is stored as-is and normalised by the enrichment script.

## Web service

| method | path | |
|--------|------|-|
| GET | `/upload_companies` | HTML upload form (no JavaScript) |
| POST | `/upload_companies` | multipart `file`; upsert by `company_name`; returns `{"imported", "updated", "total_rows"}` (+ `skipped`: rows without a company name) |
| GET | `/companies[?status=N]` | all companies; `status=pending` for not processed rows |
| GET | `/companies/<company_name>` | one company incl. extracted fields and `raw_json`; 404 if unknown |

Re-importing is idempotent and **only the CSV columns are updated** (`language … web`); `status` and the
extracted fields are never touched. The import is one transaction (all or nothing). The schema is
`schema.sql` (`CREATE TABLE IF NOT EXISTS`, run at every start).

## Status codes (`status.py`, one `IntEnum`)

`NULL` pending · `0` OK · `1` NO_IMPRESSUM_LINK_FOUND · `2` PARSING_FAILED · `3` ADDRESS_INCOMPLETE ·
`4` ERROR (unreachable, HTTP 4xx/5xx, DeepSeek error) · `5` NO_WEBSITE.
After the DeepSeek call: nothing detected → 2; detected but postcode/city/street/house_no not all filled → 3;
otherwise 0. A missing VAT number, phone or person never downgrades a complete address.

## `enrich_companies.py`

```
python enrich_companies.py [--company "ACME GmbH"] [--limit N] [--status 0]
                           [--dry-run] [--max-chars 8000] [--delay 1.0] [--respect-robots] [--js-fallback]
```
Default selection: `status IS NULL OR status <> 0`, in order of `id` (re-runnable, OK rows are skipped).
`--company` processes that company whatever its status. Per company: normalise the URL (https:// is prepended,
http:// is tried if https fails) → home page → link whose text is *impressum/imprint* (else href contains it;
`urljoin` for relative links) → Impressum page → visible text → token optimisation → DeepSeek → status → PostgreSQL.
A failing company is marked `4` and the batch continues. `--dry-run` shows the effect of the optimisation without
spending tokens.

## Token optimisation (`impressum.py`, `optimize_text`)

Visible text only (script/style/noscript/svg/head/nav/header/footer/form removed). If it has at most
`MAX_TEXT_CHARS = 8000` characters (~2000 tokens, tokens ≈ chars // 4) it is sent unchanged. Otherwise:

1. **Boilerplate lines are dropped** (cookie banners, Datenschutz, AGB, duplicate "Impressum" menu items, ©, menu words,
   language names); lines with a phone/e-mail/VAT/register/postcode/street are protected.
2. **Head anchor**: the heading that opens the legal block (*Impressum, Angaben gemäß § 5 TMG/DDG, Anbieter,
   Diensteanbieter*). Not blindly the first match - a menu item can come first - but the earliest one within 1500
   characters before the first strong signal.
3. **Tail anchors (the key idea)**: the legal data ends shortly after its last piece. The last match of phone `+49|0049`,
   VAT `DE\s?\d{9}`, e-mail, `HRB|HRA|Registergericht|Amtsgericht` or a label such as *Vertreten durch / Geschäftsführer /
   Verantwortlich* is found and the text is cut `TAIL_MARGIN = 400` characters after it. Responsibility words count only at
   the start of a line: inside disclaimers ("… sind wir verantwortlich") they would drag the cut to the end of the page.
4. **Footer markers**: if a footer or disclaimer heading (©, Datenschutz, Haftung für Inhalte, Streitschlichtung…) follows the
   last anchor, the text is cut right before it.
5. **Sandwich + verification**: keep `[head − margin : tail + margin]`; check that it still contains the head anchor and a strong
   signal, otherwise relax (tail-only / head-only / whole text).
6. **Last resorts**: key lines (street, tel, e-mail, USt, HRB, vertreten…) ± 1 line, then a head+tail window.

Every company logs `text 14230 -> 612 chars (~3557 -> ~153 tokens, -96%) [boilerplate+sandwich]` and DeepSeek's
real `prompt_tokens`/`completion_tokens`. On a synthetic bloated page the text shrinks from 10 741 to 412 characters
(−96 %) with all legal data kept.

## DeepSeek

`POST {DEEPSEEK_BASE_URL or https://api.deepseek.com}/chat/completions`, `Authorization: Bearer <key>`, model
`DEEPSEEK_MODEL` (default `deepseek-chat`), `response_format: json_object`, `max_tokens: 1024`, `temperature: 0`, the
system prompt of the task (it contains "json" and the schema). Blank or unparseable output, HTTP 429/5xx and network errors
are retried up to 3 attempts with exponential backoff; blank output after the retries → status 2, HTTP 4xx (e.g. bad key) or
network failure → status 4.

## Tests

```
python -m pip install -r requirements-dev.txt
python -m pytest -q
```
Unit tests need nothing else. The database tests (CSV import, endpoints, full enrichment against a **local fake website and a
local fake DeepSeek API**, so no real key or internet is used) run when `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` point to a
PostgreSQL and are skipped otherwise. A throw-away database:

```
docker run -d --name pgtest -e POSTGRES_DB=videos -e POSTGRES_USER=video_user -e POSTGRES_PASSWORD=video_pass -p 55432:5432 postgres:16
$env:DB_HOST="127.0.0.1"; $env:DB_PORT="55432"; $env:DB_NAME="videos"; $env:DB_USER="video_user"; $env:DB_PASSWORD="video_pass"
python -m pytest -q
docker rm -f pgtest
```
