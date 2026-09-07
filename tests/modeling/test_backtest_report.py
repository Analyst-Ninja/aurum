"""The report: six panels, two themes each, and a self-contained HTML page.

`report.html` has to open from disk with no server and no CDN, which means every image
is an inlined base64 PNG and every panel is rendered twice so CSS can pick the one
matching the reader's colour scheme. The assertions below are on that contract, not on
pixels.
"""

import base64

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest

from src.modeling.backtest import engine, report


@pytest.fixture
def predictions():
    rng = np.random.default_rng(0)
    dates = pd.bdate_range("2024-01-01", periods=60)
    symbols = [f"SYM{i:02d}" for i in range(20)]
    frame = pd.MultiIndex.from_product([dates, symbols], names=["date", "symbol"]).to_frame(
        index=False
    )
    signal = rng.normal(size=len(frame))
    frame["pred"] = signal
    frame["y"] = 0.004 * signal + 0.02 * rng.normal(size=len(frame))
    return frame


@pytest.fixture
def book(predictions):
    return engine.build_book(predictions, 5)


@pytest.fixture
def curves(book):
    return {
        f"{int(cost)} bps": engine.equity_curve(engine.run(book, cost), 5)
        for cost in (0.0, 10.0)
    }


@pytest.fixture
def yearly(book):
    return engine.yearly_table(engine.run(book, 10.0), 5)


@pytest.fixture
def randomization(predictions, book):
    return engine.randomization_test(predictions, book, 8, 42)


@pytest.fixture
def axes():
    figure, axis = plt.subplots()
    yield axis
    plt.close(figure)


@pytest.fixture
def block(predictions, curves, yearly, randomization):
    return {
        "title": "Holdout — the quoted result",
        "subtitle": "Folds 4+.",
        "predictions": predictions,
        "curves": curves,
        "yearly": yearly,
        "sweep": engine.sweep(engine.build_book(predictions, 5), [0.0, 10.0]),
        "mean_turnover": 0.42,
        "n_dates": 60,
        "randomization": randomization,
    }


THEME = report.THEMES["light"]


def test_both_themes_define_every_colour_role():
    roles = set(report.THEMES["light"])

    assert set(report.THEMES["dark"]) == roles
    assert {"surface", "ink", "positive", "negative", "ramp", "sequential"} <= roles


def test_the_decile_panel_draws_one_bar_per_bucket(axes, predictions):
    report.panel_decile(axes, predictions, THEME)

    assert len(axes.patches) <= 10
    assert axes.get_xticks().tolist() == list(range(1, 11))


def test_the_decile_panel_colours_by_the_sign_of_the_return(axes, predictions):
    report.panel_decile(axes, predictions, THEME)
    heights = [patch.get_height() for patch in axes.patches]
    colours = [patch.get_facecolor() for patch in axes.patches]

    for height, colour in zip(heights, colours):
        expected = THEME["positive"] if height >= 0 else THEME["negative"]
        assert colour[:3] == pytest.approx(
            plt.matplotlib.colors.to_rgb(expected), abs=1e-6
        )


def test_the_scatter_panel_reports_its_r2_in_the_title(axes, predictions):
    report.panel_scatter(axes, predictions, THEME)

    assert "R²" in axes.get_title(loc="left")


def test_the_scatter_panel_subsamples_a_very_large_panel(axes, monkeypatch, predictions):
    """60k points is already past what a hexbin resolves; drawing 2.7M is just slow."""
    big = pd.concat([predictions] * 60, ignore_index=True)
    assert len(big) > 60_000

    report.panel_scatter(axes, big, THEME, seed=1)

    assert axes.collections  # it drew something rather than falling over


def test_the_equity_panel_draws_and_labels_every_cost_level(axes, curves):
    report.panel_equity(axes, curves, THEME)

    assert len(axes.lines) == len(curves) + 1  # one per curve, plus the unit line
    assert {text.get_text() for text in axes.texts} == set(curves)


def test_the_equity_panel_reuses_the_last_ramp_colour_past_its_length(axes, curves):
    many = {f"{i} bps": next(iter(curves.values())) for i in range(8)}

    report.panel_equity(axes, many, THEME)

    assert len(axes.lines) == len(many) + 1


def test_an_empty_curve_is_drawn_without_a_label(axes):
    report.panel_equity(axes, {"0 bps": pd.Series(dtype="float64")}, THEME)

    assert len(axes.texts) == 0


