# Local-compute bug ledger: jaffle_shop dry run (Snowflake)

<!--
Produced by the kit's first dry run (see DRY_RUN.md next to this file). Entries LC-001..003 came from a
deliberately planted probe model, not from jaffle_shop's own models (which all matched). They are real
Snowflake-vs-local differences, recorded here as worked examples of the entry schema.
-->

- **Project:** jaffle_shop (sample project copy), Snowflake, 13 models + 2 probe models
- **Fusion:** `dbt 2.1.0` (`2.1.0.dev20261019`)
- **Hunt dates:** 2026-11-02
- **Headline:** `models 13/13 pass locally | paired 13/14 (0 broken, 1 set aside) | 5 entries`
- **Contact:** <your name / email>

## LC-001: split_part with part number 0 returns '' locally; Snowflake returns the first part

- **Status:** new
- **Signature:** `value: split_part(s, d, 0) 'a' -> ''`
- **Found by:** paired unit test `lc_kit__lc_probe_values__f1` (planted probe); 1 model
- **Symptom:** a model using `split_part(x, '-', 0)` produces empty strings locally.
- **Warehouse result:** `'a'` (Snowflake treats part 0 as part 1)
- **Local result:** `''`
- **Minimal repro:**
  ```sql
  -- models/lc_repro.sql
  select split_part('a-b-c', '-', 0) as part0
  ```
  `dbt show --select lc_repro --compute remote --output json` → `[{"PART0":"a"}]`;
  `--compute local` → `[{"PART0":""}]`
- **Suspected layer:** translation. Hypothesis: `split_part` is passed to DuckDB unchanged, and DuckDB's 0-index semantics differ.
- **Claim boundary:** Snowflake only; one binary (2026-10-19 dev build); not checked against dbt Labs' tracker or a newer release.
- **Affects:** lc_probe_values (probe)
- **Environment:** dbt 2.1.0.dev20261019, snowflake, 2026-11-02

## LC-002: listagg over zero rows returns NULL locally; Snowflake returns ''

- **Status:** new
- **Signature:** `value: listagg(empty) '' -> NULL`
- **Found by:** paired unit test `lc_kit__lc_probe_values__f1` (planted probe); 1 model
- **Symptom:** an aggregate string over an empty group is NULL locally, so `coalesce`/`=` logic downstream diverges.
- **Warehouse result:** `''`
- **Local result:** `NULL`
- **Minimal repro:**
  ```sql
  -- models/lc_repro.sql
  select (select listagg(x, ',') from (select 'x' as x where 1 = 0)) as listagg_empty
  ```
  `dbt show --select lc_repro --compute remote` → `""`; `--compute local` → `null`
- **Suspected layer:** translation (empty-input semantics of the aggregate). Hypothesis.
- **Claim boundary:** Snowflake only; one binary; scalar-subquery form only, not checked with `group by` over an empty group.
- **Affects:** lc_probe_values (probe)
- **Environment:** dbt 2.1.0.dev20261019, snowflake, 2026-11-02

## LC-003: to_char(number, '9,999.99') formats differently locally

- **Status:** new
- **Signature:** `value: to_char(number, fmt) '     7.  ' -> '7.00'`
- **Found by:** paired unit test `lc_kit__lc_probe_values__f1` (planted probe); 1 model
- **Symptom:** numeric format models produce different strings locally (padding and trailing zeros).
- **Warehouse result:** `'     7.  '` (Snowflake: `9` suppresses leading/trailing zeros, output padded)
- **Local result:** `'7.00'`
- **Minimal repro:**
  ```sql
  -- models/lc_repro.sql
  select to_char(7.00::number(16, 2), '9,999.99') as to_char_fmt
  ```
  `dbt show --select lc_repro --compute remote` vs `--compute local`
- **Suspected layer:** translation (number format model). Hypothesis.
- **Claim boundary:** Snowflake only; one format string; one binary.
- **Affects:** lc_probe_values (probe)
- **Environment:** dbt 2.1.0.dev20261019, snowflake, 2026-11-02

## LC-004: dbt pull ignores --indirect-selection when expanding the frontier

- **Status:** new
- **Signature:** `pull: --indirect-selection cautious still pulls inputs of cross-model tests`
- **Found by:** `dbt pull --preview` while writing the pull docs
- **Symptom:** `dbt pull --select stg_orders --indirect-selection cautious` also pulls `stg_order_items`, the other parent of a `relationships` test, though `dbt ls` with the same flags excludes that test.
- **Warehouse result:** n/a (selection behavior). `dbt ls --select stg_orders --indirect-selection cautious` does not list `relationships_stg_order_items_order_id__order_id__ref_stg_orders_`.
- **Local result:** pull preview includes `SELECT * FROM ...stg_order_items LIMIT 100`, with `cautious` and with `empty`
- **Minimal repro:**
  ```bash
  # any project with a relationships test from model B to model A
  dbt ls   --select A --indirect-selection cautious                       # no B->A test
  dbt pull --select A --indirect-selection cautious --sample 100 --preview  # still pulls B
  dbt pull --select A --exclude resource_type:test --sample 100 --preview   # workaround: only A's inputs
  ```
- **Suspected layer:** pull (frontier computed before indirect-selection filtering). Hypothesis.
- **Claim boundary:** may be intended (pull may deliberately include tests' inputs); one project, one binary.
- **Affects:** pull size of every selection with cross-model tests
- **Environment:** dbt 2.1.0.dev20261019, snowflake, 2026-11-02

## LC-005: time-window and JSON --sample pull whole tables when no event_time is configured

- **Status:** new
- **Signature:** `pull: --sample window/json emits no filter and ignores max_rows`
- **Found by:** `dbt pull --preview` while writing the pull docs
- **Symptom:** `--sample "30 days"`, and `--sample '{"start": ..., "end": ..., "max_rows": 50}'`, pull the full relation, with no filter and no `LIMIT`, for relations without `event_time`.
- **Warehouse result:** n/a (expected: a filter, or at least `LIMIT 50` for `max_rows`, or a warning)
- **Local result:** preview shows `SELECT * FROM <database>.<schema>.raw_orders` with no predicate or limit
- **Minimal repro:**
  ```bash
  dbt pull --select stg_orders --sample '{"start": "2017-01-01", "end": "2017-02-01", "max_rows": 50}' --preview
  dbt pull --select stg_orders --sample 50 --preview    # for contrast: LIMIT 50
  ```
- **Suspected layer:** pull (sample spec resolution). Hypothesis.
- **Claim boundary:** the window part is probably intended without `event_time`; the silently dropped `max_rows` and the missing warning are the claim. Not tested with `event_time` configured.
- **Affects:** any user who passes a window sample on sources without `event_time`
- **Environment:** dbt 2.1.0.dev20261019, snowflake, 2026-11-02
