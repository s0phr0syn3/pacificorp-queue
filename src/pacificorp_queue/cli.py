"""Command line entry point: `pacificorp-queue migrate | load`."""

import argparse
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from pacificorp_queue.db import migrate
from pacificorp_queue.fetch import FetchedPage, build_ssl_context, fetch_page
from pacificorp_queue.pipeline import ingest_page
from pacificorp_queue.sources import SOURCES, Source

log = logging.getLogger("pacificorp_queue")


def _parser() -> argparse.ArgumentParser:
    """`migrate` and `load` subcommands; both take --dsn."""
    parser = argparse.ArgumentParser(prog="pacificorp-queue", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_dsn(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--dsn",
            default=os.environ.get("DATABASE_URL"),
            help="Postgres connection string (default: $DATABASE_URL)",
        )

    add_dsn(sub.add_parser("migrate", help="create or upgrade the schema"))

    load = sub.add_parser(
        "load",
        help="fetch pages and load them",
        epilog="sources:\n"
        + "\n".join(f"  {s.key:<4} {s.description}" for s in SOURCES.values()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_dsn(load)
    load.add_argument(
        "--source",
        action="append",
        choices=sorted(SOURCES),
        help="page to load (repeatable; default: all)",
    )
    load.add_argument(
        "--html-dir",
        type=Path,
        help="read <source>.html files from this directory instead of the network",
    )
    load.add_argument(
        "--strict",
        action="store_true",
        help="fail a page that contains any unrecognized value",
    )
    return parser


def _read_local(source: Source, directory: Path) -> FetchedPage:
    """Offline mode: read <key>.html from `directory` instead of the network."""
    body = (directory / f"{source.key}.html").read_bytes()
    return FetchedPage(url=source.url, body=body, fetched_at=datetime.now(UTC))


def _missing_pages(selected: list[Source], directory: Path) -> list[Path]:
    """Pages offline mode needs that `directory` does not contain."""
    return [
        path
        for source in selected
        if not (path := directory / f"{source.key}.html").is_file()
    ]


def _connect(dsn: str) -> psycopg.Connection | None:
    """Autocommit connection (see guard.require_idle), or None after logging why not."""
    try:
        return psycopg.connect(dsn, autocommit=True)
    except psycopg.OperationalError as exc:
        log.error("cannot connect to the database: %s", " ".join(str(exc).split()))
        return None


def main(argv: list[str] | None = None) -> int:
    """Exit code: 0 ok, 1 a source failed, 2 bad usage or no database."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = _parser().parse_args(argv)
    if not args.dsn:
        log.error("no database: pass --dsn or set DATABASE_URL")
        return 2

    if args.command == "migrate":
        if (conn := _connect(args.dsn)) is None:
            return 2
        with conn:  # closes the connection on exit
            applied = migrate(conn)
        log.info("migrations applied: %s", applied or "none (up to date)")
        return 0

    selected = [SOURCES[k] for k in (args.source or SOURCES)]
    if args.html_dir and (missing := _missing_pages(selected, args.html_dir)):
        log.error(
            "--html-dir %s is missing: %s",
            args.html_dir,
            ", ".join(p.name for p in missing),
        )
        return 2
    context = None if args.html_dir else build_ssl_context()
    if (conn := _connect(args.dsn)) is None:
        return 2
    failures = 0
    with conn:
        for source in selected:
            try:
                if args.html_dir:
                    fetched = _read_local(source, args.html_dir)
                else:
                    assert context is not None  # only None in offline mode
                    fetched = fetch_page(source.url, context)
                result = ingest_page(conn, source, fetched, strict=args.strict)
            except Exception:  # deliberate: one bad page must not stop the others
                failures += 1
                log.exception("source=%s failed", source.key)
                continue
            log.info(
                "source=%s as_of=%s parsed=%d inserted=%d updated=%d unchanged=%d "
                "deleted=%d issues=%d",
                result.source,
                result.as_of,
                result.rows_parsed,
                result.rows_inserted,
                result.rows_updated,
                result.rows_unchanged,
                result.rows_deleted,
                result.issues,
            )
    if failures:
        log.error("%d of %d sources failed", failures, len(selected))
        return 1
    return 0
