"""Integration tests: need Postgres (see conftest)."""

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from pacificorp_queue import fetch
from pacificorp_queue.cli import main
from pacificorp_queue.db import migrate
from pacificorp_queue.fetch import FetchedPage
from pacificorp_queue.load import LoadError
from pacificorp_queue.model import ENTRY_COLUMNS
from pacificorp_queue.pipeline import IngestError, ingest_page
from pacificorp_queue.sources import SOURCES
from tests.helpers import FIXTURES, drop_row, edit_row, fixture_bytes, page, set_as_of

pytestmark = pytest.mark.integration

Connect = Callable[[], psycopg.Connection]


def count(conn: psycopg.Connection, table: str = "queue_entry") -> int:
    query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
    row = conn.execute(query).fetchone()
    assert row is not None
    return int(row[0])


def test_migrate_is_idempotent(connect: Connect) -> None:
    conn = connect()
    assert migrate(conn) == ["001_initial"]
    assert migrate(conn) == []


def test_table_columns_match_the_model(conn: psycopg.Connection) -> None:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'queue_entry'"
    ).fetchall()
    expected = {
        *ENTRY_COLUMNS,
        "content_hash",
        "created_at",
        "updated_at",
        "created_by",
    }
    assert {r[0] for r in rows} == expected


def test_first_load_inserts_everything(conn: psycopg.Connection) -> None:
    result = ingest_page(conn, SOURCES["cs4"], page("cs4"))
    assert (result.rows_parsed, result.rows_inserted) == (4, 4)
    assert (result.rows_updated, result.rows_unchanged, result.rows_deleted) == (
        0,
        0,
        0,
    )
    row = conn.execute(
        "SELECT status, moved_to_cluster, company_name, requested_cod, summer_mw, "
        "raw->>'Request Status', created_by FROM queue_entry "
        "WHERE queue_number = 'C4-094'"
    ).fetchone()
    assert row is not None
    assert row[:4] == ("withdrawn", "T2", None, None)
    assert str(row[4]) == "8"
    assert row[5] == "Withdrawn"
    assert row[6]  # created_by defaults to the connecting role


def test_reloading_the_same_page_is_a_no_op(conn: psycopg.Connection) -> None:
    ingest_page(conn, SOURCES["cs4"], page("cs4"))
    before = conn.execute(
        "SELECT queue_number, created_at, updated_at, content_hash FROM queue_entry "
        "ORDER BY queue_number"
    ).fetchall()

    again = ingest_page(conn, SOURCES["cs4"], page("cs4"))

    assert (again.rows_inserted, again.rows_updated, again.rows_deleted) == (0, 0, 0)
    assert again.rows_unchanged == 4
    after = conn.execute(
        "SELECT queue_number, created_at, updated_at, content_hash FROM queue_entry "
        "ORDER BY queue_number"
    ).fetchall()
    assert after == before  # not even updated_at moved
    assert count(conn, "source_load") == 2  # but both runs are on record


def test_a_changed_row_updates_only_that_row(conn: psycopg.Connection) -> None:
    ingest_page(conn, SOURCES["cs4"], page("cs4"))
    before = dict(
        conn.execute("SELECT queue_number, updated_at FROM queue_entry").fetchall()
    )

    edited = edit_row(fixture_bytes("cs4"), "C4-005", ">PACE<", ">PACW<")
    result = ingest_page(conn, SOURCES["cs4"], page("cs4", edited))

    assert (result.rows_inserted, result.rows_updated, result.rows_unchanged) == (
        0,
        1,
        3,
    )
    after = dict(
        conn.execute("SELECT queue_number, updated_at FROM queue_entry").fetchall()
    )
    assert [q for q in after if after[q] != before[q]] == ["C4-005"]
    region = conn.execute("SELECT region FROM queue_entry WHERE queue_number='C4-005'")
    assert region.fetchone() == ("PACW",)


def test_a_row_missing_from_the_republished_page_is_deleted(
    conn: psycopg.Connection,
) -> None:
    ingest_page(conn, SOURCES["cs4"], page("cs4"))
    result = ingest_page(
        conn, SOURCES["cs4"], page("cs4", drop_row(fixture_bytes("cs4"), "C4-094"))
    )
    assert (result.rows_deleted, result.rows_unchanged) == (1, 3)
    assert count(conn) == 3