def test_the_rolling_ic_panel_reports_the_mean_ic_in_its_title(axes, predictions):
    report.panel_rolling_ic(axes, predictions, THEME)

    assert "Rolling 63-day IC (mean" in axes.get_title(loc="left")


def test_the_yearly_panel_ticks_every_calendar_year(axes, yearly):
    report.panel_yearly(axes, yearly, THEME)

    assert axes.get_xticks().tolist() == yearly["year"].tolist()


def test_an_empty_yearly_table_still_titles_its_axes(axes):
    report.panel_yearly(axes, pd.DataFrame(), THEME)

    assert axes.get_title(loc="left") == "Net return by year"
    assert len(axes.patches) == 0


def test_the_randomization_panel_marks_the_actual_sharpe(axes, randomization):
    report.panel_randomization(axes, randomization, THEME)

    assert len(axes.lines) == 1  # the vertical actual-Sharpe rule
    assert "actual" in axes.texts[0].get_text()


def test_a_randomization_dict_with_no_null_draws_nothing_but_still_titles(axes):
    report.panel_randomization(axes, {}, THEME)

    assert "0 shuffles" in axes.get_title(loc="left")
    assert len(axes.texts) == 0


def test_a_panel_pair_renders_one_data_uri_per_theme(predictions):
    images = report._panel_pair(report.panel_decile, predictions)

    assert set(images) == {"light", "dark"}
    for uri in images.values():
        assert uri.startswith("data:image/png;base64,")
        assert base64.b64decode(uri.split(",", 1)[1])[:4] == b"\x89PNG"


def test_render_panels_covers_every_chart_when_a_null_is_present(block):
    panels = report.render_panels(block)

    assert [panel["title"] for panel in panels] == [
        "Prediction vs truth",
        "Predicted vs realized",
        "Equity by cost",
        "Rolling IC",
        "Yearly returns",
        "Randomization null",
    ]


def test_the_randomization_panel_is_omitted_for_a_block_without_one(block):
    del block["randomization"]

    titles = [panel["title"] for panel in report.render_panels(block)]

    assert "Randomization null" not in titles


def test_the_tearsheet_is_a_png_on_disk(tmp_path, predictions, curves, yearly, randomization):
    path = report.write_tearsheet(
        tmp_path / report.TEARSHEET, predictions, curves, yearly, randomization
    )

    assert path.read_bytes()[:4] == b"\x89PNG"


def test_a_table_renders_a_row_per_record_and_formats_floats():
    html = report._table(pd.DataFrame({"year": [2024], "net_return": [0.1234567]}))

    assert "<td>0.1235</td>" in html
    assert "<td>2024</td>" in html


def test_an_empty_table_says_so_rather_than_rendering_a_header():
    assert report._table(pd.DataFrame()) == "<p class='meta'>No rows.</p>"


def test_tiles_render_a_key_and_a_value_each():
    html = report._tiles([("Dates", "60"), ("Break-even", "12.0 bps")])

    assert html.count('class="tile"') == 2
    assert "Break-even" in html and "12.0 bps" in html


def test_figures_emit_a_light_and_a_dark_image_per_panel():
    html = report._figures([{"title": "T", "images": {"light": "L", "dark": "D"}}])

    assert 'class="light"' in html and 'class="dark"' in html
    assert 'src="L"' in html and 'src="D"' in html


def test_the_report_is_self_contained(tmp_path, block):
    summary = {"reality_checks": [{"check": "Randomization", "value": "50th", "reads": "x"}]}

    path = report.write_report(tmp_path / report.REPORT, "20260907-abc", [block], summary)
    html = path.read_text()

    assert html.startswith("<!doctype html>")
    assert "20260907-abc" in html
    assert "src=\"http" not in html and "<script" not in html


def test_the_report_leads_with_the_biases_before_any_number(tmp_path, block):
    summary = {"reality_checks": []}

    html = report.write_report(tmp_path / report.REPORT, "v", [block], summary).read_text()

    assert html.index("Read this before any number below") < html.index(block["title"])
    for bias in report.BIASES:
        assert bias in html


def test_the_report_tiles_the_swept_sharpes_and_the_break_even(tmp_path, block):
    html = report.write_report(
        tmp_path / report.REPORT, "v", [block], {"reality_checks": []}
    ).read_text()

    assert "Sharpe @0bps" in html and "Sharpe @10bps" in html
    assert "Break-even" in html
    assert "42%" in html  # mean turnover, rendered as a percentage
