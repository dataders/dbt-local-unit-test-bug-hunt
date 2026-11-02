# /// script
# requires-python = ">=3.11"
# ///
"""Compare a local (`--compute local`) run against the warehouse, and group
the local-only failures into candidate bug families.

    uv run $KIT/scripts/lc_compare.py --local target/run_results.json \
        [--remote path/to/warehouse/run_results.json] [--ledger BUG_LEDGER.md]

Copy `target/run_results.json` aside after each run: every dbt invocation
overwrites it. Without `--remote` the warehouse side is assumed green (the
usual case for a project that runs in production); confirm each candidate
with `dbt show --compute remote --select <model>` before writing it up.

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
FAILED = {"error", "fail", "runtime error"}

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
    """A stable, data-free fingerprint of an error message."""
    first = next((line for line in message.splitlines() if line.strip()), "")
    code = CODE_RE.search(message)
    text = first
    for pattern, replacement in NORMALIZERS:
        text = pattern.sub(replacement, text)
    text = text.strip()[:160]
    return f"{(code.group(1) or code.group(2)) if code else 'nocode'}: {text}"


def load(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text())
    return {r["unique_id"]: r for r in data.get("results", [])}


def kind(unique_id: str) -> str:
    return unique_id.split(".", 1)[0]


def classify(uid: str, local: dict, remote: dict | None) -> str:
    l = local.get("status")
    r = remote.get("status") if remote else None
    if l in OK:
        if r in FAILED:
            # Usually a test whose failing rows were not in the sample.
            return "remote_only_failure"
        return "pass"
    if l == "skipped":
        return "skipped_downstream"
    if l in FAILED or l == "warn":
        if r in FAILED:
            return "both_fail"
        if kind(uid) == "test":
            # Data tests on sampled data fail for sampling reasons (an orphan
            # foreign key whose parent row was not sampled) as often as for
            # engine reasons. Never a bug without a repro off the sample.
            return "local_test_failure"
        return "local_only_failure"
    return "other"


def known_signatures(ledger: Path | None) -> dict[str, str]:
    if not ledger or not ledger.is_file():
        return {}
    known, current = {}, None
    for line in ledger.read_text().splitlines():
        heading = re.match(r"^##\s+(LC-\d+)", line)
        if heading:
            current = heading.group(1)
        sig = re.match(r"^\s*[-*]?\s*\**Signature\**:\s*`?(.+?)`?\s*$", line)
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

    local = load(args.local)
    remote = load(args.remote) if args.remote else {}
    known = known_signatures(args.ledger)
    rows = []
    for uid, result in sorted(local.items()):
        cls = classify(uid, result, remote.get(uid) if args.remote else None)
        rows.append({
            "unique_id": uid,
            "class": cls,
            "local_status": result.get("status"),
            "remote_status": (remote.get(uid) or {}).get("status") if args.remote else None,
            "message": (result.get("message") or "")[:4000],
            "signature": signature(result.get("message") or "") if cls == "local_only_failure" else None,
        })

    models = [r for r in rows if kind(r["unique_id"]) in {"model", "snapshot", "seed"}]
    passing = sum(1 for r in models if r["class"] in {"pass", "remote_only_failure"})
    counts = collections.Counter(r["class"] for r in rows)
    families: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        if r["signature"]:
            families[r["signature"]].append(r)

    out = [
        "# Local-compute run comparison\n",
        f"`models {passing}/{len(models)} pass locally | candidate families {len(families)} "
        f"({sum(1 for s in families if s not in known)} new) | local-only data-test failures "
        f"{counts['local_test_failure']} | both-fail {counts['both_fail']} | skipped downstream "
        f"{counts['skipped_downstream']}`\n",
        f"Warehouse side: {'compared against ' + str(args.remote) if args.remote else 'not supplied; assumed green, confirm each candidate'}.\n",
        "## Candidate families (local-only failures, grouped by signature)\n",
        "| # | nodes | ledger | signature | example |",
        "|---|---|---|---|---|",
    ]
    ordered = sorted(families.items(), key=lambda kv: -len(kv[1]))
    for n, (sig, members) in enumerate(ordered, start=1):
        out.append(f"| {n} | {len(members)} | {known.get(sig, 'new')} | `{sig.replace('|', '/')}` | {members[0]['unique_id']} |")
    for n, (sig, members) in enumerate(ordered, start=1):
        out += [f"\n### {n}. {members[0]['unique_id']} (+{len(members) - 1} more)\n", "```", members[0]["message"][:1500], "```"]
        if len(members) > 1:
            out.append("Also: " + ", ".join(m["unique_id"].rsplit(".", 1)[-1] for m in members[1:25])
                       + (" ..." if len(members) > 25 else ""))
    if counts["local_test_failure"]:
        out += ["\n## Data tests that fail only locally (check sampling first)\n"]
        out += [f"- {r['unique_id']}" for r in rows if r["class"] == "local_test_failure"][:50]
    if counts["remote_only_failure"]:
        out += ["\n## Fail on the warehouse, pass locally (usually sampled away; not agreement)\n"]
        out += [f"- {r['unique_id']}" for r in rows if r["class"] == "remote_only_failure"][:50]
    print("\n".join(out))
    if args.json:
        args.json.write_text(json.dumps({"counts": counts, "families": {s: [m["unique_id"] for m in ms] for s, ms in ordered}, "rows": rows}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