def test_a_new_as_of_date_adds_a_snapshot_and_keeps_history(
    conn: psycopg.Connection,
) -> None:
    ingest_page(conn, SOURCES["cs4"], page("cs4"))
    later = set_as_of(fixture_bytes("cs4"), "9/14/2026", "9/21/2026")
    result = ingest_page(conn, SOURCES["cs4"], page("cs4", later))
    assert (result.rows_inserted, result.rows_updated) == (4, 0)
    assert count(conn) == 8
    dates = conn.execute(
        "SELECT as_of FROM queue_entry WHERE queue_number = 'C4-001' ORDER BY as_of"
    ).fetchall()
    assert [str(d[0]) for d in dates] == ["2026-09-14", "2026-09-21"]


def test_an_id_already_loaded_from_another_page_is_rejected(
    conn: psycopg.Connection,
) -> None:
    ingest_page(conn, SOURCES["cs4"], page("cs4"))
    with pytest.raises(LoadError, match="another page"):
        ingest_page(conn, SOURCES["cs3"], page("cs3", fixture_bytes("cs4")))
    assert count(conn) == 4
    assert count(conn, "source_load") == 1


def test_empty_page_is_refused_unless_the_source_expects_it(
    conn: psycopg.Connection,
) -> None:
    with pytest.raises(IngestError, match="no data rows"):
        ingest_page(conn, SOURCES["cs4"], page("cs4", fixture_bytes("sq")))
    result = ingest_page(conn, SOURCES["sq"], page("sq"))
    assert result.rows_parsed == 0
    assert count(conn) == 0
    assert count(conn, "source_load") == 1


def test_strict_mode_writes_nothing_when_a_value_is_unrecognized(
    conn: psycopg.Connection,
) -> None:
    bad = edit_row(fixture_bytes("cs4"), "C4-005", ">PACE<", ">PACX<")
    with pytest.raises(IngestError, match="strict"):
        ingest_page(conn, SOURCES["cs4"], page("cs4", bad), strict=True)
    assert count(conn) == 0


def test_lenient_mode_loads_nulls_the_value_and_counts_the_issue(
    conn: psycopg.Connection,
) -> None:
    bad = edit_row(fixture_bytes("cs4"), "C4-005", ">PACE<", ">PACX<")
    result = ingest_page(conn, SOURCES["cs4"], page("cs4", bad))
    assert result.issues == 1
    row = conn.execute(
        "SELECT region, raw->>'Region' FROM queue_entry WHERE queue_number='C4-005'"
    ).fetchone()
    assert row == (None, "PACX")
    issues = conn.execute("SELECT issues FROM source_load").fetchone()
    assert issues == (1,)


def test_failure_after_the_upsert_rolls_everything_back(
    conn: psycopg.Connection,
) -> None:
    @dataclass(frozen=True, slots=True)
    class NoHash(FetchedPage):
        @property
        def sha256(self) -> str:  # type: ignore[override]
            return None  # type: ignore[return-value]  # violates NOT NULL last

    good = page("cs4")
    broken = NoHash(url=good.url, body=good.body, fetched_at=good.fetched_at)
    with pytest.raises(psycopg.errors.NotNullViolation):
        ingest_page(conn, SOURCES["cs4"], broken)
    assert count(conn) == 0  # the upsert did not survive


def test_concurrent_loads_of_one_source_do_not_double_count(connect: Connect) -> None:
    migrate(connect())
    results = []
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            own = connect()
            barrier.wait()
            results.append(ingest_page(own, SOURCES["cs2"], page("cs2")))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sum(r.rows_inserted for r in results) == 6
    assert sum(r.rows_unchanged for r in results) == 6
    assert count(connect()) == 6


def test_connection_inside_a_transaction_is_refused(conn: psycopg.Connection) -> None:
    with (
        conn.transaction(),
        pytest.raises(RuntimeError, match="already in a transaction"),
    ):
        ingest_page(conn, SOURCES["cs4"], page("cs4"))


