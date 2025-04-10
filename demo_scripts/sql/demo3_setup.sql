-- Demo 3: Query Optimization with Hypothetical Indexes
-- This script creates a database with complex queries that can benefit from index optimization

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS hypopg;

-- Create a sample e-commerce schema
CREATE TABLE customers (
  id SERIAL PRIMARY KEY,
  customer_name VARCHAR(100),
  email VARCHAR(100),
  created_at TIMESTAMP DEFAULT NOW(),
  city VARCHAR(100),
  state VARCHAR(2)
);

CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  product_name VARCHAR(100),
  category VARCHAR(50),
  price NUMERIC(10,2),
  inventory_count INTEGER
);

CREATE TABLE orders (
  order_id SERIAL PRIMARY KEY,
  customer_id INTEGER REFERENCES customers(id),
  order_date TIMESTAMP DEFAULT NOW(),
  total_amount NUMERIC(12,2),
  status VARCHAR(20)
);

CREATE TABLE order_items (
  id SERIAL PRIMARY KEY,
  order_id INTEGER REFERENCES orders(order_id),
  product_id INTEGER REFERENCES products(id),
  quantity INTEGER,
  unit_price NUMERIC(10,2)
);

-- Insert sample data
INSERT INTO customers (customer_name, email, city, state)
SELECT 
  'Customer ' || i,
  'customer' || i || '@example.com',
  (ARRAY['New York', 'Chicago', 'Los Angeles', 'Houston', 'Phoenix', 'Philadelphia', 'San Antonio', 'San Diego', 'Dallas', 'San Jose'])[(i % 10) + 1],
  (ARRAY['NY', 'IL', 'CA', 'TX', 'AZ', 'PA', 'TX', 'CA', 'TX', 'CA'])[(i % 10) + 1]
FROM generate_series(1, 10000) i;

INSERT INTO products (product_name, category, price, inventory_count)
SELECT 
  'Product ' || i,
  (ARRAY['Electronics', 'Clothing', 'Books', 'Home', 'Food', 'Sports', 'Beauty', 'Toys', 'Automotive', 'Garden'])[(i % 10) + 1],
  (random() * 500)::numeric(10,2),
  (random() * 1000)::integer
FROM generate_series(1, 1000) i;

INSERT INTO orders (customer_id, order_date, total_amount, status)
SELECT 
  (random() * 9999 + 1)::integer,
  NOW() - (random() * 365 * 2)::integer * INTERVAL '1 day',
  (random() * 1000)::numeric(12,2),
  (ARRAY['Completed', 'Processing', 'Shipped', 'Cancelled', 'Returned'])[(i % 5) + 1]
FROM generate_series(1, 50000) i;

INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT 
  (random() * 49999 + 1)::integer,
  (random() * 999 + 1)::integer,
  (random() * 5 + 1)::integer,
  (random() * 500)::numeric(10,2)
FROM generate_series(1, 150000) i;

-- Create some basic indexes, but not optimal ones
CREATE INDEX idx_orders_customer_id ON orders(customer_id);
CREATE INDEX idx_order_items_product_id ON order_items(product_id);

-- Create a table of complex queries to optimize
CREATE TABLE complex_queries (
  id SERIAL PRIMARY KEY,
  name VARCHAR(100),
  description TEXT,
  query TEXT
);

