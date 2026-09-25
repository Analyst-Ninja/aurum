# AURUM — Stock Research Platform (built solo)

I wanted to build the entire stack an investment data team builds. Alone. So I did.

**Collects** — daily prices and SEC filings for 503 companies back to 2000. New data source = a config file, not code.

**Cleans** — a layered warehouse turns raw data into model-ready features. 237 automated tests run on every build, so bad data never reaches the model.

**Serves ML** — the warehouse feeds a LightGBM model that ranks stocks by expected 5-day performance. The model is early-stage; the part I built properly is everything around it — reproducible training runs, versioned model artifacts, and a train step that can never run on a failed data build.

**Runs itself** — nightly after market close, retrains monthly. One Docker image on AWS, infrastructure written as code, and a merge to main ships straight to production.

2.9M feature rows. 237 tests. Zero manual steps.

**Stack:** Python · dbt · PostgreSQL · LightGBM · Docker · Terraform · AWS · GitHub Actions

---

## Video caption (40s architecture walkthrough)

Video: `brag-output-2026-09-19-081437/aurum_showcase.mp4`

**40 seconds through AURUM — a stock-research data platform I built solo.**

One continuous diagram, one camera move. It follows the data: two sources in (market prices from Yahoo Finance, financial filings from SEC EDGAR), through one ingestion contract into Postgres, up through a dbt warehouse — bronze to silver to gold, 237 tests passing — into 2.5M rows × 193 features feeding a LightGBM model.

Then it pulls back to show the part I'm proudest of: three scheduled workflows that run the whole thing unattended, and 46 Terraform resources that declare every box you just watched light up. Merge to main, and it deploys itself.

Every number on screen is real, pulled straight from the repo.

Ingest. Transform. Schedule. Declare.
github.com/Analyst-Ninja/aurum

### Short version

Built a stock-research data platform solo — ingestion, dbt warehouse, ML, and the AWS infrastructure that runs it on a schedule. Here's the whole system in 40 seconds, one continuous shot. Every number on screen is real.

Ingest. Transform. Schedule. Declare.
github.com/Analyst-Ninja/aurum
