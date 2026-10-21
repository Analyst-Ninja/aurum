"""Persist a run: data files plus a self-contained HTML report and a text twin."""

import html
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from src.paper import report
from src.paper.backtesting import compare
from src.paper.backtesting.inputs import EngineResult
from src.paper.config import PaperConfig

CAVEATS = [
    "Holdout IC is 0.007, so short-period P&L is mostly noise; judge on months and quarters.",
    "Alpaca's free IEX feed is a small share of market volume: prices are fine, depth is not.",
    "The universe is today's S&P 500 constituents (survivorship bias); no borrow cost or market impact is modelled.",
    "Engines differ in fill timing, rounding and short handling; see the agreement table before trusting any single curve.",
]
PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2"]


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _versions() -> dict:
    out = {}
    for name in ("pandas", "vectorbt", "backtrader", "alpaca-py"):
        try:
            from importlib.metadata import version

            out[name] = version(name)
        except Exception:
            out[name] = "not installed"
    return out


def _replay_reference(models_dir: str = "models") -> dict | None:
    found = sorted(Path(models_dir).glob("*/backtest/decisions_replay.json"))
    if not found:
        return None
    data = json.loads(found[-1].read_text())
    model = data.get("model", {})
    return {
        "version": data.get("version"),
        "window": f"{data.get('start_date')} to {data.get('end_date')}",
        "sharpe_by_cost": {k: round(v["sharpe"], 2) for k, v in model.get("by_cost_bps", {}).items()},
        "break_even_bps": model.get("break_even_bps"),
    }


def _svg_lines(curves: dict[str, pd.Series], height: int = 260, width: int = 860, drawdown: bool = False) -> str:
    """Overlaid polylines on a shared 0..1 x-axis of time. Equity rebased to 100."""
    series = {}
    for name, s in curves.items():
        s = s.dropna()
        if len(s) < 2:
            continue
        y = (s / s.cummax() - 1) * 100 if drawdown else s / s.iloc[0] * 100
        series[name] = y
    if not series:
        return "<p class='muted'>not enough points to chart</p>"
    t0 = min(s.index.min() for s in series.values())
    t1 = max(s.index.max() for s in series.values())
    lo = min(s.min() for s in series.values())
    hi = max(s.max() for s in series.values())
    pad = (hi - lo) * 0.05 or 1.0
    lo, hi = lo - pad, hi + pad
    left, bottom = 52, 22

    def x(ts):
        return left + (ts - t0) / (t1 - t0) * (width - left - 8)

    def y(v):
        return 8 + (hi - v) / (hi - lo) * (height - 8 - bottom)

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" xmlns="http://www.w3.org/2000/svg">']
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        parts.append(f'<line x1="{left}" x2="{width - 8}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>')
        parts.append(f'<text x="{left - 6}" y="{y(v) + 4:.1f}" text-anchor="end" class="axis">{v:.1f}</text>')
    for label, ts in ((t0, t0), (t1, t1)):
        anchor = "start" if ts == t0 else "end"
        parts.append(f'<text x="{x(ts):.1f}" y="{height - 6}" text-anchor="{anchor}" class="axis">{ts.strftime("%Y-%m-%d")}</text>')
    for i, (name, s) in enumerate(series.items()):
        pts = " ".join(f"{x(ts):.1f},{y(v):.1f}" for ts, v in s.items())
        parts.append(f'<polyline fill="none" stroke="{PALETTE[i % len(PALETTE)]}" stroke-width="1.6" points="{pts}"/>')
    parts.append("</svg>")
    legend = " ".join(f'<span class="key"><i style="background:{PALETTE[i % len(PALETTE)]}"></i>{html.escape(n)}</span>' for i, n in enumerate(series))
    return "".join(parts) + f'<div class="legend">{legend}</div>'


def _fmt_gap(value) -> str:
    return "" if pd.isna(value) else f"{value:+.2f}"


