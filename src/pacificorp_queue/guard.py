"""Shared precondition for code that promises an atomic transaction."""

import psycopg
from psycopg.pq import TransactionStatus


def require_idle(conn: psycopg.Connection) -> None:
    """The caller's `conn.transaction()` must be the outermost one; inside an
    already-open transaction it would only be a savepoint and nothing would
    commit when it exits. Connect with autocommit=True (the CLI does)."""
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("connection is already in a transaction")
