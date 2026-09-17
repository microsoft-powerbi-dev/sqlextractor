# Validation performed

## Engine 1.2 compiled row pipeline (2026-09-16)

- **174 automated tests passed** on the export path (`pytest --ignore=tests/test_mapping_jobs.py`).
  Two tests in `tests/test_mapping_jobs.py` fail on this Windows host; they exercise the detached
  background-worker lifecycle, which 1.1 already recorded as implemented but unverified on Windows.
  Both failures predate this work and are unrelated to the export path.
- **Output is byte-identical.** `tests/test_golden_bytes.py` pins the per-window TXT SHA-256, byte
  count and row count for `examples/demo.json` (36 windows) and a new all-operations corpus
  (`tests/golden/`, 13 windows, 19 targets covering every op and every target type). Goldens were
  captured from engine 1.1.0 and still match.
- **Measured speedup 1.67x** on the AdventureWorks detail mapping (13 targets, 4 identity rules, 1
  template chain, 3 decimals) over 20,000 synthetic rows: 293.5 to 176.1 microseconds per row,
  3,406 to 5,678 rows per second. Old and new implementations were timed interleaved in one process
  over 9 repetitions and the median ratio reported, because this host drifts more than 2x between
  runs; absolute microsecond figures are not portable. The positional source path measured 174.7
  microseconds per row (1.68x). Both arms were asserted byte-identical to 1.1.0 output first.
- Component contributions measured separately: hoisting the identity namespace UUID plus the cached
  JSON encoder and pinned decimal context, 1.27x; adding the compiled closure pipeline, 1.49x; the
  bounded identity memo, a further ~1.2x on identity-heavy mappings. Positional row access
  contributes ~1.05x on the SQL path, not the 5x an isolated row-unpacking microbenchmark suggests,
  because unpacking is roughly 2 of 190 microseconds per row.
- Equivalences proved by differential test rather than inspection, and kept as permanent tests in
  `tests/test_compiled_pipeline.py`: `quantize(context=Context(prec=80))` against the previous
  per-value `localcontext()` over 399 value/scale cases including `-0`, `1.005`, `-2.5`, 38-digit
  values and every error path with matching exception types; `Context.multiply` against `*` under
  `localcontext` over 126 cases; the cached namespace UUID against nested `uuid5` over 3,000 random
  Unicode namespaces; and the cached `JSONEncoder` against `json.dumps` with the same keyword
  arguments, which leaves every mapping and run fingerprint unchanged.
- Memory: the 20,000-row streaming regression test peaks at **0.70 MiB** against its 5 MiB budget.
  The identity memo is per rule and capped at `IDENTITY_CACHE = 4096` entries, cleared wholesale on
  overflow; 40,000 rows through a four-identity mapping peak at 3.83 MiB and do not grow.
- Positional source access is covered by `tests/test_compiled_pipeline.py`, which exports the same
  fixture through a sequence-yielding source and a mapping source and compares every file hash, and
  which pins the two hazards: source index 0 is falsy but must still be read, and a missing
  positional column raises `IndexError` where a mapping raises `KeyError`, so the caught exception
  group was widened to `LookupError` to keep the "names the target, leaks no value" error contract.
- **Two deliberate behaviour changes.** `compile_rule` rejects an unknown operation, where the old
  `apply_rules` interpreter silently passed the value through; validated mappings could never reach
  that path, so only direct unvalidated calls are affected. Intermediate decimal arithmetic now uses
  a pinned `Context(prec=80)` instead of copying the ambient context, so an embedding process that
  changes `decimal.getcontext()` can no longer alter output bytes.
- No live SQL Server run. The positional path is exercised by mocks and by an equivalent
  sequence-yielding offline source only; the existing native-driver limitation still applies, and
  `SqlServerSource` still needs the scratch-database smoke test described in README before real use.

## Engine 1.1 additions (2026-09-10)

- **115 automated tests passed** on the updated engine, including canonical JSON/XLSX equivalence, multiple deterministic identities, composite key framing, source dependency projection, prefixes/suffixes/templates, real detached external-worker success, invalid output, timeout, cancellation, and unsupported-rule rejection. Unit tests do not call an AI provider or download data.
- Actual authenticated **Codex CLI 0.153.4** background normalization completed in 18.45 seconds. Its canonical order mapping exactly matched the independently defined example, with no issues. The input was `examples/adventureworks/general_mapping.md`; sanitized output/evidence is in that example's `normalization-example/` directory. No user business mapping document was supplied.
- Downloaded Microsoft AdventureWorks CSV fixtures from commit `b47eadc85277f72843647223cde53aa1b85ffcb8`, verified their pinned SHA-256 hashes, and prepared a fixed three-year sample. Source detail table has **121,317 rows**; **120,029** fall in the selected parent-order interval.
- Exported **170,597 records in 74 TXT files**: 31,201 orders, 120,029 details, 19,101 customers, 266 products. SQLite-based verification found **zero** missing order/customer/product references and **zero** order/customer mismatches. It also checked primary-ID uniqueness, deterministic IDs, reference prefixes/suffixes, counts, and completed-file hashes. Resume verified/skipped all 74 files.
- Rendered and inspected both sheets of all four new mapping workbooks; canonical JSON and workbook models match exactly.
- Developer guide includes macOS/Linux and Windows setup, VS Code debugging, detached job lifecycle, external CLI adapter contract, mapping extension instructions, and live SQL setup. The background process path was exercised on macOS; Windows lifecycle is implemented but untested. Devin was not installed, so only its external-wrapper integration contract is supplied, without a claim of native Devin validation.
- No live SQL Server database extraction or optional AdventureWorks staging SQL was run. The existing native ODBC dependency limitation still applies.

## Original engine 1.0 validation

Validated on 2026-09-09 with Python 3.12.14 on macOS ARM64.

- `python -m pytest -q`: **81 passed** (1.42 seconds on the development run).
- `python -m pip check`: no broken Python requirements.
- `python -m compileall -q src`: passed.
- Actual CLI `validate`, `dry-run`, `export`, and `export --resume` completed with the supplied offline fixture.
- Demo: 36 monthly files, five data rows, then zero rewritten files and 36 skipped files on resume.
- Memory regression: 20,000 generated records produced more than 20 MB in one window while Python allocations measured by tracemalloc stayed below 5 MiB. This checks application accumulation, not native ODBC buffering or total process RSS.
- Sample workbook: inspected all values, rendered both sheets, scanned for formula errors, exported and reimported through the CLI reader. A valid XLSX without worksheet dimensions is covered by the actual fixture.
- Legacy XLS: generated binary BIFF test fixture imported successfully; a cached-formula fixture was rejected.
- Recovery: injected source failure, interruption after data rename but before manifest commit, output corruption, mapping/settings/source changes, lock contention, and frozen rolling-range resume were exercised.
- SQL adapter: mocked metadata preflight, SQL parameter binding, bounded fetchmany calls, timeout configuration, cleanup, and snapshot commit/rollback were exercised. Selected datetime2 scale 7 is rejected to prevent precision loss.

No live SQL Server connection, query-plan benchmark, production extraction, real-driver memory measurement, native Excel application validation, or network-filesystem/power-loss validation was performed. The pyodbc wheel installed, but importing it on this development host reported a missing unixODBC dynamic library. Install the native dependencies and perform the scratch-database smoke test described in README before use against real data.

Python package versions used: openpyxl 3.1.5, xlrd 2.0.2, olefile 0.47, filelock 3.32.6, pyodbc 5.3.0, pytest 9.1.1, xlwt 1.3.0. xlwt is only used to construct test fixtures.
