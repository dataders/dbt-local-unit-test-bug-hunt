# The bug ledger

The output of a hunt is **one markdown file**, `BUG_LEDGER.md`, with one entry
per root cause. You hand it to a person at dbt Labs, who triages it. That
person is the bottleneck, so the ledger's quality bar is:

* **one well-minimized entry per root cause**, never one per affected model;
* **few entries**: five solid entries beat forty symptoms;
* **every entry reproducible from its own text**, with no access to your
  warehouse or project.

Start from [`../templates/BUG_LEDGER.md`](../templates/BUG_LEDGER.md). Check it
with:

```bash
uv run $KIT/scripts/lc_ledger.py lint BUG_LEDGER.md
uv run $KIT/scripts/lc_ledger.py next-id BUG_LEDGER.md
```

## Entry schema

```markdown
## LC-003: listagg over zero rows returns NULL locally, '' on Snowflake

- **Status:** new
- **Signature:** `value: listagg(empty) '' -> NULL`
- **Found by:** paired unit test `lc_kit__fct_x__f1` (also 4 other models)
- **Symptom:** one sentence, in user terms.
- **Warehouse result:** `''`
- **Local result:** `NULL`
- **Minimal repro:**
  ```sql
  -- models/lc_repro.sql
  select (select listagg(x, ',') from (select 'x' as x where 1 = 0)) as v
  ```
  `dbt show --select lc_repro --compute remote` vs `--compute local`
- **Kind:** dialect
- **Suspected layer:** translation (aggregate empty-input semantics). Hypothesis, not verified.
- **Claim boundary:** Snowflake only; one binary; not checked against dbt Labs' tracker.
- **Affects:** fct_x, fct_y (+3)
- **Environment:** dbt 2.1.0 (2.1.0.dev20261019), snowflake, 2026-11-02; session TZ UTC, host TZ UTC; threads 8; `--sample 1000`; pass 2
```

