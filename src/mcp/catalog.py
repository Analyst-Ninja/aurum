"""In-memory table catalog for the MCP server.

Primary source is dbt's own docs artifacts: ``target/catalog.json`` (column types from the
live warehouse) merged with ``target/manifest.json`` (model and column descriptions). They
are read once, on first use, and held in memory.

``target/`` is gitignored, so a fresh clone, CI and the container have no such files. That
is not an error: an empty catalog is filled by one ``information_schema`` query instead, and
any single table the files do not know is looked up the same way and cached. Postgres has no
``DESCRIBE`` or ``SHOW COLUMNS``; ``information_schema.columns`` is the equivalent.
"""

import json
import os
from pathlib import Path
from typing import Any

from src.mcp.db import allowed_schemas, logger, run_select
from src.utils.env import load_env

DEFAULT_TARGET = (
    Path(__file__).resolve().parents[1] / "transformation" / "aurum_dwh" / "target"
)

LIVE_COLUMNS_SQL = """
    SELECT table_schema, table_name, column_name, data_type
    FROM information_schema.columns
    WHERE table_schema = ANY(:schemas)
      AND (CAST(:tbl AS text) IS NULL OR table_name = :tbl)
    ORDER BY table_schema, table_name, ordinal_position
"""

# "schema.table" -> {"description": str, "columns": [{"name", "type", "description"}]}
_catalog: dict[str, dict[str, Any]] | None = None


def load_dbt(target_dir: Path, schemas: list[str]) -> dict[str, dict[str, Any]]:
    """Merge catalog.json and manifest.json; return ``{}`` if either is missing."""
    try:
        cat = json.loads((target_dir / "catalog.json").read_text())["nodes"]
        man = json.loads((target_dir / "manifest.json").read_text())["nodes"]
    except (OSError, ValueError, KeyError):
        return {}

    tables: dict[str, dict[str, Any]] = {}
    for node_id, node in cat.items():
        meta = node["metadata"]
        if meta["schema"] not in schemas:
            continue
        doc = man.get(node_id, {})
        col_docs = doc.get("columns", {})
        tables[f"{meta['schema']}.{meta['name']}"] = {
            "description": (doc.get("description") or "").strip(),
            "columns": [
                {
                    "name": c["name"],
                    "type": c["type"],
                    "description": (
                        col_docs.get(c["name"], {}).get("description") or ""
                    ).strip(),
                }
                for c in sorted(node["columns"].values(), key=lambda c: c["index"])
            ],
        }
    return tables


def load_live(schemas: list[str], table: str | None = None) -> dict[str, dict[str, Any]]:
    """One bound-parameter query against information_schema, grouped into catalog shape."""
    result = run_select(LIVE_COLUMNS_SQL, {"schemas": schemas, "tbl": table}, max_rows=100_000)
    tables: dict[str, dict[str, Any]] = {}
    for schema, name, column, dtype in result["rows"]:
        entry = tables.setdefault(f"{schema}.{name}", {"description": "", "columns": []})
        entry["columns"].append({"name": column, "type": dtype, "description": ""})
    return tables


def get_catalog() -> dict[str, dict[str, Any]]:
    global _catalog
    if _catalog is None:
        load_env()
        target = Path(os.getenv("AURUM_MCP_DBT_TARGET", DEFAULT_TARGET))
        schemas = allowed_schemas()
        # Built locally and assigned last: a failed load must leave _catalog as None so the
        # next call retries, not an empty dict that would be served as "no tables" forever.
        tables = load_dbt(target, schemas)
        # dbt only documents the schemas it builds; anything else allowed (e.g. the raw
        # landing tables in public) is filled from one live query.
        uncovered = [s for s in schemas if not any(k.startswith(f"{s}.") for k in tables)]
        if uncovered:
            logger.info("no dbt docs for %s under %s - loading live", uncovered, target)
            try:
                tables.update(load_live(uncovered))
            except Exception:
                if not tables:
                    raise
                logger.warning("live catalog load failed; serving dbt docs only", exc_info=True)
                return tables  # partial, so not cached - the next call tries again
        _catalog = tables
        logger.info("catalog ready: %d tables", len(_catalog))
    return _catalog


def refresh() -> int:
    """Drop the cache (e.g. after ``dbt build``); the next call reloads. Returns table count."""
    global _catalog
    _catalog = None
    return len(get_catalog())


def list_tables(schema: str) -> list[dict[str, Any]]:
    _require_schema(schema)
    prefix = f"{schema}."
    return [
        {
            "table": key[len(prefix):],
            "description": entry["description"],
            "columns": len(entry["columns"]),
        }
        for key, entry in get_catalog().items()
        if key.startswith(prefix)
    ]


def describe(schema: str, table: str) -> dict[str, Any]:
    """Cached table definition; on a miss, ask the database and cache the answer."""
    _require_schema(schema)
    catalog = get_catalog()
    key = f"{schema}.{table}"
    if key not in catalog:
        catalog.update(load_live([schema], table))
    if key not in catalog:
        raise ValueError(f"Table not found: {key}")
    return {"table": key, **catalog[key]}


def to_markdown() -> str:
    """Whole catalog as compact markdown, for the ``aurum://catalog`` resource."""
    lines = []
    for key, entry in get_catalog().items():
        lines.append(f"## {key}\n{entry['description']}\n")
        for c in entry["columns"]:
            lines.append(f"- `{c['name']}` {c['type']} {c['description']}".rstrip())
        lines.append("")
    return "\n".join(lines)


def _require_schema(schema: str) -> None:
    if schema not in allowed_schemas():
        raise ValueError(f"Schema not allowed: {schema!r}. Allowed: {allowed_schemas()}")
