# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Generate paired (remote vs local) dbt unit tests from warehouse samples.

Snowflake projects only: the generated SQL is Snowflake dialect.

Each generated unit test runs one model on a small, fixed input set:

* ``given``  -- rows sampled from each upstream relation on the warehouse,
  projected onto that relation's *real* column list and exact column types
  (read with ``adapter.get_columns_in_relation``). Row keys the relation does
  not have are dropped; columns the sample lacks are emitted as typed NULLs.
* ``expect`` -- what the warehouse itself returns for the model over exactly
  that ``given``. The oracle is computed from the fixture, never from
  production output, so anonymizing or nulling fixture values never
  invalidates it.

Each model gets the same test twice, one copy configured ``compute: remote`` and
one ``compute: local`` (the public, experimental unit-test config; it needs
``DBT_ENGINE_EXPERIMENTAL_LOCAL_UNIT_TESTS=true``, which this tool sets for its
own dbt calls). A case that passes remotely and fails locally is a
local-compute candidate.

Subcommands (run from the dbt project directory)::

    inventory  list candidate models and why any are set aside (no warehouse)
    scaffold   sample inputs + schemas from the warehouse, write tests
    harvest    record `expect` from the warehouse (twice, for determinism)
    verify     run the tests remote and local, classify, write the report
    all        scaffold + harvest + verify

Everything the tool writes goes to two places: the generated tests directory
(default ``models/_lc_kit_paired``, delete it to undo) and a work directory
(default ``target/lc-kit``). It never edits models, macros, or configs.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import decimal
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

TAG = "lc_kit_paired"
# Bump when the generated test semantics change: dbt caches parsed unit tests
# by name, so a new contract must get a new name.
FORMAT_VERSION = 3
OPT_IN_ENV = "DBT_ENGINE_EXPERIMENTAL_LOCAL_UNIT_TESTS"

# Introspection macros that would otherwise consult the warehouse at compile
# time. Overriding them keeps the test source-free and identical on both
# sides; a model that cannot compile with these empty answers is reported.
INTROSPECTION_OVERRIDES: dict[str, Any] = {
    "dbt.load_relation": None,
    "dbt.load_cached_relation": None,
    "dbt.run_query": [],
    "dbt_utils.get_column_values": [],
    "dbt_utils.get_query_results_as_dict": {},
    "dbt_utils.get_single_value": None,
    "dbt_utils.get_filtered_columns_in_relation": [],
    "dbt_utils.get_relations_by_pattern": [],
}

NONDETERMINISTIC_RE = re.compile(
    r"\b(current_timestamp|current_date|current_time|localtimestamp|sysdate|"
    r"systimestamp|getdate|now|random|rand|uniform|uuid_string|gen_random_uuid|"
    r"generate_uuid|newid|seq1|seq2|seq4|seq8)\b\s*(\(|\b)"
    r"|run_started_at|modules\.datetime",
    re.IGNORECASE,
)

# A column is treated as personal data when one of its `_`-separated parts is
# in PII_PARTS, or when it is a person's name: `name` itself, or `*_name` with
# a person-like part (`first_name`, `customer_name`). Plain `first`/`last`/
# `name` parts are NOT enough: hashing `event_name`, `plan_name` or
# `first_seen_at` breaks the model's filters on them and empties the expect.
PII_PARTS = {
    "address", "email", "phone", "ip", "street", "city", "zip", "postal", "ssn",
    "dob", "birth", "token", "secret", "password", "firstname", "lastname",
    "fullname", "surname", "username",
}
PERSON_PARTS = {
    "first", "last", "full", "middle", "given", "family", "display", "user",
    "contact", "customer", "person", "owner", "employee", "author", "manager", "rep",
}
# Single-quoted literals in a model's SQL. A sampled value equal to one of them
# is kept as is: it is already in the code, and hashing it would make
# `where status = 'active'` (or `in`, `like`, ...) match nothing.
SQL_LITERAL_RE = re.compile(r"'((?:[^']|'')*)'")

# The kit supports Snowflake only. The SQL below (row serialization, literals,
# type names) is Snowflake dialect; other adapters are refused in main().
SUPPORTED_ADAPTERS = {"snowflake"}
# One row of a subquery as a JSON string, so many relations of different
# shapes can share one `dbt show` (a UNION ALL of key + JSON row).
ROW_JSON = "to_json(object_construct_keep_null(*))"
SEMI_STRUCTURED = {"variant", "object", "array"}
# Column types a fixture literal cannot carry; their values are nulled (the
# oracle is computed from the fixture, so this narrows coverage, not truth).
UNSUPPORTED_TYPE_RE = re.compile(r"^(binary|varbinary|geography|geometry|vector)\b", re.IGNORECASE)
SIMPLE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# --------------------------------------------------------------------------- #
# manifest -> cases
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Input:
    unique_id: str
    expression: str  # ref(...) / source(...) as written in a unit test
    resource_type: str

    @property
    def key(self) -> str:
        return hashlib.sha256(self.unique_id.encode()).hexdigest()[:16]

    @property
    def jinja(self) -> str:
        return "{{ " + self.expression + " }}"


