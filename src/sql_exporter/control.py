"""SQLite control database: the authoring registry for sources, targets, and mapping versions.

The engine never reads this database. `publish` writes the canonical mapping JSON and a config
file that `sql-exporter export` runs unchanged, so run provenance stays file-based.
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .common import canonical, fingerprint, identifier, require, strict_json
from .config import check_config
from .mapping import normalize_model
from .targets import check_against_target, file_settings, normalize_target
from .transforms import rule_sources

SCHEMA_VERSION = 1
FOLDERS = ("uploads", "targets", "mappings", "configs", "jobs", "exports")
SETTING_KEYS = {"years", "start", "end", "granularity", "batch_size", "order_by", "isolation",
                "query_timeout", "connect_timeout", "source_revision"}
DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connections (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, connection_env TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL CHECK (kind IN ('table', 'query', 'jsonl')),
  connection_id INTEGER REFERENCES connections(id), schema_name TEXT, table_name TEXT, query TEXT, fixture TEXT,
  date_column TEXT NOT NULL, settings_json TEXT NOT NULL, metadata_json TEXT, metadata_sha256 TEXT,
  captured_at REAL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS targets (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, spec_json TEXT NOT NULL, origin TEXT, origin_sha256 TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mappings (
  id INTEGER PRIMARY KEY, mapping_id TEXT NOT NULL, mapping_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft', 'approved', 'retired')),
  source_id INTEGER REFERENCES sources(id), target_id INTEGER REFERENCES targets(id),
  model_json TEXT NOT NULL, fingerprint TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, approved_at REAL, approved_by TEXT,
  mapping_path TEXT, config_path TEXT, UNIQUE (mapping_id, mapping_version));
CREATE TABLE IF NOT EXISTS mapping_lineage (
  mapping_pk INTEGER NOT NULL REFERENCES mappings(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL,
  target TEXT NOT NULL, target_type TEXT NOT NULL, source TEXT, inputs_json TEXT NOT NULL, rules_json TEXT NOT NULL,
  PRIMARY KEY (mapping_pk, ordinal));
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, job_dir TEXT NOT NULL UNIQUE, provider TEXT NOT NULL, status TEXT NOT NULL,
  document TEXT, document_sha256 TEXT, source_id INTEGER, target_id INTEGER, session_id TEXT, session_url TEXT,
  result_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS exports (
  id INTEGER PRIMARY KEY, mapping_pk INTEGER NOT NULL REFERENCES mappings(id), config_path TEXT NOT NULL,
  output_dir TEXT NOT NULL, status TEXT NOT NULL, rows INTEGER, windows_written INTEGER, windows_skipped INTEGER,
  run_fingerprint TEXT, error TEXT, started_at REAL NOT NULL, finished_at REAL);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, entity TEXT NOT NULL,
  entity_id INTEGER, detail_json TEXT NOT NULL);
"""


def _row(row, *json_fields):
    if row is None:
        return None
    value = dict(row)
    for key in json_fields:
        name = key[:-5]
        value[name] = strict_json(value.pop(key)) if value.get(key) else None
    return value


