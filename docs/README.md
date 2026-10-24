# AURUM documentation

Grouped by the thing being documented. Two rules make the split predictable:

- **`architecture/` is the target; everything else is what exists.** If a doc describes Kafka,
  Snowflake, Airflow or the MCP server as working, it is in `architecture/` and it is a design.
- **`warehouse/` documents the contract; `warehouse/rationale/` documents the reasoning.** Reach for
  the first to *use* the warehouse, the second to *change* it.

`design-specs/` is the one other exception: dated records kept for history.

```
docs/
├── architecture/     the target system (spec v2.0) + diagrams — mostly not built
├── ingestion/        src/ingestion, as built
├── warehouse/        src/transformation/aurum_dwh, as built
│   └── rationale/    why each model and seed is shaped the way it is
├── modeling/         src/modeling, as built (Phase 6)
├── inference/        src/inference, as built
├── mcp/              src/mcp, as built
├── paper/            src/paper — Alpaca paper trading and backtest engines, as built
├── operations/       local setup, CI/CD, containers, Terraform layout
├── infra/            the AWS deployment — built and running on a schedule
├── security/         security review of the deployed stack
├── assets/           README banner / GitHub social-preview thumbnail
└── design-specs/     dated design records, kept for history
```

## Start here

| I want to… | Read |
|---|---|
| understand the whole target system | [`architecture/TECHNICAL_SPEC.md`](architecture/TECHNICAL_SPEC.md) |
| know what actually runs today | [README.md](../README.md) |
| set up from an empty machine | [`operations/local-setup.md`](operations/local-setup.md) |
| query the warehouse | [`warehouse/data-dictionary.md`](warehouse/data-dictionary.md) |
| add a feature, or change a model | [`warehouse/dwh-medallion.md`](warehouse/dwh-medallion.md) |
| add a new data source | [`ingestion/datasource-framework.md`](ingestion/datasource-framework.md) |
| train a model, or understand what we predict | [`modeling/modeling-design.md`](modeling/modeling-design.md) |
| change the feature set | [`modeling/feature-selection-shap.md`](modeling/feature-selection-shap.md) |
| backtest a signal | [`modeling/backtesting.md`](modeling/backtesting.md) |
| get BUY / SELL / HOLD from the latest model, in the CLI or MCP | [`inference/inference.md`](inference/inference.md) |
| paper-trade the decisions, or cross-check a backtest | [`paper/paper-trading.md`](paper/paper-trading.md) |
| deploy to AWS, or change a schedule | [`infra/aws-deployment-plan.md`](infra/aws-deployment-plan.md) |
| change Terraform or the deploy pipeline | [`operations/infra-as-code.md`](operations/infra-as-code.md), [`operations/cicd.md`](operations/cicd.md) |
| query the warehouse from Claude, or run the MCP server | [`mcp/mcp-server-design.md`](mcp/mcp-server-design.md) |
| check known security risks | [`security/risks.md`](security/risks.md) |

## architecture/

| Doc | Content |
|---|---|
| [`TECHNICAL_SPEC.md`](architecture/TECHNICAL_SPEC.md) | Spec v2.0 — full component design, constraints, build phases, target repo layout. **The target, not the code.** |
| `AURUM_ArchitectureDiagram_drawio.png` | The system diagram from the spec (target) |
| `AURUM_Architecture_AsBuilt.drawio` (+ `.xml`) | The system as built: AWS, Postgres, dbt, modeling, MCP |
| `AURUM_Architecture_Plain.drawio`, `AURUM_Architecture_Simple.drawio` | Plain-language and simplified variants |
| `aurum-architecture.html` (+ `.archify.json`, `.visual-check.*`) | Interactive architecture diagram and its render checks |
| `aurum-class-diagram.html`, `aurum-module-map.html` | Class and module maps. Check which parts are target before relying on them |

## ingestion/ — as built

| Doc | Content |
|---|---|
| [`datasource-framework.md`](ingestion/datasource-framework.md) | Registry/factory/feed flow, config reference, how to add a source, known rough edges |
| [`edgar-incremental-ingestion.md`](ingestion/edgar-incremental-ingestion.md) | Daily-index + watermark strategy for pulling only new filings |

## warehouse/ — as built

| Doc | Content |
|---|---|
| [`dwh-medallion.md`](warehouse/dwh-medallion.md) | The medallion as built: layer map, model DAG, feature catalogue with formulas, the point-in-time lag decision, the incremental-lookback rule, how to add a feature, the SHAP loop, known approximations |
| [`data-dictionary.md`](warehouse/data-dictionary.md) | Every field in every layer — landing, bronze, silver, gold — with types, grains and gotchas |

### warehouse/rationale/ — why, not what

