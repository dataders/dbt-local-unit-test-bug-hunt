# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "pyyaml>=6"]
# ///
"""Offline tests for the kit's scripts: uv run --with pytest --with pyyaml pytest scripts/test_kit.py"""

from __future__ import annotations

import decimal
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import lc_compare  # noqa: E402
import lc_fixtures as fx  # noqa: E402
import lc_ledger  # noqa: E402

# Shape of Fusion's compiled unit-test SQL (trimmed from a real Snowflake run).
COMPILED = textwrap.dedent("""\
    -- Build actual result given inputs
    WITH
      "db_s_stg_orders" as (SELECT TRY_CAST('1' AS VARCHAR) AS ORDER_ID),
      "db_s_orders_expect" as (SELECT TRY_CAST('1' AS VARCHAR) AS ORDER_ID),
      "db_s_orders_actual" as (with orders as (select * from "db_s_stg_orders") select * from orders
    )
            SELECT * FROM (
            (SELECT ORDER_ID, 'actual' AS actual_or_expected FROM "db_s_orders_actual")
            UNION ALL
            (SELECT ORDER_ID, 'expected' AS actual_or_expected FROM "db_s_orders_expect")
            ) unit_test_diff
            ORDER BY ORDER_ID""")


def test_actual_query_keeps_ctes_and_selects_actual():
    sql = fx.actual_query(COMPILED)
    assert sql.rstrip().endswith('select * from "db_s_orders_actual"')
    assert "unit_test_diff" not in sql
    assert '"db_s_orders_actual" as (' in sql


def test_projection_drops_superset_keys_and_fills_missing():
    schema = [["ID", "varchar"], ["AMOUNT", "NUMBER(16,2)"], ["NOTE", "varchar"]]
    rows = [{"id": "a", "amount": {"__decimal__": "7.42"}, "EXTRA_COL": 1}]
    projected, note = fx.project_rows(rows, schema, anonymize_pii=False)
    assert projected == [{"ID": "a", "AMOUNT": decimal.Decimal("7.42"), "NOTE": None}]
    assert note["dropped_row_keys"] == ["EXTRA_COL"]


def test_rows_sql_casts_to_exact_types():
    schema = [["ID", "varchar"], ["AMOUNT", "NUMBER(16,2)"], ["IS_X", "BOOLEAN"]]
    sql = fx.rows_sql([{"ID": "o'k", "AMOUNT": decimal.Decimal("7.42"), "IS_X": True}], schema)
    assert "cast('o''k' as varchar) as ID" in sql
    assert "cast('7.42' as NUMBER(16,2)) as AMOUNT" in sql
    assert "cast(true as BOOLEAN) as IS_X" in sql
    empty = fx.rows_sql([], schema)
    assert empty.endswith("where 1 = 0") and "cast(null as NUMBER(16,2)) as AMOUNT" in empty


def test_semi_structured_and_unsupported_types():
    assert fx.literal({"a": 1}, "VARIANT") == "cast(parse_json('{\"a\": 1}') as VARIANT)"
    rows, note = fx.project_rows([{"G": "POINT(1 2)"}], [["G", "GEOGRAPHY"]], anonymize_pii=False)
    assert rows == [{"G": None}] and note["nulled_unsupported"] == ["G"]


def test_identifier_quoting():
    assert fx.identifier("ORDER_ID") == "ORDER_ID"
    assert fx.identifier('my "col"') == '"my ""col"""'


def test_refuses_non_snowflake_projects(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"metadata": {"project_name": "p", "adapter_type": "bigquery"}, "nodes": {}}')
    monkeypatch.setattr(sys, "argv", ["lc_fixtures.py", "inventory", "--manifest", str(manifest)])
    assert fx.main() == 2
    assert "Snowflake projects only" in capsys.readouterr().err


def test_anonymize_only_pii_columns_and_is_stable():
    assert fx.anonymize("STATUS", "shipped") == "shipped"
    assert fx.anonymize("CUSTOMER_EMAIL", "a@b.com").endswith("@example.invalid")
    assert fx.anonymize("FIRST_NAME", "Ada") == fx.anonymize("FIRST_NAME", "Ada") != "Ada"


