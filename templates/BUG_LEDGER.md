# Local-compute bug ledger: <project name>

<!--
One entry per ROOT CAUSE, not per model. Few, well-minimized entries.
Schema and guidance: docs/bug-ledger.md. Check: uv run $KIT/scripts/lc_ledger.py lint BUG_LEDGER.md
Do not paste real customer data: minimal repros use made-up literals.
-->

- **Project:** <name> (<warehouse>), <n> models under the probe
- **Fusion:** `dbt --version` → <version>
- **Hunt dates:** <yyyy-mm-dd> to <yyyy-mm-dd>
- **Headline:** `models <passing>/<total> pass locally | paired <match>/<warehouse-pass> (<broken> broken) | <n> entries`
- **Contact:** <you>

## LC-001: <one-line summary: construct, what differs>

- **Status:** new
- **Signature:** `<lc_compare signature, or value: short tag>`
- **Found by:** <local run | paired unit test <name>>; <n> models
- **Symptom:** <one sentence a user would recognize>
- **Warehouse result:** <literal>
- **Local result:** <literal or error>
- **Minimal repro:**
  ```sql
  -- models/lc_repro.sql
  select ...
  ```
  `dbt show --select lc_repro --compute remote` vs `--compute local`
- **Kind:** <dialect (one SQL statement shows it) | framework (needs a dbt project)>
- **Suspected layer:** <pull | compile | translation | execution | macros | unknown>. Hypothesis: <why>
- **Claim boundary:** <what this does not show>
- **Affects:** <model names>
- **Environment:** <dbt version>, <adapter>, <date>; session TZ <zone>, host TZ <zone>; threads <n>; sample <spec>; pass <n>
