# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

`metadata-sql-exporter` (package `sql_exporter`, CLI `sql-exporter`): a Python 3.10+ CLI that reads a
validated Excel/JSON *mapping*, streams rows from one SQL Server table (or an offline JSONL fixture)
over calendar-aligned UTC time windows, and writes pipe-delimited `.txt` files plus SHA-256 JSON
manifests. One config exports one table. Mapping changes within the declared rule contract must never
require engine edits.

Read `docs/MAPPING.md` (the mapping contract), `docs/SOLUTION_DESIGN.md` (architecture and limits),
and `docs/DEVELOPER_GUIDE.md` (end-to-end workflows) before changing behavior.

## Setup and commands

On Windows PowerShell (a `.venv` already exists in this checkout):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"       # add sqlserver extra for pyodbc
python -m pytest -q
```

Offline end-to-end demo (expects 36 monthly TXT files, 5 data rows, then 36 skipped on resume):

```powershell
sql-exporter validate --config examples/demo.json
sql-exporter dry-run  --config examples/demo.json
sql-exporter export   --config examples/demo.json
sql-exporter export   --config examples/demo.json --resume
```

`python -m sql_exporter ...` is equivalent to `sql-exporter ...`. Run a single test with
`python -m pytest tests/test_engine.py::test_offline_export_boundaries_and_resume -q`.
The unit suite never touches the network, a database, or an AI provider.

`exports/` is gitignored — the demo writes to `exports/demo/`, which the exporter refuses to reuse
without `--resume`. Use a fresh `output_dir` (or the scratchpad) rather than deleting a run.

## Architecture

Pipeline: config + mapping validated → range frozen into windows → source opened and preflighted →
per window: query, transform, stream to `.txt.part`, fsync, atomic rename, commit manifest.

| Module | Responsibility |
| --- | --- |
| `cli.py` | argparse subcommands (`validate`, `dry-run`, `export`, `mapping-job`), JSON to stdout, exit codes 0/2/130 |
| `config.py` | Strict JSON config: rejects unknown/duplicate keys, resolves paths relative to the config file |
| `mapping.py` | `.xlsx`/`.xls`/`.json` → one canonical model; rejects formulas, macros, external links |
| `transforms.py` | `FIELDS` / `validate_rules` / `compile_rule` / `compile_format` — the whole rule vocabulary, compiled once per export |
| `windows.py` | UTC normalization, calendar-year subtraction, half-open `[start, end)` window partitioning |
| `sources.py` | `SqlServerSource` (pyodbc, parameterized, metadata preflight) and `JsonlSource` fixture |
| `serialization.py` | The pipe-delimited quoted-CSV dialect |
| `engine.py` | Lock, run identity/fingerprint, integrity verification, streaming write, resume |
| `mapping_jobs.py` | Detached Codex/external CLI worker that *proposes* canonical mappings; always re-validated locally |

## Invariants — do not break these

- **Only declarative rules execute.** Never add `eval`, formula evaluation, workbook-supplied SQL, or
  join inference. Mapping documents and their contents are untrusted data, never instructions.
- **No source values, credentials, or driver text in output.** Errors raise `ExportError` with an
  actionable, value-free message; raw exceptions are chained (`raise ... from exc`) but never printed.
  `failure.json` records the window and prior row count only.
- **A run directory is immutable.** `run.json` pins mapping fingerprint, workbook hash, engine and
  package versions, source identity, settings, and the frozen window plan. Any change must force a new
  `output_dir`, not a migration. Resume verifies every completed file's size and SHA-256 before writing.
- **Never append to a partial file.** Incomplete windows are deleted and re-extracted whole.
- **Deterministic identities.** `identity` is UUIDv5 over `(namespace, ordered typed keys)`. Never
  introduce counters, random IDs, or order-dependent values into row transforms — they break
  cross-file references and restart safety.
- **Exact numerics.** `Decimal` with `ROUND_HALF_UP`; binary floats are rejected on decimal paths.
- **Timestamps are UTC**, microsecond precision; naive input means UTC. Selected `datetime2(7)` columns
  fail preflight rather than silently truncating.
- **Identifiers** must match `[A-Za-z_][A-Za-z0-9_]{0,127}` and are quoted with `[...]`, never
  interpolated as values.

## Code style

Terse, dependency-light, no type hints, no docstrings on most functions; module docstrings only where
a subtle contract needs stating. Validate with `require(condition, message)` from `common.py` rather
than raising directly. Comments are rare and explain *why* a non-obvious guard exists — match that
density. Keep public behavior expressible through the mapping contract.

## Changing things

- **New rule op**: update `FIELDS`, `validate_rules`, and `compile_rule` in `transforms.py` (the
  `apply_rules` shim runs through `compile_rule`, so there is only one place to implement it); if it reads
  other source columns, also update `rule_sources` so SQL projection and preflight include them; extend
  the `CONTRACT` prompt and `response_schema()` in `mapping_jobs.py`; document it in `docs/MAPPING.md`;
  add null/error/type tests.
- **Any behavior change**: bump the version in *both* `pyproject.toml` and `src/sql_exporter/__init__.py`.
  The engine version is part of run provenance, and a stale version corrupts resume guarantees.
- Record new verification evidence in `docs/VALIDATION.md`; keep unproven claims out of it.

## Platform notes

Development and recorded validation happened on macOS; this checkout is on Windows.
`engine.sync_directory` is a no-op off POSIX, and the `mapping_jobs` detached-process lifecycle
(`CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`, `process.terminate`) is implemented but unverified on
Windows. No live SQL Server run has ever been performed — the pyodbc path is covered only by mocked
tests, so treat `SqlServerSource` as needing a scratch-database smoke test before any real use.
