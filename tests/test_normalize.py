from datetime import date
from decimal import Decimal

import pytest

from pacificorp_queue.normalize import (
    NormalizationError,
    normalize_rows,
    resolve_columns,
)
from pacificorp_queue.parse import decode_html, parse_page
from tests.helpers import fixture_bytes


def entries(key: str):  # type: ignore[no-untyped-def]
    page = parse_page(decode_html(fixture_bytes(key)))
    result = normalize_rows(key, page.as_of, page.header, page.rows)
    assert result.issues == []
    return {e.queue_number: e for e in result.entries}


def test_shifting_headers_map_to_the_same_fields() -> None:
    # cs1 labels the megawatt columns Summer/Winter, cs4 labels them S/W.
    assert entries("cs1")["C1-01"].summer_mw == Decimal("20")
    assert entries("cs4")["C4-001"].summer_mw == Decimal("3.6")
    assert entries("cs4")["C4-001"].winter_mw == Decimal("3.6")


def test_parent_column_has_two_names_and_two_shapes() -> None:
    assert entries("tc1")["TCS-01"].former_queue_numbers == ("839",)  # serial id
    assert entries("tc2")["T2-001"].former_queue_numbers == ("C4-032",)
    assert entries("tc2")["T2-013"].former_queue_numbers == ("C3-045", "C3-051")
    assert entries("cs4")["C4-001"].former_queue_numbers == ()  # no such column


def test_withdrawn_because_moved_is_flagged_not_just_withdrawn() -> None:
    plain = entries("cs4")["C4-001"]
    assert plain.status == "withdrawn"
    assert plain.moved_to_cluster == "T2"
    # a different wording of the same event
    combined = entries("cs4")["C4-049"]
    assert combined.status_explanation == "Combined with C4-050, moved to T2 Cluster"
    assert combined.moved_to_cluster == "T2"
    # an ordinary withdrawal is not a transfer
    assert entries("cs4")["C4-005"].moved_to_cluster is None


def test_excel_artifacts_become_null() -> None:
    cs1 = entries("cs1")
    assert cs1["C1-04"].qualifying_facility is None  # "1/0/1900"
    assert cs1["C1-04"].status_explanation is None  # bare "0"
    assert cs1["C1-05"].cluster_area is None  # "NA"
    assert entries("cs4")["C4-094"].requested_cod is None  # "1/0/1900"
    assert entries("tc2")["T2-003"].requested_cod is None  # "TBD"


def test_mixed_vocabularies_are_folded() -> None:
    assert entries("cs1")["C1-01"].qualifying_facility is True  # "QF"
    assert entries("tc1")["TCS-02"].qualifying_facility is False  # "NO"
    assert entries("cs1")["C1-25"].fuel_type == "Pumped Storage"  # "Pump Storage"
    assert entries("cs1")["C1-04"].fuel_type == "Wind & Battery Storage"


def test_blank_company_is_null_and_values_are_typed() -> None:
    row = entries("cs4")["C4-001"]
    assert row.company_name is None
    assert row.request_date == date(2024, 4, 4)
    assert row.region == "PACE"
    assert row.status == "withdrawn"
    assert row.raw["Request Status"] == "Withdrawn"  # original text preserved


def test_content_hash_tracks_content() -> None:
    a = entries("cs4")["C4-001"]
    assert a.content_hash == entries("cs4")["C4-001"].content_hash
    assert a.content_hash != entries("cs4")["C4-005"].content_hash


HEADER = ["Generation Interconnection Cluster Number", "Request Date", "Request Status"]
GOOD = ["C9-001", "1/2/2024", "In Progress"]


def run(row: list[str], header: list[str] = HEADER):  # type: ignore[no-untyped-def]
    return normalize_rows("t", date(2026, 9, 14), header, [row])


def test_minimal_row() -> None:
    result = run(GOOD)
    assert result.issues == []
    assert result.entries[0].status == "in_progress"


@pytest.mark.parametrize(
    "row",
    [
        ["C9-001", "1/2/2024", "Pending"],  # unknown status
        ["C9-001", "TBD", "In Progress"],  # required date blank
        ["C9-001", "13/45/2024", "In Progress"],  # unreadable date
        ["nonsense", "1/2/2024", "In Progress"],  # not a queue number
    ],
)
def test_bad_required_values_raise(row: list[str]) -> None:
    with pytest.raises(NormalizationError):
        run(row)


def test_bad_optional_values_become_null_and_are_reported() -> None:
    header = [*HEADER, "Region", "S", "Type"]
    result = run([*GOOD, "PACX", "lots", "Hydrogen"], header)
    entry = result.entries[0]
    assert (entry.region, entry.summer_mw, entry.fuel_type) == (None, None, None)
    assert {i.field for i in result.issues} == {"region", "summer_mw", "fuel_type"}
    assert {i.value for i in result.issues} == {"PACX", "lots", "Hydrogen"}
    assert entry.raw["Region"] == "PACX"  # nothing lost


def test_unrecognized_header_fails_instead_of_dropping_a_column() -> None:
    with pytest.raises(NormalizationError, match="Surprise Column"):
        resolve_columns([*HEADER, "Surprise Column"])


def test_missing_required_column_fails() -> None:
    with pytest.raises(NormalizationError, match="request_date"):
        resolve_columns(["Generation Interconnection Cluster Number", "Request Status"])


def test_duplicate_queue_number_fails() -> None:
    with pytest.raises(NormalizationError, match="duplicate"):
        normalize_rows("t", date(2026, 9, 14), HEADER, [GOOD, GOOD])
