# SQL Exporter

A working Python CLI that reads a versioned Excel mapping, streams SQL Server table data for the last three calendar years, and writes pipe-delimited `.txt` files for calendar time windows. Monthly windows are the default. Mapping changes within the documented rule contract require no engine edits.

**New in 1.1:** [step-by-step developer setup](docs/DEVELOPER_GUIDE.md), [large related AdventureWorks example](examples/adventureworks/README.md), deterministic generated identities, prefix/suffix/template rules, canonical JSON mappings, and detached Codex/external CLI normalization jobs. Existing 1.0 export directories remain intact; use a new output directory after this engine upgrade.

The repository includes a synthetic, repeatable offline demo. No database connection or production extraction was performed during development. The SQL adapter has mocked tests; a real SQL Server smoke test is still required. On the development Mac, the pyodbc package installed but its system unixODBC library was absent.

## Run the offline demo

Use Python **3.10 or newer** (tested with 3.12.14). From this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
sql-exporter validate --config examples/demo.json
sql-exporter dry-run --config examples/demo.json
sql-exporter export --config examples/demo.json
sql-exporter export --config examples/demo.json --resume
python -m pytest -q
```

On Windows, use `.venv\Scripts\Activate.ps1` instead of `source`. All commands also work as `python -m sql_exporter ...`. Initial package installation needs access to the Python package index; the installed demo itself makes no network calls.

Expected result: **36 monthly TXT files, five data records total**, with a header in every file, including empty windows. The fixed demo interval is `[2023-09-01T00:00:00Z, 2026-09-01T00:00:00Z)`. IDs 0, 6, and 7 are excluded because they fall before the start, exactly at the exclusive end, or have a null date. The second export skips all 36 verified files.

Outputs live in `exports/demo/`. Use a new `output_dir` for a fresh run; the exporter refuses to overwrite an existing run without `--resume`. Do not delete a run simply to see the demo again; use its resume command or change the demo output path.

Files to start with:

- `examples/mapping.xlsx`: editable, validated sample workbook.
- `examples/demo.json` and `examples/orders.jsonl`: fixed, synthetic offline scenario.
- `examples/sqlserver.json`: SQL Server configuration template; no credentials.
- `docs/SOLUTION_DESIGN.md`: architecture, performance, consistency, recovery, and extensions.
- `docs/MAPPING.md`: exact workbook and transformation contract.
- `examples/sqlserver_demo.sql`: optional scratch-database setup, to execute manually.

## CLI workflows

| Command | Behavior |
| --- | --- |
| `validate --config FILE` | Validate config and workbook offline. Does not inspect source rows. |
| `validate --config FILE --connect` | Also open the source and inspect SQL table metadata/server identity, or hash the JSONL fixture. Does not extract data or create output files. |
| `dry-run --config FILE` | Show the exact planned windows, generated parameterized SQL, output path, and mapping fingerprint. No connection or output writes. |
| `dry-run --config FILE --resume` | Show the existing run's frozen date range. This is a plan preview; full resume compatibility and integrity checks happen during export. |
| `export --config FILE` | Validate, check source, acquire output lock, and stream every window. |
| `export --config FILE --resume` | Verify the run and all completed files, skip those files, and restart incomplete windows. |

JSON results go to stdout, per-window progress/errors to stderr. Exit codes: `0` success, `2` validation/extraction failure, `130` interruption. No source values or connection strings are printed by the CLI. Debuggers that inspect chained Python exceptions can see underlying driver details; do not publish raw tracebacks.

## SQL Server setup

1. Install the system ODBC manager and Microsoft ODBC Driver 18 for SQL Server, then `python -m pip install -e '.[sqlserver]'`. Installing pyodbc alone does not install these native dependencies. Follow Microsoft's instructions for [macOS](https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/install-microsoft-odbc-driver-sql-server-macos?view=sql-server-ver17) or [your platform](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server?view=sql-server-ver17).
2. Copy and edit `examples/sqlserver.json`: set table, schema, UTC date column, mapping, source identifier, and an unused output directory. One config exports one table. Use separate configs/directories for additional tables.
3. Set the environment variable named by `connection_env`, using your credential manager or deployment environment. The application never reads a password from the workbook or config. A connection-string shape is `Driver={ODBC Driver 18 for SQL Server};Server=HOST;Database=DATABASE;UID=EXPORT_USER;PWD=SECRET;Encrypt=yes;TrustServerCertificate=no`. Authentication and certificate configuration depend on your environment; see [Microsoft's connection settings](https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/connection-string-keywords-and-data-source-names-dsns?view=sql-server-ver17).
4. Use an account with SELECT access to the chosen table and visibility of its column metadata. Run `validate --connect`, inspect `dry-run`, then use a **small explicit start/end interval** and a scratch output directory for the first export.
5. Compare the scratch export's manifest row count with a source count using the same `>= start AND < end` predicate and the same stable dataset. Check Unicode, decimal, date, and null fields with the receiving system before running the full range.

The sample schema uses `datetime2(6)` and `decimal(38,3)`. Selected `datetime2(7)` columns are rejected to avoid silently losing the seventh fractional digit through Python's datetime type. A date filter column may have scale 7 when it is not selected. Prepare an explicitly approved staging column if precision reduction is acceptable. `datetimeoffset`, SQL variant, spatial/CLR data, and binary data transformations are not implemented. Decimal targets reject binary floats; use SQL decimal/numeric data or exact decimal text.

The exporter issues SELECTs and sets session isolation. It does not create indexes, alter databases, or update source rows. The optional setup SQL is for a disposable test database and is never executed automatically.

## Configuration

Config JSON rejects unknown or duplicate keys. Relative paths resolve against the config file, regardless of the shell's working directory.

| Setting | Required/default and meaning |
| --- | --- |
| `config_version` | Default `1`; only version 1 supported. |
| `source` | Required: `sqlserver` or `jsonl`. |
| `source_id` | Required logical dataset identifier. |
| `schema`, `table`, `date_column` | Required source identifiers. Sample assumes `dbo.Orders.CreatedAt`; there is no automatic date-column inference. |
| `mapping`, `output_dir` | Required paths. Mapping is `.xlsx` or `.xls`. |
| `connection_env` | SQL only; defaults to `SQL_EXPORTER_CONNECTION_STRING`. |
| `fixture` | JSONL only, required path. Fixture rows must contain all selected columns and date_column. |
| `source_revision` | Optional dataset/snapshot revision label. Set this when using an immutable extract or database snapshot. It is a label, not an automatically enforced database snapshot. |
| `start`, `end` | ISO dates/timestamps or null. End defaults to current UTC instant; start defaults to end minus `years` calendar years. Explicit bounds override the rolling calculation. |
| `years` | Default `3`, allowed 1–100. Leap-day anniversary clamps to February 28 when needed. |
| `granularity` | `day`, `month` (default), `quarter`, `year`; calendar-aligned UTC boundaries. |
| `batch_size` | Default 5,000, allowed 1–100,000 rows. |
| `header` | Default `true`; header names come from ordered mapping targets. |
| `null_token` | Default `\N`; 1–32 characters, no pipe/quote/CR/LF. Any identical non-null field or header causes failure. |
| `timezone` | Default and only supported value `UTC`. Naive input timestamps are interpreted as UTC. |
| `order_by` | Default `[]`; optional source columns, ascending. Use a unique combination for reproducible ordering. JSONL preserves fixture order and does not sort. |
| `isolation` | SQL: `read_committed` (default), or `snapshot` per window. JSONL accepts only `read_committed`. |
| `query_timeout`, `connect_timeout` | Defaults 300 and 30 seconds; positive integers, maximum 86,400 and 600. Query timeout is the driver's statement timeout, not a total-run deadline. |

For rolling exports, a month-aligned end produces 36 monthly files. An end in the middle of a month normally produces 37 files because both first and last calendar months are partial. The frozen exact range is stored in `run.json`; resume never moves it forward to a new current time.

## TXT format

UTF-8 without BOM, pipe delimiter, LF record terminator, double-quote escaping. Fields containing pipe, quote, CR, or LF are enclosed in double quotes; embedded quotes double. Embedded CR/LF and Unicode are preserved. Empty strings are `""`; null is the configured unquoted token. A non-null value equal to the null token is rejected so readers can distinguish null and empty text without custom quote tracking.

Files are CSV-style TXT, so a record can occupy multiple physical lines. Use a delimiter-aware parser; splitting on `|` or counting physical lines is incorrect. Decimal output uses a decimal point, fixed configured scale, no thousands separators, and `ROUND_HALF_UP`. Integer output is exact signed 64-bit text. Dates use `YYYY-MM-DD`; datetime output uses UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`; booleans are lowercase `true`/`false`. No locale-dependent formatting is used.

