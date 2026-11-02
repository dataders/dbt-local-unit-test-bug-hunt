# Dry run: jaffle_shop on Snowflake (2026-11-02)

The kit's first end-to-end run, to check that the scripts and the docs
describe what Fusion actually does.

## Setup

* **Project:** a copy of the jaffle-shop sample project, Snowflake variant
  (13 models, 6 raw tables loaded with `dbt seed`), in a scratch
  directory. `generate_schema_name` and the source schema were pointed at a
  dedicated schema (`<target schema>_kit_dryrun`) so nothing shared was
  touched.
* **Warehouse:** Snowflake, key-pair auth.
* **Binary:** `dbt 2.1.0` (`2.1.0.dev20261019`).
* Two **probe models** were added to the copy as a planted positive
  control: one calling several Snowflake functions (`split_part`, `listagg`,
  `to_char`, `dayofweekiso`, `try_to_number`, …), one calling `strtok`.

## What ran

| Step | Command | Result |
|---|---|---|
| pull | `dbt pull --sample 500 --exclude resource_type:seed` | 6 relations, 1.6K rows, 143 KB, 4.5 s |
| run | `dbt build --compute local --exclude resource_type:seed` | 43/43 (13 models, 27 data tests, 3 unit tests) |
| compare | `lc_compare.py` on a deliberately mis-configured run (different `--target-path`) | 6 failures → 1 family, as intended |
| paired: inventory | `lc_fixtures.py inventory` | 13/13 eligible |
| paired: scaffold | `lc_fixtures.py scaffold` | 14 inputs described + sampled in 1 batch each |
| paired: harvest | `lc_fixtures.py harvest` | 12 expectations; `metricflow_time_spine` set aside (3,650-row expect) |
| paired: verify | `lc_fixtures.py verify` | jaffle: 12/12 pass both sides. With probes: 13/14 (1 value mismatch) |
| negative control | corrupted one `expect` value | classified `broken`, not counted as a match |
| minimize | one-line `models/lc_repro.sql`, `dbt show --select` on both computes | 3 differences confirmed |

Scoreboard at the end: `models 13/13 pass locally | paired 13/14 pass on
both sides (1 value mismatch, 0 broken, 1 set aside) | ledger 5 entries`.

## What it found

See [BUG_LEDGER.md](BUG_LEDGER.md). Three value differences came from the
planted probe: `split_part(…, 0)`, `listagg` over zero rows, and
`to_char` number formatting. Two pull behaviors came up while writing
the docs: `--indirect-selection` is not applied to the frontier, and
window/JSON `--sample` silently drops `max_rows`. jaffle_shop's own models
showed no differences.

## What the dry run changed in the kit

* `catalog.json` reports Snowflake `NUMBER` without precision/scale, so the
  generator reads types with `adapter.get_columns_in_relation` instead.
  Otherwise `7.42` becomes `7`.
* Fusion reports a failed unit-test diff as status `error` with message
  `Error`. The classifier reads the `Test failed:` diff block from the
  output to tell a value mismatch from a crash.
* Pull and run must share `--target-path` (documented, and shown by the
  `lc_compare` family above).
* `dbt compile --write-catalog` reloaded seed CSVs into `target/data`,
  overwriting the pulled sample. That only matters when seeds and sources
  are the same relations, as in jaffle. The generator compiles into its own
  target path.
* `dbt show --inline --compute local` appeared to skip local translation (a
  function that works in a model failed inline), so repros use a model with
  `--select`.

## Not exercised

* **A large production project.** Batching, resume, and
  `[n/total]` progress were only exercised at 1-batch scale.
* **Warehouses other than Snowflake**: out of scope. The kit is
  Snowflake-only and refuses other adapters.
* **The two-copy paired-test format (`f2`) on the warehouse side.** After
  the dry run, the kit switched to the public names: `compute: local` /
  `compute: remote` per-test configs, with the documented opt-in variable.
  The dry run itself used the earlier CLI-flag form. Re-checked afterwards:
  `--compute local` returned the same values as the original runs for
  `lc_repro`, and the regenerated `__local` copies reproduced the same match,
  mismatch, match results. The `__remote` copies could not run, because the
  dry-run schema had been dropped. They came out `broken`, as the rules
  require.
* **The Snowflake-only revision itself.** After this run the non-Snowflake
  code paths were removed and the dry-run schema was dropped. That revision
  is covered by the offline tests and an `inventory` run, not by a second
  warehouse run.
* **Incremental models, snapshots, ephemeral models, semi-structured columns,
  introspection macros.** jaffle has none, so the `is_incremental`
  override, the snapshot/ephemeral set-aside rules, and the VARIANT literals
  are covered only by offline unit tests (`scripts/test_kit.py`).
* **The nondeterminism double-run** never triggered (no nondeterministic
  model). The static detector is covered by an offline test.
* **Cut points and `--exact`**: documented from Fusion's help and the
  project's selector convention, not run here.
* **`lc_compare.py --remote`** (warehouse run_results comparison): run
  only without `--remote`.
* **`lc_ledger.py open-issue --yes`**: run only in dry-run mode and against
  the refusal of dbt Labs' repositories. No issue was filed anywhere.
* **An agent driving `SKILL.md` from a cold start.** The steps were run by
  hand.
