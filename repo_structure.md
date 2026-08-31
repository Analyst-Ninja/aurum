```md
aurum/
├── src/
│   ├── common/
│   │   └── interfaces.py              # Readable, Writable protocols + FetchRequest
│   │
│   ├── ingestion/
│   │   ├── configs/                   # connection configs (API keys, hosts, etc.)
│   │   │
│   │   ├── datasources/
│   │   │   ├── api/                   # read-focused, sometimes cache-write
│   │   │   │   ├── edgar/
│   │   │   │   │   ├── base.py
│   │   │   │   │   └── edgar_client.py
│   │   │   │   ├── yahoo/
│   │   │   │   │   ├── base.py
│   │   │   │   │   └── yahoo_client.py
│   │   │   │   └── news/
│   │   │   │       ├── base.py
│   │   │   │       └── news_client.py
│   │   │   │
│   │   │   └── storage/               # write-focused, sometimes read (swappable)
│   │   │       ├── db/
│   │   │       │   ├── base.py
│   │   │       │   ├── postgres_db.py
│   │   │       │   └── config.py
│   │   │       ├── warehouse/
│   │   │       │   ├── base.py
│   │   │       │   ├── snowflake_warehouse.py
│   │   │       │   └── config.py
│   │   │       └── s3/
│   │   │           ├── base.py
│   │   │           ├── s3_storage.py
│   │   │           └── config.py
│   │   │
│   │   ├── feeds/                     # glue: input datasource → processing → output datasource
│   │   │   ├── market_feed.py
│   │   │   ├── edgar_feed.py
│   │   │   └── news_feed.py
│   │   │
│   │   ├── schemas/
│   │   │   ├── configs/               # schema-validation configs
│   │   │   └── data_schemas/          # pydantic BaseRecord definitions
│   │   │
│   │   └── utils/
│   │       └── config_reader.py
│   │
│   ├── transformation/
│   │   └── dbt/                       # dbt-snowflake project
│   │       ├── configs/
│   │       ├── models/
│   │       │   ├── silver/
│   │       │   └── gold/
│   │       └── tests/
│   │
│   ├── modeling/
│   │   ├── feature_selection/         # SHAP-driven feature pruning
│   │   └── training/                  # model training + registry
│   │
│   ├── inference/                     # realtime inference module
│   │
│   └── mcp_server/                    # FastMCP NL→SQL server
│
├── airflow/
│   └── dags/                          # load + quality + retrain DAGs
│
├── infra/
│   ├── docker-compose.yml             # kafka, postgres, airflow
│   └── terraform/                     # Snowflake objects, Kafka topics, Postgres roles
│
├── nbs/                                # exploration notebooks
│
├── docs/
│   ├── TECHNICAL_SPEC.md
│   ├── data-dictionary.md
│   ├── datasource-framework.md
│   ├── edgar-incremental-ingestion.md
│   ├── infra-as-code.md
│   └── cicd.md
│
├── tests/                              # mirrors src/ structure
│
├── .github/
│   └── workflows/                      # CI: ruff, pytest, SonarQube, Terraform validation
│
├── .sonarlint/
├── .gitignore
├── .python-version
├── CLAUDE.md
├── README.md
├── conftest.py
├── main.py
├── pyproject.toml
├── sonar-project.properties
└── uv.lock
```