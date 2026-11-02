# Paired unit tests: proving the values match

A model that builds locally can still compute different values. A **paired
unit test** runs one model on the warehouse and locally over the *same fixed
input*. If both sides pass the same expectation, the values match for that
input. `scripts/lc_fixtures.py` generates these tests for your models.

This is the documented part of local compute: dbt's experimental **local
unit tests** ([`compute` config](https://docs.getdbt.com/reference/resource-configs/compute), [run unit tests
locally](https://docs.getdbt.com/docs/build/unit-tests#run-unit-tests-locally)). Opt in first:

```bash
export DBT_ENGINE_EXPERIMENTAL_LOCAL_UNIT_TESTS=true
```

The script also sets that variable for its own dbt calls.

## How a generated test is built

For each model `M` with inputs `I1..In`:

1. **Schema.** For each input it asks the warehouse for the relation's exact
   column list and types (`adapter.get_columns_in_relation`, run through
   `dbt show` on the warehouse), for example `SUBTOTAL NUMBER(16,2)`.
2. **`given`.** It samples `--sample-rows` rows (default 25) from each input
   on the warehouse. It then *projects* them onto that column list. Keys the
   relation does not have are dropped. Columns missing from the sample become
   typed NULLs. Every value is written as an explicit
   `cast('…' as <exact type>)`. The fixture therefore has exactly the real
   relation's shape, neither a superset nor a guess, and needs no warehouse
   metadata to run.
3. **`expect`.** It compiles the test and runs the model over that `given`
   **on the warehouse**, twice. If the two runs disagree, the model is set
   aside as nondeterministic. Otherwise the warehouse's rows become `expect`.
   The oracle is computed from the fixture itself, never copied from
   production tables. So anonymizing a fixture value, or nulling one the kit
   cannot represent, changes what is tested but never makes the oracle
   wrong.
4. **Overrides.** Incremental models get `is_incremental: false` (the
   full-refresh branch, explicitly). Common introspection macros the model
   uses (`run_query`, `dbt_utils.get_column_values`, …) are overridden to
   empty results, so compile is deterministic and source-free on both sides.
   Add project-specific ones with `--macro-override 'my_pkg.my_macro=[]'`.

5. **Two copies.** Each file holds the same test twice:
   `lc_kit__<model>__f2__remote` with `config: compute: remote`, and
   `lc_kit__<model>__f2__local` with `config: compute: local`. A per-test
   `compute` overrides any `+compute` in your `dbt_project.yml`, so the
   remote copy really runs on the warehouse.

Then `verify` runs both copies of every test in one `dbt test` and
classifies each pair (see
[reading-results.md](reading-results.md#paired-unit-tests)).

From the public docs, and worth knowing when you read results:

* the local copy needs your SQL to be **translatable to DuckDB**. A
  translation failure is a test failure, never a silent fallback to the
  warehouse. That is exactly what the kit is looking for;
* local tests fetch the **schemas of the model's direct upstream models from
  the warehouse**, so those models must already exist there. If they don't,
  both copies fail and the case is `broken`, not a finding;
* `compute: local` needs static analysis. A model with
  `static_analysis: off` cannot run locally (`dbt1401`), so the generator
  sets it aside.

## Set aside, not guessed

A model gets no test, and is listed with a reason, when the kit cannot
produce a trustworthy oracle:

| Reason | Why |
|---|---|
| nondeterministic SQL (`current_timestamp`, `random()`, `uuid_string()`, `run_started_at`, …) | output changes between runs |
| two warehouse runs over the same `given` disagreed | caught dynamically, e.g. via a macro |
| expect too large (`--max-expect-rows`, default 500) | lower `--sample-rows`; generators like date spines land here |
| ephemeral model, or ephemeral input | dbt unit tests do not support them / no relation to read a schema from |
| direct snapshot input | cannot be mocked source-free |
| python model | not unit-testable this way |
| `static_analysis: off` | local unit tests need static analysis; the local copy would fail by design |
| input could not be described or sampled | listed with the warehouse error |

## Running it

From your project directory, after `dbt parse`:

```bash
K=path/to/kit
uv run $K/scripts/lc_fixtures.py inventory                      # what would be generated, no warehouse
uv run $K/scripts/lc_fixtures.py scaffold --select 'stg_*'      # sample inputs, write tests (warehouse reads)
uv run $K/scripts/lc_fixtures.py harvest  --select 'stg_*'      # record expect on the warehouse
uv run $K/scripts/lc_fixtures.py verify   --select 'stg_*'      # run remote + local, classify
# or all three:
uv run $K/scripts/lc_fixtures.py all --select 'models/marts/*'
```

* `--select` takes model-name or path globs and is repeatable.
* `--dbt-args '--target dev'` is appended to every dbt call (pick the
  target to read from).
* `--sample-rows 25`: keep fixtures small. Every row is a line of YAML and
  every input is sampled independently.
* `--no-anonymize`: by default, string values in columns whose name suggests
  personal data (`email`, `name`, `phone`, `address`, …) are replaced by
  stable hashes. Hashing keeps joins on those columns working.
* Re-running `scaffold` keeps existing tests and their `expect`. Pass
  `--refresh` to resample.
* Warehouse work is batched (`--batch-size` relations per query), cached in
  `target/lc-kit/cache.json`, and resumable.

Outputs:

| Path | What |
|---|---|
| `models/_lc_kit_paired/*.yml` | the generated tests (tag `lc_kit_paired`). Delete the folder to undo |
| `target/lc-kit/paired-report.md` | per-model classification plus the diff for every mismatch |
| `target/lc-kit/*-report.json` | scaffold / harvest / verify details, including every set-aside reason |
| `target/lc-kit/set-aside/` | tests moved out of the project because they have no trustworthy `expect` |
| `target/lc-kit/logs/` | each dbt invocation. Sampled rows are never logged |

**Privacy.** The generated YAML contains (anonymized) sample rows from your
warehouse. Do not commit it, and review any snippet you copy into a ledger
entry. A minimal repro should use literal values you made up
([bug-ledger.md](bug-ledger.md)).

## Snowflake only

The generator writes Snowflake SQL. Rows are batched with
`to_json(object_construct_keep_null(*))`, semi-structured values are written as
`cast(parse_json('…') as VARIANT/OBJECT/ARRAY)`, and fixture casts use
Snowflake type names. It refuses to run on a project whose manifest
`adapter_type` is anything other than `snowflake`. Columns of type `BINARY`,
`GEOGRAPHY`, `GEOMETRY`, or `VECTOR` are nulled in fixtures and noted in the
scaffold report.

## Limits

* Inputs are sampled independently, so joins between two sampled inputs are
  often sparse and `expect` can be small. Raise `--sample-rows` for a model
  whose expect comes out empty, or accept it as weak evidence (the report
  counts these separately).
* A test covers one model over one input set. A match says "same values
  here", not "same values everywhere".
* `expect` values travel as JSON, so very high-precision decimals are kept
  as strings and compared after the warehouse casts them.
