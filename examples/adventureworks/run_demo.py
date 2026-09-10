"""Download pinned Microsoft sample data, prepare related fixtures, export and verify."""
import argparse
import csv
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import urllib.request

from sql_exporter.common import ExportError, file_hash, require
from sql_exporter.config import load_config
from sql_exporter.engine import export, read_manifest
from sql_exporter.mapping import load_mapping
from sql_exporter.sources import open_source
from sql_exporter.transforms import identity_value

HERE = Path(__file__).resolve().parent
COMMIT = "b47eadc85277f72843647223cde53aa1b85ffcb8"
BASE = f"https://raw.githubusercontent.com/microsoft/sql-server-samples/{COMMIT}/samples/databases/adventure-works/oltp-install-script/"
HASHES = {
    "SalesOrderHeader.csv": "cf637c7d58006ebbc13daf0b8ed6cf7f738165d298ae0b5479a59237429c1882",
    "SalesOrderDetail.csv": "220129977aa63e34fa4a1d66c2604bd2184373151efb09c98b343371ba77ab8a",
    "Customer.csv": "4704aa5b46aed326599014524c821aca50e938587e6a1133c0f6d9aae5c5efc6",
    "Product.csv": "df0379a7cba8b11b97ea5d104cf1313ef9f683137b6426f2fac6bb8c74c2a2ea",
}
START, END = "2022-07-01", "2025-07-01"
NAMES = ("orders", "details", "customers", "products")


def raw_rows(path, width):
    # Microsoft BULK INSERT fixtures are TAB-delimited without CSV quote semantics.
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for number, row in enumerate(csv.reader(stream, delimiter="\t", quoting=csv.QUOTE_NONE), 1):
            require(len(row) == width, f"Unexpected {path.name} column count at row {number}")
            yield row


