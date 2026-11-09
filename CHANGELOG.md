# Changelog

## 2026-11-09: lessons from running at scale

The kit was first checked on a 13-model sample project. Since then, the same
method ran on two production-sized Snowflake projects (about 1,100 and 1,700
models) on Fusion builds from early November 2026. This release folds what
that taught us back into the kit. Most of it is about telling real bugs from
noise.

### Generator (`scripts/lc_fixtures.py`)

* Fixture format `f3`: tests are renamed `lc_kit__<model>__f3__{remote,local}`.
  Re-run `scaffold --refresh` and `harvest` (and delete old `__f2__` files)
  before comparing with earlier results.
* Anonymization no longer hashes non-personal `*_name` columns
  (`event_name`, `plan_name`) or `first_*`/`last_*` columns
  (`first_seen_at`). A value equal to a string literal in the model's SQL is
  kept. Hashing those values used to empty the model's filters and its
  expect.
* Nondeterminism detection also reads compiled SQL (run `dbt compile`
  first), catching `current_date` and similar inside macros.
* `harvest` drops `expect` columns holding `''` or the string `'null'`
  (a dict-format expect turns both into NULL, which made the warehouse copy
  fail its own test), and reports them under `expect_columns_dropped`.
  New `--drop-expect-column MODEL.COLUMN` for clock-derived columns.
* Backslashes in fixture strings are doubled, because Snowflake processes
  escapes in string literals (JSON text with `\"` no longer fails
  `parse_json`).

### Docs

* `pull.md`: pull reads your target's relations even with `--defer`
  (missing or stale dev tables are setup); the new on-disk layout
  (`target/db/...`); size numbers; how an expired SSO token looks; one dbt
  process at a time; slow views; data-reading macros see the sample.
* `source-schemas.md`: timestamp precision and range, `TIMESTAMP_TZ` offsets,
  `FLOAT` next to `VARIANT`, `\xHH` in JSON, relation discovery, hard-coded
  relation names.
* `reading-results.md`: multi-pass runs; runner crashes and memory; error
  codes that mislead; time zones (the biggest false-positive family); new
  *product boundary* and *host* buckets; float sums between local runs;
  comparison-machinery errors.
* `paired-unit-tests.md`: never use a global `--compute local` for the paired
  run; causes of `broken`; how common empty expects are.
* `bug-ledger.md`: dialect vs framework; reproduce with the real command
  before you minimize; a fuller Environment line; families seen so far,
  split into both warehouses, Snowflake, and BigQuery (for reference: the
  scripts stay Snowflake-only).
