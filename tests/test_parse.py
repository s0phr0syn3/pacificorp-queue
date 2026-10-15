from datetime import date

import pytest

from pacificorp_queue.parse import ParseError, decode_html, parse_page
from tests.helpers import fixture_bytes


def _html(rows: str, as_of: str = "<td>As of:</td><td>9/14/2026</td>") -> str:
    return f"<table><tr>{as_of}</tr>{rows}</table>"


HEADER = "<tr><td>Id</td><td>Request Date</td></tr>"


def test_real_page_structure() -> None:
    page = parse_page(decode_html(fixture_bytes("cs4")))
    assert page.as_of == date(2026, 9, 14)
    assert page.header[0] == "Generation Interconnection Cluster Number"
    assert len(page.header) == 21
    assert [r[0] for r in page.rows] == ["C4-001", "C4-005", "C4-049", "C4-094"]
    # footnotes and spacer rows were seen but are not data
    assert page.skipped_rows > 0
    assert all(len(r) == len(page.header) for r in page.rows)


def test_serial_page_has_header_but_no_rows() -> None:
    page = parse_page(decode_html(fixture_bytes("sq")))
    assert page.as_of == date(2026, 9, 14)
    assert page.rows == []


def test_as_of_in_a_single_cell() -> None:
    html = _html(HEADER, as_of="<td>As of: 9/14/2026</td>")
    assert parse_page(html).as_of == date(2026, 9, 14)


def test_header_is_found_by_content_not_position() -> None:
    banner = "<tr><td>banner</td></tr>" * 4
    html = _html(banner + HEADER + "<tr><td>C1-01</td><td>1/2/2020</td></tr>")
    page = parse_page(html)
    assert page.header == ["Id", "Request Date"]
    assert page.rows == [["C1-01", "1/2/2020"]]


def test_whitespace_and_nbsp_are_collapsed() -> None:
    html = _html(HEADER + "<tr><td>C1-01</td><td>&nbsp;1/2/\n 2020 </td></tr>")
    assert parse_page(html).rows == [["C1-01", "1/2/ 2020"]]


def test_row_that_looks_like_data_but_has_wrong_width_is_an_error() -> None:
    html = _html(HEADER + "<tr><td>C1-01</td></tr>")
    with pytest.raises(ParseError, match="C1-01"):
        parse_page(html)


@pytest.mark.parametrize(
    "html, message",
    [
        ("<table><tr><td>Request Date</td></tr></table>", "As of"),
        (_html(HEADER, as_of="<td>As of:</td><td>soon</td>"), "unreadable"),
        (_html("<tr><td>nothing here</td></tr>"), "header"),
        (_html(HEADER) + "<table></table>", "one <table>"),
    ],
)
def test_unexpected_structure_fails_loudly(html: str, message: str) -> None:
    with pytest.raises(ParseError, match=message):
        parse_page(html)


def test_decoding_uses_declared_charset_and_is_strict() -> None:
    head = b'<meta http-equiv=Content-Type content="text/html; charset=windows-1252">'
    assert decode_html(head + b"caf\xe9") == head.decode() + "café"
    with pytest.raises(UnicodeDecodeError):
        decode_html(head + b"\x81")  # undefined in windows-1252: no silent U+FFFD
