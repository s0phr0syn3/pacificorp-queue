"""decode -> parse -> normalize -> load for one fetched page."""

import logging

import psycopg

from pacificorp_queue.fetch import FetchedPage
from pacificorp_queue.load import LoadResult, load_page
from pacificorp_queue.normalize import normalize_rows
from pacificorp_queue.parse import decode_html, parse_page
from pacificorp_queue.sources import Source

log = logging.getLogger(__name__)
_MAX_LOGGED_ISSUES = 20


class IngestError(RuntimeError):
    pass


def ingest_page(
    conn: psycopg.Connection,
    source: Source,
    fetched: FetchedPage,
    *,
    strict: bool = False,
) -> LoadResult:
    """Parse and load one page. Raises instead of loading anything doubtful:
    an empty page for a source that should have rows, or (with `strict`) any
    value that did not match a known vocabulary or format."""
    parsed = parse_page(decode_html(fetched.body))
    if not parsed.rows and not source.allow_empty:
        raise IngestError(f"{source.key}: page has no data rows; refusing to load")

    normalized = normalize_rows(source.key, parsed.as_of, parsed.header, parsed.rows)
    for issue in normalized.issues[:_MAX_LOGGED_ISSUES]:
        log.warning(
            "unexpected value source=%s queue_number=%s field=%s value=%r reason=%s",
            issue.source,
            issue.queue_number,
            issue.field,
            issue.value,
            issue.reason,
        )
    if len(normalized.issues) > _MAX_LOGGED_ISSUES:
        log.warning(
            "source=%s further issues not shown: %d",
            source.key,
            len(normalized.issues) - _MAX_LOGGED_ISSUES,
        )
    if strict and normalized.issues:
        raise IngestError(
            f"{source.key}: {len(normalized.issues)} unexpected values (strict mode)"
        )

    return load_page(
        conn,
        source,
        fetched,
        parsed.as_of,
        normalized.entries,
        issue_count=len(normalized.issues),
    )