@dataclass
class Case:
    unique_id: str
    name: str
    version: Any
    materialized: str
    path: str
    inputs: list[Input]
    macros: list[str]
    set_aside: str | None = None
    # String literals in the model's SQL, kept un-anonymized in its fixtures.
    literals: tuple[str, ...] = ()

    @property
    def test_name(self) -> str:
        suffix = self.name if self.version is None else f"{self.name}_v{self.version}"
        return f"lc_kit__{suffix}__f{FORMAT_VERSION}"

    def side(self, compute: str) -> str:
        """Name of this case's `compute: remote` / `compute: local` copy."""
        return f"{self.test_name}__{compute}"

    @property
    def filename(self) -> str:
        return f"{self.test_name}.yml"


def ref_expression(node: dict[str, Any], project: str) -> str:
    args = [node["name"]] if node["package_name"] == project else [node["package_name"], node["name"]]
    rendered = ", ".join(repr(a) for a in args)
    if node.get("version") is not None:
        rendered += f", v={node['version']!r}"
    return f"ref({rendered})"


def load_cases(manifest_path: Path, keep_nondeterministic: bool = False) -> tuple[list[Case], str]:
    manifest = json.loads(manifest_path.read_text())
    project = manifest["metadata"]["project_name"]
    adapter = manifest["metadata"].get("adapter_type") or "unknown"
    nodes, sources = manifest["nodes"], manifest.get("sources", {})
    cases: list[Case] = []
    for uid, node in nodes.items():
        if node.get("resource_type") != "model" or node.get("package_name") != project:
            continue
        config = node.get("config", {})
        if not config.get("enabled", True):
            continue
        inputs: list[Input] = []
        for dep_id in dict.fromkeys(node.get("depends_on", {}).get("nodes", [])):
            dep = nodes.get(dep_id) or sources.get(dep_id)
            if not dep:
                continue
            kind = dep.get("resource_type")
            if kind == "source":
                inputs.append(Input(dep_id, f"source({dep['source_name']!r}, {dep['name']!r})", kind))
            elif kind in {"model", "seed", "snapshot"}:
                inputs.append(Input(dep_id, ref_expression(dep, project), kind))
        case = Case(
            unique_id=uid,
            name=node["name"],
            version=node.get("version"),
            materialized=config.get("materialized", "view"),
            path=node.get("original_file_path", ""),
            inputs=sorted(inputs, key=lambda i: i.unique_id),
            macros=sorted(node.get("depends_on", {}).get("macros", [])),
            literals=sql_literals(node.get("raw_code", "") + "\n" + (node.get("compiled_code") or "")),
        )
        case.set_aside = set_aside_reason(node, case, nodes, keep_nondeterministic)
        cases.append(case)
    return sorted(cases, key=lambda c: c.unique_id), adapter


def sql_literals(sql: str) -> tuple[str, ...]:
    found = {m.group(1).replace("''", "'") for m in SQL_LITERAL_RE.finditer(sql)}
    return tuple(sorted(v for v in found if v and len(v) <= 200))


def set_aside_reason(node: dict, case: Case, nodes: dict, keep_nondeterministic: bool) -> str | None:
    """Return why a model cannot get a meaningful paired test, or None."""
    if node.get("language", "sql") != "sql":
        return "python model"
    if str(node.get("config", {}).get("static_analysis", "")).lower() == "off":
        # `compute: local` requires static analysis; such a test errors locally
        # (dbt1401) by design, which is not a conformance finding.
        return "static_analysis: off (local unit tests need it on)"
    if case.materialized == "ephemeral":
        return "ephemeral model (dbt unit tests do not support them)"
    snapshots = [i.unique_id for i in case.inputs if i.resource_type == "snapshot"]
    if snapshots:
        return "direct snapshot input cannot be mocked source-free: " + ", ".join(snapshots)
    ephemeral = [
        i.unique_id for i in case.inputs
        if nodes.get(i.unique_id, {}).get("config", {}).get("materialized") == "ephemeral"
    ]
    if ephemeral:
        return "ephemeral input has no relation to read a schema from: " + ", ".join(ephemeral)
    if not keep_nondeterministic:
        # compiled_code (present after `dbt compile`) also catches clock or
        # random calls hidden in macros, which raw_code does not show.
        match = NONDETERMINISTIC_RE.search(node.get("raw_code", "") + "\n" + (node.get("compiled_code") or ""))
        if match:
            return f"nondeterministic SQL ({match.group(0).strip(' (').lower()})"
    return None


def filter_cases(cases: list[Case], patterns: list[str]) -> list[Case]:
    if not patterns:
        return cases
    return [
        c for c in cases
        if any(fnmatch.fnmatch(c.name, p) or fnmatch.fnmatch(c.path, p) or fnmatch.fnmatch(c.unique_id, p) for p in patterns)
    ]


# --------------------------------------------------------------------------- #
# dbt invocation
# --------------------------------------------------------------------------- #


@dataclass
class Run:
    argv: list[str]
    code: int
    seconds: float
    stdout: str
    stderr: str


