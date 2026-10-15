"""The normalized record that is stored for each queue entry."""

import hashlib
import json
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """One queue request as published on one page for one as-of date.

    Field names are the column names of the `queue_entry` table.
    """

    as_of: date
    queue_number: str
    source: str
    cluster_label: str | None
    cluster_area: str | None
    former_queue_numbers: tuple[str, ...]
    request_date: date
    status: str
    status_explanation: str | None
    moved_to_cluster: str | None
    company_name: str | None
    service_type: str | None
    application_rules: str | None
    qualifying_facility: bool | None
    summer_mw: Decimal | None
    winter_mw: Decimal | None
    county: str | None
    state: str | None
    region: str | None
    point_of_interconnection: str | None
    requested_cod: date | None
    agreed_cod: date | None
    fuel_type: str | None
    facilities_study_available: bool | None
    schedule_deviation_flagged: bool | None
    raw: dict[str, str]  # the page's own header -> cell text, untouched

    def to_record(self) -> dict[str, Any]:
        """JSON-safe dict of every stored business column."""
        record: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, date):
                value = value.isoformat()
            elif isinstance(value, Decimal):
                value = str(value)
            elif isinstance(value, tuple):
                value = list(value)
            record[f.name] = value
        return record

    @property
    def content_hash(self) -> str:
        """Hash of everything stored, so a change to the page *or* to the
        normalization rules changes the hash and the row gets rewritten."""
        canonical = json.dumps(
            self.to_record(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


ENTRY_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(QueueEntry))