def test_cli_migrate_then_load_from_directory(
    schema_dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    assert main(["migrate", "--dsn", schema_dsn]) == 0
    args = ["load", "--dsn", schema_dsn, "--html-dir", str(FIXTURES), "--source", "cs4"]
    assert main(args) == 0
    assert main(args) == 0
    with psycopg.connect(schema_dsn) as check:
        assert count(check) == 4
        assert count(check, "source_load") == 2
    assert "inserted=4" in caplog.text
    assert "unchanged=4" in caplog.text
    assert "skipped_rows=7" in caplog.text  # the fixture's banner, legend, spacers


def test_cli_rejects_a_missing_html_dir_before_touching_the_database(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Unreachable DSN: the check must fail first, with one line and no traceback.
    args = ["load", "--dsn", "postgresql://nobody@127.0.0.1:1/x"]
    args += ["--html-dir", str(tmp_path / "nope"), "--source", "cs4"]
    assert main(args) == 2
    assert "cs4.html" in caplog.text
    assert "Traceback" not in caplog.text


def test_cli_reports_an_unreachable_database_in_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert main(["migrate", "--dsn", "postgresql://nobody@127.0.0.1:1/x"]) == 2
    assert "cannot connect to the database" in caplog.text
    assert "Traceback" not in caplog.text


def test_cli_reports_a_tampered_root_ca_in_one_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(fetch, "PINNED_ROOT_SHA256", "00:" * 31 + "00")
    args = ["load", "--dsn", "postgresql://nobody@127.0.0.1:1/x", "--source", "cs4"]
    assert main(args) == 2
    assert "fingerprint" in caplog.text
    assert "Traceback" not in caplog.text


def test_cli_exit_code_reports_a_failed_source(schema_dsn: str, tmp_path: Path) -> None:
    main(["migrate", "--dsn", schema_dsn])
    (tmp_path / "cs4.html").write_bytes(b"<html>not a queue page</html>")
    args = ["load", "--dsn", schema_dsn, "--html-dir", str(tmp_path), "--source", "cs4"]
    assert main(args) == 1  # unparseable page: not silent, not exit 0


FULL_DIR = os.environ.get("PCQ_FULL_PAGES_DIR")


@pytest.mark.skipif(
    not FULL_DIR, reason="opt-in: set PCQ_FULL_PAGES_DIR to saved pages"
)
def test_full_pages_reproduce_known_counts_and_lineage(
    conn: psycopg.Connection,
) -> None:
    assert FULL_DIR
    expected = {
        "tc1": 56,
        "cs1": 59,
        "cs2": 212,
        "cs3": 193,
        "cs4": 122,
        "tc2": 92,
        "sq": 0,
    }
    for key, rows in expected.items():
        body = (Path(FULL_DIR) / f"{key}.html").read_bytes()
        result = ingest_page(conn, SOURCES[key], page(key, body), strict=True)
        assert result.rows_parsed == rows, key
    assert count(conn) == 734

    again = [
        ingest_page(
            conn, SOURCES[k], page(k, (Path(FULL_DIR) / f"{k}.html").read_bytes())
        )
        for k in expected
    ]
    assert sum(r.rows_inserted + r.rows_updated + r.rows_deleted for r in again) == 0

    # Lineage checks against the 2026-09-14 snapshot.
    # 1. Every parent a T2 request names exists in the loaded data.
    dangling = conn.execute(
        "SELECT t.queue_number, p FROM queue_entry t, unnest(t.former_queue_numbers) p "
        "WHERE t.source = 'tc2' AND NOT EXISTS ("
        "  SELECT 1 FROM queue_entry q WHERE q.queue_number = p)"
    ).fetchall()
    assert dangling == []
    # 2. Every request the page says moved to T2 is the parent of some T2 request.
    childless = conn.execute(
        "SELECT q.queue_number FROM queue_entry q WHERE q.moved_to_cluster = 'T2' "
        "AND NOT EXISTS (SELECT 1 FROM queue_entry t WHERE "
        "q.queue_number = ANY(t.former_queue_numbers))"
    ).fetchall()
    assert childless == []
    # 3. The reverse does not hold in the source: T2-001 names C4-032 as its parent,
    #    but C4-032's own row just says WITHDRAWN. Recorded so nobody assumes that
    #    "has a T2 child" implies "moved_to_cluster is set".
    unmarked = conn.execute(
        "SELECT t.queue_number, p FROM queue_entry t, unnest(t.former_queue_numbers) p "
        "JOIN queue_entry q ON q.queue_number = p AND q.moved_to_cluster IS NULL "
        "WHERE t.source = 'tc2'"
    ).fetchall()
    assert unmarked == [("T2-001", "C4-032")]
