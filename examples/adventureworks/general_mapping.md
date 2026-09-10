# Sales order header business mapping

Create one mapping named aw_orders, version 1.0.0, contract version 1.
The source is Sales.SalesOrderHeader. Source SalesOrderID and CustomerID are
non-null integer keys. OrderDate is a non-null UTC datetime. SubTotal is an exact
SQL money/decimal value. Comment is nullable text. This document describes all
six required output columns in their exact output order.

1. order_uid: non-null string, generated as UUIDv5 with namespace
   `adventureworks/order` from the source integer SalesOrderID. No source value
   is copied directly into this target. Use the identity rule with that one key.
2. customer_uid: non-null string, generated as UUIDv5 with namespace
   `adventureworks/customer` from source integer CustomerID. The customer reference
   export must use the same namespace and integer key to reproduce this value.
3. order_reference: non-null string. Read SalesOrderID, explicitly convert to text,
   add prefix `AW-SO-`, then append suffix `-WEB`. For 43659 expect AW-SO-43659-WEB.
4. order_date: non-null datetime copied from OrderDate, canonical UTC output.
5. subtotal: non-null decimal copied from SubTotal, scale 2, ROUND_HALF_UP.
6. comment: non-null string copied from Comment. Replace null with empty text,
   trim leading/trailing whitespace, add prefix `[AW] `, and suffix ` / export`.

No sequential row numbers, joins, aggregations, or executable prose are required.
