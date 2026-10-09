"""Read-only Postgres access for the MCP server.

Safety is the database's job, not this module's: the engine opens every session with
``default_transaction_read_only=on`` and a ``statement_timeout``, and each query runs in a
``SET TRANSACTION READ ONLY`` transaction that is rolled back. There is deliberately no
``commit()`` in this file. ``check_select`` only exists to turn the obvious mistakes into
a clear error before Postgres turns them into an opaque one.

stdout is the JSON-RPC stream under the stdio transport, so everything here logs to stderr.
"""

import logging
import os
import re
import sys
import time
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import SQLAlchemyError

from src.utils.env import load_env

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("aurum.mcp")

ALLOWED_FIRST_WORDS = ("select", "with", "explain", "show")


def allowed_schemas() -> list[str]:
    load_env()
    raw = os.getenv("AURUM_MCP_SCHEMAS", "gold")
    return [s.strip() for s in raw.split(",") if s.strip()]


def max_rows_ceiling() -> int:
    load_env()
    return int(os.getenv("AURUM_MCP_MAX_ROWS", "1000"))


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Build the engine on first use, so importing the server never touches the network."""
    load_env()
    user = os.getenv("AURUM_MCP_USERNAME")
    password = os.getenv("AURUM_MCP_PASSWORD")
    if not user:
        logger.warning(
            "AURUM_MCP_USERNAME unset - falling back to AURUM_USERNAME, the warehouse "
            "owner. Create the read-only role in infra/sql/mcp_readonly_role.sql."
        )
        user, password = os.getenv("AURUM_USERNAME"), os.getenv("AURUM_PASSWORD")

    timeout_ms = int(os.getenv("AURUM_MCP_TIMEOUT_MS", "30000"))
    url = URL.create(
        "postgresql",
        username=user,
        password=password,
        host=os.getenv("HOST"),
        port=int(os.getenv("PORT", "5432")),
        database=os.getenv("AURUM_MCP_DB_NAME", "aurum"),
    )
    return create_engine(
        url,
        pool_size=1,
        pool_pre_ping=True,
        connect_args={
            "application_name": "aurum-mcp",
            "options": (
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={timeout_ms}"
            ),
        },
    )


# One left-to-right pass so a `--` inside a string is not a comment and a `;` inside a
# string is not a statement break. Order matters: dollar-quotes before plain strings.
_NOISE = re.compile(
    r"""--[^\n]*|/\*.*?\*/|\$(\w*)\$.*?\$\1\$|'(?:[^']|'')*'|"(?:[^"]|"")*" """.strip(),
    re.DOTALL,
)

# Whole-word hits anywhere in the query. `INTO` catches SELECT ... INTO, `UPDATE` catches
# FOR UPDATE row locks, and a write after EXPLAIN is caught here too.
WRITE_WORDS = frozenset(
    "insert update delete merge drop create alter truncate grant revoke copy into "
    "call do lock vacuum reindex cluster refresh comment set reset".split()
)


def read_query_problem(sql: str) -> str | None:
    """Why ``sql`` is not a single read query, or ``None`` if it looks like one.

    Comments and quoted text are blanked first, so keywords inside a string literal or a
    quoted column name neither trip the check nor hide behind it. This is a screen, not a
    parser: the READ ONLY transaction is what actually stops a write.
    """
    bare = _NOISE.sub(" ", sql).strip().rstrip(";").strip()
    if not bare:
        return "Empty query."
    if ";" in bare:
        return "Only one statement per call."
    words = re.findall(r"[a-z_]+", bare.lower())
    if words[0] not in ALLOWED_FIRST_WORDS:
        return f"Only read queries are allowed (start with {', '.join(ALLOWED_FIRST_WORDS)})."
    hit = sorted(WRITE_WORDS.intersection(words))
    if hit:
        return f"Not a read query: contains {', '.join(hit).upper()}."
    return None


def is_read_query(sql: str) -> bool:
    return read_query_problem(sql) is None


def check_select(sql: str) -> str:
    """Return ``sql`` without a trailing ``;``, or raise ``ValueError`` saying why not."""
    problem = read_query_problem(sql)
    if problem:
        raise ValueError(problem)
    return sql.strip().rstrip(";").strip()


def error_message(error: SQLAlchemyError) -> str:
    """Clean message plus SQLSTATE. The raw text can carry the DSN, so it only hits stderr."""
    logger.error("query failed: %s", error)
    orig = getattr(error, "orig", None)
    state = getattr(orig, "pgcode", None) or "unknown"
    first_line = str(orig or error).strip().splitlines()[0] if (orig or error) else ""
    return f"Query failed (SQLSTATE {state}): {first_line}"


def run_select(
    sql: str, params: dict[str, Any] | None = None, max_rows: int | None = None
) -> dict[str, Any]:
    """Run one read query and return at most ``max_rows`` rows.

    Asks for ``max_rows + 1`` so ``truncated`` is a fact rather than a guess.
    """
    cleaned = check_select(sql)
    limit = min(max_rows or max_rows_ceiling(), max_rows_ceiling())
    started = time.perf_counter()
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            result = conn.execution_options(stream_results=True).execute(
                text(cleaned), params or {}
            )
            columns = list(result.keys())
            fetched = result.fetchmany(limit + 1)
            conn.rollback()
    except SQLAlchemyError as error:
        raise RuntimeError(error_message(error)) from None

    elapsed_ms = round((time.perf_counter() - started) * 1000)
    rows = [[_jsonable(v) for v in row] for row in fetched[:limit]]
    logger.info("rows=%d truncated=%s ms=%d", len(rows), len(fetched) > limit, elapsed_ms)
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": len(fetched) > limit,
        "elapsed_ms": elapsed_ms,
    }


def _jsonable(value: Any) -> Any:
    """Dates and Decimals are not JSON; everything else passes through."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
