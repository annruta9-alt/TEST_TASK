# Video web service in Docker (Apache + mod_wsgi + PostgreSQL)

Two containers started together with Docker Compose:

| service | image | role |
|---------|-------|------|
| `web`   | built from `Dockerfile` (Ubuntu 24.04) | Apache 2.4 + mod_wsgi running the Flask video service |
| `db`    | `postgres:16` | PostgreSQL (replaces SQLite) |

Named volumes keep the data across restarts: `db_data` (PostgreSQL data) and `uploads` (video files).

## Requirements

Docker Desktop (or Docker Engine + the Compose plugin). Nothing else: Apache, mod_wsgi, Python and
PostgreSQL live inside the containers.

## Run

```
docker compose up --build
```

The service is then available at <http://localhost:8000> (host 8000 -> Apache 80 in the container).
Stop with `Ctrl+C`, or run in the background with `docker compose up --build -d`.

> If port 8000 is already in use (e.g. the old `python web_service.py` from Test Task 2 is still
> running), stop that process first or change the left side of `"8000:80"` in `docker-compose.yml`.

## Try it

PowerShell (note `curl.exe`, not the `curl` alias):

```
curl.exe -F "file=@video/Acme.webm" -F "company_name=ACME GmbH" http://localhost:8000/upload
curl.exe "http://localhost:8000/get_video_url?company_name=ACME%20GmbH"
curl.exe http://localhost:8000/videos
```

Open the returned `video_url` in a browser to play the video.

The recorder from Test Task 1/2 works unchanged against the container:

```
python capture_video.py --server http://127.0.0.1:8000 --view-seconds 10
```

## Check persistence

```
docker compose down          # removes containers, KEEPS volumes
docker compose up            # data and videos are still there
curl.exe "http://localhost:8000/get_video_url?company_name=ACME%20GmbH"
```

`docker compose down -v` also deletes the volumes (full reset).

## Endpoints

| method | path | purpose |
|--------|------|---------|
| POST | `/upload` | multipart: `file`, `company_name`; upsert by company name |
| GET | `/get_video_url?company_name=...` | returns the URL, 404 if unknown |
| GET | `/media/<file>` | the video (served directly by Apache via `Alias`) |
| GET | `/videos` | list records |
| GET | `/health` | checks the DB connection (used by the compose healthcheck) |

## Configuration

DB settings are passed to `web` through environment variables, nothing is hard-coded in the code:
`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`.

The password defaults to `video_pass` (as in the task). To change it, copy `.env.example` to `.env`
(it is git-ignored) and set `DB_PASSWORD`; Compose picks it up automatically. Note that PostgreSQL
only reads the password when the data volume is first created, so after changing it run
`docker compose down -v` once. In production use Docker secrets or a secret manager.

## How it works

- `Dockerfile` – Ubuntu 24.04 + `apache2` + `libapache2-mod-wsgi-py3` (taken from the same distribution
  as the system Python, so the module and the interpreter always match; that is why `mod-wsgi` is
  not in `requirements.txt`). Python packages live in a virtualenv `/opt/venv`, which mod_wsgi uses
  through `python-home`.
- `apache/video.conf` – VirtualHost on port 80: `Alias /media/` for the uploaded files, everything
  else goes to Flask through `WSGIScriptAlias`; logs go to stdout/stderr (`docker compose logs web`).
- `app.wsgi` – `from web_service import app as application`.
- `docker-entrypoint.sh` – starts Apache in the foreground keeping the container environment
  (`apache2ctl` would wipe `DB_*`).
- Startup order: `web` depends on `db` with `condition: service_healthy` (`pg_isready`). On top of
  that the service retries the DB connection for up to 60 s at start, so it never crashes if the DB
  is a bit late.
- A new DB connection is opened per request (psycopg2 connections are not thread-safe); the table is
  created at startup under a PostgreSQL advisory lock because several Apache worker processes start
  at once.

## Useful commands

```
docker compose ps
docker compose logs -f web
docker compose exec web curl http://localhost/health
docker compose exec db psql -U video_user -d videos -c "select * from videos"
```
