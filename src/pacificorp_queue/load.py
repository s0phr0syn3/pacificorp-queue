"""Idempotent load of one page's entries into Postgres."""

from dataclasses import dataclass
from datetime import date

import psycopg
from psycopg.types.json import Jsonb

from pacificorp_queue.fetch import FetchedPage
from pacificorp_queue.guard import require_idle
from pacificorp_queue.model import ENTRY_COLUMNS, QueueEntry
from pacificorp_queue.sources import Source

_COLUMNS = (*ENTRY_COLUMNS, "content_hash")
_UPDATE_COLUMNS = [c for c in _COLUMNS if c not in ("as_of", "queue_number")]

# Identifiers come from the dataclass definition, never from input. Values are
# passed as one jsonb parameter and unnested server-side: one round trip for the
# whole page, typed by the table's own column types.
_UPSERT = f"""
INSERT INTO queue_entry ({", ".join(_COLUMNS)})
SELECT {", ".join(_COLUMNS)}
FROM jsonb_populate_recordset(NULL::queue_entry, %s)
ON CONFLICT (as_of, queue_number) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in _UPDATE_COLUMNS)},
    updated_at = now()
"""


class LoadError(RuntimeError):
    """Loading would corrupt stored data, so nothing was written."""


@dataclass(frozen=True, slots=True)
class LoadResult:
    """Row counts for one load; also written to `source_load`."""

    source: str
    as_of: date
    rows_parsed: int
    rows_inserted: int
    rows_updated: int
    rows_unchanged: int
    rows_deleted: int
    issues: int


def load_page(
    conn: psycopg.Connection,
    source: Source,
    fetched: FetchedPage,
    as_of: date,
    entries: list[QueueEntry],
    *,
    issue_count: int,
) -> LoadResult:
    """Make the stored rows for (source, as_of) equal to `entries`.

    Only new or changed rows are written; unchanged rows (same content hash)
    are not touched, so `updated_at` means "last time the content changed".
    Rows stored for this source and as-of date but absent from the page are
    deleted (the utility republished that date with fewer rows). Everything,
    including the provenance row, happens in one transaction that is serialized
    per source by an advisory lock.
    """
    if any(e.as_of != as_of or e.source != source.key for e in entries):
        raise LoadError("entries do not match the page's source / as-of date")

    require_idle(conn)
    with conn.transaction():
        # One writer per source; hashtext() turns the name into the int lock key.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"pacificorp_queue.load.{source.key}",),
        )
        numbers = [e.queue_number for e in entries]

        # An id may only ever come from one page. Overwriting another page's
        # row would silently corrupt it.
        clash = conn.execute(
            "SELECT queue_number, source FROM queue_entry "
            "WHERE as_of = %s AND source <> %s AND queue_number = ANY(%s)",
            (as_of, source.key, numbers),
        ).fetchall()
        if clash:
            raise LoadError(f"queue numbers already loaded from another page: {clash}")

        existing: dict[str, str] = dict(
            conn.execute(
                "SELECT queue_number, content_hash FROM queue_entry "
                "WHERE as_of = %s AND source = %s",
                (as_of, source.key),
            ).fetchall()
        )
        # New rows, plus rows whose content hash differs from the stored one.
        changed = [e for e in entries if existing.get(e.queue_number) != e.content_hash]
        inserted = sum(1 for e in changed if e.queue_number not in existing)
        updated = len(changed) - inserted

        if changed:
            payload = [
                {**e.to_record(), "content_hash": e.content_hash} for e in changed
            ]
            conn.execute(_UPSERT, (Jsonb(payload),))
        # Rows the utility dropped from this as-of date's page.
        deleted = conn.execute(
            "DELETE FROM queue_entry "
            "WHERE as_of = %s AND source = %s AND NOT (queue_number = ANY(%s))",
            (as_of, source.key, numbers),
        ).rowcount

        result = LoadResult(
            source=source.key,
            as_of=as_of,
            rows_parsed=len(entries),
            rows_inserted=inserted,
            rows_updated=updated,
            rows_unchanged=len(entries) - len(changed),
            rows_deleted=deleted,
            issues=issue_count,
        )
        conn.execute(
            """
            INSERT INTO source_load (
                source, source_url, as_of, content_sha256, http_last_modified,
                http_etag, fetched_at, rows_parsed, rows_inserted, rows_updated,
                rows_unchanged, rows_deleted, issues
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                source.key,
                fetched.url,
                as_of,
                fetched.sha256,
                fetched.last_modified,
                fetched.etag,
                fetched.fetched_at,
                result.rows_parsed,
                result.rows_inserted,
                result.rows_updated,
                result.rows_unchanged,
                result.rows_deleted,
                result.issues,
            ),
        )
    return result
