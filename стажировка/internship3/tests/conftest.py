import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

DB_VARS = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")


@pytest.fixture(scope="session")
def database():
    """Skips the test when no PostgreSQL is configured/reachable (DB_* variables)."""
    if not all(os.environ.get(v) for v in DB_VARS):
        pytest.skip("set DB_HOST, DB_NAME, DB_USER, DB_PASSWORD to run the database tests")
    import db
    try:
        db.init_db(wait_seconds=3)
    except Exception as exc:
        pytest.skip(f"PostgreSQL is not reachable: {exc}")
    return db


@pytest.fixture
def clean_companies(database):
    def wipe():
        conn = database.connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("DELETE FROM companies")
        finally:
            conn.close()
    wipe()
    yield database
    wipe()