def _fmt_agrees(value) -> str:
    if pd.isna(value):
        return ""
    return "yes" if value else "NO"


_FORMATTERS = {
    "total_return": "{:+.2%}".format,
    "max_drawdown": "{:+.2%}".format,
    "pnl": "${:+,.0f}".format,
    "sharpe": "{:.2f}".format,
    "gap_vs_sim_pp": _fmt_gap,
    "max_daily_gap_pp": _fmt_gap,
    "agrees": _fmt_agrees,
}


def _fmt(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for column, formatter in _FORMATTERS.items():
        if column in out:
            out[column] = out[column].map(formatter)
    return out


def _period_html(equity: pd.Series, code: str, capital: float) -> str:
    t = report.period_table(equity, code, capital)
    shown = pd.DataFrame(
        {
            "period ending": t.index.strftime("%Y-%m-%d"),
            "start": t["start"].map("{:,.0f}".format),
            "end": t["end"].map("{:,.0f}".format),
            "P&L": t["pnl"].map("{:+,.0f}".format),
            "return": t["return"].map("{:+.2%}".format),
            "": t["partial"].map({True: "partial", False: ""}),
        }
    )
    return shown.to_html(index=False, border=0, classes="tbl")


CSS = """
:root{--bg:#fff;--fg:#111827;--mut:#6b7280;--line:#e5e7eb;--card:#f9fafb}
@media (prefers-color-scheme:dark){:root{--bg:#0f172a;--fg:#e5e7eb;--mut:#94a3b8;--line:#334155;--card:#1e293b}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px}
main{max-width:920px;margin:0 auto}h1{font-size:22px}h2{font-size:17px;margin-top:32px}
.muted,.axis{color:var(--mut);fill:var(--mut);font-size:12px}.grid{stroke:var(--line)}
.tbl{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
.tbl th,.tbl td{padding:4px 8px;text-align:right;border-bottom:1px solid var(--line)}
.tbl th:first-child,.tbl td:first-child{text-align:left}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;overflow-x:auto}
.legend{margin-top:6px}.key{margin-right:14px;font-size:13px}.key i{display:inline-block;width:12px;height:3px;margin-right:5px;vertical-align:middle}
.err{color:#dc2626}svg{width:100%;height:auto}
"""


def write_run(
    config: PaperConfig,
    results: list[EngineResult],
    benchmark: pd.Series,
    periods: list[str],
    start: str,
    end: str,
    out_root: Path | None = None,
) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = out_root or Path(config.out_dir) / "backtest"
    run_dir = root / f"{stamp}-{config.bar_interval}"
    run_dir.mkdir(parents=True, exist_ok=True)

    ok = [r for r in results if r.error is None and len(r.equity)]
    table = compare.agreement(results, config.capital)
    replay = _replay_reference()
    git_sha = _write_data_files(run_dir, config, results, ok, table, replay, periods, start, end)

    curves = {r.name: r.equity for r in ok}
    bench = benchmark.dropna()
    if len(bench) > 1:
        curves[config.benchmark] = bench / bench.iloc[0] * config.capital

    page = _html_report(run_dir, config, results, ok, table, replay, curves, periods, start, end, git_sha)
    (run_dir / "report.html").write_text(page)
    markdown = _markdown_report(run_dir, config, results, ok, table, bench, periods, start, end)
    (run_dir / "report.md").write_text(markdown)

    latest = root / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(run_dir.name)
    return run_dir


def _write_data_files(run_dir, config, results, ok, table, replay, periods, start, end) -> str:
    """Per-engine CSVs plus params.json and summary.json. Returns the git sha it recorded."""
    for r in ok:
        r.equity.to_csv(run_dir / f"equity_{r.name}.csv", header=["equity"])
        r.orders.to_csv(run_dir / f"orders_{r.name}.csv", index=False)

    tables = {r.name: {c: report.period_table(r.equity, c, config.capital).reset_index().astype(str).to_dict("records") for c in periods} for r in ok}
    git_sha = _git_sha()
    params = {
        "run_id": run_dir.name,
        "window": [start, end],
        "git_sha": git_sha,
        "libraries": _versions(),
        "config": config.__dict__,
        "engines": {r.name: ("ok" if r.error is None else f"skipped: {r.error}") for r in results},
        "notes": {r.name: r.note for r in results if r.note},
    }
    (run_dir / "params.json").write_text(json.dumps(params, indent=2, default=str))
    (run_dir / "summary.json").write_text(
        json.dumps({"agreement": table.to_dict("records"), "periods": tables, "replay_reference": replay}, indent=2, default=str)
    )
    return git_sha


def _card(inner: str) -> str:
    return f"<div class='card'>{inner}</div>"


def _html_report(run_dir, config, results, ok, table, replay, curves, periods, start, end, git_sha) -> str:
    mode = "long-only" if config.long_only else "long/short"
    h = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>AURUM backtest {html.escape(run_dir.name)}</title><style>{CSS}</style></head><body><main>"]
    h.extend(
        [
            f"<h1>AURUM backtest cross-check</h1><p class='muted'>{start} to {end} &middot; {config.bar_interval} bars &middot; capital ${config.capital:,.0f} &middot; cost {config.cost_bps:g} bps &middot; horizon {config.horizon}d &middot; {mode} &middot; git {git_sha}</p>",
            "<h2>Agreement</h2>" + _card(_fmt(table).to_html(index=False, border=0, classes="tbl")),
            f"<p class='muted'>Gap is total return minus the in-house sim's, in percentage points; engines beyond &plusmn;{compare.TOLERANCE_PP} pp are marked NO and are a difference to explain, not a result to average.</p>",
            "<h2>Equity (rebased to 100)</h2>" + _card(_svg_lines(curves)),
            "<h2>Drawdown (%)</h2>" + _card(_svg_lines({r.name: r.equity for r in ok}, drawdown=True)),
        ]
    )
    for r in ok:
        h.append(f"<h2>{html.escape(r.name)} &mdash; P&amp;L by period</h2>")
        if r.note:
            h.append(f"<p class='muted'>{html.escape(r.note)}</p>")
        h.extend(f"<h3>{report.NAME[code]}</h3>" + _card(_period_html(r.equity, code, config.capital)) for code in periods)
    skipped = [r for r in results if r.error]
    if skipped:
        h.append("<h2>Skipped engines</h2>")
        h.extend(f"<p class='err'><b>{html.escape(r.name)}</b>: <code>{html.escape(r.error)}</code></p>" for r in skipped)
    if replay:
        h.append("<h2>Reference: close-to-close replay</h2><p class='muted'>" + html.escape(json.dumps(replay)) + "</p>")
    h.append("<h2>Caveats</h2><ul>" + "".join(f"<li>{html.escape(c)}</li>" for c in CAVEATS) + "</ul>")
    h.append("<p class='muted'>Files in this folder: params.json, summary.json, equity_*.csv, orders_*.csv, report.md.</p></main></body></html>")
    return "".join(h)


def _markdown_report(run_dir, config, results, ok, table, bench, periods, start, end) -> str:
    md = [f"# AURUM backtest cross-check ({run_dir.name})", f"{start} to {end}, {config.bar_interval}, capital {config.capital:,.0f}, cost {config.cost_bps:g} bps", "", _fmt(table).to_string(index=False)]
    for r in ok:
        md += ["", f"## {r.name}", report.render(r.equity, periods, config.capital, bench if len(bench) > 1 else None)]
    for r in results:
        if r.error:
            md += ["", f"SKIPPED {r.name}: {r.error}"]
    md += ["", "Caveats:"] + [f"- {c}" for c in CAVEATS]
    return "\n".join(md)
