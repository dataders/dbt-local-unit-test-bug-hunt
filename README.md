# dbt local unit test bug hunt

**Snowflake projects only.** Run **your own dbt project** under dbt Fusion's local compute
(`--compute local`, which executes on DuckDB on your machine). Find
where it disagrees with your warehouse, and write the disagreements up as a
short bug ledger for dbt Labs.

You can follow it by hand with the docs, or give `SKILL.md` to an agent
(Claude Code, Codex, …) and let it drive. Either way it **only discovers,
reproduces, and reports**: no Fusion patches, nothing filed on dbt Labs'
repos.

## What you need

* A dbt project on **Snowflake**. Other warehouses are not supported: the
  scripts generate Snowflake SQL and refuse other adapters.
* A Fusion build of `dbt` with `--compute local` (`dbt --version`).
* A Snowflake profile that can **read** your project's sources. Key-pair
  auth is best; browser SSO works for small projects.
* [`uv`](https://docs.astral.sh/uv/) for the kit's scripts (they declare
  their own dependencies).
* Room on the machine. A small project needs little, but a large one
  (1,000+ models) can use tens of GB of local state at `--sample 10000`, and
  tens of GB of RAM while it runs. Run one dbt process at a time.
* A scratch branch or `git worktree` of your project. The kit adds two
  selectors and a folder of generated tests, and none of it should be
  committed.

## What's public, and what's experimental

Local compute is **experimental**. It has two parts, and the kit uses both.

* **Documented:** running **unit tests** locally with DuckDB, via the
  `compute: local` config on a unit test (or `+compute: local` project-wide).
  You opt in with an environment variable:
  ```bash
  export DBT_ENGINE_EXPERIMENTAL_LOCAL_UNIT_TESTS=true
  ```
  See the [`compute` config reference](https://docs.getdbt.com/reference/resource-configs/compute) and
  [Run unit tests locally](https://docs.getdbt.com/docs/build/unit-tests#run-unit-tests-locally). The kit's paired tests use
  exactly this.
* **Beyond the public docs:** the `--compute local` CLI flag, running whole
  projects locally (`dbt run --compute local`), and `dbt pull`. Current
  Fusion builds accept them, but they are not documented. Treat everything
  the kit says about them as observed behavior on one build, not as a
  product contract.

Error messages may say `sidecar`. That means the same thing as `local`.
`--compute` may not appear in `dbt --help` on your build; it is accepted
anyway.

## Quickstart

From your project directory (`KIT=path/to/kit`):

```bash
# 0. setup: opt in, selectors, ledger (see docs/frontiers-and-selectors.md)
export DBT_ENGINE_EXPERIMENTAL_LOCAL_UNIT_TESTS=true   # documented opt-in for local unit tests
cat $KIT/templates/selectors.lc-kit.yml       # paste under `selectors:` in selectors.yml
cp  $KIT/templates/BUG_LEDGER.md BUG_LEDGER.md
dbt compile && dbt ls --selector local_dag --resource-type model | wc -l
dbt show --compute remote --inline "show parameters like 'TIMEZONE' in session"   # note it, and `date +%Z`

# 1. pull a sample of the probe's inputs (experimental, undocumented: always --sample, --preview first)
dbt pull --selector local_dag --exclude resource_type:test --sample 1000 --preview
dbt pull --selector local_dag --exclude resource_type:test --sample 1000

# 2. run the probe locally (experimental, undocumented) and group the failures
dbt run --selector local_dag --compute local      # repeat until the pass count stops growing
cp target/run_results.json lc-local-1.json
uv run $KIT/scripts/lc_compare.py --local lc-local-1.json --ledger BUG_LEDGER.md

# 3. paired unit tests (documented local unit tests): same inputs, warehouse vs local, values compared
uv run $KIT/scripts/lc_fixtures.py all --select 'models/staging/*'
cat target/lc-kit/paired-report.md

# 4-6. classify, minimize, write one ledger entry per root cause
uv run $KIT/scripts/lc_ledger.py lint BUG_LEDGER.md
```

Then add the new failures to `local_blockers` (or as cut points), widen
the probe, and repeat. When you're done, send `BUG_LEDGER.md` to your dbt
Labs contact.

## Read next

| If you want to… | Read |
|---|---|
| understand what `dbt pull` fetches, how sampling works, auth | [docs/pull.md](docs/pull.md) |
| know how names and types change locally | [docs/source-schemas.md](docs/source-schemas.md) |
| set up the probe and blocker selectors, use cut points | [docs/frontiers-and-selectors.md](docs/frontiers-and-selectors.md) |
| tell a real bug from sampling or setup noise | [docs/reading-results.md](docs/reading-results.md) |
| generate and read paired unit tests | [docs/paired-unit-tests.md](docs/paired-unit-tests.md) |
| write, de-duplicate, and hand over the ledger | [docs/bug-ledger.md](docs/bug-ledger.md) |
| see a finished example | [examples/jaffle-dryrun/](examples/jaffle-dryrun/) |
| see what changed in the kit | [CHANGELOG.md](CHANGELOG.md) |

## Ground rules

1. Never change your models, macros, configs, or packages to make local
   compute pass. A local failure is the finding.
2. The warehouse is only read (`dbt pull`, `dbt show --compute remote`).
3. Count a value match only when a paired test passes on **both** sides.
   A test that fails on the warehouse is `broken`, never a match.
4. One entry per root cause, minimized, with made-up literals. No customer
   data in the ledger.
5. Nothing is filed on dbt Labs' repositories. Optionally,
   `lc_ledger.py open-issue` files an entry on a findings repo **you** name.

## License

[Apache-2.0](LICENSE)
