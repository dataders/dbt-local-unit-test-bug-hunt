"""Synthetic run_results checks for the offline comparison CLI."""

import json
import sys

import lc_compare
import pytest


def run_results(path, *results):
    path.write_text(json.dumps({"results": results}), encoding="utf-8")
    return path


def run_compare(tmp_path, monkeypatch, capsys, local, *args):
    report_path = tmp_path / "comparison.json"
    argv = ["lc_compare.py", "--local", str(local), "--json", str(report_path), *map(str, args)]
    monkeypatch.setattr(sys, "argv", argv)
    assert lc_compare.main() == 0
    return json.loads(report_path.read_text(encoding="utf-8")), capsys.readouterr().out


def result(name, status, message=None, kind="model"):
    return {"unique_id": f"{kind}.example.{name}", "status": status, "message": message}


def test_mixed_scope_requires_warehouse_pass_for_local_only(tmp_path, monkeypatch, capsys):
    error = "Example diagnostic"
    local = run_results(
        tmp_path / "local.json",
        result("local_failed", "error", error),
        result("both_failed", "error", error),
        result("remote_skipped", "error", error),
        result("remote_warning", "error", error),
        result("remote_unknown", "error", error),
        result("remote_missing", "error", error),
        result("local_skipped", "skipped"),
        result("local_unknown", "incomplete"),
        result("local_passed", "success"),
        result("example_seed", "success", kind="seed"),
        result("relationship", "fail", error, kind="test"),
        result("freshness", "warn", error, kind="test"),
    )
    remote = run_results(
        tmp_path / "remote.json",
        result("local_failed", "success"),
        result("both_failed", "failed"),
        result("remote_skipped", "skipped"),
        result("remote_warning", "warn"),
        result("remote_unknown", "unsupported"),
        result("local_skipped", "success"),
        result("local_unknown", "success"),
        result("local_passed", "success"),
        result("local_missing", "error"),
        result("relationship", "pass", kind="test"),
        result("freshness", "pass", kind="test"),
    )
    report, markdown = run_compare(tmp_path, monkeypatch, capsys, local, "--remote", remote)
    rows = {row["unique_id"].rsplit(".", 1)[-1]: row for row in report["rows"]}
    assert rows["local_failed"]["class"] == "local_only_failure"
    assert rows["both_failed"]["class"] == "both_fail"
    for name, outcome in (
        ("remote_skipped", "skipped"),
        ("remote_warning", "warn"),
        ("remote_unknown", "unknown"),
        ("remote_missing", "missing"),
    ):
        assert rows[name]["class"] == "unverified_local_failure"
        assert rows[name]["remote_outcome"] == outcome
    assert rows["local_missing"]["class"] == "missing_local_result"
    assert rows["local_missing"]["local_outcome"] == "missing"
    assert rows["local_skipped"]["class"] == "skipped_downstream"
    assert rows["local_unknown"]["class"] == "unknown_local_outcome"
    assert rows["relationship"]["class"] == "local_test_failure"
    assert rows["freshness"]["class"] == "local_warning"
    assert report["model_coverage"] == {
        "local_reported": 9,
        "local_passed": 1,
        "local_failed": 6,
        "local_skipped": 1,
        "local_other": 1,
        "warehouse_reported": 9,
        "local_only_reported": 1,
        "warehouse_only_reported": 1,
        "both_measured": 3,
    }
    assert "models 1/7 pass locally | 9 model results" in markdown
    assert "| 1 | 1 | 4 | new | `nocode: Example diagnostic` |" in markdown


@pytest.mark.parametrize("field", ["**Signature:**", "**Signature**:", "Signature:"])
def test_local_only_keeps_ledger_signature_but_skips_blank_diagnostics(tmp_path, monkeypatch, capsys, field):
    local = run_results(
        tmp_path / "local.json",
        result("error", "error", "Example diagnostic"),
        result("empty", "error", " \n"),
        result("passed", "success"),
    )
    ledger = tmp_path / "BUG_LEDGER.md"
    ledger.write_text(f"## LC-001: Example\n- {field} `nocode: Example diagnostic`\n", encoding="utf-8")
    report, markdown = run_compare(tmp_path, monkeypatch, capsys, local, "--ledger", ledger)
    assert report["counts"] == {"local_pass_unverified": 1, "unverified_local_failure": 2}
    assert report["model_coverage"]["both_measured"] is None
    assert {row["remote_outcome"] for row in report["rows"]} == {"not_supplied"}
    assert report["families"] == {"nocode: Example diagnostic": ["model.example.error"]}
    assert "Warehouse outcomes are unverified." in markdown
    assert "| 1 | 0 | 1 | LC-001 |" in markdown
    assert "Local failures without a diagnostic (1)" in markdown


def test_empty_warehouse_artifact_is_distinct_from_absent(tmp_path, monkeypatch, capsys):
    local = run_results(tmp_path / "local.json", result("local_passed", "success"))
    remote = run_results(tmp_path / "remote.json")
    report, _ = run_compare(tmp_path, monkeypatch, capsys, local, "--remote", remote)
    assert report["rows"][0]["remote_outcome"] == "missing"
    assert report["model_coverage"]["warehouse_reported"] == 0
    assert report["model_coverage"]["both_measured"] == 0


@pytest.mark.parametrize("message", ["Error", " failed ", "Runtime  error:", "none", "NULL", None])
def test_failed_status_with_status_only_message(tmp_path, monkeypatch, capsys, message):
    local = run_results(
        tmp_path / "local.json",
        result("generic", "failed", message),
        result("detailed", "failed", "Error: Example diagnostic"),
    )
    remote = run_results(tmp_path / "remote.json", result("generic", "success"), result("detailed", "success"))
    report, markdown = run_compare(tmp_path, monkeypatch, capsys, local, "--remote", remote)
    assert report["counts"] == {"local_only_failure": 2}
    assert report["families"] == {"nocode: Error: Example diagnostic": ["model.example.detailed"]}
    assert "Local failures without a diagnostic (1)" in markdown
    assert "model.example.generic (warehouse pass)" in markdown


@pytest.mark.parametrize("data, reason", [
    ({}, "must contain a results array"),
    ({"results": [{}]}, "has no valid unique_id"),
    ({"results": [result("same", "error"), result("same", "pass")]}, "contains duplicate result"),
])
def test_rejects_invalid_result_artifact(tmp_path, monkeypatch, capsys, data, reason):
    local = tmp_path / "local.json"
    local.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["lc_compare.py", "--local", str(local)])
    with pytest.raises(SystemExit) as error:
        lc_compare.main()
    assert error.value.code == 2
    assert reason in capsys.readouterr().err
