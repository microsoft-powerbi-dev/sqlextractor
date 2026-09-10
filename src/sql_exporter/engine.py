import hashlib
import os
import platform
from importlib.metadata import version
from pathlib import Path

from filelock import FileLock, Timeout

from . import __version__
from .common import ExportError, canonical, file_hash, fingerprint, require, strict_json
from .config import requested_settings
from .serialization import record
from .transforms import transform
from .windows import iso, resolve_range, timestamp, window_name, windows


def sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def private_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def atomic_json(path, value):
    partial = path.with_name(path.name + ".part")
    partial.unlink(missing_ok=True)
    with private_file(partial) as stream:
        stream.write((canonical(value) + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, path)
    sync_directory(path.parent)


def read_manifest(path):
    try:
        value = strict_json(path.read_text(encoding="utf-8"))
        require(isinstance(value, dict), "Manifest must be an object")
        return value
    except (OSError, ExportError) as exc:
        raise ExportError(f"Cannot read {path.name}; restore it or use a new output directory") from exc


def make_plan(config, mapping, saved=None):
    if saved is not None:
        start, end = timestamp(saved["start"]), timestamp(saved["end"])
    else:
        start, end = resolve_range(config["start"], config["end"], config["years"])
    intervals = list(windows(start, end, config["granularity"]))
    require(len(intervals) <= 40000, "Too many windows")
    return {"start": iso(start), "end": iso(end), "windows": [
        {"name": window_name(a, b), "start": iso(a), "end": iso(b)} for a, b in intervals]}


def export(config, mapping, source, resume=False, progress=None):
    output = Path(config["output_dir"])
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not any(p.is_symlink() for p in output.iterdir()), "Output directory contains symlinks")
    lock = FileLock(str(output / ".export.lock"), timeout=0)
    try:
        with lock:
            return _export_locked(config, mapping, source, output, resume, progress)
    except Timeout as exc:
        raise ExportError("Another export holds this output directory lock") from exc


def _export_locked(config, mapping, source, output, resume, progress):
    run_path = output / "run.json"
    base = {"manifest_version": 1, "engine_version": __version__, "mapping_fingerprint": mapping["fingerprint"],
            "settings": requested_settings(config), "source_identity": source.identity,
            "runtime": {"python": platform.python_version(), "packages": {
                name: version(name) for name in ["openpyxl", "xlrd", "olefile", "filelock"]
                + (["pyodbc"] if config["source"] == "sqlserver" else [])}}}
    if resume:
        require(run_path.is_file(), "Resume requires an existing run.json")
        run = read_manifest(run_path)
        require(all(run.get(k) == v for k, v in base.items()), "Resume refused: mapping, engine, source, or settings changed; use a new output directory")
        require(run.get("run_fingerprint") == fingerprint({k: v for k, v in run.items() if k != "run_fingerprint"}), "Run manifest fingerprint is invalid")
        plan = make_plan(config, mapping, run["plan"])
        require(plan == run["plan"], "Run window plan is invalid")
    else:
        require(not any(p.name != ".export.lock" for p in output.iterdir()), "Output directory is not empty; use --resume or a new directory")
        plan = make_plan(config, mapping)
        run = base | {"plan": plan, "mapping": mapping["model"], "workbook_sha256": mapping["workbook_sha256"]}
        run["run_fingerprint"] = fingerprint(run)
        atomic_json(run_path, run)
    expected = {"run.json", ".export.lock", "failure.json", "failure.json.part"}
    for window in plan["windows"]:
        expected.update(window["name"] + suffix for suffix in (".txt", ".txt.part", ".json", ".json.part"))
    require(all(p.name in expected and not p.is_symlink() for p in output.iterdir()), "Output directory contains unexpected files or symlinks")
    completed = {}
    # Verify all completed windows before writing any new data.
    for window in plan["windows"]:
        manifest_path = output / (window["name"] + ".json")
        if manifest_path.exists():
            manifest = read_manifest(manifest_path)
            data_path = output / (window["name"] + ".txt")
            require(manifest.get("run_fingerprint") == run["run_fingerprint"] and manifest.get("window") == window
                    and manifest.get("status") == "complete" and type(manifest.get("rows")) is int and manifest["rows"] >= 0,
                    "Completed window manifest is incompatible")
            require(data_path.is_file() and data_path.stat().st_size == manifest.get("bytes")
                    and file_hash(data_path) == manifest.get("sha256"), "Completed output failed integrity verification; restore it or start a new directory")
            completed[window["name"]] = manifest
    written = skipped = total_rows = 0
    for window in plan["windows"]:
        name = window["name"]
        if name in completed:
            skipped += 1
            total_rows += completed[name]["rows"]
            for suffix in (".txt.part", ".json.part"):
                (output / (name + suffix)).unlink(missing_ok=True)
            continue
        partial = output / (name + ".txt.part")
        final = output / (name + ".txt")
        # These are uncommitted artifacts owned by the verified run; restart the whole window.
        for path in (partial, final, output / (name + ".json.part")):
            path.unlink(missing_ok=True)
        rows = 0
        digest = hashlib.sha256()
        byte_count = 0
        iterator = None
        try:
            with private_file(partial) as stream:
                def write(values):
                    nonlocal byte_count
                    data = record(values, config["null_token"])
                    stream.write(data)
                    digest.update(data)
                    byte_count += len(data)
                if config["header"]:
                    write([c["target"] for c in mapping["model"]["columns"]])
                iterator = iter(source.rows(timestamp(window["start"]), timestamp(window["end"])))
                for row in iterator:
                    write(transform(row, mapping["model"]["columns"]))
                    rows += 1
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(partial, final)
            sync_directory(output)
            manifest = {"status": "complete", "run_fingerprint": run["run_fingerprint"], "window": window,
                        "rows": rows, "bytes": byte_count, "sha256": digest.hexdigest()}
            atomic_json(output / (name + ".json"), manifest)
            written += 1
            total_rows += rows
            if progress:
                progress(f"Completed {window['start']} to {window['end']}: {rows} rows")
        except Exception as exc:
            atomic_json(output / "failure.json", {"run_fingerprint": run["run_fingerprint"], "window": window,
                                                   "rows_written_before_failure": rows, "status": "failed"})
            message = str(exc) if isinstance(exc, ExportError) else "I/O or source failure"
            raise ExportError(f"Window {name} failed after {rows} rows: {message}. Correct the issue and resume with unchanged settings, or start a new directory.") from exc
        finally:
            if iterator is not None and hasattr(iterator, "close"):
                iterator.close()
    (output / "failure.json").unlink(missing_ok=True)
    (output / "failure.json.part").unlink(missing_ok=True)
    return {"output_dir": str(output), "windows_written": written, "windows_skipped": skipped,
            "rows": total_rows, "run_fingerprint": run["run_fingerprint"]}