def run_dbt(args: argparse.Namespace, argv: list[str], label: str, *, log_output: bool = True) -> Run:
    command = [args.dbt, *argv]
    if args.dbt_args:
        command += shlex.split(args.dbt_args)
    # Opt in to local unit tests; drop any DBT_COMPUTE so the warehouse side is
    # decided by each test's `compute` config, never by the caller's shell.
    env = {k: v for k, v in os.environ.items() if k != "DBT_COMPUTE"} | {OPT_IN_ENV: "true"}
    started = time.monotonic()
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout, env=env)
        result = Run(command, proc.returncode, time.monotonic() - started, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as exc:
        result = Run(command, 124, time.monotonic() - started, str(exc.stdout or ""), str(exc.stderr or ""))
    log = args.work_dir / "logs" / f"{label}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    header = f"$ {shlex.join(command)}\nexit={result.code} seconds={result.seconds:.1f}\n"
    # `dbt show` output carries sampled warehouse rows: keep it out of logs.
    body = (result.stdout + "\n--- stderr ---\n" + result.stderr) if log_output else "output not logged (sampled rows)\n"
    log.write_text(header + body)
    return result


def parse_rows(output: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder(parse_float=decimal.Decimal)
    for index, char in enumerate(output):
        if char != "[":
            continue
        try:
            value, _ = decoder.raw_decode(output, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, list) and all(isinstance(r, dict) for r in value):
            return value
    raise ValueError("dbt show produced no JSON rows")


def show(args: argparse.Namespace, sql: str, label: str, limit: int) -> list[dict[str, Any]]:
    target = args.work_dir / "show-target"
    result = run_dbt(
        args,
        ["show", "--inline", sql, "--output", "json", "--quiet",
         "--limit", str(limit), "--target-path", str(target), "--no-write-json"],
        label,
        log_output=False,
    )
    if result.code:
        # Errors (unlike rows) are worth keeping: re-log stderr/stdout tail.
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-30:])
        (args.work_dir / "logs" / f"{label}.log").write_text(
            f"$ {shlex.join(result.argv[:4])} ...\nexit={result.code}\n{redact(tail)}\n"
        )
        raise RuntimeError(f"dbt show failed ({label}); see {args.work_dir / 'logs' / (label + '.log')}")
    return parse_rows(result.stdout)


def redact(text: str) -> str:
    return re.sub(r"https://\S+", "<url>", text)


def get(row: dict[str, Any], name: str) -> Any:
    """Read a column from a `dbt show` row regardless of identifier case."""
    if name in row:
        return row[name]
    for key, value in row.items():
        if key.lower() == name.lower():
            return value
    raise KeyError(name)


def sql_string(value: str) -> str:
    # Snowflake string literals process backslash escapes ('\\"' is '"'), so JSON
    # text with escaped quotes would no longer parse. Double each backslash.
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def bisect(items: list, label: str, fetch, on_single_failure) -> None:
    """Run `fetch` on a batch; on failure split it until single items fail."""
    try:
        fetch(items, label)
    except (RuntimeError, ValueError, KeyError) as error:
        if len(items) == 1:
            on_single_failure(items[0], str(error))
            return
        mid = len(items) // 2
        bisect(items[:mid], label + "a", fetch, on_single_failure)
        bisect(items[mid:], label + "b", fetch, on_single_failure)


# --------------------------------------------------------------------------- #
# schemas and samples
# --------------------------------------------------------------------------- #


def normalize_type(data_type: str) -> str:
    """Snowflake reports TEXT as `character varying(16777216)`; spell it `varchar`."""
    t = data_type.strip()
    m = re.fullmatch(r"character varying\((\d+)\)", t, re.IGNORECASE)
    if m:
        return "varchar" if m.group(1) == "16777216" else f"varchar({m.group(1)})"
    return t


def fetch_schemas(args: argparse.Namespace, inputs: list[Input], cache: dict) -> None:
    """Exact column lists and types for each input relation, via the adapter."""
    pending = [i for i in inputs if i.unique_id not in cache["schemas"]]

    def fetch(batch: list[Input], label: str) -> None:
        parts = []
        for item in batch:
            parts.append(
                "{% set cols = adapter.get_columns_in_relation(" + item.expression + ") %}"
                f"select '{item.key}' as lc_key, "
                "'{{ tojson(cols | map(attribute=\"name\") | list) | replace(\"'\", \"''\") }}' as lc_names, "
                "'{{ tojson(cols | map(attribute=\"data_type\") | list) | replace(\"'\", \"''\") }}' as lc_types"
            )
        rows = show(args, "\nunion all\n".join(parts), label, limit=len(batch) + 1)
        by_key = {get(r, "lc_key"): r for r in rows}
        for item in batch:
            row = by_key[item.key]
            names, types = json.loads(get(row, "lc_names")), json.loads(get(row, "lc_types"))
            if not names:
                raise ValueError(f"no columns for {item.unique_id}")
            cache["schemas"][item.unique_id] = [[n, normalize_type(t)] for n, t in zip(names, types)]

    def failed(item: Input, error: str) -> None:
        cache["schema_errors"][item.unique_id] = error

    for n, batch in enumerate(chunks(pending, args.batch_size), start=1):
        print(f"[schemas {n}/{-(-len(pending) // args.batch_size)}] {len(batch)} relation(s)", flush=True)
        bisect(batch, f"schemas-{n:04d}", fetch, failed)
        save_cache(args, cache)


