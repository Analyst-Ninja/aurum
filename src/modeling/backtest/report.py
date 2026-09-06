"""The visual half of #56: a tearsheet PNG and a standalone HTML report.

The report exists because a `metrics.json` cannot answer "does this model actually
rank stocks". The headline panel is prediction against truth — mean realized forward
return by predicted decile — and a monotone rise across those ten bars is the whole
claim of a cross-sectional model, visible in one glance. Everything else on the page
is the cost of acting on it.

`report.html` is self-contained: matplotlib renders to PNG bytes which are inlined as
base64 data URIs, so the file opens from disk with no server, no CDN and no notebook.
Each figure is rendered twice, once per theme, and CSS shows the one matching the
reader's mode — a PNG cannot adapt to `prefers-color-scheme` on its own.

Colour follows the data's job, not decoration: the cost sweep is one blue ramp ordered
by cost (an ordinal scale), returns by decile and by year are blue/red diverging about
zero (polarity), and the randomization null is muted gray with the actual Sharpe called
out in the critical status colour.
"""

import base64
import io
import logging
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  — backend must be set before pyplot

from src.modeling.evaluate import metrics  # noqa: E402

logger = logging.getLogger(__name__)

REPORT = "report.html"
TEARSHEET = "tearsheet.png"

# Palettes from the data-viz reference instance, validated for both surfaces. The cost
# ramp is a single-hue ordinal scale (`--ordinal`, all checks pass in both modes).
THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "series": "#2a78d6",
        "positive": "#2a78d6",
        "negative": "#d03b3b",
        "ramp": ["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"],
        "sequential": "Blues",
    },
    "dark": {
        "surface": "#1a1a19",
        "ink": "#ffffff",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "series": "#3987e5",
        "positive": "#3987e5",
        "negative": "#e66767",
        "ramp": ["#b7d3f6", "#6da7ec", "#2a78d6", "#184f95"],
        "sequential": "Blues_r",
    },
}


def _style(axes, theme: dict, title: str, xlabel: str = "", ylabel: str = "") -> None:
    """Recessive chrome: hairline grid, no top/right spine, muted ticks."""
    axes.set_facecolor(theme["surface"])
    axes.set_title(title, color=theme["ink"], fontsize=11, loc="left", pad=10)
    axes.set_xlabel(xlabel, color=theme["muted"], fontsize=9)
    axes.set_ylabel(ylabel, color=theme["muted"], fontsize=9)
    axes.tick_params(colors=theme["muted"], labelsize=8)
    axes.grid(True, color=theme["grid"], linewidth=0.6, zorder=0)
    axes.set_axisbelow(True)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axes.spines[side].set_color(theme["axis"])


def panel_decile(axes, predictions: pd.DataFrame, theme: dict) -> None:
    """Prediction against truth: mean realized return by predicted decile.

    The headline. A model that ranks produces a rise from decile 1 to decile 10; a
    model that does not produces noise, and the chart says so rather than averaging it
    into a single IC.
    """
    frame = predictions.dropna(subset=["pred", "y"])
    decile = metrics.deciles(frame["pred"].to_numpy(), frame["date"].to_numpy())
    means = frame.groupby(decile.to_numpy(), observed=True)["y"].mean()

    colours = [theme["positive"] if v >= 0 else theme["negative"] for v in means]
    axes.bar(means.index, means.to_numpy() * 1e4, color=colours, width=0.7, zorder=3)
    axes.axhline(0.0, color=theme["axis"], linewidth=1.0, zorder=2)
    _style(
        axes,
        theme,
        "Predicted decile vs realized 5-day return",
        "predicted decile (10 = most favoured)",
        "mean realized return (bps)",
    )
    axes.set_xticks(range(1, 11))


