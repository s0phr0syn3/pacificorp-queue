from datetime import UTC, datetime
from pathlib import Path

from pacificorp_queue.fetch import FetchedPage
from pacificorp_queue.sources import SOURCES

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(key: str) -> bytes:
    return (FIXTURES / f"{key}.html").read_bytes()


def page(key: str, body: bytes | None = None) -> FetchedPage:
    return FetchedPage(
        url=SOURCES[key].url,
        body=fixture_bytes(key) if body is None else body,
        fetched_at=datetime.now(UTC),
    )


def edit_row(body: bytes, queue_number: str, old: str, new: str) -> bytes:
    """Replace `old` with `new` at its first occurrence after the row's id cell."""
    start = body.index(f">{queue_number}<".encode())
    at = body.index(old.encode(), start)
    return body[:at] + new.encode() + body[at + len(old) :]


def drop_row(body: bytes, queue_number: str) -> bytes:
    marker = body.index(f">{queue_number}<".encode())
    start = body.rfind(b"<tr", 0, marker)
    end = body.index(b"</tr>", marker) + len(b"</tr>")
    return body[:start] + body[end:]


def set_as_of(body: bytes, old: str, new: str) -> bytes:
    assert body.count(old.encode()) == 1, "as-of text must be unique in the fixture"
    return body.replace(old.encode(), new.encode())
