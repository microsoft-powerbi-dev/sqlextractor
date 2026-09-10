"""Example mapping definitions used to build the checked-in JSON/XLSX fixtures."""


def identity(entity, keys):
    return {"op": "identity", "namespace": "adventureworks/" + entity, "keys": keys}


def column(source, target, kind="string", rules=None, nullable=False, scale=None):
    return {"ordinal": 0, "source": source, "target": target, "type": kind,
            "nullable": nullable, "rules": rules or [], "scale": scale}


def models():
    outputs = {
        "orders": [
            column(None, "order_uid", rules=[identity("order", ["SalesOrderID"])]),
            column(None, "customer_uid", rules=[identity("customer", ["CustomerID"])]),
            column("SalesOrderID", "order_reference", rules=[{"op": "to_string"}, {"op": "prefix", "value": "AW-SO-"}, {"op": "suffix", "value": "-WEB"}]),
            column("OrderDate", "order_date", "datetime"),
            column("SubTotal", "subtotal", "decimal", scale=2),
            column("Comment", "comment", rules=[{"op": "default", "value": ""}, {"op": "trim"}, {"op": "prefix", "value": "[AW] "}, {"op": "suffix", "value": " / export"}]),
        ],
        "details": [
            column(None, "detail_uid", rules=[identity("order-line", ["SalesOrderID", "SalesOrderDetailID"])]),
            column(None, "order_uid", rules=[identity("order", ["SalesOrderID"])]),
            column(None, "customer_uid", rules=[identity("customer", ["CustomerID"])]),
            column(None, "product_uid", rules=[identity("product", ["ProductID"])]),
            column("SalesOrderID", "sales_order_id", "integer"),
            column("SalesOrderDetailID", "sales_order_detail_id", "integer"),
            column(None, "line_reference", rules=[{"op": "template", "value": "{SalesOrderID}/{SalesOrderDetailID}"}, {"op": "prefix", "value": "AW-LINE-"}, {"op": "suffix", "value": "-ITEM"}]),
            column("OrderDate", "order_date", "datetime"),
            column("OrderQty", "quantity", "integer"),
            column("UnitPrice", "unit_price", "decimal", scale=4),
            column("UnitPriceDiscount", "discount", "decimal", scale=4),
            column("LineTotal", "line_total", "decimal", scale=2),
            column("CarrierTrackingNumber", "tracking_reference", rules=[{"op": "default", "value": "UNTRACKED"}, {"op": "prefix", "value": "TRACK-"}, {"op": "suffix", "value": "-AW"}]),
        ],
        "customers": [
            column(None, "customer_uid", rules=[identity("customer", ["CustomerID"])]),
            column("CustomerID", "customer_id", "integer"),
            column("AccountNumber", "account_reference", rules=[{"op": "prefix", "value": "CRM-"}, {"op": "suffix", "value": "-ACTIVE"}]),
            column("TerritoryID", "territory_id", "integer", nullable=True),
        ],
        "products": [
            column(None, "product_uid", rules=[identity("product", ["ProductID"])]),
            column("ProductID", "product_id", "integer"),
            column("ProductNumber", "catalog_reference", rules=[{"op": "prefix", "value": "AW-"}, {"op": "suffix", "value": "-CAT"}]),
            column("Name", "name", rules=[{"op": "trim"}]),
            column("ListPrice", "list_price", "decimal", scale=2),
        ],
    }
    for name, columns in outputs.items():
        for ordinal, definition in enumerate(columns, 1):
            definition["ordinal"] = ordinal
        yield name, {"metadata": {"contract_version": 1, "mapping_id": "aw_" + name, "mapping_version": "1.0.0"}, "columns": columns}