def panel_scatter(axes, predictions: pd.DataFrame, theme: dict, seed: int = 42) -> None:
    """Predicted score against realized return, as a density."""
    frame = predictions.dropna(subset=["pred", "y"])
    if len(frame) > 60_000:
        frame = frame.sample(60_000, random_state=seed)
    x, y = frame["pred"].to_numpy(), frame["y"].to_numpy() * 1e4

    axes.hexbin(x, y, gridsize=45, cmap=theme["sequential"], mincnt=1, zorder=3)
    slope, intercept = np.polyfit(x, y, 1)
    line = np.linspace(x.min(), x.max(), 50)
    axes.plot(line, slope * line + intercept, color=theme["negative"], linewidth=2, zorder=4)
    axes.axhline(0.0, color=theme["axis"], linewidth=1.0, zorder=2)
    _style(
        axes,
        theme,
        f"Predicted vs realized (R² {metrics.r2(y, slope * x + intercept):.4f})",
        "prediction",
        "realized return (bps)",
    )


def panel_equity(axes, curves: dict[str, pd.Series], theme: dict) -> None:
    """Compounded equity at each swept cost level."""
    for index, (label, curve) in enumerate(curves.items()):
        colour = theme["ramp"][min(index, len(theme["ramp"]) - 1)]
        axes.plot(curve.index, curve.to_numpy(), color=colour, linewidth=2, zorder=3)
        if len(curve):
            axes.annotate(
                label,
                (curve.index[-1], curve.iloc[-1]),
                color=colour,
                fontsize=8,
                xytext=(4, 0),
                textcoords="offset points",
                va="center",
            )
    axes.axhline(1.0, color=theme["axis"], linewidth=1.0, zorder=2)
    _style(axes, theme, "Equity by cost per side", "", "growth of 1")


def panel_rolling_ic(axes, predictions: pd.DataFrame, theme: dict) -> None:
    """63-day rolling information coefficient."""
    frame = predictions.dropna(subset=["pred", "y"])
    series = metrics.ic_by_date(
        frame["y"].to_numpy(dtype="float64"),
        frame["pred"].to_numpy(dtype="float64"),
        frame["date"].to_numpy(),
    )
    rolling = series.rolling(63, min_periods=21).mean()

    axes.plot(rolling.index, rolling.to_numpy(), color=theme["series"], linewidth=2, zorder=3)
    axes.axhline(0.0, color=theme["axis"], linewidth=1.0, zorder=2)
    axes.axhline(
        float(series.mean()), color=theme["negative"], linewidth=1.5, linestyle="--", zorder=3
    )
    _style(axes, theme, f"Rolling 63-day IC (mean {series.mean():.4f})", "", "IC")


def panel_yearly(axes, yearly: pd.DataFrame, theme: dict) -> None:
    """Net return by calendar year — is this one good decade or a strategy?"""
    if yearly.empty:
        _style(axes, theme, "Net return by year")
        return
    colours = [
        theme["positive"] if v >= 0 else theme["negative"] for v in yearly["net_return"]
    ]
    axes.bar(yearly["year"], yearly["net_return"] * 100.0, color=colours, width=0.7, zorder=3)
    axes.axhline(0.0, color=theme["axis"], linewidth=1.0, zorder=2)
    _style(axes, theme, "Net return by year (10 bps per side)", "", "net return (%)")
    axes.set_xticks(yearly["year"].tolist())


def panel_randomization(axes, randomization: dict, theme: dict) -> None:
    """The real Sharpe against a null that has the same book and no information."""
    null = np.asarray(randomization.get("null_sharpes", []), dtype="float64")
    if null.size:
        axes.hist(null, bins=40, color=theme["muted"], zorder=3)
    actual = randomization.get("actual_sharpe", float("nan"))
    if np.isfinite(actual):
        axes.axvline(actual, color=theme["negative"], linewidth=2, zorder=4)
        axes.annotate(
            f"actual {actual:.2f} — {randomization.get('percentile', float('nan')):.1f}th pct",
            (actual, axes.get_ylim()[1] * 0.9),
            color=theme["negative"],
            fontsize=8,
            xytext=(6, 0),
            textcoords="offset points",
        )
    _style(
        axes,
        theme,
        f"Randomization null ({randomization.get('n_shuffles', 0)} shuffles)",
        "tranche Sharpe",
        "count",
    )