def write_row(stream, row):
    stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def prepare(raw_dir=None):
    data = HERE / "data"
    data.mkdir(exist_ok=True)
    raw = Path(raw_dir).resolve() if raw_dir else data / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for name, expected in HASHES.items():
        target = raw / name
        if not target.exists():
            print("Downloading " + name, file=sys.stderr)
            partial = target.with_suffix(".part")
            with urllib.request.urlopen(BASE + name, timeout=60) as response, partial.open("wb") as stream:
                shutil.copyfileobj(response, stream, length=1024 * 1024)
            require(file_hash(partial) == expected, "Downloaded sample hash mismatch")
            partial.replace(target)
        require(file_hash(target) == expected, f"Reference hash mismatch for {name}")
    require(not any((data / (name + ".jsonl")).exists() for name in NAMES),
            "Prepared fixtures already exist; use export/verify. To reprepare, use a clean copy of this example")
    # Only the 31k-row parent index and reference ID sets are in memory; details stream.
    headers, customers, products = {}, set(), set()
    counts, raw_counts = dict.fromkeys(NAMES, 0), dict.fromkeys(NAMES, 0)
    with (data / "orders.jsonl.part").open("w", encoding="utf-8") as orders:
        for row in raw_rows(raw / "SalesOrderHeader.csv", 26):
            raw_counts["orders"] += 1
            order = {"SalesOrderID": int(row[0]), "OrderDate": row[2], "CustomerID": int(row[10]),
                     "SubTotal": row[19], "Comment": row[23] or None}
            headers[order["SalesOrderID"]] = (order["OrderDate"], order["CustomerID"])
            if START <= order["OrderDate"] < END:
                customers.add(order["CustomerID"])
                write_row(orders, order)
                counts["orders"] += 1
    with (data / "details.jsonl.part").open("w", encoding="utf-8") as details:
        for row in raw_rows(raw / "SalesOrderDetail.csv", 11):
            raw_counts["details"] += 1
            order_id = int(row[0])
            require(order_id in headers, "Detail references a missing order")
            order_date, customer = headers[order_id]
            if START <= order_date < END:
                product_id = int(row[4])
                products.add(product_id)
                write_row(details, {"SalesOrderID": order_id, "SalesOrderDetailID": int(row[1]),
                         "CarrierTrackingNumber": row[2] or None, "OrderQty": int(row[3]), "ProductID": product_id,
                         "UnitPrice": row[6], "UnitPriceDiscount": row[7], "LineTotal": row[8],
                         "OrderDate": order_date, "CustomerID": customer})
                counts["details"] += 1
    with (data / "customers.jsonl.part").open("w", encoding="utf-8") as stream:
        for row in raw_rows(raw / "Customer.csv", 7):
            raw_counts["customers"] += 1
            if int(row[0]) in customers:
                write_row(stream, {"CustomerID": int(row[0]), "AccountNumber": row[4], "TerritoryID": int(row[3]) if row[3] else None, "ExportDate": END})
                counts["customers"] += 1
    with (data / "products.jsonl.part").open("w", encoding="utf-8") as stream:
        for row in raw_rows(raw / "Product.csv", 25):
            raw_counts["products"] += 1
            if int(row[0]) in products:
                write_row(stream, {"ProductID": int(row[0]), "Name": row[1], "ProductNumber": row[2], "ListPrice": row[9], "ExportDate": END})
                counts["products"] += 1
    require(counts["customers"] == len(customers) and counts["products"] == len(products), "Missing reference data")
    for name in NAMES:
        (data / (name + ".jsonl.part")).replace(data / (name + ".jsonl"))
    provenance = {"repository": "https://github.com/microsoft/sql-server-samples", "commit": COMMIT,
                  "raw_files": [{"url": BASE + name, "sha256": sha} for name, sha in HASHES.items()],
                  "raw_counts": raw_counts, "selected_counts": counts,
                  "transaction_start_inclusive": START, "transaction_end_exclusive": END,
                  "reference_snapshot_date": END, "reference_policy": "Only customers/products referenced by selected orders/details",
                  "enrichment": "Details receive OrderDate and CustomerID from SalesOrderHeader; reference ExportDate is an explicit snapshot label, not ModifiedDate",
                  "fixture_sha256": {name: file_hash(data / (name + ".jsonl")) for name in NAMES}}
    (data / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    return provenance


def run_exports(resume=False):
    results = {}
    require((HERE / "data/provenance.json").is_file(), "Run prepare first")
    provenance = read_manifest(HERE / "data/provenance.json")
    for name in NAMES:
        config = load_config(HERE / (name + ".json"))
        require(file_hash(config["fixture"]) == provenance["fixture_sha256"][name], "Prepared fixture has changed")
        mapping = load_mapping(config["mapping"])
        print("Exporting " + name, file=sys.stderr)
        with open_source(config, mapping) as source:
            results[name] = export(config, mapping, source, resume=resume,
                                   progress=lambda message: print(message, file=sys.stderr))
    return results


def output_rows(name):
    config = load_config(HERE / (name + ".json"))
    output = Path(config["output_dir"])
    run = read_manifest(output / "run.json")
    for window in run["plan"]["windows"]:
        path = output / (window["name"] + ".txt")
        manifest = read_manifest(path.with_suffix(".json"))
        require(manifest["status"] == "complete" and manifest["run_fingerprint"] == run["run_fingerprint"]
                and manifest["sha256"] == file_hash(path) and manifest["bytes"] == path.stat().st_size,
                "Invalid output integrity")
        with path.open(encoding="utf-8", newline="") as stream:
            yield from csv.DictReader(stream, delimiter="|")


def verify():
    counts = dict.fromkeys(NAMES, 0)
    previews = {}
    # SQLite keeps uniqueness/reference verification bounded for larger datasets.
    with tempfile.TemporaryDirectory(prefix="aw-check-") as directory, closing(sqlite3.connect(str(Path(directory) / "check.db"))) as db:
        db.executescript("CREATE TABLE customers(id TEXT PRIMARY KEY); CREATE TABLE products(id TEXT PRIMARY KEY); "
                         "CREATE TABLE orders(id TEXT PRIMARY KEY, customer TEXT); "
                         "CREATE TABLE details(id TEXT PRIMARY KEY, parent TEXT, customer TEXT, product TEXT);")
        for name in NAMES:
            previews[name] = []
            for row in output_rows(name):
                if name == "orders":
                    require(row["order_reference"].startswith("AW-SO-") and row["order_reference"].endswith("-WEB"), "Order affix mismatch")
                    source_id = int(row["order_reference"][6:-4])
                    require(row["order_uid"] == identity_value({"id": source_id}, "adventureworks/order", ["id"]), "Order identity mismatch")
                    db.execute("INSERT INTO orders VALUES (?,?)", (row["order_uid"], row["customer_uid"]))
                elif name == "details":
                    keys = {"SalesOrderID": int(row["sales_order_id"]), "SalesOrderDetailID": int(row["sales_order_detail_id"])}
                    require(row["detail_uid"] == identity_value(keys, "adventureworks/order-line", list(keys)), "Detail identity mismatch")
                    require(row["line_reference"] == f"AW-LINE-{keys['SalesOrderID']}/{keys['SalesOrderDetailID']}-ITEM", "Detail template/affix mismatch")
                    db.execute("INSERT INTO details VALUES (?,?,?,?)", (row["detail_uid"], row["order_uid"], row["customer_uid"], row["product_uid"]))
                else:
                    id_name = "customer" if name == "customers" else "product"
                    require(row[id_name + "_uid"] == identity_value({"id": int(row[id_name + "_id"])}, "adventureworks/" + id_name, ["id"]), "Reference identity mismatch")
                    db.execute("INSERT INTO " + name + " VALUES (?)", (row[id_name + "_uid"],))
                counts[name] += 1
                if len(previews[name]) < 3:
                    previews[name].append(row)
        checks = {
            "orders_missing_customer": "SELECT COUNT(*) FROM orders o LEFT JOIN customers c ON o.customer=c.id WHERE c.id IS NULL",
            "details_missing_order": "SELECT COUNT(*) FROM details d LEFT JOIN orders o ON d.parent=o.id WHERE o.id IS NULL",
            "details_missing_customer": "SELECT COUNT(*) FROM details d LEFT JOIN customers c ON d.customer=c.id WHERE c.id IS NULL",
            "details_missing_product": "SELECT COUNT(*) FROM details d LEFT JOIN products p ON d.product=p.id WHERE p.id IS NULL",
            "detail_customer_mismatch": "SELECT COUNT(*) FROM details d JOIN orders o ON d.parent=o.id WHERE d.customer<>o.customer",
        }
        checks = {name: db.execute(query).fetchone()[0] for name, query in checks.items()}
        require(not any(checks.values()), "Related file reference checks failed")
    provenance = read_manifest(HERE / "data/provenance.json")
    require(counts == provenance["selected_counts"], "Export row counts differ from prepared fixtures")
    result = {"status": "passed", "rows": counts, "reference_checks": checks, "previews": previews,
              "source_commit": COMMIT, "note": "Export identities, uniqueness, references, affixes, file hashes, and row counts verified. SQL Server was not used."}
    output = Path(load_config(HERE / "orders.json")["output_dir"]).parent
    (output / "verification.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    shutil.copy2(HERE / "data/provenance.json", output / "provenance.json")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "export", "verify"])
    parser.add_argument("--raw-dir", help="Use already downloaded Microsoft fixture files")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        result = prepare(args.raw_dir) if args.action == "prepare" else run_exports(args.resume) if args.action == "export" else verify()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ExportError, OSError, sqlite3.Error, ValueError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