-- Insert sample complex queries
INSERT INTO complex_queries (name, description, query) VALUES
  ('Customer Order History', 
   'Get all orders for a specific customer with product details',
   'SELECT o.order_id, o.order_date, o.status, p.product_name, oi.quantity, oi.unit_price
    FROM orders o
    JOIN order_items oi ON o.order_id = oi.order_id
    JOIN products p ON oi.product_id = p.id
    WHERE o.customer_id = $customer_id
    ORDER BY o.order_date DESC;'),
   
  ('Orders by State', 
   'Find all orders from customers in a specific state',
   'SELECT o.order_id, c.customer_name, p.product_name, o.order_date 
    FROM orders o 
    JOIN customers c ON o.customer_id = c.id 
    JOIN order_items oi ON o.order_id = oi.order_id 
    JOIN products p ON oi.product_id = p.id 
    WHERE c.state = $state 
    ORDER BY o.order_date DESC;'),
    
  ('Recent Orders with Filters', 
   'Find recent orders with date and status filters',
   'SELECT o.order_id, c.customer_name, p.product_name, o.order_date 
    FROM orders o 
    JOIN customers c ON o.customer_id = c.id 
    JOIN order_items oi ON o.order_id = oi.order_id 
    JOIN products p ON oi.product_id = p.id 
    WHERE o.order_date > $start_date AND o.status = $status 
    ORDER BY o.order_date DESC 
    LIMIT 100;'),
    
  ('Category Sales Report', 
   'Aggregate sales by product category',
   'SELECT p.category, COUNT(DISTINCT o.order_id) as order_count, SUM(oi.quantity) as units_sold, SUM(oi.quantity * oi.unit_price) as revenue
    FROM products p
    JOIN order_items oi ON p.id = oi.product_id
    JOIN orders o ON oi.order_id = o.order_id
    WHERE o.order_date BETWEEN $start_date AND $end_date
    GROUP BY p.category
    ORDER BY revenue DESC;'),
    
  ('Customer Spending by State', 
   'Analyze customer spending patterns by state',
   'SELECT c.state, COUNT(DISTINCT c.id) as customer_count, COUNT(DISTINCT o.order_id) as order_count, SUM(o.total_amount) as total_spent
    FROM customers c
    JOIN orders o ON c.id = o.customer_id
    WHERE o.order_date >= $start_date
    GROUP BY c.state
    ORDER BY total_spent DESC;'),
    
  ('Out of Stock Risk', 
   'Find products at risk of going out of stock based on sales velocity',
   'SELECT p.id, p.product_name, p.category, p.inventory_count,
       SUM(oi.quantity) as units_sold,
       p.inventory_count / NULLIF(SUM(oi.quantity), 0) as weeks_remaining
    FROM products p
    JOIN order_items oi ON p.id = oi.product_id
    JOIN orders o ON oi.order_id = o.order_id
    WHERE o.order_date >= NOW() - INTERVAL ''4 weeks''
    GROUP BY p.id, p.product_name, p.category, p.inventory_count
    HAVING p.inventory_count / NULLIF(SUM(oi.quantity), 0) < 4
    ORDER BY weeks_remaining ASC;');

-- Create a function to run all complex queries for testing
CREATE OR REPLACE FUNCTION run_complex_queries() RETURNS void AS $$
DECLARE
  random_customer_id INTEGER;
  random_state VARCHAR(2);
  random_status VARCHAR(20);
  start_date TIMESTAMP;
  end_date TIMESTAMP;
BEGIN
  -- Get random values for parameters
  SELECT id INTO random_customer_id FROM customers ORDER BY random() LIMIT 1;
  SELECT state INTO random_state FROM customers ORDER BY random() LIMIT 1;
  SELECT status INTO random_status FROM orders ORDER BY random() LIMIT 1;
  
  start_date := NOW() - INTERVAL '1 year';
  end_date := NOW();
  
  -- Execute each query with appropriate parameters
  -- Customer Order History
  PERFORM o.order_id, o.order_date, o.status, p.product_name, oi.quantity, oi.unit_price
  FROM orders o
  JOIN order_items oi ON o.order_id = oi.order_id
  JOIN products p ON oi.product_id = p.id
  WHERE o.customer_id = random_customer_id
  ORDER BY o.order_date DESC;
  
  -- Orders by State
  PERFORM o.order_id, c.customer_name, p.product_name, o.order_date 
  FROM orders o 
  JOIN customers c ON o.customer_id = c.id 
  JOIN order_items oi ON o.order_id = oi.order_id 
  JOIN products p ON oi.product_id = p.id 
  WHERE c.state = random_state 
  ORDER BY o.order_date DESC;
  
  -- Recent Orders with Filters
  PERFORM o.order_id, c.customer_name, p.product_name, o.order_date 
  FROM orders o 
  JOIN customers c ON o.customer_id = c.id 
  JOIN order_items oi ON o.order_id = oi.order_id 
  JOIN products p ON oi.product_id = p.id 
  WHERE o.order_date > start_date AND o.status = random_status 
  ORDER BY o.order_date DESC 
  LIMIT 100;
  
  -- Category Sales Report
  PERFORM p.category, COUNT(DISTINCT o.order_id) as order_count, SUM(oi.quantity) as units_sold, SUM(oi.quantity * oi.unit_price) as revenue
  FROM products p
  JOIN order_items oi ON p.id = oi.product_id
  JOIN orders o ON oi.order_id = o.order_id
  WHERE o.order_date BETWEEN start_date AND end_date
  GROUP BY p.category
  ORDER BY revenue DESC;
  
  -- Customer Spending by State
  PERFORM c.state, COUNT(DISTINCT c.id) as customer_count, COUNT(DISTINCT o.order_id) as order_count, SUM(o.total_amount) as total_spent
  FROM customers c
  JOIN orders o ON c.id = o.customer_id
  WHERE o.order_date >= start_date
  GROUP BY c.state
  ORDER BY total_spent DESC;
  
  -- Out of Stock Risk
  PERFORM p.id, p.product_name, p.category, p.inventory_count,
    SUM(oi.quantity) as units_sold,
    p.inventory_count / NULLIF(SUM(oi.quantity), 0) as weeks_remaining
  FROM products p
  JOIN order_items oi ON p.id = oi.product_id
  JOIN orders o ON oi.order_id = o.order_id
  WHERE o.order_date >= NOW() - INTERVAL '4 weeks'
  GROUP BY p.id, p.product_name, p.category, p.inventory_count
  HAVING p.inventory_count / NULLIF(SUM(oi.quantity), 0) < 4
  ORDER BY weeks_remaining ASC;
END;
$$ LANGUAGE plpgsql;

-- Run the complex queries to populate pg_stat_statements
SELECT run_complex_queries();
SELECT run_complex_queries();
SELECT run_complex_queries();

-- Create a view to make it easier to grab the slow query text
CREATE OR REPLACE VIEW slow_query AS
SELECT 'SELECT o.order_id, c.customer_name, p.product_name, o.order_date 
FROM orders o 
JOIN customers c ON o.customer_id = c.id 
JOIN order_items oi ON o.order_id = oi.order_id 
JOIN products p ON oi.product_id = p.id 
WHERE o.order_date > ''2022-01-01'' AND c.state = ''CA'' 
ORDER BY o.order_date DESC LIMIT 100;' AS query;

-- Update statistics
ANALYZE;

-- Print summary
SELECT 'Query Optimization demo setup completed successfully.';
SELECT 'The database contains complex queries that can benefit from index optimization.';
SELECT 'Use the explain_query tool with the hypothetical_indexes parameter to test index improvements.'; 
