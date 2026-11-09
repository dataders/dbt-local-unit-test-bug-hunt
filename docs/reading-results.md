# Reading results

A local run produces a lot of red the first time. Most of it is a handful of
causes. The job is to sort each failure into one of the buckets below, and
only the first two become ledger entries.

## The comparison

Copy `target/run_results.json` aside after every run, because the next dbt
command overwrites it. Then:

```bash
cp target/run_results.json lc-local.json
uv run $KIT/scripts/lc_compare.py --local lc-local.json --ledger BUG_LEDGER.md
# optionally, against a warehouse run of the same selection:
uv run $KIT/scripts/lc_compare.py --local lc-local.json --remote lc-remote.json --ledger BUG_LEDGER.md
```

The first line is the run's headline:

```text
models 1,204/1,260 pass locally | candidate families 7 (3 new) | local-only data-test failures 41 | both-fail 2 | skipped downstream 18
```

* **models passing locally** is the number to move. It only means "built",
  not "same values". Values are what paired tests check.
* **candidate families** groups local-only failures by a normalized error
  signature (names, numbers, and quoted values stripped). One family is
  usually one root cause, so 50 models failing with
  `Scalar Function with name X does not exist` are one entry, not 50.
  `(3 new)` counts families whose signature is not already in your ledger.
* **local-only data-test failures** are listed separately and are *not*
  candidates yet (see sampling, below).
* **skipped downstream** models did not run because a parent failed. They
  are not failures of their own.
* Count models from the per-node results (what `lc_compare.py` does), not
  from dbt's `Summary:` line: its total also counts hooks and no-op nodes.

### Multi-pass runs are normal

Models whose macros introspect other relations at compile time (for example
`get_relations_by_pattern`, or `run_query` against a model) can only compile
once a previous pass has built what they look at. So run the probe again
until the number of successes stops growing (two or three passes is typical,
the last one with `--full-refresh` if you have incrementals), and copy
`run_results.json` aside after each pass. Report which pass the headline
counts.

## The buckets

| Bucket | How you recognize it | Ledger? |
|---|---|---|
| **Local-compute error** | builds on the warehouse, errors locally: unsupported function, type error, static-analysis error, local engine crash | yes |
| **Value mismatch** | builds on both, a paired unit test shows different values | yes |
| Setup | every model fails the same way: `Table ... does not exist` (pull/run used different target path or target), auth errors, missing package | no, fix the setup |
| Sampling | a data test fails locally only (orphan keys from independent samples, a rare value missing); a model is empty locally | no, unless it reproduces on a fixed input |
| Warehouse/project | fails on the warehouse too (`both-fail`), or a data test that fails remotely and passes locally (it was sampled away) | no, it is your project's own issue |
| Out of scope | warehouse-only features: ML/LLM functions, external functions, Python UDFs, `information_schema` sources | blocker only, `LINK: none` |
| Product boundary | the model reads a relation pull never fetched: introspection at render time, or a hard-coded `db.schema.table` with no `ref()`/`source()` | one entry for the boundary, not per model; report the headline both with and without these models |
| Host | a runner crash or out-of-memory that fails many unrelated models at once (below) | no, rerun on a quiet host first |

Before calling something a local-compute error, **confirm the warehouse
side** with a read-only run of the same model:

```bash
dbt show --compute remote --select <model> --limit 5
```

If that fails too, it is not a local-compute bug.

### Crashes, host load, and memory

A local run executes in a separate runner process. When that process dies,
**every model in flight fails at once**, often more than a hundred, with
messages like `stdout closed` or a broken connection to the runner. Those
are one event, not a hundred bugs. In the campaign, almost all such bursts
came from host load or memory pressure: a large project peaked at 46-74 GiB
of RAM. Before recording a crash:

1. check free memory (`free -g`) and that no other heavy job shares the box;
2. rerun once, alone, with fewer `--threads`;
3. record it only if it reproduces on a quiet host. Then record it **once**,
   as layer `execution`, with the crash rate ("3 of 5 runs") and the thread
   count.

