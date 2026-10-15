"""Table-driven normalization of parsed page rows into `QueueEntry` records.

Everything page-specific lives in the tables below (FIELDS and the vocabularies).
To support a renamed column or a new spelling, add a row to a table; the
functions at the bottom do not change.

Policy for bad data:
  * Required fields (queue number, request date, status) that are missing or
    unreadable raise `NormalizationError`. A row without them is not usable.
  * Optional fields that are unreadable become NULL and are reported as an
    `Issue`. The original text is always kept in `QueueEntry.raw`.
  * Known sentinels (blank, a bare 0 in text columns, NA for cluster area, TBD,
    and the Excel zero date 1/0/1900) become NULL deliberately and are not issues.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pacificorp_queue.model import QueueEntry


class NormalizationError(ValueError):
    """The page cannot be turned into records safely."""


@dataclass(frozen=True, slots=True)
class Issue:
    """An optional value that could not be read; stored as NULL and reported."""

    source: str
    queue_number: str
    field: str
    value: str
    reason: str


# --- Sentinels ---------------------------------------------------------------

# Excel exports a numeric 0 as the text "0" and a date 0 as "1/0/1900".
TEXT_SENTINELS = frozenset({"", "0"})
EXCEL_ZERO_DATE = "1/0/1900"
DATE_SENTINELS = frozenset({"", "TBD", EXCEL_ZERO_DATE})

# --- Vocabularies (casefolded raw text -> stored value) ---------------------------

STATUS = {
    "withdrawn": "withdrawn",
    "in progress": "in_progress",
    "suspended": "suspended",
    "in service": "in_service",
}

# Stored exactly as published. "ER/NR" and "NR/ER" are not merged because the
# page does not say whether the order carries meaning.
SERVICE_TYPE = {v.casefold(): v for v in ("ER", "NR", "ER/NR", "NR/ER")}

APPLICATION_RULES = {
    v.casefold(): v for v in ("LGI", "SGI", "OGI", "OLGI", "UGI", "WGI", "QFLGI")
}

REGION = {v.casefold(): v for v in ("PACE", "PACW")}

# "Yes"/"QF" and "No"/"NO" are the same answers spelled differently. The Excel
# zero date appears in this column in some rows and is treated as unknown.
QUALIFYING_FACILITY = {"yes": True, "qf": True, "no": False}

# Case variants are folded to one spelling. Two merges are judgement calls and
# are visible here: "Pump Storage" -> "Pumped Storage", "Battery" -> "Battery Storage".
FUEL_TYPE = {
    v.casefold(): v
    for v in (
        "Solar",
        "Wind",
        "Battery Storage",
        "Solar & Battery Storage",
        "Wind & Battery Storage",
        "Natural Gas",
        "Natural Gas & Battery Storage",
        "Geothermal",
        "Pumped Storage",
        "Hydro",
        "Nuclear",
        "Steam & Gas",
        "Steam",
        "Waste Heat",
        "Biogas",
        "Biomass",
    )
} | {"pump storage": "Pumped Storage", "battery": "Battery Storage"}

_QUEUE_ID_RE = re.compile(r"^[A-Z0-9]+-\d+$")
_FORMER_ID_RE = re.compile(r"^[A-Z0-9]+(-\d+)?$")  # serial ids are bare numbers
# Matches "Request moved to T2 Cluster" and also
# "Combined with C4-050, moved to T2 Cluster".
_MOVED_RE = re.compile(r"\bmoved to (\S+) cluster\s*$", re.IGNORECASE)

# --- Cell parsers: take cleaned cell text, return a value or raise ValueError ---


def _text(s: str) -> str | None:
    return None if s in TEXT_SENTINELS else s


def _cluster_area(s: str) -> str | None:
    # "NA" (not applicable) is how the page marks a request with no cluster area.
    return None if s in TEXT_SENTINELS or s == "NA" else s


def _vocab(table: Mapping[str, Any]) -> Callable[[str], Any]:
    """Parser mapping casefolded text through `table`; unknown text raises."""

    def parse(s: str) -> Any:
        if s in TEXT_SENTINELS:
            return None
        try:
            return table[s.casefold()]
        except KeyError:
            raise ValueError("not in vocabulary") from None

    return parse


def _date(s: str) -> date | None:
    if s in DATE_SENTINELS:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        raise ValueError("not a m/d/yyyy date") from None


def _mw(s: str) -> Decimal | None:
    """Blank is NULL; a literal 0 is a real zero, unlike in text columns."""
    if s == "":
        return None
    try:
        value = Decimal(s)
    except InvalidOperation:
        raise ValueError("not a number") from None
    if not value.is_finite() or value < 0:
        raise ValueError("not a non-negative finite number")
    return value


def _state(s: str) -> str | None:
    if s in TEXT_SENTINELS:
        return None
    if not re.fullmatch(r"[A-Z]{2}", s):
        raise ValueError("not a two-letter state code")
    return s


def _queue_number(s: str) -> str:
    if not _QUEUE_ID_RE.match(s):
        raise ValueError("not a queue number like C4-001")
    return s


def _former_numbers(s: str) -> tuple[str, ...]:
    if s in TEXT_SENTINELS:
        return ()
    ids = tuple(part.strip() for part in s.split(","))
    if not all(_FORMER_ID_RE.match(i) for i in ids):
        raise ValueError("not a comma-separated list of queue numbers")
    return ids


def _flag(true_text: str) -> Callable[[str], bool]:
    """A column that is blank, or contains one link label (e.g. 'Available')."""

    def parse(s: str) -> bool:
        if s == "":
            return False
        if s.casefold() == true_text.casefold():
            return True
        raise ValueError(f"expected blank or {true_text!r}")

    return parse


@dataclass(frozen=True, slots=True)
class Field:
    """How one `QueueEntry` attribute is read: accepted headers plus a parser."""

    name: str  # QueueEntry attribute
    headers: tuple[str, ...]  # accepted spellings of the page's column header
    parse: Callable[[str], Any]
    required: bool = False  # header must exist and the value must parse
    absent: Any = None  # value when an optional header is not on the page


FIELDS: tuple[Field, ...] = (
    Field(
        "queue_number",
        ("Generation Interconnection Cluster Number",),
        _queue_number,
        required=True,
    ),
    Field("cluster_label", ("Cluster",), _text),
    Field("cluster_area", ("Cluster Area",), _cluster_area),
    Field(
        "former_queue_numbers",
        ("Former Queue Number", "Former Serial Queue Number"),
        _former_numbers,
        absent=(),
    ),
    Field("request_date", ("Request Date",), _date, required=True),
    Field("status", ("Request Status",), _vocab(STATUS), required=True),
    Field("status_explanation", ("Request Status Explanation",), _text),
    Field("company_name", ("Company Name",), _text),
    Field("service_type", ("Service Type",), _vocab(SERVICE_TYPE)),
    Field("application_rules", ("Application Rules",), _vocab(APPLICATION_RULES)),
    # An unknown spelling raises KeyError, which normalize_rows treats as bad data.
    Field(
        "qualifying_facility",
        ("Qualifying Facility",),
        lambda s: (
            None
            if s in DATE_SENTINELS or s == "0"
            else QUALIFYING_FACILITY[s.casefold()]
        ),
    ),
    Field("summer_mw", ("S", "Summer"), _mw),
    Field("winter_mw", ("W", "Winter"), _mw),
    Field("county", ("County",), _text),
    Field("state", ("ST",), _state),
    Field("region", ("Region",), _vocab(REGION)),
    Field("point_of_interconnection", ("Point of Interconnection",), _text),
    Field(
        "requested_cod",
        ("Customer Requested Commercial Operations Date",),
        _date,
    ),
    Field("agreed_cod", ("Agreed to Commercial Operations Date",), _date),
    Field("fuel_type", ("Type",), _vocab(FUEL_TYPE)),
    Field("facilities_study_available", ("Facilities Study",), _flag("Available")),
    Field("schedule_deviation_flagged", ("Schedule Deviation",), _flag("More Info")),
)

_HEADER_TO_FIELD: dict[str, Field] = {
    h.casefold(): f for f in FIELDS for h in f.headers
}


def resolve_columns(header: Sequence[str]) -> dict[str, int]:
    """Map field name -> column index, failing loudly on anything unexpected."""
    unknown = [h for h in header if h.casefold() not in _HEADER_TO_FIELD]
    if unknown:
        raise NormalizationError(f"unrecognized column headers: {unknown}")
    columns: dict[str, int] = {}
    for index, h in enumerate(header):
        name = _HEADER_TO_FIELD[h.casefold()].name
        if name in columns:
            raise NormalizationError(f"duplicate column for {name!r}: {h!r}")
        columns[name] = index
    missing = [f.name for f in FIELDS if f.required and f.name not in columns]
    if missing:
        raise NormalizationError(f"missing required columns: {missing}")
    return columns


@dataclass(frozen=True, slots=True)
class NormalizedPage:
    """Entries ready to load, plus the optional values that were unreadable."""

    entries: list[QueueEntry]
    issues: list[Issue]


def _moved_to(explanation: str | None) -> str | None:
    """Target cluster when a 'Withdrawn' row is really a transfer, else None."""
    match = _MOVED_RE.search(explanation) if explanation else None
    return match.group(1) if match else None


def normalize_rows(
    source: str, as_of: date, header: Sequence[str], rows: Sequence[Sequence[str]]
) -> NormalizedPage:
    """Turn parsed rows into entries, applying the policy in the module docstring.

    Raises NormalizationError for unknown headers, unusable required values and
    duplicate queue numbers.
    """
    if not rows:
        return NormalizedPage([], [])
    columns = resolve_columns(header)
    entries: list[QueueEntry] = []
    issues: list[Issue] = []
    seen: set[str] = set()

    for row in rows:
        values: dict[str, Any] = {}
        row_id = row[columns["queue_number"]]
        for f in FIELDS:
            if f.name not in columns:
                values[f.name] = f.absent
                continue
            cell = row[columns[f.name]]
            try:
                value = f.parse(cell)
            except (ValueError, KeyError) as exc:  # KeyError: qualifying_facility
                if f.required:
                    raise NormalizationError(
                        f"{row_id}: {f.name} {cell!r}: {exc}"
                    ) from exc
                value = f.absent
                issues.append(Issue(source, row_id, f.name, cell, str(exc)))
            if f.required and value is None:
                raise NormalizationError(f"{row_id}: {f.name} is blank")
            values[f.name] = value

        if values["queue_number"] in seen:
            raise NormalizationError(f"duplicate queue number {values['queue_number']}")
        seen.add(values["queue_number"])

        entries.append(
            QueueEntry(
                as_of=as_of,
                source=source,
                moved_to_cluster=_moved_to(values["status_explanation"]),
                raw=dict(zip(header, row, strict=True)),
                **values,
            )
        )
    return NormalizedPage(entries, issues)
