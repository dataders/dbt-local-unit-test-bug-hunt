# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "pyyaml>=6"]
# ///
"""Offline tests for the kit's scripts: uv run --with pytest --with pyyaml pytest scripts/test_kit.py"""

from __future__ import annotations

import decimal
import hashlib
import json
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
    for status in (None, "skipped", "warn", "partial success", "anything else"):
        assert fx.classify(ok, {"status": status}, False) == "unknown"
        assert fx.classify({"status": status}, ok, False) == "unknown"
    assert fx.classify(ok, None, False) == "unknown"


def test_verify_requires_harvest_and_retains_fresh_evidence(tmp_path, monkeypatch):
    args = fx.parser().parse_args([
        "verify", "--work-dir", str(tmp_path / "work"), "--tests-dir", str(tmp_path / "tests"),
    ])
    cases = [
        fx.Case(f"model.sample.{name}", name, None, "table", f"{name}.sql", [], [])
        for name in ("matched", "empty", "skipped", "unharvested", "edited", "error")
    ]
    cases.append(fx.Case("model.sample.versioned.v2", "versioned", 2, "table", "versioned_v2.sql", [], []))
    monkeypatch.setattr(fx, "selected_cases", lambda _: cases)
    harvest = {}
    for case in cases:
        payload, _ = fx.test_payload(case, {}, {}, False)
        if case.name in {"matched", "edited", "versioned"}:
            for unit_test in payload["unit_tests"]:
                unit_test["expect"] = {"rows": [{"id": 1}]}
        path = args.tests_dir / case.filename
        fx.write_yaml(path, payload)
        if case.name != "unharvested":
            harvest[case.unique_id] = hashlib.sha256(path.read_bytes()).hexdigest()
        if case.name == "edited":
            with path.open("a") as file:
                file.write("# edited after harvest\n")
    fx.write_report(args, "harvest-report.json", {"fixture_sha256": harvest})

    def fake_dbt(_, argv, label, *, log_dir):
        names = argv[argv.index("--select") + 1:argv.index("--target-path")]
        assert all("unharvested" not in name and "edited" not in name for name in names)
        target = Path(argv[argv.index("--target-path") + 1])
        target.mkdir()
        results = [
            {"unique_id": f"unit_test.sample.{case.name}.{case.side(side)}", "status": "pass"}
            for case in cases if case.name in {"matched", "empty", "skipped", "error"}
            for side in ("remote", "local")
        ]
        versioned = cases[-1]
        results += [
            {"unique_id": f"unit_test.sample.versioned.{versioned.side(side)}.v2", "status": "pass"}
            for side in ("remote", "local")
        ]
        results.append({"unique_id": f"unit_test.sample.versioned.{versioned.side('local')}.v1", "status": "error"})
        for result in results:
            uid = result["unique_id"]
            if ".skipped." in uid and uid.endswith("__local"):
                result["status"] = "skipped"
            if ".error." in uid and uid.endswith("__local"):
                result.update(status="error", message="Error")
        # Same name in a dependency package cannot supply the local result.
        results.append({"unique_id": f"unit_test.dependency.skipped.{cases[2].side('local')}", "status": "pass"})
        (target / "run_results.json").write_text(json.dumps({"metadata": {"invocation_id": "synthetic-1"}, "results": results}))
        (log_dir / f"{label}.log").write_text("dbt test\n")
        return fx.Run(argv, 1, 0.1, "[error] nearby model had a compilation error\n", "")

    monkeypatch.setattr(fx, "run_dbt", fake_dbt)
    assert fx.command_verify(args) == 0
    report = json.loads((args.work_dir / "paired-report.json").read_text())
    assert report["counts"] == {"match": 2, "match_empty": 1, "unknown": 3, "local_error": 1}
    assert report["warehouse_pass"] == 5
    assert report["local_errors_without_diagnostic"] == 1
    first = Path(report["run_dir"])
    artifact = (first / "run_results.json").read_bytes()
    assert report["execution"]["run_results_sha256"] == hashlib.sha256(artifact).hexdigest()
    assert report["execution"]["invocation_id"] == "synthetic-1"
    assert (first / "verify.log").exists()

    def no_artifact(_, argv, label, *, log_dir):
        return fx.Run(argv, 124, 0.1, "", "")

    monkeypatch.setattr(fx, "run_dbt", no_artifact)
    assert fx.command_verify(args) == 1
    report = json.loads((args.work_dir / "paired-report.json").read_text())
    assert report["counts"] == {"unknown": 7}
    assert report["execution"]["error"] == "dbt did not write run_results.json"
    assert Path(report["run_dir"]) != first
    assert (first / "run_results.json").read_bytes() == artifact