def _encode(figure, theme: dict) -> str:
    """Figure to a base64 PNG data URI on the theme's surface."""
    buffer = io.BytesIO()
    figure.savefig(
        buffer, format="png", dpi=140, facecolor=theme["surface"], bbox_inches="tight"
    )
    plt.close(figure)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _panel_pair(draw, *args) -> dict[str, str]:
    """Render one panel once per theme; CSS picks which the reader sees."""
    images = {}
    for name, theme in THEMES.items():
        figure, axes = plt.subplots(figsize=(7.2, 3.6), facecolor=theme["surface"])
        draw(axes, *args, theme)
        images[name] = _encode(figure, theme)
    return images


def write_tearsheet(
    path: Path, predictions: pd.DataFrame, curves: dict, yearly: pd.DataFrame, randomization: dict
) -> Path:
    """The four panels #56 names, on one light-mode figure."""
    theme = THEMES["light"]
    figure, axes = plt.subplots(2, 2, figsize=(14, 8), facecolor=theme["surface"])
    panel_decile(axes[0][0], predictions, theme)
    panel_equity(axes[0][1], curves, theme)
    panel_yearly(axes[1][0], yearly, theme)
    panel_randomization(axes[1][1], randomization, theme)
    figure.tight_layout()
    figure.savefig(path, dpi=140, facecolor=theme["surface"], bbox_inches="tight")
    plt.close(figure)
    return path


def render_panels(block: dict) -> list[dict]:
    """Every panel for one block (holdout or folds), each in both themes."""
    panels = [
        ("Prediction vs truth", _panel_pair(panel_decile, block["predictions"])),
        ("Predicted vs realized", _panel_pair(panel_scatter, block["predictions"])),
        ("Equity by cost", _panel_pair(panel_equity, block["curves"])),
        ("Rolling IC", _panel_pair(panel_rolling_ic, block["predictions"])),
        ("Yearly returns", _panel_pair(panel_yearly, block["yearly"])),
    ]
    if block.get("randomization"):
        panels.append(
            ("Randomization null", _panel_pair(panel_randomization, block["randomization"]))
        )
    return [{"title": title, "images": images} for title, images in panels]


BIASES = [
    "Survivorship: <code>company_meta.csv</code> is today's S&amp;P 500 applied back to "
    "2000, so every name in this test survived to be in the index.",
    "Point-in-time lag is approximated (#47) — fundamentals are assumed available on a "
    "fixed lag rather than on their real filing date.",
    "Labels overlap: consecutive 5-day forward returns share four of five days, which "
    "inflates any statistic computed on the daily series. Sharpe here is taken across "
    "five non-overlapping tranches for exactly this reason.",
    "Borrow cost is assumed zero and every S&amp;P 500 name is assumed shortable. Both "
    "are assumptions, not facts.",
    "Fills are assumed at <code>adj_close</code> with no slippage beyond the modelled "
    "cost. An execution model is Phase 7.",
]