| Field | What goes in it |
|---|---|
| Status | `new` → `sent` (handed over), or `not-a-bug`, `duplicate`, `wont-report` |
| Signature | the `lc_compare.py` signature for errors, or a short `value: …` tag for mismatches. This is what de-duplicates |
| Found by | local run, paired test, or both, plus how many models hit it |
| Symptom | what a user sees, in one sentence |
| Warehouse result / Local result | the two answers, literally |
| Minimal repro | the smallest SQL that shows it, with made-up literal values, plus the commands |
| Kind | `dialect` or `framework` ([below](#dialect-or-framework)). Optional, but it routes the entry |
| Suspected layer | where it probably goes wrong (below), marked as a hypothesis |
| Claim boundary | what this entry does *not* show (below) |
| Affects | the models in your project that hit it (names only) |
| Environment | `dbt --version`, adapter, date, plus anything the result depends on: Snowflake session `TIMEZONE` and host time zone, `--threads`, the `--sample` spec, and which run pass it was seen on |
| Issue | optional: the link, if you filed it on your own findings repo |

### Suspected layer

Name one, as a hypothesis. It routes the entry to the right person.

| Layer | Typical symptom |
|---|---|
| `pull` | wrong rows/relations pulled, sampling not applied, a source cannot be pulled |
| `compile` | the model fails before it runs locally: static-analysis errors, unresolved columns or types |
| `translation` | a warehouse function or construct is translated to DuckDB wrongly or not at all (`Scalar Function ... does not exist`, or a value differs in one function) |
| `execution` | the local engine crashes, hangs, or runs out of memory |
| `macros` | adapter macros (materializations, introspection) behave differently locally |
| `unknown` | fine, say so |

### Claim boundary

One line on what the entry does *not* prove. Examples: "Snowflake only", "seen
on one binary, not checked on a newer release", "may be intended: DuckDB
documents this behavior", "not checked whether dbt Labs already tracks this".
An honest boundary saves the triager more time than any other field.

### Dialect or framework?

Ask: **can one SQL statement show it, with no dbt project around it?**

* **Yes: a dialect bug.** A function, cast, or construct gives a different
  answer or errors locally. The repro is one `select` over literal values
  (`select ... from (values (...), (...)) as t(a, b)`), wrapped in a
  throwaway model. Most entries are this kind.
* **No: a framework bug.** It needs dbt around it: pull selecting the wrong
  relations, a materialization or incremental strategy, introspection,
  the unit-test comparison, a crash under load. The repro is a tiny scratch
  project (a few models and a seed) plus the **exact** commands.

When a framework symptom is caused by a dialect gap (for example an
incremental merge failing because one function is not translated), record
the dialect gap: it is the thing to fix.

## Minimizing

0. **First reproduce with the real command**: the same selector, exclusions,
   target and sample you ran. Narrowing the selection changes the frontier
   (a parent that was built locally is now pulled from the warehouse
   instead), and that can make the bug disappear or create a different one.
   Only once it reproduces as run, start cutting.
1. Start from the failing model and its paired test diff (or error).
2. Find the one expression that differs: comment out columns until the diff
   disappears.
3. Replace the model's inputs with literals in a `select`. In the dry run,
   three of the four differing columns of a 9-column model reduced to one
   function call each.
4. Put it in a throwaway model (`models/lc_repro.sql` in a scratch copy, not
   your real project) and confirm both sides:
   ```bash
   dbt show --select lc_repro --compute remote --output json
   dbt show --select lc_repro --compute local --output json
   ```
   Use `--select` on a model, not `--inline`. Inline SQL may skip the local
   translation step, so its result is not a valid repro.
5. If one repro shows several unrelated differences, split it: **one entry
   per root cause.**

## De-duplicating

Before adding an entry:

* run `lc_compare.py --ledger BUG_LEDGER.md`. Families already in the
  ledger are marked with their `LC-` id;
* search the ledger for the function or construct name;
* if it is the same root cause, add the model to **Affects** of the existing
  entry instead of creating a new one;
* `lc_ledger.py lint` fails on two open entries with the same Signature.

If you have read access to dbt Labs' tracker, you can search it too and note
the result in the claim boundary. You don't have to: the triager does that.

### Families seen so far

Families that came up repeatedly on large projects in November 2026, on
Snowflake and on BigQuery. Some are fixed on newer builds and some are not:
check on yours. If yours matches one, it is still worth an entry (with your
Affects count), but one entry per family, not per function call.

#### Both warehouses

* **Window frames**: a window `order by` with no explicit frame should
  default to a `RANGE` frame, so rows with equal sort keys share one running
  total. Locally they could be summed one row at a time.
* **Float sums**: a `sum` over floats can differ in the last digits, even
  between two local runs ([reading-results.md](reading-results.md#paired-unit-tests)).
* **Errors from unused columns**: a local-only error from a column that a
  shared CTE defines but the failing query never uses.
* **Recursive CTEs**: column names or scope lost in recursive or shared
  subqueries.
* **Incremental models**: strategy details not applied locally, for example
  `delete+insert` without a `unique_key`, or `on_schema_change`.
* **Introspective macros**: queries that a macro sends to the catalog
  (`run_query`, `get_relations_by_pattern`, information-schema lookups)
  returning nothing, or failing to translate, under local compute
  ([source-schemas.md](source-schemas.md#names)).

#### Snowflake

* **Untranslated functions or clauses** (`Scalar Function ... does not
  exist`, parser errors): `IGNORE NULLS` in window functions,
  `LISTAGG(DISTINCT ...) WITHIN GROUP`, `ARRAY_AGG`/`ARRAYAGG`,
  `CONDITIONAL_CHANGE_EVENT`, windowed `PERCENTILE_CONT`, `RLIKE`,
  two-argument `FLOOR`, `STRTOK`, `ROUND` over a `VARCHAR`, `TRANSFORM`
  lambdas.
* **NULL and empty-input semantics**: `COUNT_IF` over all NULLs,
  `ARRAY_APPEND` and `ARRAY_DISTINCT` with NULLs, `REGEXP_SUBSTR` with no
  match, `DIV0`/`DIV0NULL` with a bare NULL, `LISTAGG` over zero rows.
* **`VARIANT` and arrays**: subscripts (`v[0]`, `v:path`), `ARRAY_SLICE`
  bounds, `LATERAL FLATTEN`, arrays turned to strings, `PARSE_JSON`
  strictness.
* **Epochs and timestamps**: numbers or `VARIANT` values cast to
  `TIMESTAMP_*` (`TRY_TO_TIMESTAMP`, `::timestamp_ntz`) read as epoch
  seconds, epoch date parts, precision arguments of timestamp helpers.
* **Calendar math**: `DATEDIFF(week, ...)` counted by Monday boundaries,
  `DATEADD` for weeks and quarters, `ADD_MONTHS` at month end, `DATEDIFF`
  over mixed date and timestamp operands.
* **Numbers**: wide `NUMBER(38, s)` values, decimal literals cast to
  `FLOAT`, `MEDIAN` midpoints, `TRY_TO_NUMBER` scale.
* **Formatting and parsing**: `TO_CHAR` format models, `SPLIT_PART` with
  part 0 or an empty delimiter.
* **Set operations**: `EXCEPT`/`INTERSECT` duplicate handling.
* **Time zones**: whole-hour shifts on `TIMESTAMP_LTZ`, and
  `TIMESTAMP_TZ` dates or text that depend on the host time zone
  ([reading-results.md](reading-results.md#time-zones)).

#### BigQuery (for reference)

The kit's scripts support Snowflake only. These are listed so a BigQuery
user hunting by hand can recognize them; they come from one large project,
so coverage is thinner than for Snowflake.

* **Untranslated functions or clauses**: `UNPIVOT`, `APPROX_QUANTILES`,
  `JSON_VALUE_ARRAY`, `NORMALIZE`, one-argument `SPLIT`, `INSTR` with
  position and occurrence, `EXTRACT(WEEK(<WEEKDAY>))`, `COLLATE`, the
  `SAFE.` prefix and `SAFE_DIVIDE`, SQL UDFs with `ANY TYPE` arguments or no
  `RETURNS`.
* **NULL semantics**: `CONCAT`, `ARRAY_TO_STRING`, `LEAST` and
  `ARRAY_CONCAT` with a NULL argument; NULL arrays (BigQuery returns an
  empty array); default NULL placement in `ORDER BY`.
* **`NUMERIC` and `BIGNUMERIC`**: inexact division, `NUMERIC` cast to
  `STRING` with padding or trailing zeros, boundary literals, `BIGNUMERIC`
  refused.
* **Dates**: `FORMAT_DATE` specifiers, `LAST_DAY` with `WEEK`/`ISOYEAR`
  parts, `DATE_DIFF`/`DATETIME_DIFF` of timestamps, `EXTRACT(WEEK)`.
* **Arrays, structs, `UNNEST`**: `UNNEST ... WITH OFFSET`, correlated
  `JOIN UNNEST`, positional structs coming back NULL, empty structs,
  `ARRAY_AGG(... LIMIT n)` ignoring the limit, `REGEXP_EXTRACT_ALL` returning
  the whole match instead of the capturing group, struct casts.
* **`BYTES`**: `LENGTH` of bytes, bytes-literal escapes, `SHA` digests.
* **Types and joins**: string literals in `CASE` branches next to a typed
  branch, `FULL OUTER JOIN ... USING` key columns, nested table aliases.
* **Framework**: project IDs with a hyphen left unquoted in local DDL,
  `insert_overwrite` incrementals (`_dbt_max_partition`, `partition_by`),
  local columns given the wrong warehouse's types, backtick identifiers in
  `run_query`, many `DESCRIBE TABLE` "not supported" warnings, and
  reauthentication errors (`invalid_rapt`: run `gcloud auth login` again).

## Handing it over

Send the file (or a link to it) to your dbt Labs contact. Mark handed-over
entries `sent`.

Optionally, the kit can open one issue per entry on **a repository you
configure**, such as your team's findings repo:

```bash
uv run $KIT/scripts/lc_ledger.py open-issue BUG_LEDGER.md --entry LC-003 --repo my-org/lc-findings         # dry run
uv run $KIT/scripts/lc_ledger.py open-issue BUG_LEDGER.md --entry LC-003 --repo my-org/lc-findings --yes   # files it
```

It searches that repo for the entry id first and will not file a duplicate.
It refuses any `dbt-labs/*` repository: the kit never files on dbt Labs' repos.
