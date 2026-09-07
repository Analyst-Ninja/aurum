"""The EDGAR financial-statement source.

Two things carry the weight here: `_melt_statement`, which turns a wide statement
(concept rows, period columns) into the long form the landing table expects, and the
thread pool, which must record a per-ticker failure as a result rather than let one
bad ticker abort the other 502.
"""

import pandas as pd
import pytest

from src.ingestion.datasources.api.edgar import financial_stmts as fs


@pytest.fixture(autouse=True)
def user_agent(monkeypatch):
    monkeypatch.setattr(fs, "get_sec_user_agent", lambda: "AURUM you@example.com")


def _source(**overrides):
    config = {"type": "edgar_financials", "name": "income", "period": "annual"}
    config.update(overrides)
    return fs.FinancialStmtsDatasource(config)


def _wide(period_columns):
    """A statement as edgartools returns it: concept in the index, periods as columns."""
    return pd.DataFrame(
        {"label": ["Revenue", "Net income"], **{c: [1.0, 2.0] for c in period_columns}},
        index=pd.Index(["Revenues", "NetIncomeLoss"], name="concept"),
    )


def test_defaults_are_the_documented_ones():
    source = fs.FinancialStmtsDatasource({"name": "income"})

    assert (source.timeout, source.period, source.stmt_type) == (10, "annual", "income")


def test_annual_periods_melt_into_an_fy_column():
    melted = _source(period="annual")._melt_statement(_wide(["FY 2024", "FY 2025"]), "AAPL")

    assert "FY" in melted.columns
    assert sorted(melted["FY"].unique()) == ["FY 2024", "FY 2025"]
    assert len(melted) == 4  # 2 concepts x 2 periods


def test_quarterly_periods_melt_into_a_qtr_column():
    melted = _source(period="quarterly")._melt_statement(_wide(["Q1 2025", "Q2 2025"]), "AAPL")

    assert "QTR" in melted.columns
    assert sorted(melted["QTR"].unique()) == ["Q1 2025", "Q2 2025"]


def test_every_column_is_uppercased_because_postgres_identifiers_are_quoted():
    melted = _source()._melt_statement(_wide(["FY 2025"]), "AAPL")

    assert all(column == column.upper() for column in melted.columns)


def test_each_row_is_tagged_with_the_ticker_it_came_from():
    melted = _source()._melt_statement(_wide(["FY 2025"]), "MSFT")

    assert melted["SYMBOL"].unique().tolist() == ["MSFT"]


def test_identifier_columns_survive_the_melt_as_id_vars():
    melted = _source()._melt_statement(_wide(["FY 2025"]), "AAPL")

    assert {"CONCEPT", "LABEL", "VALUE"} <= set(melted.columns)


def test_an_unknown_period_melts_nothing_and_says_so_by_leaving_no_value_rows():
    melted = _source(period="monthly")._melt_statement(_wide(["FY 2025"]), "AAPL")

    assert melted.empty


class _Statement:
    def __init__(self, frame):
        self._frame = frame

    def to_dataframe(self):
        return self._frame


class _Company:
    """Records which statement accessor the config selected."""

    seen: list[tuple[str, str, int]] = []

    def __init__(self, ticker):
        self.ticker = ticker
        if ticker == "BAD":
            raise RuntimeError("no such company")

    def _record(self, kind, periods, period):
        _Company.seen.append((kind, period, periods))
        return _Statement(_wide(["FY 2025"]))

    def income_statement(self, periods, period):
        return self._record("income", periods, period)

    def cash_flow_statement(self, periods, period):
        return self._record("cashflow", periods, period)

    def balance_sheet(self, periods, period):
        return self._record("balance_sheet", periods, period)


@pytest.fixture
def company(monkeypatch):
    _Company.seen = []
    monkeypatch.setattr(fs, "Company", _Company)
    return _Company


@pytest.mark.parametrize(
    "stmt_type", ["income", "cashflow", "balance_sheet"]
)
def test_the_config_picks_which_statement_is_fetched(company, stmt_type):
    ticker, melted = _source(stmt_type=stmt_type)._get_statement("AAPL")

    assert ticker == "AAPL"
    assert company.seen[0][0] == stmt_type
    assert melted["SYMBOL"].unique().tolist() == ["AAPL"]


def test_the_period_and_period_count_reach_edgartools(company):
    _source(period="quarterly")._get_statement("AAPL", periods=8)

    assert company.seen[0][1:] == ("quarterly", 8)


def test_an_unknown_statement_type_melts_the_empty_frame_rather_than_raising(company):
    ticker, result = _source(stmt_type="equity")._get_statement("AAPL")

    assert ticker == "AAPL"
    assert isinstance(result, pd.DataFrame)


def test_a_failing_ticker_comes_back_as_its_exception(company):
    ticker, result = _source()._get_statement("BAD")

    assert ticker == "BAD"
    assert isinstance(result, RuntimeError)


def test_every_ticker_is_fetched_and_keyed_by_name(company, capsys):
    results = _source()._get_all_statements(["AAPL", "MSFT", "BAD"], max_workers=2)

    assert set(results) == {"AAPL", "MSFT", "BAD"}
    assert isinstance(results["BAD"], RuntimeError)
    assert "BAD: FAILED" in capsys.readouterr().out


def test_combine_keeps_only_the_frames_and_stacks_them(company):
    source = _source()
    results = source._get_all_statements(["AAPL", "MSFT", "BAD"], max_workers=2)

    combined = source._combine_results(results)

    assert sorted(combined["SYMBOL"].unique()) == ["AAPL", "MSFT"]


def test_read_data_sets_the_sec_identity_before_fetching(company, monkeypatch):
    identities = []
    monkeypatch.setattr(fs, "set_identity", identities.append)
    monkeypatch.setattr(fs, "get_snp500_symbols", lambda agent, timeout: ["AAPL", "MSFT"])

    frame = fs.FinancialStmtsDatasource({"name": "income"}).read_data("2026-01-01", {})

    assert identities == ["AURUM you@example.com"]
    assert sorted(frame["SYMBOL"].unique()) == ["AAPL", "MSFT"]


def test_write_data_is_a_no_op_because_an_api_is_read_only():
    assert _source().write_data("2026-01-01", pd.DataFrame({"a": [1]})) is None
