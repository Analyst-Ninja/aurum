import pandas as pd
import pytest

from src.paper.ledger import Ledger


@pytest.fixture
def ledger(tmp_path):
    return Ledger(f"sqlite:///{tmp_path}/nested/ledger.db")


def test_creates_parent_directory_for_sqlite(tmp_path):
    Ledger(f"sqlite:///{tmp_path}/deep/er/ledger.db")

    assert (tmp_path / "deep" / "er").is_dir()


def test_read_missing_table_is_empty(ledger):
    assert ledger.read("decisions").empty


def test_append_round_trips_and_skips_empty_frames(ledger):
    ledger.append("orders", pd.DataFrame({"symbol": ["A"], "qty": [3.0]}))
    ledger.append("orders", pd.DataFrame(columns=["symbol", "qty"]))

    got = ledger.read("orders")
    assert list(got["symbol"]) == ["A"]


def test_decided_reflects_recorded_days(ledger):
    assert ledger.decided("2026-01-05") is False

    ledger.append("decisions", pd.DataFrame({"asof": ["2026-01-05"], "symbol": ["A"]}))

    assert ledger.decided("2026-01-05") is True
    assert ledger.decided("2026-01-06") is False


def test_snapshots_empty_before_first_write(ledger):
    assert ledger.snapshots().empty


def test_snapshots_come_back_sorted_with_utc_index(ledger):
    ledger.append(
        "snapshots",
        pd.DataFrame({"ts": ["2026-01-06T15:00:00+00:00", "2026-01-05T15:00:00+00:00"], "equity": [2.0, 1.0]}),
    )
    snaps = ledger.snapshots()

    assert list(snaps["equity"]) == [1.0, 2.0]
    assert str(snaps.index.tz) == "UTC"
