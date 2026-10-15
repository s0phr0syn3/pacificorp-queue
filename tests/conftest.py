"""Database fixtures.

By default a throwaway Postgres 18 is started with testcontainers (needs
Docker). Set PCQ_TEST_DSN to use an existing server instead; every test runs in
its own schema, which is dropped afterwards, so nothing else in that database
is touched. Missing Docker makes the integration tests error, not skip.
"""

import os
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from pacificorp_queue.db import migrate


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    explicit = os.environ.get("PCQ_TEST_DSN")
    if explicit:
        yield explicit
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:18-alpine", driver=None) as container:
        yield container.get_connection_url()


@pytest.fixture
def schema_dsn(pg_dsn: str) -> Iterator[str]:
    """A DSN whose search_path is a fresh, empty schema."""
    schema = f"pcq_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    yield make_conninfo(pg_dsn, options=f"-c search_path={schema}")
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def connect(schema_dsn: str) -> Iterator[Callable[[], psycopg.Connection]]:
    opened: list[psycopg.Connection] = []

    def make() -> psycopg.Connection:
        conn = psycopg.connect(schema_dsn, autocommit=True)
        opened.append(conn)
        return conn

    yield make
    for conn in opened:
        conn.close()


@pytest.fixture
def conn(connect: Callable[[], psycopg.Connection]) -> psycopg.Connection:
    connection = connect()
    migrate(connection)
    return connection