def fetch_samples(args: argparse.Namespace, inputs: list[Input], cache: dict) -> None:
    pending = [i for i in inputs if i.unique_id not in cache["samples"] and i.unique_id in cache["schemas"]]
    loads = json.JSONDecoder(parse_float=decimal.Decimal).decode

    def fetch_batch(batch: list[Input], label: str) -> None:
        if len(batch) == 1:
            # Bisected down to one relation: read it plainly, without the
            # JSON wrapper, in case the wrapper was what failed.
            item = batch[0]
            rows = show(args, f"select * from {item.jinja}", label, limit=args.sample_rows)
            cache["samples"][item.unique_id] = [jsonable(r) for r in rows]
            return
        parts = [
            f"select '{i.key}' as lc_key, {ROW_JSON} as lc_row "
            f"from (select * from {i.jinja} limit {args.sample_rows}) as t"
            for i in batch
        ]
        rows = show(args, "\nunion all\n".join(parts), label, limit=args.sample_rows * len(batch) + 1)
        by_key: dict[str, list] = collections.defaultdict(list)
        for row in rows:
            by_key[get(row, "lc_key")].append(jsonable(loads(get(row, "lc_row"))))
        for item in batch:
            cache["samples"][item.unique_id] = by_key.get(item.key, [])

    def failed(item: Input, error: str) -> None:
        cache["sample_errors"][item.unique_id] = error

    for n, batch in enumerate(chunks(pending, args.batch_size), start=1):
        print(f"[samples {n}/{-(-len(pending) // args.batch_size)}] {len(batch)} relation(s)", flush=True)
        bisect(batch, f"samples-{n:04d}", fetch_batch, failed)
        save_cache(args, cache)


