# Frontiers, the probe, and the blocker ledger

> **Experimental, beyond the public docs.** The public dbt docs cover local
> compute for **unit tests** only ([`compute` config](https://docs.getdbt.com/reference/resource-configs/compute),
> [running unit tests locally](https://docs.getdbt.com/docs/build/unit-tests#run-unit-tests-locally)).
> Running a whole selection with `dbt run --compute local`, and `dbt pull`, are not covered there. What this page describes is what the kit's
> authors observed on one Fusion build. It can change without notice, so check
> it on yours.

Two named selectors drive the whole hunt:

* **`local_dag`**, the **probe**: everything you want to run locally.
* **`local_blockers`**, the **blocker ledger**: what is known to fail
  locally, each entry with a reason. The probe is "your DAG minus the
  blockers".

Each round, you run the probe and record the new failures. Their subtrees
then go into the blockers, so the next run gets further. The blocker list
is your running to-do list. When Fusion fixes something, the entry's
`REMOVE:` condition tells you to delete it and re-run.

## Setting them up

Selectors live in `selectors.yml` at your project root. Copy the entries
from [`../templates/selectors.lc-kit.yml`](../templates/selectors.lc-kit.yml)
under its `selectors:` key (create the file with a top-level `selectors:`
if you have none).

This is the **one edit** the kit makes to your project. Keep it out of your
commits: work on a scratch branch or a separate checkout (`git worktree add
../myproject-lc`), or `git stash` it afterwards. Never "fix" a model, macro,
config, or package so that it passes locally. A failure caused by your
project's code is either a local-compute bug (record it) or a real project
bug. A real project bug goes into a normal PR to your project, not into the
hunt.

```yaml
selectors:
  - name: local_blockers
    definition:
      union:
        # WHY:    dbt1401 "static analysis produced no logical plan" for this model.
        # LINK:   LC-004
        # REMOVE: when `dbt run --compute local -s fct_sessions` builds.
        - 'fct_sessions+'

        # WHY:    calls SNOWFLAKE.CORTEX.COMPLETE, which has no local equivalent.
        # LINK:   none (warehouse-only by design)
        # REMOVE: never
        - 'fct_ticket_summaries+'

  - name: local_dag
    definition:
      union:
        - 'fqn:*'
        - exclude:
            - method: selector
              value: local_blockers
            - 'resource_type:seed'
```

Start the probe small if your project is large: `'models/staging+'`, or a
single domain (`'+fct_orders'`). Widen it once staging runs clean.

Check what the probe selects before you pull:

```bash
dbt ls --selector local_dag --resource-type model | wc -l
dbt pull --selector local_dag --exclude resource_type:test --sample 1000 --preview
```

`fqn:*` matches sources as well as models. So the source of a model you
blocked can still appear in the pull preview. That is harmless (one extra
sampled table). If `pull --selector` ever selects something clearly
different from `ls --selector`, mirror the exclusions on the pull with
explicit `--exclude` arguments, and record the difference (layer `pull`).

## The entry convention

Every blocker gets three comment lines, directly above it:

| Line | Says |
|---|---|
| `# WHY:` | the symptom (error code + message) and the cause, if known |
| `# LINK:` | the ledger entry (`LC-00n`), an upstream issue if one exists, or `none yet` |
| `# REMOVE:` | the condition under which the entry can be deleted |

A trailing `+` (`'model_x+'`) blocks the model **and its descendants**. They
would only be skipped anyway, and skipping them keeps the run summary
readable.

Seed `local_blockers` only with models that are genuinely out of scope
(warehouse-only functions such as ML/LLM UDFs, sources your role cannot
read). Everything else should be discovered by running.

## Cut points

Blocking `model_x+` gives up on everything downstream of `model_x`. A **cut
point** gives up on `model_x` alone. Exclude it *without* `+`, and use the
same selection for pull and run. `model_x` is then outside the selection but
read by its children, which puts it on the frontier. Pull fetches its
already-built table from the warehouse, and its children run locally on top
of it:

```bash
# model_x fails locally; its children should still be exercised
dbt pull --selector local_dag --exclude model_x --exclude resource_type:test --sample 1000
dbt run  --selector local_dag --exclude model_x --compute local
```

Once you have several, keep them as a selector (`local_cut_points`, entries
without `+`, excluded from `local_dag` alongside the blockers). For a
one-off, `dbt pull --select model_x --exact` fetches just that model's built
output. Use cut points once the first wave of failures is recorded: they turn
one failure from "hides 200 models" into "hides 1".

## The frontier, precisely

For a selection `S`, the frontier is the set of relations `S` reads but does
not build: sources, models outside `S`, incremental `{{ this }}`, and snapshot
state. `dbt pull -s S` fetches the frontier. `dbt pull -s S --exact` fetches
`S` itself. Every local run can only be as good as its frontier. If a model
reads a relation that was never pulled, it fails with
`Table ... does not exist`, which is a setup problem. Re-pull with the same
selection you run.