def test_harvest_records_empty_oracle_and_does_not_compile_empty_selection(tmp_path, monkeypatch):
    args = fx.parser().parse_args([
        "harvest", "--work-dir", str(tmp_path / "work"), "--tests-dir", str(tmp_path / "tests"),
    ])
    case = fx.Case("model.sample.empty", "empty", None, "table", "empty.sql", [], [])
    monkeypatch.setattr(fx, "selected_cases", lambda _: [case])
    assert fx.command_harvest(args) == 1

    path = args.tests_dir / case.filename
    payload, _ = fx.test_payload(case, {}, {}, False)
    fx.write_yaml(path, payload)
    target = tmp_path / "compile"
    compiled = target / "compiled" / f"{case.side('remote')}.sql"
    compiled.parent.mkdir(parents=True)
    compiled.write_text(COMPILED)
    monkeypatch.setattr(fx, "compile_tests", lambda *_: target)
    monkeypatch.setattr(fx, "show", lambda *_, **__: [])
    assert fx.command_harvest(args) == 0
    report = fx.read_harvest_report(args)
    assert report["empty_expect"] == [case.unique_id]
    assert report["fixture_sha256"][case.unique_id] == hashlib.sha256(path.read_bytes()).hexdigest()


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
    assert [t["name"] for t in tests] == ["lc_kit__m__f3__remote", "lc_kit__m__f3__local"]
    assert [t["config"]["compute"] for t in tests] == ["remote", "local"]
    assert tests[0]["given"] == tests[1]["given"]
    assert all(t["overrides"]["macros"]["is_incremental"] is False for t in tests)


def test_static_analysis_off_is_set_aside():
    case = fx.Case("model.p.m", "m", None, "table", "m.sql", [], [])
    node = {"raw_code": "select 1 as x", "config": {"static_analysis": "off"}}
    assert "static_analysis" in fx.set_aside_reason(node, case, {}, False)


def test_anonymize_spares_non_person_name_columns():
    for column in ("EVENT_NAME", "PLAN_NAME", "FIRST_SEEN_AT", "LAST_UPDATED_AT", "STATUS"):
        assert fx.anonymize(column, "x") == "x", column
    for column in ("NAME", "FIRST_NAME", "CUSTOMER_NAME", "USERNAME", "BILLING_CITY", "IP_ADDRESS"):
        assert fx.anonymize(column, "x") != "x", column


def test_values_equal_to_sql_literals_are_kept():
    node = {"raw_code": "select * from {{ ref('a') }} where contact_name in ('Ada', 'O''Brien')"}
    literals = fx.sql_literals(node["raw_code"])
    assert {"Ada", "O'Brien", "a"} <= set(literals)
    rows, _ = fx.project_rows([{"CONTACT_NAME": "Ada"}, {"CONTACT_NAME": "Bob"}],
                              [["CONTACT_NAME", "varchar"]], anonymize_pii=True, keep=frozenset(literals))
    assert rows[0]["CONTACT_NAME"] == "Ada" and rows[1]["CONTACT_NAME"].startswith("anon_")


def test_nondeterminism_in_compiled_code_is_set_aside():
    case = fx.Case("model.p.m", "m", None, "table", "m.sql", [], [])
    node = {"raw_code": "select {{ days_since('d') }} as n", "compiled_code": "select datediff(day, d, current_date()) as n"}
    assert "nondeterministic" in fx.set_aside_reason(node, case, {}, False)


def test_expect_drops_unrepresentable_and_requested_columns():
    case = fx.Case("model.p.fct_x", "fct_x", None, "table", "m.sql", [], [])
    rows = [{"ID": 1, "NOTE": "", "DAYS_SINCE": 3}, {"ID": 2, "NOTE": "ok", "DAYS_SINCE": 4}]
    drop = fx.expect_columns_to_drop(case, rows, ["fct_x.days_since", "other.ID"])
    assert set(drop) == {"NOTE", "DAYS_SINCE"}
    assert fx.expect_columns_to_drop(case, [{"ID": 1}], []) == {}


def test_backslashes_survive_snowflake_string_escapes():
    assert fx.sql_string('{"a": "say \\"hi\\""}') == "'{\"a\": \"say \\\\\"hi\\\\\"\"}'"
    assert fx.literal('{"p": "C:\\\\x"}', "VARIANT").count("\\\\\\\\") == 1
