# AURUM — EDGAR Incremental Ingestion Strategy

> How the EDGAR producer fetches only NEW data — never re-pulling full history each run.

---

## The Core Problem

AURUM's main EDGAR source — `companyfacts/CIK{cik}.json` — **has no filter parameters**. Every call returns the full history of every metric the company ever reported (20+ years). Naively refreshing 500 S&P companies = 500 full pulls per run.

**The fix:** EDGAR publishes a daily index of every filing submitted that day. Use it to find *which companies filed something new*, then pull companyfacts only for those CIKs.

Typical daily volume for the S&P 500: **0–5 filers** on a normal day, 20–50/day at earnings-season peak — versus 500 full pulls.

---

## The Daily Index

```
https://www.sec.gov/Archives/edgar/daily-index/{YEAR}/QTR{N}/
```

| Quarter | Months |
|---------|--------|
| QTR1 | Jan–Mar |
| QTR2 | Apr–Jun |
| QTR3 | Jul–Sep |
| QTR4 | Oct–Dec |

Directory listing as JSON: `.../QTR{N}/index.json` (the **only** JSON here — filing index files themselves are `.idx` text).

Four `.idx` files per business day, published **~10 PM ET**, same content sorted differently:

| File | Sorted by | Note |
|------|-----------|------|
| `master.YYYYMMDD.idx` | CIK | **Pipe-delimited — use this one** |
| `company.YYYYMMDD.idx` | Company name | Fixed-width — avoid |
| `form.YYYYMMDD.idx` | Form type | |
| `crawler.YYYYMMDD.idx` | Full path | |

⚠️ Filename has a **dot before the date**: `master.20260630.idx`.

### `master.idx` format

Pipe-delimited; ~8 header lines before data:

```
CIK|Company Name|Form Type|Date Filed|Filename
--------------------------------------------------------------------------------
320193|APPLE INC|10-Q|20260630|edgar/data/320193/0000320193-26-000100-index.htm
```

Full filing URL = `https://www.sec.gov/` + `Filename`. CIK is **not** zero-padded here — pad to 10 digits before joining to companyfacts URLs.

---

## Producer Algorithm

```
1. Run daily 2 AM UTC (after SEC's ~10 PM ET publish)
2. Read watermark (last processed date)
3. For each date from watermark+1 .. safe-latest:
     fetch master.{date}.idx        (404 → weekend/holiday → skip)
     parse; filter form_type IN ('10-Q','10-K','8-K') AND cik IN S&P500
4. For each unique filer CIK:
     pull companyfacts/CIK{cik}.json
     keep rows where filed >= watermark
     publish one Kafka message per fact → edgar.filings
5. Update watermark only after successful publish
```

### Reference implementation

```python
import requests
import pandas as pd
from io import StringIO
from datetime import date, timedelta

# Required — SEC returns 403 without a proper User-Agent
HEADERS = {
    "User-Agent": "AURUM-Project rohit.kumar011997@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}

SP500_CIKS: set[str] = set()  # load from landing.company_meta


def get_quarter(d: date) -> int:
    return (d.month - 1) // 3 + 1


def get_daily_index(target_date: date, form_types=("10-Q", "10-K", "8-K")) -> pd.DataFrame:
    """Fetch master.idx for one date. Empty DataFrame on weekend/holiday (404)."""
    url = (
        f"https://www.sec.gov/Archives/edgar/daily-index/"
        f"{target_date.year}/QTR{get_quarter(target_date)}/"
        f"master.{target_date.strftime('%Y%m%d')}.idx"
    )
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code == 404:
        return pd.DataFrame()
    resp.raise_for_status()

    data_lines = [l for l in resp.text.strip().split("\n")
                  if "|" in l and not l.startswith("CIK")]
    if not data_lines:
        return pd.DataFrame()

    df = pd.read_csv(
        StringIO("\n".join(data_lines)), sep="|", header=None,
        names=["cik", "company_name", "form_type", "date_filed", "filename"],
        dtype=str,
    )
    df["cik"] = df["cik"].str.strip().str.zfill(10)
    df["date_filed"] = pd.to_datetime(df["date_filed"].str.strip(), format="%Y%m%d").dt.date
    df["form_type"] = df["form_type"].str.strip()
    df["full_url"] = "https://www.sec.gov/" + df["filename"].str.strip()
    return df[df["form_type"].isin(form_types)].reset_index(drop=True)


def get_new_sp500_filers(since_date: date, until_date: date | None = None) -> pd.DataFrame:
    """All S&P 500 companies that filed 10-Q/10-K/8-K in the window — the incremental trigger."""
    until_date = until_date or (date.today() - timedelta(1))
    frames = []
    current = since_date
    while current <= until_date:
        df = get_daily_index(current)
        if not df.empty and SP500_CIKS:
            df = df[df["cik"].isin(SP500_CIKS)]
        if not df.empty:
            frames.append(df)
        current += timedelta(1)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
```

---

## Watermark Management

v2 keeps watermarks next to the data — a `meta.watermarks` Postgres table (Airflow Variable acceptable for local dev):

```sql
CREATE TABLE meta.watermarks (
    pipeline    text PRIMARY KEY,   -- 'edgar_daily_index'
    last_run    date NOT NULL,
    updated_at  timestamptz DEFAULT now()
);
```

```python
# in the producer / DAG task
last_run = get_watermark("edgar_daily_index")          # e.g. 2026-07-09; default 2022-01-01 on first run (≈4y backfill)
new_filers = get_new_sp500_filers(
    since_date=last_run + timedelta(1),
    until_date=date.today() - timedelta(2),            # see timing note below
)
publish_facts_for(new_filers["cik"].unique())
save_watermark("edgar_daily_index", date.today() - timedelta(2))  # only after successful publish
```

**Timing note:** index files land ~10 PM ET. A 2 AM UTC run (= 9 PM ET previous day) may race the publish — use `today − 2` as the safe upper bound; `today − 1` only if running after 5 AM UTC.

---

## Schedule

| Job | Schedule | Work |
|-----|----------|------|
| `edgar_producer` daily run | `0 2 * * 1-5` (2 AM UTC Mon–Fri) | Parse new master.idx → publish facts for new S&P filers |
| Backfill | Manual | `get_new_sp500_filers(since, until)` over a longer window |

Daily (not weekly) matters: 8-K earnings releases arrive daily in earnings season; a weekly pull delays fundamentals up to 7 days.

---

## Gotchas

| Issue | Detail |
|-------|--------|
| Daily files not JSON | Only the directory `index.json` is JSON; filing indexes are `.idx` text |
| Dot in filename | `master.20260630.idx` — dot before date required |
| Weekends/holidays | No file → 404 → treat as empty day |
| **SEC blocks datacenter IPs** | AWS/GCP/Azure get 403. Run the producer from a local/residential machine |
| User-Agent mandatory | `Name email@example.com` — 403 without it; use a real email |
| Publish timing | ~10 PM ET; see timing note above |
| Amended filings | `10-Q/A`, `10-K/A` appear in the index — process them; they correct prior values (dedup on latest `filed_date` downstream) |
| Rate limit 10 req/s | `time.sleep(0.1)` between companyfacts calls |

*Adapted from Obsidian vault `AURUM-Incremental-Data-Strategy.md` (2026-07-02) for the v2 Kafka-producer architecture — 2026-07-12.*
