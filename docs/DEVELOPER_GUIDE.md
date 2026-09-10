# Developer guide: install, normalize mappings, export, and extend

This guide takes a developer from a clean machine to a tested export. The repository has two separate execution paths: an optional AI job converts a general business mapping document into structured metadata; the deterministic export engine validates that metadata and streams source rows. Exporting does not call an LLM.

## 1. Get the repository and tools

Clone your copy of the repository or extract the delivered source ZIP and enter its `sql-exporter` directory. No remote Git repository is assumed or configured by this project.

Install Python 3.10+ and VS Code. Python 3.12.14 was used for the recorded validation. Python dependencies install from PyPI; do not copy a `.venv` from another machine. No Node.js, Codex, or SQL Server installation is needed for offline exports and tests. The sample Excel workbooks are already supplied.

For optional AI normalization, install a supported Node.js runtime and Codex CLI using [OpenAI's CLI installation instructions](https://developers.openai.com/codex/cli). A Devin integration uses the external adapter contract in step 9; no native Devin CLI installation was found or tested on the development machine.

## 2. Create an isolated Python environment

macOS/Linux:

```sh
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pip check
```

Windows PowerShell:

```powershell
py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m pip check
```

If shell activation is restricted, invoke `.venv/bin/python` on macOS/Linux or `.venv\Scripts\python.exe` on Windows directly. Do not change system execution policy just to activate a virtual environment. Install using that interpreter's `-m pip`, then use `python -m sql_exporter` in place of `sql-exporter` if the executable is not on PATH.

## 3. Open and configure VS Code

Choose **File → Open Folder** and select the repository root (the folder containing pyproject.toml). Install the Microsoft Python and Python Debugger extensions if they are not present. Use **Python: Select Interpreter** from the Command Palette to select this folder's `.venv` interpreter. Open **Terminal → New Terminal** and activate the environment as above.

The supplied `.vscode/settings.json` configures pytest discovery. The Run and Debug menu includes validation, dry-run, and export configurations; it prompts for a config path. The selected Python interpreter is used. Start with validation so a debugger session does not accidentally launch a large export.

## 4. Run the automated tests

```sh
python -m pytest -q
```

The current suite includes calendar boundaries, serialization, decimal conversions, mapping validation, identity/reference rules, source projection, partial-file recovery, resume integrity, and detached background-worker success/failure/cancel/timeout tests. Test workers use a local fake external provider; tests do not call Codex, Devin, a database, or the network. The larger AdventureWorks downloads/exports are a separate integration example, not part of the quick unit suite.

## 5. Run the small offline example

```sh
sql-exporter validate --config examples/demo.json
sql-exporter dry-run --config examples/demo.json
sql-exporter export --config examples/demo.json
sql-exporter export --config examples/demo.json --resume
```

A fresh checkout produces 36 monthly TXT files and five rows in `exports/demo`. The resume invocation verifies and skips all 36 files. Inspect a TXT and its same-name JSON manifest. Files use a quoted pipe-delimited CSV dialect; use `csv.reader(..., delimiter="|")`, not string splitting.

If you are working in the original development directory and exports/demo already belongs to engine 1.0, the 1.1 engine will refuse resume. Preserve the old output. Copy the config, use an unused output_dir such as `../exports/demo-v1_1`, and start a fresh export. Engine/runtime/mapping/settings changes intentionally prevent mixed runs.

## 6. Run the large related AdventureWorks example

```sh
python examples/adventureworks/run_demo.py prepare
python examples/adventureworks/run_demo.py export
python examples/adventureworks/run_demo.py verify
python examples/adventureworks/run_demo.py export --resume
```

Skip prepare if fixtures are already present. Preparation downloads four pinned Microsoft sample files and verifies their hashes. It copies selected values into JSONL, enriches details with parent OrderDate/CustomerID, and collects the used customer/product references. The dates are an explicit historical sample interval, 2022-07-01 through 2025-07-01 exclusive. Master references are a separate one-day snapshot label.

Expected output: **74 TXT files and 170,597 total rows**: 31,201 orders, 120,029 details, 19,101 customers, 266 products. Four identities can appear in each detail: detail_uid, order_uid, customer_uid, product_uid. Verification checks their uniqueness, generation, parent/customer/product references, counts, prefixes/suffixes, and file integrity. Results and first rows are in `exports/adventureworks/verification.json`; source URLs/hashes are in provenance.json.

See `examples/adventureworks/README.md` for the relationship diagram, table-level mappings, fixed-date explanation, and optional SQL staging route. The fixture adapter scans its file per window, so this example's runtime is not a SQL Server performance benchmark.

## 7. Understand the standardized mapping layer

All supported mappings enter one canonical model:

```json
{
  "metadata": {"contract_version": 1, "mapping_id": "orders", "mapping_version": "1.0.0"},
  "columns": [
    {
      "ordinal": 1, "source": null, "target": "order_uid",
      "type": "string", "nullable": false,
      "rules": [{"op":"identity","namespace":"adventureworks/order","keys":["SalesOrderID"]}],
      "scale": null
    }
  ]
}
```

Excel `.xls`/`.xlsx` and canonical `.json` map to the same validator and semantic fingerprint. `normalize_model` handles JSON; `parse_mapping` handles the workbook tables. Changing workbook formatting does not change semantics. Changing an identity namespace, ordered key set, output column, nullability, rule, or mapping version does.

Use exact source identifiers. Every source field referenced by identity/template rules is included in SQL projection and preflight checks, even when the target's source cell is blank. Rules reference source values, never a previously calculated output column. Identity uses typed JSON key values and UUIDv5; parent and foreign-key mappings must agree on namespace and key types. These are deterministic generated identifiers, not counters or SQL table identity columns.

Prefix/suffix require strings. A numeric reference pipeline is `to_string → prefix → suffix`. Template supports plain `{SourceColumn}` placeholders and literal text, followed by other operations. It cannot evaluate expressions, traverse attributes, or access files. Full rules and type handling are in `MAPPING.md`.

## 8. Start Codex normalization in the background

Install and authenticate Codex once using your account:

```sh
npm install -g @openai/codex
codex login
codex login status
codex --version
codex exec --help
```

The native adapter was tested with Codex CLI 0.153.4. It uses documented noninteractive execution, JSON events, structured final output, and read-only sandbox options. See [OpenAI's noninteractive documentation](https://developers.openai.com/codex/noninteractive). Your account supplies authentication and usage; the repository does not contain a key or model override.

Start with the included general document:

```sh
sql-exporter mapping-job start --input examples/adventureworks/general_mapping.md --timeout 600
```

The command returns immediately with job_dir and PID. It launches a detached Python worker, which invokes Codex CLI with the extracted document and the supported mapping contract. The actual Codex sample run completed successfully and produced exactly the same normalized order mapping as the independently defined example.

Use the returned job_dir, replacing the placeholder below:

```sh
sql-exporter mapping-job status --job-dir ".mapping-jobs/RETURNED_JOB_ID"
sql-exporter mapping-job cancel --job-dir ".mapping-jobs/RETURNED_JOB_ID"
```

If Codex is installed outside PATH, pass `--codex-bin /absolute/path/to/codex`. `--jobs-dir` selects where jobs live. Timeout defaults to 600 seconds and may be 1–3,600 seconds. This is a one-shot background process, not a scheduler, daemon, file watcher, or reboot-persistent service. The start command does not wait for completion; use status to inspect it.

Accepted documents are UTF-8 Markdown/text/JSON and general XLS/XLSX sheets. Excel is read as tabular data before prompting; formulas and Excel errors are rejected. Input is limited to 10 MiB on disk and 200 KB of extracted text. Convert PDF/Word to text first; those formats and OCR are not implemented. No general business document was supplied, so the included sample was used for the recorded run. The selected document is sent to the chosen model provider through its CLI.

The worker supports these states:

The status command returns exit code 0 when it successfully reads a status, even when the reported job state is failed; automation must inspect the JSON `status` field.

| State | Meaning / next action |
| --- | --- |
| queued / running | Worker owns the job; inspect logs/status as needed. |
| completed | Candidate parsed and passed all local contract checks; proposed mappings are ready to review/use. |
| failed | Provider returned an error, invalid JSON, unsupported rule, invalid contract, or unresolved requirement. Inspect error and candidate/issues, revise the document, start a new job. |
| timed_out / cancelled | Provider process was stopped; no mapping result is promoted. Start another job when appropriate. |
| interrupted | Status detected that the worker PID no longer exists. Start a new job. A system crash/reboot is not automatically resumed. |

Job artifacts:

- `prompt.txt`: frozen extracted document plus contract. Treat as confidential if the source document is confidential.
- `job.json`, `worker.json`, `status.json`: input hash, provider settings, worker PID, lifecycle.
- `events.jsonl`, `provider.log`, `worker.log`: Codex events/diagnostics and worker errors. External mode uses candidate.json directly and need not have events.jsonl.
- `candidate.json`: raw structured provider response. Its issues identify uncertainty or unsupported semantics.
- `mappings/*.json`: canonical locally validated proposals, created only after the entire response passes validation.
- `result.json`: proposal paths, fingerprints, input/candidate hashes, and issues.

The job does not edit repository code, change export configs, connect to SQL Server, or append export records. The prompt tells the model to treat document content as data and use no tools. Read-only sandboxing restricts model file writes; it is not a promise that the external CLI has no access to the user's other configured capabilities. Use only trusted local CLI configuration, and keep source credentials out of mapping documents. POSIX background lifecycle/cancellation was exercised on macOS; the Windows process path is implemented but not verified here, and descendants of an external Windows wrapper may need wrapper-owned cleanup.

Review business meaning, source column names, nullability, scales, key namespaces, and unresolved warnings. Validation proves that the proposed contract is executable, not that an inferred business rule is correct. No approval gate is built into the CLI: after your review, copy the desired JSON to a stable location, set config.mapping to that path, select a fresh output_dir, and run validate/dry-run/export.

Example adoption on macOS/Linux (replace the job ID):

```sh
cp .mapping-jobs/RETURNED_JOB_ID/mappings/aw_orders.json examples/adventureworks/reviewed-orders.mapping.json
cp examples/adventureworks/orders.json examples/adventureworks/reviewed-orders.json
```

Edit reviewed-orders.json: set `mapping` to `reviewed-orders.mapping.json` and `output_dir` to `../../exports/reviewed-orders`. Then:

```sh
sql-exporter validate --config examples/adventureworks/reviewed-orders.json
sql-exporter dry-run --config examples/adventureworks/reviewed-orders.json
sql-exporter export --config examples/adventureworks/reviewed-orders.json
```

## 9. Connect another CLI, including a Devin wrapper

There is a native Codex adapter and a generic **external** adapter. A native Devin command syntax is not assumed, and no Devin integration was run. To integrate your organization's installed Devin CLI, write a small wrapper around its documented noninteractive interface that:

1. Reads the supplied prompt from stdin.
2. Passes it to the approved CLI/provider.
3. Waits for completion.
4. Prints only one JSON object to stdout with root `mappings` and `issues`. Column `rules_json` is a JSON array encoded as text; consult the generated response.schema.json or `response_schema()` in mapping_jobs.py.
5. Writes diagnostics to stderr and exits nonzero on failure. It must own cleanup for any remote job or subprocess it creates.

Put an argument array in a local JSON file, for example:

```json
["/absolute/path/to/python", "/absolute/path/to/devin_mapping_wrapper.py"]
```

Then run:

```sh
sql-exporter mapping-job start --provider external --command-file local-provider.json --input general-mapping.md
```

Arguments are passed with `shell=False`; shell strings, redirects, pipes, and command substitution are not interpreted. Use absolute wrapper paths because the provider working directory is the job's work folder. Authentication, remote cancellation, and provider-specific flags belong to the wrapper. The repository tests cover this external stdin/stdout contract with a real detached local subprocess.

## 10. Set up SQL Server in a new environment

Install the native Microsoft ODBC Driver 18 and the system ODBC manager as required by your OS, then:

```sh
python -m pip install -e '.[sqlserver,test]'
python -c "import pyodbc; print(pyodbc.drivers())"
```

On macOS the pyodbc wheel also requires unixODBC. Follow [Microsoft's macOS installation instructions](https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/install-microsoft-odbc-driver-sql-server-macos) or [driver downloads for your platform](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server). This development host lacked unixODBC; no native-driver import or live SQL query succeeded here.

Set SQL_EXPORTER_CONNECTION_STRING in the environment using your normal credential management. The connection string should specify server, database, authentication, encryption, and certificate verification. Do not put credentials in config JSON, Excel, Git, a normalization document, or provider logs. Supply a least-privilege account with SELECT and metadata visibility for the chosen table/staging tables.

Copy examples/sqlserver.json and edit source_id, schema, table, date_column, mapping, output_dir, and a short explicit start/end interval. Ensure the date data is UTC; the engine cannot infer a database timezone. Selected datetime2 columns need scale <=6. Then:

```sh
sql-exporter validate --config examples/sqlserver.json --connect
sql-exporter dry-run --config examples/sqlserver.json
sql-exporter export --config examples/sqlserver.json
```

Use your copied config path when running these commands. Compare counts with the same half-open predicate on a stable source, inspect sample decimal/date/Unicode values, and confirm the downstream parser accepts quoted multiline pipe records. For related AdventureWorks files, the optional stage_sqlserver.sql and four *.sqlserver.json configs show explicit join preparation. The generic engine does not execute workbook-supplied SQL or infer joins.

## 11. Change a mapping or add a rule

For a supported mapping change, edit its workbook or canonical JSON, increment mapping_version, validate, and export to a new output directory. No engine edits are needed for new columns, identity namespaces, prefix/suffix values, templates, defaults, or supported lookup changes.

For a new operation, update FIELDS, validate_rules, and apply_rules in transforms.py. If it reads other source columns, update rule_sources so SQL projection/preflight includes them. Add semantic tests for nulls, error cases, and type behavior. Update the normalization CONTRACT/schema guidance, MAPPING.md, and examples as needed. Increment the engine version in both pyproject.toml and __init__.py when behavior changes. Changing code without updating versions undermines resume provenance.

Run the relevant tests, then the suite. Avoid adding an eval path to make prose executable. For joins and cross-row logic, prepare a reviewed SQL staging source or design a separate explicit extension. Never insert order-dependent random IDs or counters into row transforms: they would break references/restarts unless a durable allocation design is added.

## 12. Prepare a release or handoff

Deliver src/, tests/, examples/ (excluding generated data unless intentionally distributing it), docs/, README.md, pyproject.toml, and editor configuration. Include the Microsoft license with AdventureWorks-derived output. Exclude `.venv`, `.git`, `__pycache__`, `.pytest_cache`, `.mapping-jobs`, credentials, and generated exports from the clean source ZIP. Distribute verified demo output as a separate bundle if wanted.

For reproducible deployment, record exact Python, package, native ODBC driver, and CLI versions in your deployment environment. pyproject.toml currently specifies compatible version ranges, not a cross-platform lockfile. Run manifests pin the observed runtime/package identities and refuse changed environments on resume. Keep the completed output directory intact and backed up; a manifest's SHA-256 checks corruption, not tamper-proof authenticity or historical database consistency.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| sql-exporter command missing | Activate the correct environment or use `python -m sql_exporter`. |
| Codex not found | Install/login, check PATH, or pass --codex-bin. |
| Native ODBC library load error | Install system unixODBC/driver; reinstalling only the Python wheel is insufficient. |
| Normalize job failed | Inspect status error, provider.log, candidate.json/issues; correct document ambiguity or CLI/auth failure, then start a new job. |
| Unknown op / invalid column | Use MAPPING.md exact names, keys, types, boolean values, and decimal scale. |
| Identity reference mismatch | Check namespaces, ordered key lists, and integer-versus-text key types across files. |
| Existing output / resume refused | Preserve it; use a new directory for changed mapping/settings/engine/runtime. |
| Export interrupted | Use --resume with unchanged settings; incomplete windows restart from the beginning. |
| Corrupt completed file | Restore a known-good matching file/manifest or start a new run; no silent repair. |
| No rows in restored AdventureWorks | Inspect its date range; different releases can use different historical years. |

Read `VALIDATION.md` for the recorded test evidence and remaining platform/database limitations.
