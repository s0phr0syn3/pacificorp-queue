"""Turn an OASIS queue page (Excel-exported HTML) into header + raw string rows.

This module knows about HTML only. Meaning (column names, vocabularies, dates)
lives in `normalize`.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser

_CHARSET_RE = re.compile(rb"charset=[\"']?([\w-]+)", re.IGNORECASE)
_AS_OF_RE = re.compile(r"as of:?\s*(\S*)", re.IGNORECASE)
_ROW_ID_RE = re.compile(r"^[A-Z0-9]+-\d+$")
# The header row is the one containing this cell. It sits at index 2 today, but
# locating it by content survives a banner row being added or removed.
_HEADER_MARKER = "request date"
_HEADER_SEARCH_ROWS = 10


class ParseError(ValueError):
    """The page does not have the structure this parser relies on."""


@dataclass(frozen=True, slots=True)
class ParsedPage:
    as_of: date
    header: list[str]
    rows: list[list[str]]
    skipped_rows: int  # banner, legend and spacer rows below the header


def clean(text: str) -> str:
    """Collapse all whitespace, including non-breaking spaces, to single spaces."""
    return " ".join(text.split())


def decode_html(body: bytes) -> str:
    """Decode using the page's declared charset (windows-1252 for these pages).

    Decoding is strict: an undecodable byte is an error, not a replacement
    character that could end up in the database.
    """
    match = _CHARSET_RE.search(body[:4096])
    charset = match.group(1).decode("ascii") if match else "windows-1252"
    return body.decode(charset)


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.table_count = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._ignore: str | None = None  # inside <style> / <script>

    def _close_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append(clean("".join(self._cell)))
        self._cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row is not None:
            self.rows.append(self._row)
        self._row = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script"):
            self._ignore = tag
        elif tag == "table":
            self.table_count += 1
        elif tag == "tr":
            self._close_row()  # tolerate a missing </tr>
            self._row = []
        elif tag in ("td", "th"):
            self._close_cell()  # tolerate a missing </td>
            if self._row is not None:
                self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == self._ignore:
            self._ignore = None
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_row()

    def handle_data(self, data: str) -> None:
        if self._ignore is None and self._cell is not None:
            self._cell.append(data)


def _find_as_of(rows: list[list[str]]) -> date:
    found: set[date] = set()
    for row in rows[:_HEADER_SEARCH_ROWS]:
        for i, cell in enumerate(row):
            match = _AS_OF_RE.fullmatch(cell)
            if not match:
                continue
            value = match.group(1)
            if not value:  # "As of:" and the date are separate cells
                value = next((c for c in row[i + 1 :] if c), "")
            try:
                found.add(datetime.strptime(value, "%m/%d/%Y").date())
            except ValueError as exc:
                raise ParseError(f"unreadable 'As of' value {value!r}") from exc
    if len(found) != 1:
        raise ParseError(f"expected exactly one 'As of' date, found {sorted(found)}")
    return found.pop()


def parse_page(html: str) -> ParsedPage:
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    if parser.table_count != 1:
        raise ParseError(f"expected exactly one <table>, found {parser.table_count}")
    rows = parser.rows

    as_of = _find_as_of(rows)

    header_index = next(
        (
            i
            for i, row in enumerate(rows[:_HEADER_SEARCH_ROWS])
            if _HEADER_MARKER in (c.casefold() for c in row)
        ),
        None,
    )
    if header_index is None:
        raise ParseError(f"no header row containing {_HEADER_MARKER!r}")
    header = rows[header_index]

    data: list[list[str]] = []
    skipped = 0
    for row in rows[header_index + 1 :]:
        if len(row) == len(header) and any(row):
            data.append(row)
            continue
        # Banner/legend/spacer rows have a different cell count (merged cells).
        # One that starts like a queue id is a data row we failed to read.
        if row and _ROW_ID_RE.match(row[0]):
            raise ParseError(
                f"row {row[0]!r} has {len(row)} cells, header has {len(header)}"
            )
        skipped += 1
    return ParsedPage(as_of=as_of, header=header, rows=data, skipped_rows=skipped)
