"""Schema migrations."""

from importlib.resources import files

import psycopg

from pacificorp_queue.guard import require_idle

# Arbitrary constant; serializes concurrent `migrate` runs.
_MIGRATION_LOCK_KEY = 7_283_401


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply any unapplied migrations/NNN_*.sql in order, in one transaction.

    Returns the versions applied by this call (empty when up to date).
    """
    migrations = sorted(
        (p for p in (files("pacificorp_queue") / "migrations").iterdir()),
        key=lambda p: p.name,
    )
    applied_now: list[str] = []
    require_idle(conn)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migration (
                version    text        PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now(),
                applied_by text        NOT NULL DEFAULT current_user
            )
            """
        )
        done = {row[0] for row in conn.execute("SELECT version FROM schema_migration")}
        for path in migrations:
            if not path.name.endswith(".sql"):
                continue
            version = path.name.removesuffix(".sql")
            if version in done:
                continue
            # bytes, not str: psycopg's type hints only accept literal str queries.
            conn.execute(path.read_text("utf-8").encode())
            conn.execute(
                "INSERT INTO schema_migration (version) VALUES (%s)", (version,)
            )
            applied_now.append(version)
    return applied_now
