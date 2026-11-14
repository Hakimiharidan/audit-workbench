"""DuckDB connection with no file or network access once locked."""

from __future__ import annotations

import duckdb

LOCK_STATEMENTS = (
    "SET enable_external_access = false",
    "SET autoinstall_known_extensions = false",
    "SET autoload_known_extensions = false",
    "SET lock_configuration = true",
)


def connect() -> duckdb.DuckDBPyConnection:
    """An in-memory connection. Extension auto-install/auto-load are off from the start."""
    con = duckdb.connect(":memory:", config={
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
        "threads": 1,  # deterministic and light; data sizes here are small
    })
    return con


def lock(con: duckdb.DuckDBPyConnection) -> None:
    """After this, SQL cannot read/write files, attach databases, install extensions or change settings."""
    for stmt in LOCK_STATEMENTS:
        con.execute(stmt)


def qi(name: str) -> str:
    """Quote an identifier."""
    return '"' + str(name).replace('"', '""') + '"'
