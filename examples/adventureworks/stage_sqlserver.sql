-- Optional: execute manually against a restored AdventureWorks sample database.
-- The dates match the PINNED CSV sample used by this example (2022-2025).
-- Other AdventureWorks backups may have different dates: adjust BOTH this script
-- and all four *.sqlserver.json configs. No source tables are modified.
-- Existing ExportAW schema/tables cause CREATE statements to fail; nothing is dropped.
CREATE SCHEMA ExportAW;
GO
SELECT SalesOrderID, CustomerID, OrderDate, SubTotal, Comment
INTO ExportAW.Orders
FROM Sales.SalesOrderHeader
WHERE OrderDate >= '2022-07-01' AND OrderDate < '2025-07-01';
GO
SELECT d.SalesOrderID, d.SalesOrderDetailID, d.ProductID, d.OrderQty,
       d.UnitPrice, d.UnitPriceDiscount, d.LineTotal, d.CarrierTrackingNumber,
       h.OrderDate, h.CustomerID
INTO ExportAW.Details
FROM Sales.SalesOrderDetail d
JOIN ExportAW.Orders h ON h.SalesOrderID=d.SalesOrderID;
GO
SELECT c.CustomerID, c.AccountNumber, c.TerritoryID,
       CAST('2025-07-01' AS datetime2(6)) AS ExportDate
INTO ExportAW.Customers
FROM Sales.Customer c
WHERE EXISTS (SELECT 1 FROM ExportAW.Orders h WHERE h.CustomerID=c.CustomerID);
GO
SELECT p.ProductID, p.Name, p.ProductNumber, p.ListPrice,
       CAST('2025-07-01' AS datetime2(6)) AS ExportDate
INTO ExportAW.Products
FROM Production.Product p
WHERE EXISTS (SELECT 1 FROM ExportAW.Details d WHERE d.ProductID=p.ProductID);
GO
CREATE UNIQUE CLUSTERED INDEX IX_Orders ON ExportAW.Orders (OrderDate,SalesOrderID);
CREATE UNIQUE CLUSTERED INDEX IX_Details ON ExportAW.Details (OrderDate,SalesOrderID,SalesOrderDetailID);
CREATE UNIQUE CLUSTERED INDEX IX_Customers ON ExportAW.Customers (CustomerID);
CREATE UNIQUE CLUSTERED INDEX IX_Products ON ExportAW.Products (ProductID);
GO
SELECT 'orders' AS entity, COUNT_BIG(*) AS rows FROM ExportAW.Orders
UNION ALL SELECT 'details', COUNT_BIG(*) FROM ExportAW.Details
UNION ALL SELECT 'customers', COUNT_BIG(*) FROM ExportAW.Customers
UNION ALL SELECT 'products', COUNT_BIG(*) FROM ExportAW.Products;