Example reader:

```python
import csv
with open("window.txt", encoding="utf-8", newline="") as stream:
    for row in csv.DictReader(stream, delimiter="|"):
        row = {key: None if value == r"\N" else value for key, value in row.items()}
```

## Recovery and changes

Consume a TXT only when its matching JSON manifest says `complete` and its SHA-256 and byte count match. `run.json` records the normalized mapping, workbook byte hash, mapping fingerprint, exact windows, source identity, engine/runtime versions, and settings. Each window manifest adds row count, byte count, and SHA-256.

On failure, `.txt.part` may remain; after a crash, an uncommitted `.txt` may also remain without a manifest. Under a verified resume, the exporter removes only those known incomplete artifacts and re-extracts the entire window. It never appends to a partial file. A failed row is not silently skipped or written to a reject file. `failure.json` identifies the window and number of prior rows without storing source values.

Changing mappings, versions, bounds, source identity, fixture contents, or settings requires a **new output directory**. Formatting-only workbook changes are allowed on resume because the canonical mapping is unchanged; the run retains the original workbook byte hash. A corrupt completed file is refused, not silently repaired. Restore it from a known-good copy or start a new run. A crash while initially creating `run.json` before it is committed also requires a new directory.

Resume preserves file/metadata consistency, not a historical database snapshot. READ COMMITTED can observe concurrent changes; SNAPSHOT is transaction-consistent within each window only and requires database configuration. For a repeatable three-year export, use an immutable source or database snapshot supplied by your database team. See [the solution design](docs/SOLUTION_DESIGN.md) for operational limits and extension points.