STYLE = """
:root { color-scheme: light dark; }
body { margin: 0; font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
  background: #f9f9f7; color: #0b0b0b; }
main { max-width: 1000px; margin: 0 auto; padding: 40px 24px 80px; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 19px; margin: 44px 0 4px; }
p.lede, .meta { color: #52514e; margin: 0 0 8px; }
.meta { font-size: 13px; }
.warn { border-left: 3px solid #d03b3b; padding: 12px 16px; margin: 24px 0;
  background: #fcfcfb; border-radius: 0 8px 8px 0; }
.warn h3 { margin: 0 0 8px; font-size: 14px; text-transform: uppercase;
  letter-spacing: .06em; color: #52514e; }
.warn ul { margin: 0; padding-left: 18px; color: #52514e; font-size: 14px; }
.tiles { display: flex; flex-wrap: wrap; gap: 12px; margin: 20px 0; }
.tile { flex: 1 1 150px; background: #fcfcfb; border: 1px solid rgba(11,11,11,.10);
  border-radius: 10px; padding: 14px 16px; }
.tile .k { font-size: 12px; color: #898781; text-transform: uppercase;
  letter-spacing: .05em; }
.tile .v { font-size: 24px; margin-top: 4px; }
figure { margin: 20px 0 0; background: #fcfcfb; border: 1px solid rgba(11,11,11,.10);
  border-radius: 10px; padding: 12px; }
figure img { width: 100%; display: block; }
img.dark { display: none; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 14px;
  font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid #e1e0d9; }
th:first-child, td:first-child { text-align: left; }
th { color: #898781; font-weight: 600; font-size: 12px; text-transform: uppercase; }
code { background: rgba(11,11,11,.05); padding: 1px 5px; border-radius: 4px;
  font-size: .9em; }
@media (prefers-color-scheme: dark) {
  body { background: #0d0d0d; color: #fff; }
  p.lede, .meta, .warn ul, .warn h3, .tile .k { color: #c3c2b7; }
  .warn, .tile, figure { background: #1a1a19; border-color: rgba(255,255,255,.10); }
  th, td { border-bottom-color: #2c2c2a; }
  code { background: rgba(255,255,255,.08); }
  img.light { display: none; }
  img.dark { display: block; }
}
"""


def _tiles(items: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<div class="tile"><div class="k">{key}</div><div class="v">{value}</div></div>'
        for key, value in items
    )
    return f'<div class="tiles">{cells}</div>'


def _table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "<p class='meta'>No rows.</p>"
    head = "".join(f"<th>{column}</th>" for column in frame.columns)
    body = "".join(
        "<tr>"
        + "".join(
            f"<td>{value:.4f}</td>" if isinstance(value, float) else f"<td>{value}</td>"
            for value in row
        )
        + "</tr>"
        for row in frame.itertuples(index=False)
    )
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _figures(panels: list[dict]) -> str:
    return "".join(
        f'<figure><img class="light" alt="{panel["title"]}" src="{panel["images"]["light"]}">'
        f'<img class="dark" alt="{panel["title"]}" src="{panel["images"]["dark"]}"></figure>'
        for panel in panels
    )


def write_report(path: Path, version: str, blocks: list[dict], summary: dict) -> Path:
    """Assemble the standalone HTML report."""
    biases = "".join(f"<li>{line}</li>" for line in BIASES)
    sections = []
    for block in blocks:
        sweep = block["sweep"]
        tiles = [
            ("Dates", f"{block['n_dates']:,}"),
            ("Sharpe @0bps", f"{sweep['by_cost_bps']['0bps']['sharpe']:.2f}"),
            ("Sharpe @10bps", f"{sweep['by_cost_bps']['10bps']['sharpe']:.2f}"),
            ("Break-even", f"{sweep['break_even_bps']:.1f} bps"),
            ("Turnover / rebal.", f"{block['mean_turnover']:.0%}"),
        ]
        sections.append(
            f"<h2>{block['title']}</h2>"
            f"<p class='meta'>{block['subtitle']}</p>"
            f"{_tiles(tiles)}"
            f"{_figures(render_panels(block))}"
            f"<h3>Yearly</h3>{_table(block['yearly'])}"
        )

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AURUM backtest — {version}</title><style>{STYLE}</style></head>
<body><main>
<h1>Backtest — {version}</h1>
<p class="lede">Long the top decile, short the bottom, dollar-neutral, five overlapping
five-day tranches. Simulated only: decisions are emitted, never auto-traded.</p>
<div class="warn"><h3>Read this before any number below</h3><ul>{biases}</ul></div>
{"".join(sections)}
<h2>Reality checks</h2>
{_table(pd.DataFrame(summary["reality_checks"]))}
</main></body></html>
"""
    path.write_text(html)
    logger.info("Wrote %s", path)
    return path