def test_overrides_incremental_and_used_introspection_only():
    case = fx.Case("model.p.m", "m", None, "incremental", "m.sql", [], ["macro.dbt_utils.get_column_values"])
    overrides = fx.macro_overrides(case, {})
    assert overrides["is_incremental"] is False
    assert overrides["dbt_utils.get_column_values"] == []
    assert "dbt.run_query" not in overrides


def test_nondeterministic_detection():
    case = fx.Case("model.p.m", "m", None, "table", "m.sql", [], [])
    node = {"raw_code": "select current_timestamp() as loaded_at", "language": "sql"}
    assert "nondeterministic" in fx.set_aside_reason(node, case, {}, False)
    assert fx.set_aside_reason({"raw_code": "select 1 as current_total"}, case, {}, False) is None


def test_classify_paired():
    ok, fail = {"status": "pass"}, {"status": "error", "diff": "a -> b"}
    assert fx.classify(ok, ok, empty_expect=False) == "match"
    assert fx.classify(ok, ok, empty_expect=True) == "match_empty"
    assert fx.classify(ok, fail, False) == "mismatch"
    assert fx.classify(ok, {"status": "error", "diff": None}, False) == "local_error"
    assert fx.classify({"status": "error"}, ok, False) == "broken"


def test_failure_blocks_parse_fusion_output():
    out = "x\nTest failed: lc_kit__m__f1\nFAIL db.s.lc_kit__m__f1\n| A | B -> C |\n\n=== Execution Summary ===\n"
    assert fx.failure_blocks(out) == {"lc_kit__m__f1": "FAIL db.s.lc_kit__m__f1\n| A | B -> C |"}


def test_signature_groups_same_root_cause():
    a = lc_compare.signature("Table 'RAW_A' does not exist in schema DEV.SCHEMA_X")
    b = lc_compare.signature("Table 'RAW_B' does not exist in schema PROD.OTHER")
    assert a == b
    c = lc_compare.signature("[error] [Generic (dbt1000)]: Scalar Function with name strtok does not exist!")
    assert c.startswith("dbt1000:")


def test_ledger_lint_and_guard(tmp_path):
    ledger = tmp_path / "BUG_LEDGER.md"
    entry = textwrap.dedent("""\
        ## LC-00{n}: thing
        - **Status:** new
        - **Signature:** `value: same`
        - **Symptom:** s
        - **Warehouse result:** a
        - **Local result:** b
        - **Minimal repro:**
          ```sql
          select 1
          ```
        - **Suspected layer:** unknown
        - **Claim boundary:** c
        - **Environment:** e
        """)
    ledger.write_text("# L\n\n" + entry.format(n=1) + "\n" + entry.format(n=2))
    assert lc_ledger.lint(ledger) == 1  # duplicate signature
    assert lc_ledger.open_issue(ledger, "LC-001", "dbt-labs/anything", yes=True) == 2


def test_payload_has_remote_and_local_copies():
    case = fx.Case("model.p.m", "m", None, "incremental", "m.sql", [fx.Input("source.p.s.t", "source('s', 't')", "source")], [])
    cache = {"schemas": {"source.p.s.t": [["ID", "varchar"]]}, "samples": {"source.p.s.t": [{"ID": "a"}]}}
    payload, _ = fx.test_payload(case, cache, {}, anonymize_pii=False)
    tests = payload["unit_tests"]
    assert [t["name"] for t in tests] == ["lc_kit__m__f2__remote", "lc_kit__m__f2__local"]
    assert [t["config"]["compute"] for t in tests] == ["remote", "local"]
    assert tests[0]["given"] == tests[1]["given"]
    assert all(t["overrides"]["macros"]["is_incremental"] is False for t in tests)


def test_static_analysis_off_is_set_aside():
    case = fx.Case("model.p.m", "m", None, "table", "m.sql", [], [])
    node = {"raw_code": "select 1 as x", "config": {"static_analysis": "off"}}
    assert "static_analysis" in fx.set_aside_reason(node, case, {}, False)