def jsonable(value: Any) -> Any:
    """Decimal -> exact string so the cache round-trips through JSON."""
    if isinstance(value, decimal.Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    return value


def unjson(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__decimal__"}:
            return decimal.Decimal(value["__decimal__"])
        return {k: unjson(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unjson(v) for v in value]
    return value


def cache_path(args: argparse.Namespace) -> Path:
    return args.work_dir / "cache.json"


def load_cache(args: argparse.Namespace) -> dict:
    path = cache_path(args)
    base = {"schemas": {}, "samples": {}, "schema_errors": {}, "sample_errors": {}, "sample_rows": args.sample_rows}
    if path.is_file():
        cached = json.loads(path.read_text())
        if cached.get("sample_rows") == args.sample_rows:
            base.update(cached)
            # Errors are retried on every run; only successes are cached.
            base["schema_errors"], base["sample_errors"] = {}, {}
    return base


def save_cache(args: argparse.Namespace, cache: dict) -> None:
    cache_path(args).parent.mkdir(parents=True, exist_ok=True)
    cache_path(args).write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- #
# fixture rendering
# --------------------------------------------------------------------------- #


def is_pii_column(column: str) -> bool:
    lowered = column.lower()
    parts = set(lowered.split("_")) | {lowered}
    if parts & PII_PARTS:
        return True
    return lowered == "name" or (lowered.endswith("_name") and bool(parts & PERSON_PARTS))


def anonymize(column: str, value: Any, keep: frozenset[str] | set[str] = frozenset()) -> Any:
    if not isinstance(value, str) or value in keep:
        return value
    if not is_pii_column(column):
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()[:10]
    if "@" in value:
        return f"user_{digest}@example.invalid"
    return f"anon_{digest}"


def identifier(name: str) -> str:
    if SIMPLE_IDENTIFIER.match(name):
        return name
    return '"' + name.replace('"', '""') + '"'


def literal(value: Any, sql_type: str) -> str:
    if value is None:
        return f"cast(null as {sql_type})"
    base = sql_type.split("(")[0].strip().lower()
    if base in SEMI_STRUCTURED:
        text = value if isinstance(value, str) else json.dumps(unjson_plain(value), sort_keys=True)
        return f"cast(parse_json({sql_string(text)}) as {sql_type})"
    if isinstance(value, bool):
        return f"cast({'true' if value else 'false'} as {sql_type})"
    if isinstance(value, (dict, list)):
        value = json.dumps(unjson_plain(value), sort_keys=True)
    return f"cast({sql_string(str(value))} as {sql_type})"


def unjson_plain(value: Any) -> Any:
    value = unjson(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: unjson_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unjson_plain(v) for v in value]
    return value


def project_rows(
    rows: list[dict[str, Any]], schema: list[list[str]], anonymize_pii: bool,
    keep: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Project sampled rows onto the relation's real columns.

    Returns rows keyed by the schema's column names plus a small report of
    what was dropped or nulled, so superset/subset drift is visible.
    """
    names = [n for n, _ in schema]
    lookup = {n.lower(): n for n in names}
    dropped: set[str] = set()
    nulled: set[str] = set()
    unsupported = {n for n, t in schema if UNSUPPORTED_TYPE_RE.search(t.strip())}
    projected = []
    for row in rows:
        out: dict[str, Any] = {n: None for n in names}
        for key, value in row.items():
            name = key if key in out else lookup.get(key.lower())
            if name is None:
                dropped.add(key)
                continue
            value = unjson(value)
            if name in unsupported and value is not None:
                nulled.add(name)
                value = None
            out[name] = anonymize(name, value, keep) if anonymize_pii else value
        projected.append(out)
    return projected, {"dropped_row_keys": sorted(dropped), "nulled_unsupported": sorted(nulled)}


def rows_sql(rows: list[dict[str, Any]], schema: list[list[str]]) -> str:
    if not rows:
        cols = ", ".join(f"{literal(None, t)} as {identifier(n)}" for n, t in schema)
        return f"select * from (select {cols}) as lc_empty where 1 = 0"
    selects = []
    for row in rows:
        cols = ",\n  ".join(f"{literal(row[n], t)} as {identifier(n)}" for n, t in schema)
        selects.append("select\n  " + cols)
    return "\nunion all\n".join(selects)


def macro_overrides(case: Case, extra: dict[str, Any]) -> dict[str, Any]:
    used = {m.split(".", 1)[-1] for m in case.macros} | set(case.macros)
    overrides = {}
    for name, value in INTROSPECTION_OVERRIDES.items():
        short = name.split(".")[-1]
        if any(m.endswith("." + short) or m == short for m in used):
            overrides[name] = value
    overrides.update(extra)
    if case.materialized == "incremental":
        # Unit tests take the full-refresh branch unless told otherwise; say so
        # explicitly so neither side needs the live `this` relation.
        overrides["is_incremental"] = False
    return overrides


def test_payload(case: Case, cache: dict, extra_overrides: dict, anonymize_pii: bool) -> tuple[dict, dict]:
    given, notes = [], {}
    for item in case.inputs:
        schema = cache["schemas"][item.unique_id]
        rows, note = project_rows(
            [r for r in cache["samples"].get(item.unique_id, [])], schema, anonymize_pii, frozenset(case.literals)
        )
        if note["dropped_row_keys"] or note["nulled_unsupported"]:
            notes[item.unique_id] = note
        given.append({"input": item.expression, "format": "sql", "rows": rows_sql(rows, schema)})
    test: dict[str, Any] = {
        "name": case.test_name,
        "model": case.name,
        "description": (
            "Generated by lc_fixtures.py: given = warehouse samples projected onto each "
            "input's real columns; expect = the warehouse's own output over this given."
        ),
        "given": given,
        "expect": {"rows": []},
    }
    overrides = macro_overrides(case, extra_overrides)
    if overrides:
        test["overrides"] = {"macros": overrides}
    if case.version is not None:
        test["versions"] = {"include": [case.version]}
    # Identical copies except for `compute`. A per-test config also overrides
    # any project-wide `+compute`, so the remote copy really runs remotely.
    copies = []
    for compute in ("remote", "local"):
        copy = {"name": case.side(compute), **{k: v for k, v in test.items() if k != "name"}}
        copy["config"] = {"tags": [TAG], "compute": compute}
        copies.append(copy)
    return {"unit_tests": copies}, notes


class LiteralDumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper: yaml.SafeDumper, data: str):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


LiteralDumper.add_representer(str, _str_presenter)


def write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# Generated by lc_fixtures.py (local-compute bug-hunt kit). Safe to delete.\n"
    path.write_text(header + yaml.dump(payload, Dumper=LiteralDumper, sort_keys=False, width=100))


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def selected_cases(args: argparse.Namespace) -> list[Case]:
    cases, adapter = load_cases(args.manifest, args.keep_nondeterministic)
    args.adapter = adapter
    return filter_cases(cases, args.select)


def write_report(args: argparse.Namespace, name: str, payload: dict) -> Path:
    path = args.work_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), **payload}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    return path


def command_inventory(args: argparse.Namespace) -> int:
    cases = selected_cases(args)
    reasons = collections.Counter((c.set_aside or "").split(" (")[0].split(":")[0] for c in cases if c.set_aside)
    print(f"{len(cases)} model(s), {sum(1 for c in cases if not c.set_aside)} eligible, adapter={args.adapter}")
    for reason, count in reasons.most_common():
        print(f"  set aside {count:>4}  {reason}")
    path = write_report(args, "inventory.json", {"cases": [asdict(c) for c in cases]})
    print(f"details: {path}")
    return 0


def command_scaffold(args: argparse.Namespace) -> int:
    cases = selected_cases(args)
    eligible = [c for c in cases if not c.set_aside]
    inputs = list({i.unique_id: i for c in eligible for i in c.inputs}.values())
    print(f"scaffold: {len(eligible)}/{len(cases)} model(s) eligible, {len(inputs)} distinct input relation(s)")
    cache = load_cache(args)
    fetch_schemas(args, inputs, cache)
    fetch_samples(args, inputs, cache)
    extra = dict(parse_override(o) for o in args.macro_override)
    written, skipped, notes = [], {}, {}
    for case in eligible:
        missing = [i.unique_id for i in case.inputs if i.unique_id not in cache["schemas"] or i.unique_id not in cache["samples"]]
        if missing:
            skipped[case.unique_id] = "no schema/sample for input(s): " + ", ".join(missing)
            continue
        payload, case_notes = test_payload(case, cache, extra, not args.no_anonymize)
        path = args.tests_dir / case.filename
        if path.is_file() and not args.refresh:
            # Keep an existing test (and its harvested expect) unless asked.
            written.append(str(path))
            continue
        write_yaml(path, payload)
        written.append(str(path))
        if case_notes:
            notes[case.unique_id] = case_notes
    report = write_report(args, "scaffold-report.json", {
        "models": len(cases),
        "written": len(written),
        "set_aside": {c.unique_id: c.set_aside for c in cases if c.set_aside},
        "skipped_missing_inputs": skipped,
        "schema_errors": cache["schema_errors"],
        "sample_errors": cache["sample_errors"],
        "projection_notes": notes,
        "sample_rows": args.sample_rows,
    })
    print(f"wrote {len(written)} test file(s) to {args.tests_dir}; "
          f"{len(skipped)} skipped for missing inputs; report: {report}")
    return 0


def parse_override(text: str) -> tuple[str, Any]:
    name, _, value = text.partition("=")
    return name.strip(), json.loads(value) if value else None


ACTUAL_RE = re.compile(r"'actual'\s+as\s+actual_or_expected\s+from\s+([^\s)]+)", re.IGNORECASE)
WRAPPER_RE = re.compile(r"\)\s*select\s+\*\s+from\s*\(\s*\(\s*select\b", re.IGNORECASE)


def actual_query(compiled: str) -> str:
    """Turn Fusion's compiled unit-test SQL into a query for the actual rows.

    The compiled test is `WITH <given CTEs>, <expect CTE>, <x_actual> AS
    (<model>) SELECT * FROM ((actual) UNION ALL (expected))`. Keep the CTEs
    and select the actual CTE alone.
    """
    match = ACTUAL_RE.search(compiled)
    if not match:
        raise ValueError("no actual CTE found in compiled unit test")
    wrappers = [m for m in WRAPPER_RE.finditer(compiled) if m.start() < match.start()]
    if not wrappers:
        raise ValueError("no actual/expected wrapper found in compiled unit test")
    cut = wrappers[-1].start() + 1
    body = compiled[:cut]
    # Fusion prefixes a comment line; it is harmless, but drop a trailing ';'.
    return body.rstrip().rstrip(";") + "\nselect * from " + match.group(1)


def compile_tests(args: argparse.Namespace, cases: list[Case]) -> Path:
    target = args.work_dir / "compile-target"
    shutil.rmtree(target, ignore_errors=True)
    # Running the remote copies with empty expectations is the reliable way to
    # get every unit test compiled; their pass/fail here is not used.
    run_dbt(args, ["test", "--resource-type", "unit_test",
                   "--select", *[c.side("remote") for c in cases], "--target-path", str(target)],
            "harvest-compile")
    return target


def canonical(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)


def expect_value(value: Any) -> Any:
    value = unjson(value)
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() and "." not in str(value) else str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(unjson_plain(value), sort_keys=True)
    return value


def unrepresentable(value: Any) -> bool:
    """Values a dict-format expect row cannot carry: '' and the string 'null'
    both come back as SQL NULL, so the warehouse copy would fail its own test."""
    return isinstance(value, str) and value.strip().lower() in {"", "null"}


def expect_columns_to_drop(case: Case, rows: list[dict[str, Any]], requested: list[str]) -> dict[str, str]:
    """Columns to leave out of `expect` (dbt compares only the listed columns)."""
    drop: dict[str, str] = {}
    for spec in requested:
        model, _, column = spec.rpartition(".")
        if model.lower() == case.name.lower():
            for row in rows:
                for key in row:
                    if key.lower() == column.lower():
                        drop[key] = "--drop-expect-column"
    for row in rows:
        for key, value in row.items():
            if key not in drop and unrepresentable(unjson(value)):
                drop[key] = "value '' or 'null' cannot be expressed in expect"
    return drop


def command_harvest(args: argparse.Namespace) -> int:
    cases = [c for c in selected_cases(args) if not c.set_aside and (args.tests_dir / c.filename).is_file()]
    print(f"harvest: {len(cases)} generated test(s)")
    target = compile_tests(args, cases)
    compiled = {p.stem: p for p in (target / "compiled").rglob("lc_kit__*.sql")}
    loads = json.JSONDecoder(parse_float=decimal.Decimal).decode
    results: dict[str, Any] = {}
    set_aside: dict[str, str] = {}
    queries: dict[str, str] = {}
    for case in cases:
        path = compiled.get(case.side("remote"))
        if path is None:
            set_aside[case.unique_id] = "unit test did not compile on the warehouse (see logs/harvest-compile.log)"
            continue
        try:
            queries[case.unique_id] = actual_query(path.read_text())
        except ValueError as error:
            set_aside[case.unique_id] = str(error)
    by_id = {c.unique_id: c for c in cases}

    def fetch(batch: list[str], label: str) -> None:
        runs = []
        for attempt in range(2):
            if len(batch) > 1:
                parts = [
                    f"select '{by_id[u].test_name}' as lc_key, {ROW_JSON} as lc_row from ({queries[u]}) as t"
                    for u in batch
                ]
                rows = show(args, "\nunion all\n".join(parts), f"{label}-{attempt}", limit=(args.max_expect_rows + 1) * len(batch))
                got: dict[str, list] = collections.defaultdict(list)
                for row in rows:
                    got[get(row, "lc_key")].append(loads(get(row, "lc_row")))
                runs.append({u: got.get(by_id[u].test_name, []) for u in batch})
            else:
                u = batch[0]
                rows = show(args, queries[u], f"{label}-{attempt}", limit=args.max_expect_rows + 1)
                runs.append({u: rows})
        for u in batch:
            first, second = runs[0][u], runs[1][u]
            if len(first) > args.max_expect_rows:
                hint = "lower --sample-rows" if by_id[u].inputs else "the model generates rows without inputs"
                set_aside[u] = f"expect has more than {args.max_expect_rows} rows; {hint}"
            elif canonical(first) != canonical(second):
                set_aside[u] = "nondeterministic: two warehouse runs over the same given disagreed"
            else:
                results[u] = first

    def failed(u: str, error: str) -> None:
        set_aside[u] = f"warehouse could not run the model over the given: {error}"

    pending = sorted(queries)
    for n, batch in enumerate(chunks(pending, args.expect_batch_size), start=1):
        print(f"[expect {n}/{-(-len(pending) // args.expect_batch_size)}] {len(batch)} test(s)", flush=True)
        bisect(batch, f"expect-{n:04d}", fetch, failed)

    dropped_columns: dict[str, dict[str, str]] = {}
    for u, rows in list(results.items()):
        drop = expect_columns_to_drop(by_id[u], rows, args.drop_expect_column)
        if drop and rows and all(set(r) <= set(drop) for r in rows):
            set_aside[u] = "every expect column was dropped: " + ", ".join(sorted(drop))
            del results[u]
        elif drop:
            dropped_columns[u] = drop
    for u, rows in results.items():
        path = args.tests_dir / by_id[u].filename
        payload = read_yaml(path)
        drop = dropped_columns.get(u, {})
        expect = {"rows": [{k: expect_value(v) for k, v in r.items() if k not in drop} for r in rows]}
        for test in payload["unit_tests"]:
            test["expect"] = expect
        write_yaml(path, payload)
    for u in set_aside:
        # A test with no trustworthy oracle must not run as if it had one.
        path = args.tests_dir / by_id[u].filename
        if path.is_file():
            aside = args.work_dir / "set-aside" / path.name
            aside.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), aside)
    report = write_report(args, "harvest-report.json", {
        "harvested": len(results),
        "empty_expect": sorted(u for u, r in results.items() if not r),
        "expect_columns_dropped": dropped_columns,
        "set_aside": set_aside,
    })
    print(f"harvested {len(results)} expectation(s) ({sum(1 for r in results.values() if not r)} empty, "
          f"{len(dropped_columns)} with dropped columns); set aside {len(set_aside)}; report: {report}")
    return 0


def run_paired(args: argparse.Namespace, cases: list[Case]) -> dict[str, dict]:
    """One `dbt test` over both copies of every case; results keyed by test name."""
    target = args.work_dir / "verify-target"
    shutil.rmtree(target, ignore_errors=True)
    names = [c.side(compute) for c in cases for compute in ("remote", "local")]
    argv = ["test", "--resource-type", "unit_test", "--select", *names, "--target-path", str(target)]
    result = run_dbt(args, argv, "verify")
    results_path = target / "run_results.json"
    if not results_path.is_file():
        print(f"  no run_results.json (exit {result.code}); see logs/verify.log")
        return {}
    diffs = failure_blocks(result.stdout)
    errors = error_blocks(result.stdout + "\n" + result.stderr)
    out = {}
    for item in json.loads(results_path.read_text()).get("results", []):
        name = item.get("unique_id", "").rsplit(".", 1)[-1]
        entry = {"status": item.get("status"), "message": (item.get("message") or "")[:2000], "diff": None}
        if entry["status"] != "pass":
            # Fusion reports a failed diff as status `error` with message
            # `Error`; the diff table (or the real error) is only in stdout.
            entry["diff"] = diffs.get(name)
            model = name.removeprefix("lc_kit__").rsplit("__f", 1)[0]
            # Both copies of a case run together; never take the other copy's error.
            other = name[: -len("__local")] + "__remote" if name.endswith("__local") else name[: -len("__remote")] + "__local"
            match = next((e for e in errors if name in e), None) or next(
                (e for e in errors if model in e and other not in e), None
            )
            entry["message"] = (match or entry["message"])[:2000]
        out[name] = entry
    return out


def failure_blocks(stdout: str) -> dict[str, str]:
    """`Test failed: <name>` sections (the expected -> actual diff tables)."""
    blocks: dict[str, str] = {}
    current, lines = None, []
    for line in stdout.splitlines():
        if line.startswith("Test failed: ") or line.startswith("==="):
            if current:
                blocks[current] = "\n".join(lines).strip()
            current = line.removeprefix("Test failed: ").strip() if line.startswith("Test failed: ") else None
            lines = []
        elif current:
            lines.append(line)
    if current:
        blocks[current] = "\n".join(lines).strip()
    return blocks


def error_blocks(text: str) -> list[str]:
    blocks, lines = [], None
    for line in text.splitlines():
        if line.startswith("[error]"):
            if lines:
                blocks.append("\n".join(lines))
            lines = [line]
        elif lines is not None:
            if not line.strip() or line.startswith(("[warning]", "===")):
                blocks.append("\n".join(lines))
                lines = None
            else:
                lines.append(line)
    if lines:
        blocks.append("\n".join(lines))
    return [redact(b) for b in blocks]


def classify(remote: dict | None, local: dict | None, empty_expect: bool) -> str:
    if remote is None or local is None:
        return "unknown"
    if remote["status"] != "pass":
        return "broken"
    if local["status"] == "pass":
        return "match_empty" if empty_expect else "match"
    return "mismatch" if local.get("diff") else "local_error"


def command_verify(args: argparse.Namespace) -> int:
    cases = [c for c in selected_cases(args) if (args.tests_dir / c.filename).is_file()]
    print(f"verify: {len(cases)} generated test(s)")
    if not cases:
        return 1
    results = run_paired(args, cases)
    rows = []
    for case in cases:
        expect = read_yaml(args.tests_dir / case.filename)["unit_tests"][0]["expect"]["rows"]
        r, l = results.get(case.side("remote")), results.get(case.side("local"))
        rows.append({
            "model": case.name, "test": case.test_name, "class": classify(r, l, not expect),
            "remote": r, "local": l, "expect_rows": len(expect),
        })
    counts = collections.Counter(r["class"] for r in rows)
    warehouse_pass = sum(counts[k] for k in ("match", "match_empty", "mismatch", "local_error"))
    matched = counts["match"] + counts["match_empty"]
    harvest = args.work_dir / "harvest-report.json"
    aside = len(json.loads(harvest.read_text()).get("set_aside", {})) if harvest.is_file() else 0
    line = (f"paired {matched}/{warehouse_pass} pass on both sides "
            f"({counts['mismatch']} value mismatch, {counts['local_error']} local error, "
            f"{counts['broken']} broken, {counts['unknown']} unknown, {aside} set aside; "
            f"{counts['match_empty']} of the matches have an empty expect)")
    report = write_report(args, "paired-report.json", {"scoreboard": line, "counts": dict(counts), "cases": rows})
    md = [f"# Paired unit tests\n\n`{line}`\n", "| model | class | expect rows | local message |", "|---|---|---|---|"]
    order = lambda r: (r["class"] not in {"mismatch", "local_error"}, r["model"])  # noqa: E731
    for r in sorted(rows, key=order):
        msg = (r["local"] or {}).get("message", "").splitlines()[0:1]
        md.append(f"| {r['model']} | {r['class']} | {r['expect_rows']} | {(msg[0] if msg else '')[:160].replace('|', '/')} |")
    for r in sorted(rows, key=order):
        detail = (r["local"] or {}).get("diff") or (r["class"] == "local_error" and (r["local"] or {}).get("message"))
        if r["class"] in {"mismatch", "local_error"} and detail:
            md += [f"\n## {r['model']} ({r['class']})\n", "Diff cells read `warehouse -> local`.\n" if r["class"] == "mismatch" else "",
                   "```", detail, "```"]
    (args.work_dir / "paired-report.md").write_text("\n".join(md) + "\n")
    print(line)
    print(f"report: {report} (and paired-report.md)")
    return 0


def command_all(args: argparse.Namespace) -> int:
    return command_scaffold(args) or command_harvest(args) or command_verify(args)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["inventory", "scaffold", "harvest", "verify", "all"])
    p.add_argument("--select", action="append", default=[], help="model name/path glob; repeatable")
    p.add_argument("--manifest", type=Path, default=Path("target/manifest.json"))
    p.add_argument("--tests-dir", type=Path, default=Path("models/_lc_kit_paired"))
    p.add_argument("--work-dir", type=Path, default=Path("target/lc-kit"))
    p.add_argument("--dbt", default="dbt", help="dbt (Fusion) executable")
    p.add_argument("--dbt-args", default="", help="extra args for every dbt call, e.g. '--target dev'")
    p.add_argument("--sample-rows", type=int, default=25, help="rows sampled per input relation")
    p.add_argument("--max-expect-rows", type=int, default=500)
    p.add_argument("--batch-size", type=int, default=40, help="relations per warehouse query")
    p.add_argument("--expect-batch-size", type=int, default=10)
    p.add_argument("--timeout", type=int, default=1800, help="seconds per dbt call")
    p.add_argument("--macro-override", action="append", default=[], metavar="NAME=JSON",
                   help="extra unit-test macro override, e.g. 'my_pkg.get_regions=[]'")
    p.add_argument("--no-anonymize", action="store_true", help="keep PII-looking string values as sampled")
    p.add_argument("--keep-nondeterministic", action="store_true",
                   help="do not set aside models whose SQL calls now()/random()/...")
    p.add_argument("--drop-expect-column", action="append", default=[], metavar="MODEL.COLUMN",
                   help="leave a column out of expect, e.g. a clock-derived 'fct_x.DAYS_SINCE'; repeatable")
    p.add_argument("--refresh", action="store_true", help="rewrite existing generated tests (drops their expect)")
    return p


def main() -> int:
    args = parser().parse_args()
    if not args.manifest.is_file():
        print(f"no manifest at {args.manifest}; run `dbt parse` (or `dbt compile`) first", file=sys.stderr)
        return 2
    adapter = json.loads(args.manifest.read_text())["metadata"].get("adapter_type")
    if adapter not in SUPPORTED_ADAPTERS:
        print(f"this kit supports Snowflake projects only (manifest adapter_type: {adapter!r})", file=sys.stderr)
        return 2
    return {
        "inventory": command_inventory,
        "scaffold": command_scaffold,
        "harvest": command_harvest,
        "verify": command_verify,
        "all": command_all,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
