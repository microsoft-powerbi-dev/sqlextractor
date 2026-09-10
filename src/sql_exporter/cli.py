import argparse
import json
import sys

from .common import ExportError
from .config import load_config
from .engine import export, make_plan, read_manifest
from .mapping import load_mapping
from .sources import open_source, select_sql
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stream SQL Server data using a validated Excel mapping")
    sub = parser.add_subparsers(dest="command", required=True)
    job = sub.add_parser("mapping-job", help="Normalize a business mapping document in a detached background process")
    actions = job.add_subparsers(dest="action", required=True)
    start = actions.add_parser("start")
    start.add_argument("--input", required=True)
    start.add_argument("--jobs-dir", default=".mapping-jobs")
    start.add_argument("--provider", choices=["codex", "external"], default="codex")
    start.add_argument("--command-file")
    start.add_argument("--codex-bin", default="codex")
    start.add_argument("--timeout", type=int, default=600)
    for action in ("status", "cancel"):
        actions.add_parser(action).add_argument("--job-dir", required=True)
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
                result = start_job(args.input, args.jobs_dir, args.provider, args.command_file, args.timeout, args.codex_bin)
            else:
                result = (status_job if args.action == "status" else cancel_job)(args.job_dir)
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