class Control:
    def __init__(self, workspace, create=False):
        self.root = Path(workspace).resolve()
        self.path = self.root / "control.db"
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            for name in FOLDERS:
                (self.root / name).mkdir(exist_ok=True)
        require(self.path.exists() or create, "Workspace has no control.db; run 'sql-exporter studio init --workspace DIR'")
        with self.db() as db:
            db.executescript(DDL)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            version = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
            require(version == str(SCHEMA_VERSION), "control.db schema version is not supported by this release")

    @contextmanager
    def db(self):
        # A connection per operation keeps the threaded Studio server free of shared cursors.
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def audit(self, db, actor, action, entity, entity_id, detail=None):
        db.execute("INSERT INTO audit (at, actor, action, entity, entity_id, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
                   (time.time(), actor, action, entity, entity_id, canonical(detail or {})))

    # Connections hold only the *name* of an environment variable; secrets never enter the workspace.
    def save_connection(self, name, connection_env, actor="human"):
        identifier(name)
        identifier(connection_env)
        with self.db() as db:
            db.execute("INSERT INTO connections (name, connection_env, created_at) VALUES (?, ?, ?) "
                       "ON CONFLICT(name) DO UPDATE SET connection_env = excluded.connection_env", (name, connection_env, time.time()))
            pk = db.execute("SELECT id FROM connections WHERE name = ?", (name,)).fetchone()[0]
            self.audit(db, actor, "save", "connection", pk, {"connection_env": connection_env})
        return pk

    def connections(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM connections ORDER BY name")]

    def connection(self, pk):
        with self.db() as db:
            row = db.execute("SELECT * FROM connections WHERE id = ?", (pk,)).fetchone()
        require(row is not None, "Unknown connection")
        return dict(row)

    def save_source(self, name, kind, date_column, connection_id=None, schema=None, table=None, query=None,
                    fixture=None, settings=None, metadata=None, actor="human"):
        identifier(name)
        identifier(date_column)
        settings = settings or {}
        require(isinstance(settings, dict) and set(settings) <= SETTING_KEYS, "Unknown source setting")
        probe = self.source_config({"name": name, "kind": kind, "date_column": date_column, "connection_id": connection_id,
                                    "schema_name": schema, "table_name": table, "query": query, "fixture": fixture,
                                    "settings": settings}, "probe.json", "probe")
        check_config(probe, self.root / "configs")
        if metadata is not None:
            require(isinstance(metadata, list) and all(isinstance(c, dict) and identifier(c.get("name")) for c in metadata), "Invalid source metadata")
        now = time.time()
        with self.db() as db:
            db.execute(
                "INSERT INTO sources (name, kind, connection_id, schema_name, table_name, query, fixture, date_column, settings_json, "
                "metadata_json, metadata_sha256, captured_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET kind = excluded.kind, connection_id = excluded.connection_id, "
                "schema_name = excluded.schema_name, table_name = excluded.table_name, query = excluded.query, fixture = excluded.fixture, "
                "date_column = excluded.date_column, settings_json = excluded.settings_json, "
                "metadata_json = COALESCE(excluded.metadata_json, sources.metadata_json), "
                "metadata_sha256 = COALESCE(excluded.metadata_sha256, sources.metadata_sha256), "
                "captured_at = COALESCE(excluded.captured_at, sources.captured_at)",
                (name, kind, connection_id, schema, table, probe.get("query"), fixture, date_column, canonical(settings),
                 None if metadata is None else canonical(metadata), None if metadata is None else fingerprint(metadata),
                 None if metadata is None else now, now))
            pk = db.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()[0]
            self.audit(db, actor, "save", "source", pk, {"kind": kind})
        return pk

    def sources(self):
        with self.db() as db:
            return [_row(r, "settings_json", "metadata_json") for r in db.execute("SELECT * FROM sources ORDER BY name")]

    def source(self, pk):
        with self.db() as db:
            row = _row(db.execute("SELECT * FROM sources WHERE id = ?", (pk,)).fetchone(), "settings_json", "metadata_json")
        require(row is not None, "Unknown source")
        return row

    def source_config(self, source, mapping_path, output_dir):
        config = {"config_version": 1, "source_id": source["name"], "date_column": source["date_column"],
                  "mapping": mapping_path, "output_dir": output_dir} | (source.get("settings") or {})
        if source["kind"] == "jsonl":
            require(source["fixture"] and source["schema_name"] and source["table_name"], "jsonl sources need fixture, schema and table")
            config |= {"source": "jsonl", "fixture": str(Path(source["fixture"]).resolve()), "schema": source["schema_name"], "table": source["table_name"]}
        else:
            require(source["connection_id"] is not None, "SQL Server sources need a connection")
            config |= {"source": "sqlserver", "connection_env": self.connection(source["connection_id"])["connection_env"]}
            if source["kind"] == "query":
                require(source["query"] and not source["table_name"], "Query sources need a query and no table")
                config["query"] = source["query"]
            else:
                require(source["schema_name"] and source["table_name"], "Table sources need schema and table")
                config |= {"schema": source["schema_name"], "table": source["table_name"]}
        return config

    def save_target(self, spec, origin=None, origin_sha256=None, actor="human"):
        spec = normalize_target(spec)
        now = time.time()
        with self.db() as db:
            db.execute("INSERT INTO targets (name, spec_json, origin, origin_sha256, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                       "ON CONFLICT(name) DO UPDATE SET spec_json = excluded.spec_json, origin = excluded.origin, "
                       "origin_sha256 = excluded.origin_sha256, updated_at = excluded.updated_at",
                       (spec["name"], canonical(spec), origin, origin_sha256, now, now))
            pk = db.execute("SELECT id FROM targets WHERE name = ?", (spec["name"],)).fetchone()[0]
            self.audit(db, actor, "save", "target", pk, {"origin": origin, "columns": len(spec["columns"])})
        return pk

    def targets(self):
        with self.db() as db:
            return [_row(r, "spec_json") for r in db.execute("SELECT * FROM targets ORDER BY name")]

    def target(self, pk):
        with self.db() as db:
            row = _row(db.execute("SELECT * FROM targets WHERE id = ?", (pk,)).fetchone(), "spec_json")
        require(row is not None, "Unknown target")
        return row

    def review(self, model, source_id=None, target_id=None):
        """Value-free issues that block approval. Drafts may be saved with issues."""
        issues = []
        if target_id is not None:
            issues += check_against_target(model, self.target(target_id)["spec"])
        if source_id is not None:
            metadata = self.source(source_id)["metadata"]
            if metadata:
                known = {c["name"] for c in metadata}
                used = {c["source"] for c in model["columns"] if c["source"]}
                for column in model["columns"]:
                    used.update(rule_sources(column["rules"]))
                issues += [f"Source column {name} is not in the captured source metadata" for name in sorted(used - known)]
        return issues

    def save_mapping(self, model, source_id=None, target_id=None, actor="human", pk=None):
        model = normalize_model(model)
        meta = model["metadata"]
        issues = self.review(model, source_id, target_id)
        now = time.time()
        with self.db() as db:
            existing = db.execute("SELECT id, status FROM mappings WHERE mapping_id = ? AND mapping_version = ?",
                                  (meta["mapping_id"], meta["mapping_version"])).fetchone()
            require(existing is None or existing["status"] == "draft",
                    "That mapping version is approved or retired and immutable; bump mapping_version to edit")
            require(pk is None or existing is None or existing["id"] == pk, "Another draft already uses this mapping_id and version")
            if pk is not None and existing is None:
                current = db.execute("SELECT status FROM mappings WHERE id = ?", (pk,)).fetchone()
                require(current is not None and current["status"] == "draft", "Only drafts can be edited")
            pk = pk or (existing["id"] if existing else None)
            values = (meta["mapping_id"], meta["mapping_version"], source_id, target_id, canonical(model), fingerprint(model), now)
            if pk is None:
                pk = db.execute("INSERT INTO mappings (mapping_id, mapping_version, source_id, target_id, model_json, fingerprint, "
                                "updated_at, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
                                values + (actor, now)).lastrowid
            else:
                db.execute("UPDATE mappings SET mapping_id = ?, mapping_version = ?, source_id = ?, target_id = ?, model_json = ?, "
                           "fingerprint = ?, updated_at = ? WHERE id = ?", values + (pk,))
            db.execute("DELETE FROM mapping_lineage WHERE mapping_pk = ?", (pk,))
            db.executemany("INSERT INTO mapping_lineage VALUES (?, ?, ?, ?, ?, ?, ?)", [
                (pk, c["ordinal"], c["target"], c["type"], c["source"],
                 canonical(list(dict.fromkeys(([c["source"]] if c["source"] else []) + rule_sources(c["rules"])))), canonical(c["rules"]))
                for c in model["columns"]])
            self.audit(db, actor, "save", "mapping", pk, {"fingerprint": fingerprint(model), "issues": len(issues)})
        return {"id": pk, "fingerprint": fingerprint(model), "issues": issues, "status": "draft"}

    def mappings(self):
        with self.db() as db:
            return [dict(r) for r in db.execute(
                "SELECT id, mapping_id, mapping_version, status, source_id, target_id, fingerprint, created_by, created_at, "
                "updated_at, approved_at, approved_by, mapping_path, config_path FROM mappings ORDER BY mapping_id, id")]

    def mapping(self, pk):
        with self.db() as db:
            row = _row(db.execute("SELECT * FROM mappings WHERE id = ?", (pk,)).fetchone(), "model_json")
            require(row is not None, "Unknown mapping")
            row["lineage"] = [_row(r, "inputs_json", "rules_json") for r in
                              db.execute("SELECT * FROM mapping_lineage WHERE mapping_pk = ? ORDER BY ordinal", (pk,))]
        return row

    def approve(self, pk, actor="human"):
        require(actor == "human", "Only a person can approve a mapping; agents may save drafts")
        row = self.mapping(pk)
        require(row["status"] == "draft", "Only drafts can be approved")
        require(row["source_id"] is not None and row["target_id"] is not None, "Link a source and a target before approving")
        issues = self.review(row["model"], row["source_id"], row["target_id"])
        require(not issues, "Resolve review issues before approving: " + "; ".join(issues[:5]))
        with self.db() as db:
            db.execute("UPDATE mappings SET status = 'approved', approved_at = ?, approved_by = ? WHERE id = ? AND status = 'draft'",
                       (time.time(), actor, pk))
            self.audit(db, actor, "approve", "mapping", pk, {"fingerprint": row["fingerprint"]})
        return self.publish(pk, actor)

    def retire(self, pk, actor="human"):
        with self.db() as db:
            changed = db.execute("UPDATE mappings SET status = 'retired', updated_at = ? WHERE id = ? AND status != 'retired'", (time.time(), pk)).rowcount
            require(changed == 1, "Unknown or already retired mapping")
            self.audit(db, actor, "retire", "mapping", pk)

    def build_config(self, row):
        stem = f"{row['mapping_id']}-{row['mapping_version']}"
        config = self.source_config(self.source(row["source_id"]), f"../mappings/{stem}.json", f"../exports/{stem}")
        return stem, config | file_settings(self.target(row["target_id"])["spec"])

    def publish(self, pk, actor="human"):
        """Write the files the engine runs. Each approved version gets its own immutable output_dir."""
        from .engine import atomic_json
        row = self.mapping(pk)
        require(row["status"] == "approved", "Only approved mappings can be published")
        stem, config = self.build_config(row)
        check_config(dict(config), self.root / "configs")
        written = {}
        for folder, value in (("mappings", row["model"]), ("configs", config)):
            path = self.root / folder / (stem + ".json")
            if path.exists():
                require(strict_json(path.read_text(encoding="utf-8")) == value,
                        f"{folder}/{path.name} exists with different content; it is immutable once published")
            else:
                atomic_json(path, value)
            written[folder] = str(path)
        with self.db() as db:
            db.execute("UPDATE mappings SET mapping_path = ?, config_path = ? WHERE id = ?", (written["mappings"], written["configs"], pk))
            self.audit(db, actor, "publish", "mapping", pk, {"config": Path(written["configs"]).name})
        return {"id": pk, "mapping_path": written["mappings"], "config_path": written["configs"]}

    def record_job(self, job_dir, provider, status, document=None, document_sha256=None, source_id=None, target_id=None,
                   session_id=None, session_url=None, result=None):
        now = time.time()
        with self.db() as db:
            db.execute("INSERT INTO jobs (job_dir, provider, status, document, document_sha256, source_id, target_id, session_id, "
                       "session_url, result_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                       "ON CONFLICT(job_dir) DO UPDATE SET status = excluded.status, "
                       "session_id = COALESCE(excluded.session_id, jobs.session_id), session_url = COALESCE(excluded.session_url, jobs.session_url), "
                       "result_json = COALESCE(excluded.result_json, jobs.result_json), updated_at = excluded.updated_at",
                       (str(job_dir), provider, status, document, document_sha256, source_id, target_id, session_id, session_url,
                        None if result is None else canonical(result), now, now))
            return db.execute("SELECT id FROM jobs WHERE job_dir = ?", (str(job_dir),)).fetchone()[0]

    def jobs(self):
        with self.db() as db:
            return [_row(r, "result_json") for r in db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 200")]

    def job(self, pk):
        with self.db() as db:
            row = _row(db.execute("SELECT * FROM jobs WHERE id = ?", (pk,)).fetchone(), "result_json")
        require(row is not None, "Unknown job")
        return row

    def start_export(self, pk, config_path, output_dir):
        with self.db() as db:
            return db.execute("INSERT INTO exports (mapping_pk, config_path, output_dir, status, started_at) VALUES (?, ?, ?, 'running', ?)",
                              (pk, config_path, output_dir, time.time())).lastrowid

    def finish_export(self, export_pk, result=None, error=None):
        result = result or {}
        with self.db() as db:
            db.execute("UPDATE exports SET status = ?, rows = ?, windows_written = ?, windows_skipped = ?, run_fingerprint = ?, "
                       "error = ?, finished_at = ? WHERE id = ?",
                       ("failed" if error else "completed", result.get("rows"), result.get("windows_written"),
                        result.get("windows_skipped"), result.get("run_fingerprint"), error, time.time(), export_pk))

    def exports(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM exports ORDER BY id DESC LIMIT 200")]

    def audit_log(self, limit=200):
        with self.db() as db:
            return [_row(r, "detail_json") for r in db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]
