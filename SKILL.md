---
name: local-compute-bug-hunt
description: Run a user's own Snowflake dbt project under dbt Fusion local compute (`--compute local`) and surface where it diverges from the warehouse. Set up, pull, run, paired unit tests, classify, minimize, and write one deduplicated markdown bug ledger for a human at dbt Labs. Use when someone wants to find, reproduce, or report local-compute conformance bugs in their project. Discovery and reporting only, never patching Fusion.
---

# Local-compute bug hunt

You help a user find where dbt Fusion's local execution (`--compute local`,
DuckDB) disagrees with their warehouse on **their own dbt project**, and
write it up for a human triager at dbt Labs. The project is the test suite;
the warehouse is the oracle.

You **discover, reproduce, and report**. You do not patch Fusion, verify
fixes, or open PRs.

**What's documented.** Local compute is experimental. The public docs cover
only **local unit tests**: the `compute: local` unit-test config, enabled
with `DBT_ENGINE_EXPERIMENTAL_LOCAL_UNIT_TESTS=true`
([`compute` config](https://docs.getdbt.com/reference/resource-configs/compute), [run unit tests
locally](https://docs.getdbt.com/docs/build/unit-tests#run-unit-tests-locally)). The paired tests use exactly that. The
`--compute local` CLI flag, whole-project `dbt run --compute local`, and
`dbt pull` work on current Fusion builds but are **not** publicly
documented. Present them to the user as experimental, describe only what
you observe, and never claim how they work internally. Error messages may
say `sidecar`, which means `local`.

**Snowflake projects only.** The scripts generate Snowflake SQL and refuse
any other `adapter_type`. If the user's project is on another warehouse, say
so and stop. Don't try to adapt the kit.

Paths below are relative to this kit's directory (call it `$KIT`). The human
docs in `$KIT/docs/` explain every step in depth. Read the relevant one
before a step you have not done before in this session.

## Rules (hard)

1. **Never edit the user's project to make local compute pass.** Models,
   macros, configs, packages, and `dbt_project.yml` stay as they are. A
   failure they cause is a local-compute bug, a fixture problem, or a genuine
   project bug. In the last case, tell the user; it goes in their own PR,
   never in the hunt. The only things the kit adds to the project:
   - the `local_blockers` / `local_dag` (and optional
     `local_cut_points`) entries in `selectors.yml`;
   - generated tests under `models/_lc_kit_paired/` (delete the folder to
     undo);
   - throwaway repro models (`models/lc_repro*.sql`), deleted after use.
   Prefer a scratch branch or `git worktree`. Never commit any of it unless
   the user asks.
2. **Never file anything on dbt Labs' repositories (`dbt-labs/*`)**, and never open issues anywhere
   without the user's explicit yes. If the user configured a findings repo,
   `scripts/lc_ledger.py open-issue` files one entry there after a
   duplicate search.
3. **The warehouse is read-only for you**: `dbt pull`, `dbt show --compute
   remote`, and the fixture generator only read. Do not `dbt run/build
   --compute remote` (that writes to the user's schemas) unless the user
   asks for a warehouse-side comparison run and names the target.
4. **Matches are counted only when proven.** Paired scoreboard = cases
   passing on both sides out of cases passing on the warehouse. A case that
   fails on the warehouse is `broken`: report it separately, never as a
   match. A scoped run claims only its slice. Something you did not compare
   is unknown, not a match.
5. **Low volume, high quality.** One ledger entry per root cause. Group by
   signature, de-duplicate against the ledger before adding, and prefer one
   minimized repro over many symptoms. A triager reads every entry.
6. **Privacy.** Sampled rows and generated fixtures contain the user's data.
   Never paste them into the ledger. Repros use made-up literals.
7. **Budget disk and time.** Always pull with `--sample` (default 1000).
   Check free disk before a large pull (`df -h .`). Long commands print
   `[n/total]` progress. Tell the user before anything you expect to take
   more than 10 minutes.

## The loop

### 0. Set up (once)

* Confirm the project is on Snowflake (`adapter_type` in
  `target/manifest.json` after `dbt parse`, or the profile's `type`).
* `export DBT_ENGINE_EXPERIMENTAL_LOCAL_UNIT_TESTS=true` (the documented
  opt-in. Without it, `compute: local` fails with `InvalidConfig (dbt1005)`
  naming the variable).
* Confirm `dbt --version` is a Fusion build that runs locally:
  `dbt show --compute local --inline "select 1 as ok"`. Record the version:
  it goes in every entry.
* Confirm warehouse auth works read-only: `dbt show --compute remote
  --inline "select 1 as ok"`. Recommend key-pair / service-account auth
  over browser SSO for anything long ([docs/pull.md](docs/pull.md#auth-and-warehouse-load)).
* Add the probe and ledger selectors from `templates/selectors.lc-kit.yml`
  ([docs/frontiers-and-selectors.md](docs/frontiers-and-selectors.md)). Seed
  `local_blockers` only with genuinely out-of-scope models (warehouse-only
  functions). Start the probe small for big projects (`models/staging+`).
* Copy `templates/BUG_LEDGER.md` to the user's chosen location (default: the
  project root, uncommitted) and fill the header.
* `dbt parse`, then `dbt ls --selector local_dag --resource-type model | wc -l`
  gives the denominator.

### 1. Pull (experimental, undocumented)

```bash
dbt pull --selector local_dag --exclude resource_type:test --sample 1000 --preview   # check first
dbt pull --selector local_dag --exclude resource_type:test --sample 1000
```

Pull and run must use the same target, vars, and `--target-path`. A pull
failure on one relation is itself a candidate (layer `pull`): record it,
block or cut the affected model, and continue.

### 2. Run (experimental, undocumented)

```bash
dbt run --selector local_dag --compute local
cp target/run_results.json lc-local-<n>.json
uv run $KIT/scripts/lc_compare.py --local lc-local-<n>.json --ledger BUG_LEDGER.md
```

Prefer `dbt run` (models) for the probe. Local data tests on sampled data
are sampling-sensitive ([docs/reading-results.md](docs/reading-results.md)).
For each **new** candidate family, confirm the warehouse side read-only:
`dbt show --compute remote --select <example model> --limit 5`.

### 3. Paired unit tests (values; documented local unit tests)

```bash
uv run $KIT/scripts/lc_fixtures.py inventory
uv run $KIT/scripts/lc_fixtures.py all --select '<glob>'     # scaffold + harvest + verify
```

Start with a slice (one folder) and widen. Read
`target/lc-kit/paired-report.md`: each `mismatch` shows a diff whose cells
read `warehouse -> local`. Rule out ties, float last digits, and time-zone
or "now" effects before calling it a bug
([docs/paired-unit-tests.md](docs/paired-unit-tests.md)).

### 4. Classify

Put every failure in one bucket (from [docs/reading-results.md](docs/reading-results.md)):
local-compute error, value mismatch, setup, sampling, warehouse/project, or
out of scope. Only the first two become ledger entries. Out-of-scope models
become blockers with `LINK: none`.

### 5. Minimize

For each new family, take one example and reduce it to the smallest SQL
that still differs: drop columns, then replace inputs with literals. Run it
as a throwaway **model** with `dbt show --select lc_repro --compute remote`
and `--compute local`, not `--inline`, which may skip local translation.
One repro showing several unrelated differences becomes several entries.
Delete the throwaway model afterwards.

### 6. Write it up

* `uv run $KIT/scripts/lc_ledger.py next-id BUG_LEDGER.md`, then add the
  entry using the schema in [docs/bug-ledger.md](docs/bug-ledger.md):
  symptom, minimal repro, warehouse vs local result, suspected layer
  (marked as a hypothesis), claim boundary, affects, environment.
* Same root cause as an existing entry: extend its **Affects**; no new
  entry.
* Add or update the blocker in `selectors.yml` with
  `WHY` / `LINK: LC-00n` / `REMOVE`, and use a cut point where the children
  should still run.
* `uv run $KIT/scripts/lc_ledger.py lint BUG_LEDGER.md` must pass.

Then go back to step 1 with the wider probe. Stop when the probe runs clean,
when the remaining failures are all recorded, or when the user says so.

## Reporting to the user

Lead every update with the headline numbers, then only what moved:

```text
models 1,204/1,260 pass locally | paired 340/362 pass both (9 broken, 31 set aside) | ledger 6 entries (2 new) | blockers 4
```

Then, briefly: what was found, what is **not** proven, and what runs next.
Questions to the user come batched as one numbered list with a default per
item. When the hunt ends, hand over the ledger path and say which entries
are ready to send.

## Files

| Path | Purpose |
|---|---|
| `README.md` | human quickstart |
| `docs/pull.md` | how `dbt pull` works: frontier, sampling, sample size, auth, where data lands |
| `docs/source-schemas.md` | names and types, warehouse → local |
| `docs/frontiers-and-selectors.md` | probe/ledger selectors, entry convention, cut points |
| `docs/reading-results.md` | the buckets, the comparison report, the paired scoreboard |
| `docs/paired-unit-tests.md` | how the fixture generator builds given/expect, set-aside rules |
| `docs/bug-ledger.md` | entry schema, minimizing, de-duplicating, handing over |
| `scripts/lc_compare.py` | local vs warehouse run_results, grouped into candidate families |
| `scripts/lc_fixtures.py` | paired unit-test generator (inventory/scaffold/harvest/verify/all) |
| `scripts/lc_ledger.py` | ledger lint, next id, optional issue on a user-configured repo |
| `templates/` | selectors and ledger templates |
| `examples/jaffle-dryrun/` | the kit's own dry run: ledger and what it exercised |
