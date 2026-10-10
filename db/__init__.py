"""
Database
========

Postgres when a database is configured (DATABASE_URL or DB_HOST), SQLite
otherwise, so `python -m app.main` works on a laptop with nothing running.
"""

from functools import cache
from os import getenv
from pathlib import Path
from urllib.parse import quote

from agno.db.base import BaseDb

DB_ID = "pr-agent-db"


def _postgres_url() -> str | None:
    """Build a psycopg URL from DATABASE_URL, or from the DB_* parts."""
    url = getenv("DATABASE_URL", "").strip()
    if url:
        # Railway, Neon and friends hand out `postgresql://`; agno talks psycopg.
        for scheme in ("postgresql://", "postgres://"):
            if url.startswith(scheme):
                return f"postgresql+psycopg://{url[len(scheme) :]}"
        return url

    host = getenv("DB_HOST", "").strip()
    if not host:
        return None

    driver = getenv("DB_DRIVER", "postgresql+psycopg")
    user = getenv("DB_USER", "ai")
    password = quote(getenv("DB_PASS", "ai"), safe="")
    port = getenv("DB_PORT", "5432")
    database = getenv("DB_DATABASE", "ai")
    return f"{driver}://{user}:{password}@{host}:{port}/{database}"


@cache
def get_db() -> BaseDb:
    """The shared database instance. Memoized so every caller reuses one object."""
    url = _postgres_url()
    if url:
        from agno.db.postgres import PostgresDb

        return PostgresDb(id=DB_ID, db_url=url)

    from agno.db.sqlite import SqliteDb

    db_file = getenv("SQLITE_DB_FILE", str(Path(__file__).resolve().parents[1] / "pr_agent.db"))
    return SqliteDb(id=DB_ID, db_file=db_file)


__all__ = ["DB_ID", "get_db"]