| Doc | Content |
|---|---|
| [`bronze-models-rationale.md`](warehouse/rationale/bronze-models-rationale.md) | `br_*`: dedup, typing, the incremental mirror |
| [`silver-staging-models-rationale.md`](warehouse/rationale/silver-staging-models-rationale.md) | `stg_*`: price adjustment, the statement union, the company dimension |
| [`silver-intermediate-models-rationale.md`](warehouse/rationale/silver-intermediate-models-rationale.md) | `int_*`: feature engineering, TTM, the point-in-time join, and the long form of the lookback/warm-up rules |
| [`gold-models-rationale.md`](warehouse/rationale/gold-models-rationale.md) | `mart_*`: the cross-sectional transform, the target/leakage contract, walk-forward folds |
| [`concept-map-rationale.md`](warehouse/rationale/concept-map-rationale.md) | Why each XBRL concept is mapped, dropped or ranked in `seeds/concept_map.csv`, with measured coverage |
| [`selected-features-seed.md`](warehouse/rationale/selected-features-seed.md) | The SHAP selection loop, and why `seeds/selected_features.csv` must exist before anything is trained |

## modeling/ — as built

Phase 6 ([#50](https://github.com/Analyst-Ninja/aurum/issues/50)), built under `src/modeling/` and
run monthly on AWS.

| Doc | Content |
|---|---|
| [`pipeline-runbook.md`](modeling/pipeline-runbook.md) | The 11-step run, command by command |
| [`modeling-design.md`](modeling/modeling-design.md) | **Start here.** What we predict and why (`fwd_ret_5d_excess`), why LightGBM, the feature space, the metrics, the known limitations |
| [`preprocessing-contract.md`](modeling/preprocessing-contract.md) | Row filters, the three column deny-lists, NaN policy, and the train/inference symmetry contract |
| [`training-and-retraining.md`](modeling/training-and-retraining.md) | Purged walk-forward folds, hyperparameters, the flat-file registry, retraining triggers, promotion, the container |
| [`feature-selection-shap.md`](modeling/feature-selection-shap.md) | The SHAP loop, producer side — what writes `seeds/selected_features.csv` |
| [`backtesting.md`](modeling/backtesting.md) | Portfolio construction, cost sweep, factor attribution, the three reality checks |

## inference/ — as built

| Doc | Content |
|---|---|
| [`inference.md`](inference/inference.md) | `decide` (top decile BUY, bottom SELL, rest HOLD, stale-date guard), `replay` against a momentum baseline, and the MCP tools that expose them |

## mcp/ — as built

| Doc | Content |
|---|---|
| [`mcp-server-design.md`](mcp/mcp-server-design.md) | The read-only MCP server over Postgres `gold`: catalog from dbt `target/*.json` with an `information_schema` fallback, six tools (`list_tables`, `describe_table`, `run_query`, `refresh_catalog`, `get_decisions`, `score_symbols`), stdio and streamable-HTTP transports, the read-only role, and the EC2 host. Supersedes `architecture/TECHNICAL_SPEC.md` §3.9 for Postgres |

## paper/ — as built

| Doc | Content |
|---|---|
| [`paper-trading.md`](paper/paper-trading.md) | Alpaca paper broker, simulated broker, ledger, the four backtest engines and `crosscheck`. Paper money only |

## operations/

| Doc | Content |
|---|---|
| [`local-setup.md`](operations/local-setup.md) | Empty machine to backtest, all in Docker: Postgres, ingest, dbt, train, SHAP, backtests, decisions |
| [`cicd.md`](operations/cicd.md) | The four GitHub Actions workflows: CI, Terraform validate and gated apply, bootstrap, destroy |
| [`training-container.md`](operations/training-container.md) | The one image and compose service: four workloads, host-Postgres networking, bind mounts, non-root |
| [`infra-as-code.md`](operations/infra-as-code.md) | The Terraform layout as built: files, resources, bootstrap root, `scripts/deploy.sh`, destroy flow |

## infra/

| Doc | Content |
|---|---|
| [`aws-deployment-plan.md`](infra/aws-deployment-plan.md) | The AWS deployment **as built**: RDS Postgres, one image on ECR run as four ECS Fargate task definitions, three Step Functions state machines on EventBridge Scheduler crons, Terraform. Carries the schedule DAG, why the ten-state modelling chain collapsed into one task, the abandoned $20/month ceiling, the code constraints that shape it, and a troubleshooting table. Epic [#71](https://github.com/Analyst-Ninja/aurum/issues/71) |

## security/

| Doc | Content |
|---|---|
| [`risks.md`](security/risks.md) | Security review of the deployed stack and its findings |

## assets/

`aurum-thumbnail.svg` is the README banner; `aurum-thumbnail.png` is the same image for upload to
GitHub Settings → Social preview. See [`assets/README.md`](assets/README.md).

## design-specs/

Dated design records written before the work they describe. Kept for provenance — **read them as
history, not as current state.** Where they disagree with `ingestion/` or `warehouse/`, those win.

---

`repo_structure.md` at the repo root is an aspirational tree and does not match `src/`.
