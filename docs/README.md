# AURUM documentation

Grouped by the thing being documented. Two rules make the split predictable:

- **`architecture/` is the target; everything else is what exists.** If a doc describes Kafka,
  Snowflake, Airflow or the MCP server as working, it is in `architecture/` and it is a design.
- **`warehouse/` documents the contract; `warehouse/rationale/` documents the reasoning.** Reach for
  the first to *use* the warehouse, the second to *change* it.

```
docs/
├── architecture/     the target system (spec v2.0) — mostly not built
├── ingestion/        src/ingestion, as built
├── warehouse/        src/transformation/aurum_dwh, as built
│   └── rationale/    why each model and seed is shaped the way it is
├── operations/       CI/CD and infrastructure
└── design-specs/     dated design records, kept for history
```

## Start here

| I want to… | Read |
|---|---|
| understand the whole target system | [`architecture/TECHNICAL_SPEC.md`](architecture/TECHNICAL_SPEC.md) |
| know what actually runs today | [README.md](../README.md) § Current state |
| query the warehouse | [`warehouse/data-dictionary.md`](warehouse/data-dictionary.md) |
| add a feature, or change a model | [`warehouse/dwh-medallion.md`](warehouse/dwh-medallion.md) |
| add a new data source | [`ingestion/datasource-framework.md`](ingestion/datasource-framework.md) |

## architecture/

| Doc | Content |
|---|---|
| [`TECHNICAL_SPEC.md`](architecture/TECHNICAL_SPEC.md) | Spec v2.0 — full component design, constraints, build phases, target repo layout. **The target, not the code.** |
| `AURUM_ArchitectureDiagram_drawio.png` | The system diagram from the spec |

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

## operations/

| Doc | Content |
|---|---|
| [`cicd.md`](operations/cicd.md) | GitHub Actions: lint, tests, SonarCloud gate, Terraform validation |
| [`infra-as-code.md`](operations/infra-as-code.md) | Terraform for Snowflake objects, Kafka topics, Postgres roles; why plan/apply stays local |

## design-specs/

Dated design records written before the work they describe. Kept for provenance — **read them as
history, not as current state.** Where they disagree with `ingestion/` or `warehouse/`, those win.

---

`repo_structure.md` at the repo root is an aspirational tree and does not match `src/`.
