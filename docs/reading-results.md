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

## The buckets

| Bucket | How you recognize it | Ledger? |
|---|---|---|
| **Local-compute error** | builds on the warehouse, errors locally: unsupported function, type error, static-analysis error, local engine crash | yes |
| **Value mismatch** | builds on both, a paired unit test shows different values | yes |
| Setup | every model fails the same way: `Table ... does not exist` (pull/run used different target path or target), auth errors, missing package | no, fix the setup |
| Sampling | a data test fails locally only (orphan keys from independent samples, a rare value missing); a model is empty locally | no, unless it reproduces on a fixed input |
| Warehouse/project | fails on the warehouse too (`both-fail`), or a data test that fails remotely and passes locally (it was sampled away) | no, it is your project's own issue |
| Out of scope | warehouse-only features: ML/LLM functions, external functions, Python UDFs, `information_schema` sources | blocker only, `LINK: none` |

Before calling something a local-compute error, **confirm the warehouse
side** with a read-only run of the same model:

```bash
dbt show --compute remote --select <model> --limit 5
```

If that fails too, it is not a local-compute bug.

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
  the entry.
* **Time zone / "now"**: anything that depends on the session time zone or
  the current time. The generator sets aside the obvious cases.
