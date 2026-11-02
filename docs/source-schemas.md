# Source schemas: names and types, warehouse → local

> **Experimental, beyond the public docs.** The public dbt docs cover local
> compute for **unit tests** only ([`compute` config](https://docs.getdbt.com/reference/resource-configs/compute),
> [running unit tests locally](https://docs.getdbt.com/docs/build/unit-tests#run-unit-tests-locally)).
> Pulled data and whole-project local runs are not covered there. What this page describes is what the kit's
> authors observed on one Fusion build. It can change without notice, so check
> it on yours.

Local compute keeps your project's relation names. A model that reads
`{{ source('ecom', 'raw_orders') }}` on the warehouse
reads the same three-part name locally. What changes is where the bytes come
from and which engine types hold them.

## Names

1. `source()` / `ref()` render to `database.schema.identifier` with the
   **target you run with**. A `schema: "{{ target.schema }}"` source, or a
   custom `generate_schema_name`, renders differently per target.
2. `dbt pull` reads that relation from the warehouse and writes it to
   `target/data/<database>/<schema>/<identifier>/output.parquet`
   ([pull.md](pull.md#where-the-data-lands)).
3. `dbt run --compute local` renders the names again and looks for those
   files.

So **pull and run must agree on target, vars, and target path**. If you
pull with `--target prod` and run with `--target dev`, every staging model
fails with `Table '...' does not exist in schema ...`. That is a setup
mismatch, not a bug.

Things that do differ locally and are worth knowing:

* **Case.** Snowflake stores unquoted names upper-case. Locally the same
  upper-case names are used. A model that quotes a lower-case identifier
  (`"my_col"`) on Snowflake refers to a different column than `my_col`, both
  remotely and locally. If it works remotely and not locally, that is a
  candidate.
* **Reserved names.** DuckDB reserves some schema names that warehouses
  allow, most notably `information_schema`. A source living in a schema of
  that name cannot be pulled. Put the models that read it in
  `local_blockers` (or cut them, see
  [frontiers-and-selectors.md](frontiers-and-selectors.md)) and record it
  once.
* **Introspection.** Macros that ask the warehouse about relations at
  compile time (`dbt_utils.get_column_values`, `run_query`,
  `adapter.get_columns_in_relation`, `get_relations_by_pattern`) answer from
  the *local* catalog when running locally. A model whose columns are
  generated from such a query can compile with fewer columns locally
  (`No column ... found`). One entry covers every model hit this way, so
  check the ledger before adding it.

## Types

Pulled data is converted to DuckDB types. Observed on Snowflake:

| Snowflake | Local (Parquet/DuckDB) | Watch for |
|---|---|---|
| `TEXT`, `VARCHAR(n)` | `VARCHAR` | length limits are not enforced locally |
| `NUMBER(38,0)` | `BIGINT` | values beyond ±9.2e18 do not fit |
| `NUMBER(p,s)`, s > 0 | `DECIMAL(p,s)` | arithmetic result scale can differ |
| `FLOAT` | `DOUBLE` | last-digit differences after aggregation |
| `TIMESTAMP_NTZ` | `TIMESTAMP` | |
| `TIMESTAMP_TZ` / `_LTZ` | `TIMESTAMP WITH TIME ZONE` | session time zone affects display and `::date` |
| `VARIANT`, `OBJECT`, `ARRAY` | `JSON` or nested types | path access, `parse_json` strictness, `::type` casts |
| `BOOLEAN`, `DATE`, `TIME` | same | |

To see the local schema of a pulled relation:

```bash
uv run --with duckdb python -c "import duckdb; print(duckdb.sql(\"describe select * from 'target/data/<db>/<schema>/<table>/output.parquet'\"))"
```

and the warehouse's exact schema:

```bash
dbt show --compute remote --limit 1 --inline \
  "{% set cols = adapter.get_columns_in_relation(source('ecom', 'raw_orders')) %}select '{% for c in cols %}{{ c.name }} {{ c.data_type }}; {% endfor %}' as cols"
```

A type difference by itself is not a bug. It is a bug when it changes a
model's **values**, and paired unit tests are how you show that
([paired-unit-tests.md](paired-unit-tests.md)).

> `catalog.json` (`dbt compile --write-catalog`) also lists columns, but on
> Snowflake it reports `NUMBER` without precision and scale. Do not use it to
> type fixtures: `cast('7.42' as NUMBER)` is `7`.
