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
- **Suspected layer:** translation (aggregate empty-input semantics). Hypothesis, not verified.
- **Claim boundary:** Snowflake only; one binary; not checked against dbt Labs' tracker.
- **Affects:** fct_x, fct_y (+3)
- **Environment:** dbt 2.1.0 (2.1.0.dev20261019), snowflake, 2026-11-02
```

| Field | What goes in it |
|---|---|
| Status | `new` → `sent` (handed over), or `not-a-bug`, `duplicate`, `wont-report` |
| Signature | the `lc_compare.py` signature for errors, or a short `value: …` tag for mismatches. This is what de-duplicates |
| Found by | local run, paired test, or both, plus how many models hit it |
| Symptom | what a user sees, in one sentence |
| Warehouse result / Local result | the two answers, literally |
| Minimal repro | the smallest SQL that shows it, with made-up literal values, plus the commands |
| Suspected layer | where it probably goes wrong (below), marked as a hypothesis |
| Claim boundary | what this entry does *not* show (below) |
| Affects | the models in your project that hit it (names only) |
| Environment | `dbt --version`, adapter, date |
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

## Minimizing

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
