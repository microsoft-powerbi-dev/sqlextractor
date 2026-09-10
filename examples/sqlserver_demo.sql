-- OPTIONAL: Execute manually in a disposable test database only.
-- This script intentionally fails if dbo.Orders already exists. It drops nothing.
-- The exporter itself never executes this setup script.
CREATE TABLE dbo.Orders (
    OrderId bigint NOT NULL PRIMARY KEY,
    CreatedAt datetime2(6) NULL,
    Customer nvarchar(100) NULL,
    Amount decimal(38,3) NOT NULL,
    Status nvarchar(1) NOT NULL,
    Note nvarchar(max) NULL
);
GO
INSERT INTO dbo.Orders (OrderId, CreatedAt, Customer, Amount, Status, Note) VALUES
(0, '2023-08-31T23:59:59.999999', N'excluded before start', 1.000, N'A', NULL),
(1, '2023-09-01T00:00:00', N'  José  ', 10.005, N'A', N'contains | pipe'),
(2, '2023-09-30T23:59:59.999999', N'Zoë', -1.005, N'C', N'said "hello"'),
(3, '2023-10-01T00:00:00', N'東京', 0.000, N'A', N'line one' + NCHAR(10) + N'line two'),
(4, '2024-02-29T12:30:00', N'Leap day', 9007199254740993.010, N'P', N''),
(5, '2026-08-31T23:59:59.999999', NULL, 12.500, N'A', NULL),
(6, '2026-09-01T00:00:00', N'excluded at end', 1.000, N'A', NULL),
(7, NULL, N'excluded null date', 1.000, N'A', NULL);
GO
-- For comparison with examples/demo.json, configure SQL end = 2026-09-01T00:00:00Z.
-- Expected 5. Run against a stable dataset so count/export see the same data.
SELECT COUNT_BIG(*) AS expected_rows
FROM dbo.Orders
WHERE CreatedAt >= '2023-09-01T00:00:00' AND CreatedAt < '2026-09-01T00:00:00';
