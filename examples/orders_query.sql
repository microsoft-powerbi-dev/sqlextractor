SELECT o.OrderId, o.CreatedAt, c.Name AS Customer, o.Amount, o.Status, o.Note
FROM dbo.Orders AS o
JOIN dbo.Customers AS c ON c.CustomerId = o.CustomerId
WHERE o.Status <> 'X'
