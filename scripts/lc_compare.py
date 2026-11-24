# /// script
# requires-python = ">=3.11"
# ///
"""Compare a local (`--compute local`) run against the warehouse and group
local failures into candidate bug families.

    uv run $KIT/scripts/lc_compare.py --local target/run_results.json \
        [--remote path/to/warehouse/run_results.json] [--ledger BUG_LEDGER.md]

Copy `target/run_results.json` aside after each run. The next dbt invocation
can overwrite it. A warehouse result must pass for the same node to label an
error as local-only. Without that evidence, confirm the local candidate with
`dbt show --compute remote --select <model>` before writing it up.

Outputs a markdown summary on stdout and, with `--json`, a machine-readable
copy. Signatures are normalized error text (identifiers, numbers, quoted
names and paths replaced) so that one root cause hitting fifty models shows
up as one family, and so a ledger entry's `Signature:` line can be matched.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

OK = {"success", "pass"}
FAILED = {"error", "fail", "failed", "runtime error"}
MEASURED = {"pass", "fail"}
CANDIDATE_CLASSES = {"local_only_failure", "unverified_local_failure"}
STATUS_ONLY_MESSAGES = OK | FAILED | {"", "none", "null", "skipped", "warn"}

NORMALIZERS = [
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"(?:[\w.-]+/)+[\w.-]+\.(?:sql|yml|yaml|py|csv)(?::\d+)*"), "<path>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r'"[^"]*"'), '"<id>"'),
    (re.compile(r"'[^']*'"), "'<val>'"),
    (re.compile(r"`[^`]*`"), "`<id>`"),
    (re.compile(r"\b[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*){1,2}\b"), "<relation>"),
    (re.compile(r"\b\d+(\.\d+)?\b"), "<n>"),
    (re.compile(r"\s+"), " "),
]
CODE_RE = re.compile(r"\((dbt\d{4})\)|\b(dbt\d{4})\b")


def signature(message: str) -> str:
    """The normalized text used to group similar error messages."""
    first = next((line for line in message.splitlines() if line.strip()), "")
    code = CODE_RE.search(message)
    text = first
    for pattern, replacement in NORMALIZERS:
        text = pattern.sub(replacement, text)
    text = text.strip()[:160]
    return f"{(code.group(1) or code.group(2)) if code else 'nocode'}: {text}"


def load(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError(f"{path} must contain a results array")
    results = {}
    for index, row in enumerate(data["results"]):
        uid = row.get("unique_id") if isinstance(row, dict) else None
        if not isinstance(uid, str) or not uid.strip():
            raise ValueError(f"{path} result {index + 1} has no valid unique_id")
        if uid in results:
            raise ValueError(f"{path} contains duplicate result {uid}")
        results[uid] = row
    return results


def kind(unique_id: str) -> str:
    return unique_id.split(".", 1)[0]


def outcome(result: dict | None) -> str:
    if result is None:
        return "missing"
    status = result.get("status")
    if not isinstance(status, str):
        return "unknown"
    status = status.strip().lower()
    if status in OK:
        return "pass"
    if status in FAILED:
        return "fail"
    if status in {"skipped", "warn"}:
        return status
    return "unknown"


def classify(uid: str, local: str, remote: str) -> str:
    if local == "missing":
        return "missing_local_result"
    if local == "pass":
        return {"pass": "pass", "fail": "remote_only_failure"}.get(remote, "local_pass_unverified")
    if local == "skipped":
        return "skipped_downstream"
    if local == "warn":
        return "local_warning"
    if local != "fail":
        return "unknown_local_outcome"
    if remote == "fail":
        return "both_fail"
    if kind(uid) == "test":
        return "local_test_failure" if remote == "pass" else "unverified_local_test_failure"
    return "local_only_failure" if remote == "pass" else "unverified_local_failure"


def compare(local: dict[str, dict], remote: dict[str, dict] | None) -> list[dict]:
    remote_rows = remote if remote is not None else {}
    rows = []
    for uid in sorted(local.keys() | remote_rows.keys()):
        local_result = local.get(uid)
        remote_result = remote_rows.get(uid)
        local_outcome = outcome(local_result)
        remote_outcome = outcome(remote_result) if remote is not None else "not_supplied"
        cls = classify(uid, local_outcome, remote_outcome)
        raw_message = local_result.get("message") if local_result is not None else None
        message = raw_message if isinstance(raw_message, str) else ""
        normalized_message = " ".join(message.strip().lower().rstrip(".:!").split())
        message_signature = None
        if cls in CANDIDATE_CLASSES and normalized_message not in STATUS_ONLY_MESSAGES:
            message_signature = signature(message)
        rows.append({
            "unique_id": uid,
            "class": cls,
            "local_status": local_result.get("status") if local_result is not None else None,
            "remote_status": remote_result.get("status") if remote_result is not None else None,
            "local_outcome": local_outcome,
            "remote_outcome": remote_outcome,
            "message": message[:4000],
            "signature": message_signature,
        })
    return rows


def model_coverage(rows: list[dict], remote_supplied: bool) -> dict:
    models = [row for row in rows if kind(row["unique_id"]) == "model"]
    local = collections.Counter(row["local_outcome"] for row in models)
    remote = collections.Counter(row["remote_outcome"] for row in models)
    return {
        "local_reported": len(models) - local["missing"],
        "local_passed": local["pass"],
        "local_failed": local["fail"],
        "local_skipped": local["skipped"],
        "local_other": local["warn"] + local["unknown"],
        "warehouse_reported": len(models) - remote["missing"] if remote_supplied else None,
        "local_only_reported": remote["missing"] if remote_supplied else None,
        "warehouse_only_reported": local["missing"] if remote_supplied else None,
        "both_measured": sum(
            row["local_outcome"] in MEASURED and row["remote_outcome"] in MEASURED for row in models
        ) if remote_supplied else None,
    }


def known_signatures(ledger: Path | None) -> dict[str, str]:
    if not ledger or not ledger.is_file():
        return {}
    known, current = {}, None
    for line in ledger.read_text().splitlines():
        heading = re.match(r"^##\s+(LC-\d+)", line)
        if heading:
            current = heading.group(1)
        sig = re.match(r"^\s*[-*]?\s*\**Signature(?:\*+:|:\**)[ \t]*`?(.+?)`?\s*$", line)
        if sig and current:
            known[sig.group(1).strip()] = current
    return known


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local", type=Path, required=True, help="run_results.json from the --compute local run")
    parser.add_argument("--remote", type=Path, help="run_results.json from a warehouse run of the same selection")
    parser.add_argument("--ledger", type=Path, help="BUG_LEDGER.md; families already recorded are marked")
    parser.add_argument("--json", type=Path, help="also write the full comparison here")
    args = parser.parse_args()

    try:
        local = load(args.local)
        remote = load(args.remote) if args.remote else None
    except (OSError, ValueError) as error:
        parser.error(str(error))
    known = known_signatures(args.ledger)
    rows = compare(local, remote)
    coverage = model_coverage(rows, remote_supplied=remote is not None)
    measured_locally = coverage["local_passed"] + coverage["local_failed"]
    counts = collections.Counter(r["class"] for r in rows)
    families: dict[str, list[dict]] = collections.defaultdict(list)
    no_diagnostic = []
    for r in rows:
        if r["signature"]:
            families[r["signature"]].append(r)
        elif r["class"] in CANDIDATE_CLASSES:
            no_diagnostic.append(r)

    out = [
        "# Local-compute run comparison\n",
        f"`models {coverage['local_passed']}/{measured_locally} pass locally | "
        f"{coverage['local_reported']} model results | "
        f"candidate families {len(families)} ({sum(s not in known for s in families)} new) | "
        f"both-fail {counts['both_fail']}`\n",
        f"Local model results include {coverage['local_skipped']} skipped and {coverage['local_other']} "
        "without a pass or fail status.\n",
    ]
    if remote is None:
        out.append("Warehouse outcomes are unverified. Confirm candidates against the warehouse.\n")
    else:
        out.append(
            f"Warehouse artifact `{args.remote}` contains {coverage['warehouse_reported']} model results. "
            f"Model results appear only locally for {coverage['local_only_reported']} and only on the "
            f"warehouse for {coverage['warehouse_only_reported']}. Both artifacts record a pass or fail "
            f"for {coverage['both_measured']} models.\n"
        )
    out += [
        "## Candidate families\n",
        "| # | warehouse passed | warehouse unverified | ledger | signature | example |",
        "|---|---|---|---|---|---|",
    ]
    ordered = sorted(families.items(), key=lambda kv: -len(kv[1]))
    for n, (sig, members) in enumerate(ordered, start=1):
        verified = sum(m["class"] == "local_only_failure" for m in members)
        out.append(
            f"| {n} | {verified} | {len(members) - verified} | {known.get(sig, 'new')} | "
            f"`{sig.replace('|', '/')}` | {members[0]['unique_id']} |"
        )
    for n, (sig, members) in enumerate(ordered, start=1):
        out += [f"\n### {n}. {members[0]['unique_id']} (+{len(members) - 1} more)\n", "```", members[0]["message"][:1500], "```"]
        if len(members) > 1:
            out.append("Also: " + ", ".join(m["unique_id"].rsplit(".", 1)[-1] for m in members[1:25])
                       + (" ..." if len(members) > 25 else ""))
    if no_diagnostic:
        out += [f"\n## Local failures without a diagnostic ({len(no_diagnostic)})\n"]
        out += [f"- {r['unique_id']} (warehouse {r['remote_outcome']})" for r in no_diagnostic[:50]]
    sections = {
        "local_test_failure": "Data tests that fail only locally (check sampling first)",
        "unverified_local_test_failure": "Local data test failures with unverified warehouse outcomes",
        "local_warning": "Local warnings",
        "remote_only_failure": "Fail on the warehouse, pass locally",
        "missing_local_result": "Warehouse results without a local result",
        "unknown_local_outcome": "Local results with an unknown outcome",
    }
    for cls, title in sections.items():
        if counts[cls]:
            out.append(f"\n## {title} ({counts[cls]})\n")
            out += [f"- {r['unique_id']}" for r in rows if r["class"] == cls][:50]
    print("\n".join(out))
    if args.json:
        report = {
            "counts": counts,
            "model_coverage": coverage,
            "families": {s: [m["unique_id"] for m in ms] for s, ms in ordered},
            "rows": rows,
        }
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
