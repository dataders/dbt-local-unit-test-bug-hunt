# /// script
# requires-python = ">=3.11"
# ///
"""Check the bug ledger, and optionally file one entry as an issue on a repo
*you* configure.

    uv run $KIT/scripts/lc_ledger.py lint BUG_LEDGER.md
    uv run $KIT/scripts/lc_ledger.py next-id BUG_LEDGER.md
    uv run $KIT/scripts/lc_ledger.py open-issue BUG_LEDGER.md --entry LC-002 --repo my-org/lc-findings [--yes]

`lint` enforces the entry schema (see docs/bug-ledger.md) and flags
duplicate signatures, the most common way a ledger fills with symptoms
instead of root causes.

`open-issue` never targets a dbt-labs/* repository: the ledger is handed to a human at
dbt Labs, who decides what goes upstream. Without `--yes` it only prints the
`gh` command it would run. With `--yes` it searches the repo for the entry id
first and refuses to file a duplicate.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REQUIRED = [
    "Status", "Signature", "Symptom", "Warehouse result", "Local result",
    "Minimal repro", "Suspected layer", "Claim boundary", "Environment",
]
STATUSES = {"new", "sent", "not-a-bug", "duplicate", "wont-report"}
FORBIDDEN_OWNERS = {"dbt-labs"}
HEADING = re.compile(r"^##\s+(LC-\d{3,})\s*:\s*(.+?)\s*$")
FIELD = re.compile(r"^\s*[-*]\s+\*\*([^*:]+):\*\*\s*(.*)$")


def parse(path: Path) -> list[dict]:
    entries, current = [], None
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        heading = HEADING.match(line)
        if heading:
            current = {"id": heading.group(1), "title": heading.group(2), "line": number, "fields": {}, "body": []}
            entries.append(current)
            continue
        if line.startswith("## ") and current is not None:
            current = None  # a non-entry section (e.g. "## Notes") ends the entry
            continue
        if current is None:
            continue
        current["body"].append(line)
        field = FIELD.match(line)
        if field:
            current["fields"][field.group(1).strip()] = field.group(2).strip()
    return entries


def lint(path: Path) -> int:
    entries = parse(path)
    problems = []
    seen_ids, seen_sigs = {}, {}
    for e in entries:
        where = f"{path}:{e['line']} {e['id']}"
        if e["id"] in seen_ids:
            problems.append(f"{where}: duplicate id (also line {seen_ids[e['id']]})")
        seen_ids[e["id"]] = e["line"]
        for name in REQUIRED:
            if not e["fields"].get(name) and name != "Minimal repro":
                problems.append(f"{where}: missing **{name}:**")
        if "**Minimal repro:**" not in "\n".join(e["body"]) or "```" not in "\n".join(e["body"]):
            problems.append(f"{where}: Minimal repro needs a fenced code block")
        status = e["fields"].get("Status", "").split()[0].strip("`").lower() if e["fields"].get("Status") else ""
        if status and status not in STATUSES:
            problems.append(f"{where}: Status '{status}' not one of {sorted(STATUSES)}")
        sig = e["fields"].get("Signature", "").strip("` ")
        if sig and status not in {"duplicate", "not-a-bug"}:
            if sig in seen_sigs:
                problems.append(f"{where}: same Signature as {seen_sigs[sig]}; merge them (one entry per root cause)")
            seen_sigs[sig] = e["id"]
    open_count = sum(1 for e in entries if e["fields"].get("Status", "").lower().startswith(("new", "sent")))
    print(f"{len(entries)} entries ({open_count} new/sent), {len(problems)} problem(s)")
    for p in problems:
        print("  " + p)
    return 1 if problems else 0


def next_id(path: Path) -> int:
    ids = [int(e["id"].split("-")[1]) for e in parse(path)] if path.is_file() else []
    print(f"LC-{(max(ids) + 1 if ids else 1):03d}")
    return 0


def open_issue(path: Path, entry_id: str, repo: str, yes: bool) -> int:
    if repo.lower().split("/", 1)[0] in FORBIDDEN_OWNERS:
        print(f"refusing: {repo} is not a target for this kit; hand the ledger to dbt Labs instead", file=sys.stderr)
        return 2
    entry = next((e for e in parse(path) if e["id"] == entry_id), None)
    if entry is None:
        print(f"no entry {entry_id} in {path}", file=sys.stderr)
        return 2
    title = f"[{entry_id}] {entry['title']}"
    body = "\n".join(entry["body"]).strip() + "\n\n_Filed from a local-compute bug ledger._\n"
    command = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    if not yes:
        print("would run (add --yes to file):")
        print(f"  gh issue create --repo {repo} --title {title!r} --body <entry {entry_id}, {len(body)} chars>")
        return 0
    found = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "all", "--search", f"{entry_id} in:title", "--json", "url"],
        capture_output=True, text=True,
    )
    if found.returncode:
        print(found.stderr, file=sys.stderr)
        return found.returncode
    if found.stdout.strip() not in {"", "[]"}:
        print(f"already filed: {found.stdout.strip()}")
        return 0
    created = subprocess.run(command, capture_output=True, text=True)
    print(created.stdout or created.stderr)
    return created.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("lint", "next-id"):
        sub.add_parser(name).add_argument("ledger", type=Path)
    issue = sub.add_parser("open-issue")
    issue.add_argument("ledger", type=Path)
    issue.add_argument("--entry", required=True)
    issue.add_argument("--repo", required=True, help="owner/name of YOUR findings repo (never dbt-labs/*)")
    issue.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if args.command == "lint":
        return lint(args.ledger)
    if args.command == "next-id":
        return next_id(args.ledger)
    return open_issue(args.ledger, args.entry, args.repo, args.yes)


if __name__ == "__main__":
    sys.exit(main())
