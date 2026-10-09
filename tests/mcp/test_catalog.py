"""Catalog: dbt docs files first, live information_schema as the fallback."""

import json

import pytest

from src.mcp import catalog


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(catalog, "_catalog", None)
    monkeypatch.setattr(catalog, "load_env", lambda: None)
    monkeypatch.setattr(catalog, "allowed_schemas", lambda: ["gold"])


def _write_dbt(tmp_path):
    node = "model.p.mart_a"
    (tmp_path / "catalog.json").write_text(
        json.dumps(
            {
                "nodes": {
                    node: {
                        "metadata": {"schema": "gold", "name": "mart_a"},
                        "columns": {
                            "b": {"name": "b", "type": "text", "index": 2},
                            "a": {"name": "a", "type": "date", "index": 1},
                        },
                    },
                    "model.p.br_x": {
                        "metadata": {"schema": "bronze", "name": "br_x"},
                        "columns": {},
                    },
                }
            }
        )
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "nodes": {
                    node: {
                        "description": "A mart.",
                        "columns": {"a": {"description": "The date."}},
                    }
                }
            }
        )
    )


def test_dbt_files_give_types_descriptions_and_column_order(tmp_path):
    _write_dbt(tmp_path)

    tables = catalog.load_dbt(tmp_path, ["gold"])

    assert list(tables) == ["gold.mart_a"]  # bronze filtered out
    assert tables["gold.mart_a"]["description"] == "A mart."
    cols = tables["gold.mart_a"]["columns"]
    assert [c["name"] for c in cols] == ["a", "b"]
    assert cols[0] == {"name": "a", "type": "date", "description": "The date."}
    assert cols[1]["description"] == ""


def test_missing_files_give_an_empty_catalog(tmp_path):
    assert catalog.load_dbt(tmp_path, ["gold"]) == {}


def test_an_empty_catalog_is_filled_live_once(tmp_path, monkeypatch):
    monkeypatch.setenv("AURUM_MCP_DBT_TARGET", str(tmp_path))
    calls = []

    def fake_live(schemas, table=None):
        calls.append(table)
        return {"gold.t": {"description": "", "columns": []}}

    monkeypatch.setattr(catalog, "load_live", fake_live)

    catalog.get_catalog()
    catalog.get_catalog()

    assert calls == [None]


def test_describe_miss_falls_back_to_live_and_caches(tmp_path, monkeypatch):
    _write_dbt(tmp_path)
    monkeypatch.setenv("AURUM_MCP_DBT_TARGET", str(tmp_path))
    calls = []

    def fake_live(schemas, table=None):
        calls.append(table)
        cols = [{"name": "z", "type": "int", "description": ""}]
        return {"gold.new_t": {"description": "", "columns": cols}}

    monkeypatch.setattr(catalog, "load_live", fake_live)

    assert catalog.describe("gold", "new_t")["columns"][0]["name"] == "z"
    catalog.describe("gold", "new_t")

    assert calls == ["new_t"]  # second call served from memory


def test_unknown_table_and_disallowed_schema_raise(tmp_path, monkeypatch):
    _write_dbt(tmp_path)
    monkeypatch.setenv("AURUM_MCP_DBT_TARGET", str(tmp_path))
    monkeypatch.setattr(catalog, "load_live", lambda schemas, table=None: {})

    with pytest.raises(ValueError, match="not found"):
        catalog.describe("gold", "nope")
    with pytest.raises(ValueError, match="not allowed"):
        catalog.describe("bronze", "br_x")


def test_list_tables_only_returns_the_requested_schema(tmp_path, monkeypatch):
    _write_dbt(tmp_path)
    monkeypatch.setenv("AURUM_MCP_DBT_TARGET", str(tmp_path))

    assert catalog.list_tables("gold") == [
        {"table": "mart_a", "description": "A mart.", "columns": 2}
    ]
