import argparse
import json
import sys

from .common import ExportError
from .config import load_config
from .engine import export, make_plan, read_manifest
from .mapping import load_mapping
from .sources import open_source, select_sql
from .common import strict_json
from .targets import import_target
from pathlib import Path


def tooling(args):
    if args.command == "describe":
        from . import catalog
        if args.fixture:
            return catalog.describe_fixture(args.fixture)
        if args.query_file:
            return catalog.describe_query(args.connection_env, Path(args.query_file).read_text(encoding="utf-8"))
        schema, _, table = args.table.partition(".")
        return catalog.describe_table(args.connection_env, schema, table)
    if args.command == "target-spec":
        return import_target(args.input)
    if args.command == "mcp":
        from .mcp_server import serve
        serve(args.workspace, args.transport, args.host, args.port)
        return None
    from .control import Control
    if args.action == "init":
        control = Control(args.workspace, create=True)
        return {"workspace": str(control.root), "control_db": str(control.path)}
    from .studio.server import serve as studio
    studio(args.workspace, args.port, not args.no_browser)
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stream SQL Server data using a validated Excel mapping")
    sub = parser.add_subparsers(dest="command", required=True)
    job = sub.add_parser("mapping-job", help="Normalize a business mapping document in a detached background process")
    actions = job.add_subparsers(dest="action", required=True)
    start = actions.add_parser("start")
    start.add_argument("--input", help="Business mapping document; optional when --source-metadata and --target-spec are given")
    start.add_argument("--jobs-dir", default=".mapping-jobs")
    start.add_argument("--provider", choices=["codex", "external", "devin"], default="codex")
    start.add_argument("--source-metadata", help="JSON array from 'sql-exporter describe'")
    start.add_argument("--target-spec", help="Target layout: .xsd, .xlsx/.xls Target sheet, .json spec, or sample file")
    start.add_argument("--poll-interval", type=int, default=10, help="Devin status poll interval in seconds")
    start.add_argument("--command-file")
    start.add_argument("--codex-bin", default="codex")
    start.add_argument("--timeout", type=int, default=600)
    for action in ("status", "cancel"):
        actions.add_parser(action).add_argument("--job-dir", required=True)
    describe = sub.add_parser("describe", help="Print source column metadata (read-only; no rows)")
    describe.add_argument("--connection-env", default="SQL_EXPORTER_CONNECTION_STRING")
    group = describe.add_mutually_exclusive_group(required=True)
    group.add_argument("--table", help="schema.table or schema.view")
    group.add_argument("--query-file", help="File containing one read-only SELECT")
    group.add_argument("--fixture", help="Offline JSONL fixture")
    target = sub.add_parser("target-spec", help="Normalize a target layout file into a target spec JSON")
    target.add_argument("--input", required=True)
    studio = sub.add_parser("studio", help="Mapping Studio: control database and drag-and-drop UI")
    studio_actions = studio.add_subparsers(dest="action", required=True)
    studio_actions.add_parser("init").add_argument("--workspace", required=True)
    serve = studio_actions.add_parser("serve")
    serve.add_argument("--workspace", required=True)
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true")
    mcp = sub.add_parser("mcp", help="Serve SQL metadata and mapping tools over the Model Context Protocol")
    mcp.add_argument("--workspace", required=True)
    mcp.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8766)
    for name in ("validate", "dry-run", "export"):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True)
        if name == "validate":
            command.add_argument("--connect", action="store_true", help="Also validate source identity/schema (no extraction)")
        else:
            command.add_argument("--resume", action="store_true", help="Use the existing output run (dry-run shows its frozen range)")
    args = parser.parse_args(argv)
    try:
        if args.command == "mapping-job":
            from .mapping_jobs import start_job, status_job, cancel_job
            if args.action == "start":
                metadata = strict_json(Path(args.source_metadata).read_text(encoding="utf-8")) if args.source_metadata else None
                spec = import_target(args.target_spec)["spec"] if args.target_spec else None
                result = start_job(args.input, args.jobs_dir, args.provider, args.command_file, args.timeout, args.codex_bin,
                                   metadata, spec, args.poll_interval)
            else:
                result = (status_job if args.action == "status" else cancel_job)(args.job_dir)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.command in {"describe", "target-spec", "studio", "mcp"}:
            result = tooling(args)
            if result is not None:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        config = load_config(args.config)
        mapping = load_mapping(config["mapping"])
        if args.command == "validate":
            result = {"valid": True, "mapping_fingerprint": mapping["fingerprint"], "mapping": mapping["model"]["metadata"],
                      "targets": len(mapping["model"]["columns"]), "source_checked": False}
            if args.connect:
                with open_source(config, mapping) as source:
                    result.update(source_checked=True, source_identity=source.identity)
        elif args.command == "dry-run":
            saved = read_manifest(Path(config["output_dir"]) / "run.json")["plan"] if args.resume else None
            result = {"mapping_fingerprint": mapping["fingerprint"], "output_dir": config["output_dir"],
                      "plan": make_plan(config, mapping, saved), "sql": select_sql(config, mapping),
                      "parameter_contract": "UTC-naive datetime values: start inclusive, end exclusive",
                      "source_checked": False, "note": "No connection, rows, or output writes. Resume compatibility and file integrity are checked by export --resume."}
        else:
            with open_source(config, mapping) as source:
                result = export(config, mapping, source, args.resume, lambda text: print(text, file=sys.stderr))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ExportError, OSError, KeyError, TypeError, ValueError) as exc:
        # Raw driver errors and row values never reach the CLI.
        message = str(exc) if isinstance(exc, ExportError) else "Invalid config, manifest, or file access; check paths and permissions"
        print("Error: " + message, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Incomplete windows can be restarted with export --resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