An intermittent crash that disappears at `--threads 1` is still a finding:
say so in the entry. An out-of-memory kill is not a bug claim on its own.
If dbt seems to hang forever, look for a dead (`defunct`) `dbt-db-runner`
process: dbt can wait on a runner that has already exited.

### Error codes can mislead

* A local unit test whose translated SQL is not valid DuckDB can be
  reported as `DbConnectionFailed (dbt1300)`. It is a translation problem,
  not a connection problem.
* A missing table can be wrapped as `Generic (dbt1000)`. Read the message,
  not just the code.

`lc_compare.py` groups by the normalized message, so these still group
correctly.

### Time zones

The most common false positive in the campaign: 20 of 29 persistent value
mismatches on one project were exact **whole-hour shifts** (7 or 8 hours) on
`TIMESTAMP_LTZ` values. The Snowflake session ran in a US time zone, and
local compute in UTC. Before you record a timestamp difference:

* check the session zone: `dbt show --compute remote --inline "show
  parameters like 'TIMEZONE' in session"`, and your host's (`date +%Z`);
* if the difference is a whole number of hours, it is the time zone, not
  the function. Group every such case under **one** entry and say which
  zones were involved;
* to see whether anything else is left, compare the shift with the session
  offset, or cast both sides to `TIMESTAMP_NTZ` in UTC in a minimal repro.
  Some builds pin local compute to UTC whatever your host's `TZ` is, so
  changing `TZ` is not a reliable test.

How local compute should pick its zone was still changing in November 2026
(some builds print a warning when the zones differ). Record the zones in
the entry's Environment line.

### Views look green

A view "builds" by creating the view. No rows are computed. In the dry run, a
view using three functions with different local semantics built green
locally. Only the paired unit test caught the value differences. Treat
"models passing locally" as a lower bound on what is broken, never as proof
of fidelity.

### Sampling, in more detail

Pull samples each relation independently ([pull.md](pull.md#sampling)). So:

* `relationships` tests fail locally because the parent row was not
  sampled;
* `unique`/`not_null`/`accepted_values` can *pass* locally when the
  offending row was not sampled. In the dry run, a test that fails on
  Snowflake passed locally;
* joins and filters can make a model come out empty, which hides bugs
  rather than causing them.

A data-test difference becomes a candidate only once it reproduces on a
fixed input that both engines see, which is what a paired unit test or a
minimal repro gives you.

## Paired unit tests

`lc_fixtures.py verify` prints its own headline:

```text
paired 12/13 pass on both sides (1 value mismatch, 0 local error, 1 broken, 0 unknown, 1 set aside; 0 of the matches have an empty expect)
```

* The denominator is **cases that pass on the warehouse**. A case that
  fails on the warehouse is `broken` (a fixture problem). It is counted
  separately and is never a match.
* `value mismatch`: the model ran on both sides over identical inputs and
  returned different rows. The report shows the diff, with each cell reading
  `warehouse -> local`.
* `local error`: the model errored locally over inputs on which the warehouse
  succeeded.
* `set aside`: no trustworthy oracle (nondeterministic, too large, not
  unit-testable). Not counted either way.
* `empty expect`: the warehouse returned no rows for this input, so the test
  proves little. Report it, don't celebrate it.

Value mismatches worth a second look before you record them:

* **Ties.** `row_number()` / `rank()` / `first_value()` / `listagg` without a
  total order can legitimately differ between engines. The model is
  ambiguous, not the engine.
* **Float last digits** after aggregation. Real, but low priority. Say so in
  the entry. A float `sum` can even differ between two *local* runs (it
  depends on the order rows are added), so run the local side twice before
  recording one.
* **Time zone / "now"**: anything that depends on the session time zone or
  the current time. The generator sets aside the obvious cases; see
  [Time zones](#time-zones) and
  [paired-unit-tests.md](paired-unit-tests.md#when-the-warehouse-copy-fails-broken).
* **The comparison itself failed.** Errors like `Cannot compare types
  VARIANT and VARCHAR`, a timestamp-with-zone vs without-zone comparison,
  or a column named like a keyword (`DEFAULT`) come from the unit-test
  comparison, not from the model. They are still worth one framework entry,
  but not a dialect entry per model.
