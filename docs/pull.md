# How `dbt pull` works

> **Experimental, beyond the public docs.** The public dbt docs cover local
> compute for **unit tests** only ([`compute` config](https://docs.getdbt.com/reference/resource-configs/compute),
> [running unit tests locally](https://docs.getdbt.com/docs/build/unit-tests#run-unit-tests-locally)).
> `dbt pull` is not covered there. What this page describes is what the kit's
> authors observed on one Fusion build. It can change without notice, so check
> it on yours.

`dbt pull` copies the data a selection needs from your warehouse to your
machine, so that `dbt run --compute local` can build that selection on a
local DuckDB engine. It is the only step of the bug hunt that reads row data
from the warehouse (the fixture generator in
[paired-unit-tests.md](paired-unit-tests.md) is the other, and it is
optional).

Facts below were observed on `dbt 2.1.0.dev20261019`. Pull is moving fast, so
**preview before you pull** (`--preview`, below) and trust what it prints over
this page.

## What gets pulled: the frontier

`dbt pull --select S` does not pull `S`. It pulls the **frontier** of `S`:
every relation `S` reads that `S` does not build itself.

```text
selection S (models you will run locally)
   ├─ sources it reads                    → pulled
   ├─ upstream models outside S           → pulled (their built table/view, from the warehouse)
   ├─ seeds                               → loaded from the CSV in your project, not pulled
   ├─ incremental models in S: {{ this }} → pulled, unless --full-refresh
   └─ snapshots in S: existing state      → pulled
```

So `dbt pull --select stg_orders` pulls the source `raw_orders`. And
`dbt pull --select orders` (a mart) pulls the *built* `stg_orders` and
`order_items` tables. The staging models are not rebuilt locally because
they are not in the selection.

Modifiers:

| Flag | Effect |
|---|---|
| `--select / --exclude / --selector` | the selection whose frontier is pulled |
| `--exact` | pull the selected nodes' own built outputs instead of their inputs. Used to bridge over a model that fails locally (see [frontiers-and-selectors.md](frontiers-and-selectors.md#cut-points)) |
| `--full-refresh` | skip `{{ this }}` for incremental models; they rebuild from upstream only |
| `--sample SPEC` | limit what is pulled (next section) |
| `--preview` | print the SQL pull would run and stop. No data moves |

**Tests are part of the selection.** `--select stg_orders` also selects the
data tests attached to it. That includes a `relationships` test whose *other*
side is `stg_order_items`, and pull then fetches `stg_order_items` too. In the
dry run `--indirect-selection cautious` did **not** stop this for pull, even
though it does for `dbt ls`. Add `--exclude resource_type:test` to pull only
what the models need.

## Sampling

```bash
dbt pull --selector local_dag --sample 1000         # row cap per relation
dbt pull --selector local_dag --sample "30 days"    # time window
dbt pull --selector local_dag --sample '{"start": "2024-07-01", "end": "2024-07-08", "max_rows": 1000}'
```

* **Without `--sample`, pull copies whole tables.** On a real project that
  can mean hundreds of GB and a long warehouse bill. Always pass `--sample`.
* **A row cap is a plain `LIMIT`**: `SELECT * FROM <relation> LIMIT 1000`,
  one query per relation, no `ORDER BY`. You get whichever rows the warehouse
  returns first.
* **Samples are independent per relation.** A sampled `orders` and a
  sampled `customers` mostly do not share keys, so joins come out sparse and
  `relationships` tests fail locally. That is a property of the sample, not a
  local-compute bug (see [reading-results.md](reading-results.md)).
* **Time windows need an event time.** In the dry run, `"30 days"` and the
  JSON form pulled the *whole table* (no filter, and `max_rows` was not
  applied either) for relations with no `event_time` configured. Check with
  `--preview` before relying on a window.
* Pulled data replaces the previous pull for the same relation. Change the
  sample size, re-pull.

**Choosing a sample size.** 1000 rows is a good default: big enough to hit
most `CASE` branches, small enough to pull a large project in minutes. Raise
it only when you suspect sampling is hiding something, for example a model that
filters to a rare value and comes out empty. Lower it for very wide tables.

## Where the data lands

Each pulled relation becomes one Parquet file under the **target path**:

```text
target/data/<database>/<schema>/<table>/output.parquet
```

The local engine reads those files, so **`pull` and the local `run` must use
the same `--target-path`** (and the same `--target` / profile, see
[source-schemas.md](source-schemas.md)). A run with a different target path
fails with `Table '...' does not exist in schema ...`. Every relation fails
with the same message, which is how you recognize it.

Inspect a pulled relation with DuckDB:

```bash
uv run --with duckdb python -c "import duckdb; print(duckdb.sql(\"select * from 'target/data/<db>/<schema>/<table>/output.parquet' limit 5\"))"
```

`dbt clean` deletes `target/`, and with it every pulled file.

## Auth and warehouse load

* Pull uses the credentials of the profile target you pass (`--target`, or
  the profile's default). It runs only reads: metadata lookups plus one
  `SELECT ... LIMIT n` per relation. It does not write to the warehouse.
* Selecting from a **view** executes that view. Sampling a view over a
  billion-row table is not cheap even with `LIMIT`.
* **Browser SSO** (`authenticator: externalbrowser`) works, but tokens can
  expire in the middle of a long pull and the pull has to be restarted. For
  anything beyond a small project, use Snowflake key-pair auth.
* Read access is enough. The role must be able to `SELECT` every source in
  the frontier. A source you cannot read fails the pull for that relation.
  Keep the models that read it in `local_blockers`.

## A safe first pull

```bash
dbt parse
dbt pull --selector local_dag --exclude resource_type:test --sample 1000 --preview   # look first
dbt pull --selector local_dag --exclude resource_type:test --sample 1000
du -sh target/data
```
