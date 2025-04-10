-- Demo 6: Advanced Workload Analysis
-- This script creates a database with simulated production workload patterns

-- Install required extensions
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Create sample schema
CREATE TABLE users (
  id SERIAL PRIMARY KEY,
  username VARCHAR(100) UNIQUE,
  email VARCHAR(100) UNIQUE,
  created_at TIMESTAMP DEFAULT NOW(),
  last_login TIMESTAMP
);

CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  name VARCHAR(100),
  price NUMERIC(10,2),
  stock INTEGER,
  category VARCHAR(50)
);

CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  user_id INTEGER REFERENCES users(id),
  created_at TIMESTAMP DEFAULT NOW(),
  status VARCHAR(20)
);

CREATE TABLE order_items (
  id SERIAL PRIMARY KEY,
  order_id INTEGER REFERENCES orders(id),
  product_id INTEGER REFERENCES products(id),
  quantity INTEGER,
  price NUMERIC(10,2)
);

CREATE TABLE page_views (
  id SERIAL PRIMARY KEY,
  user_id INTEGER REFERENCES users(id),
  page VARCHAR(100),
  viewed_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE user_sessions (
  id SERIAL PRIMARY KEY,
  user_id INTEGER REFERENCES users(id),
  session_start TIMESTAMP DEFAULT NOW(),
  session_end TIMESTAMP,
  ip_address VARCHAR(50),
  user_agent TEXT
);

CREATE TABLE shopping_carts (
  id SERIAL PRIMARY KEY,
  user_id INTEGER REFERENCES users(id),
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW(),
  status VARCHAR(20) DEFAULT 'active'
);

CREATE TABLE cart_items (
  id SERIAL PRIMARY KEY,
  cart_id INTEGER REFERENCES shopping_carts(id),
  product_id INTEGER REFERENCES products(id),
  quantity INTEGER,
  added_at TIMESTAMP DEFAULT NOW()
);

-- Load sample data
INSERT INTO users (username, email, created_at, last_login)
SELECT 
  'user' || i, 
  'user' || i || '@example.com',
  NOW() - (random() * 365)::integer * INTERVAL '1 day',
  NOW() - (random() * 30)::integer * INTERVAL '1 day'
FROM generate_series(1, 10000) i;

INSERT INTO products (name, price, stock, category)
SELECT 
  'Product ' || i,
  (random() * 1000)::numeric(10,2),
  (random() * 100)::integer,
  (ARRAY['Electronics', 'Clothing', 'Home', 'Books', 'Food', 'Sports', 'Beauty', 'Toys', 'Automotive', 'Garden'])[(i % 10) + 1]
FROM generate_series(1, 1000) i;

INSERT INTO orders (user_id, created_at, status)
SELECT 
  (random() * 9999 + 1)::integer,
  NOW() - (random() * 180)::integer * INTERVAL '1 day',
  (ARRAY['completed', 'processing', 'shipped', 'cancelled'])[(i % 4) + 1]
FROM generate_series(1, 50000) i;

INSERT INTO order_items (order_id, product_id, quantity, price)
SELECT 
  (random() * 49999 + 1)::integer,
  (random() * 999 + 1)::integer,
  (random() * 5 + 1)::integer,
  (random() * 1000)::numeric(10,2)
FROM generate_series(1, 100000) i;

INSERT INTO page_views (user_id, page, viewed_at)
SELECT 
  (random() * 9999 + 1)::integer,
  (ARRAY['home', 'product', 'cart', 'checkout', 'profile', 'order_history', 'category', 'search', 'about', 'contact'])[(i % 10) + 1],
  NOW() - (random() * 30)::integer * INTERVAL '1 day' - (random() * 24)::integer * INTERVAL '1 hour'
FROM generate_series(1, 500000) i;

INSERT INTO user_sessions (user_id, session_start, session_end, ip_address)
SELECT 
  (random() * 9999 + 1)::integer,
  NOW() - (random() * 30)::integer * INTERVAL '1 day' - (random() * 24)::integer * INTERVAL '1 hour',
  NOW() - (random() * 30)::integer * INTERVAL '1 day' - (random() * 24)::integer * INTERVAL '1 hour' + (random() * 120)::integer * INTERVAL '1 minute',
  '192.168.' || (random() * 255)::integer || '.' || (random() * 255)::integer
FROM generate_series(1, 50000) i;

INSERT INTO shopping_carts (user_id, created_at, updated_at, status)
SELECT 
  (random() * 9999 + 1)::integer,
  NOW() - (random() * 30)::integer * INTERVAL '1 day',
  NOW() - (random() * 30)::integer * INTERVAL '1 day' + (random() * 72)::integer * INTERVAL '1 hour',
  CASE WHEN random() > 0.7 THEN 'active' ELSE 'abandoned' END
FROM generate_series(1, 20000) i;

INSERT INTO cart_items (cart_id, product_id, quantity)
SELECT 
  (random() * 19999 + 1)::integer,
  (random() * 999 + 1)::integer,
  (random() * 3 + 1)::integer
FROM generate_series(1, 50000) i;

-- Create a function to run random queries
CREATE OR REPLACE FUNCTION run_random_workload() RETURNS void AS $$
DECLARE
  user_id_var INTEGER;
  product_id_var INTEGER;
  order_id_var INTEGER;
  category_var VARCHAR;
  start_date TIMESTAMP;
  end_date TIMESTAMP;
BEGIN
  -- Select random values to use in queries
  SELECT id INTO user_id_var FROM users ORDER BY random() LIMIT 1;
  SELECT id INTO product_id_var FROM products ORDER BY random() LIMIT 1;
  SELECT id INTO order_id_var FROM orders ORDER BY random() LIMIT 1;
  SELECT category INTO category_var FROM products ORDER BY random() LIMIT 1;
  
  start_date := NOW() - INTERVAL '30 days';
  end_date := NOW();
  
  -- Run a random mix of queries
  CASE (random() * 20)::integer
    WHEN 0 THEN
      -- User profile query
      PERFORM * FROM users WHERE id = user_id_var;
    
    WHEN 1 THEN
      -- Product search
      PERFORM * FROM products WHERE category = category_var ORDER BY price LIMIT 10;
    
    WHEN 2 THEN
      -- Order history
      PERFORM o.id, o.created_at, SUM(oi.price * oi.quantity) 
      FROM orders o JOIN order_items oi ON o.id = oi.order_id 
      WHERE o.user_id = user_id_var
      GROUP BY o.id, o.created_at ORDER BY o.created_at DESC;
    
    WHEN 3 THEN
      -- Product detail with stock
      PERFORM p.*, SUM(oi.quantity) as total_ordered
      FROM products p LEFT JOIN order_items oi ON p.id = oi.product_id
      WHERE p.id = product_id_var
      GROUP BY p.id;
    
    WHEN 4 THEN
      -- Dashboard summary (expensive query)
      PERFORM COUNT(*), SUM(oi.price * oi.quantity)
      FROM orders o JOIN order_items oi ON o.id = oi.order_id
      WHERE o.created_at > (NOW() - INTERVAL '7 days');
    
    WHEN 5 THEN
      -- Insert page view
      INSERT INTO page_views (user_id, page)
      VALUES (
        user_id_var, 
        (ARRAY['home', 'product', 'cart', 'checkout', 'profile'])[(random() * 4)::integer + 1]
      );
    
    WHEN 6 THEN
      -- Update product stock (batch update)
      UPDATE products SET stock = stock - 1 
      WHERE id = product_id_var AND stock > 0;
    
    WHEN 7 THEN
      -- Order analysis by category
      PERFORM p.category, COUNT(*), SUM(oi.quantity)
      FROM order_items oi JOIN products p ON oi.product_id = p.id
      JOIN orders o ON oi.order_id = o.id
      WHERE o.created_at > (NOW() - INTERVAL '30 days')
      GROUP BY p.category ORDER BY SUM(oi.quantity) DESC;
    
    WHEN 8 THEN
      -- User session analysis
      PERFORM user_id, COUNT(*), MIN(viewed_at), MAX(viewed_at)
      FROM page_views
      WHERE viewed_at > (NOW() - INTERVAL '1 day')
      GROUP BY user_id HAVING COUNT(*) > 5
      ORDER BY COUNT(*) DESC LIMIT 10;
    
    WHEN 9 THEN
      -- Complex join for reporting (expensive query)
      PERFORM u.id, u.username, COUNT(o.id) as order_count, SUM(oi.price * oi.quantity) as total_spent
      FROM users u
      JOIN orders o ON u.id = o.user_id
      JOIN order_items oi ON o.id = oi.order_id
      WHERE u.created_at > (NOW() - INTERVAL '90 days')
      GROUP BY u.id, u.username
      ORDER BY total_spent DESC LIMIT 100;
      
    WHEN 10 THEN
      -- User search (inefficient)
      PERFORM * FROM users 
      WHERE username ILIKE '%' || (array['a','b','c','d','e','f'])[(random()*5)::integer + 1] || '%'
      LIMIT 20;
      
    WHEN 11 THEN
      -- Cart abandonment analysis
      PERFORM DATE_TRUNC('day', c.updated_at) as day, COUNT(*) as abandoned_count
      FROM shopping_carts c
      WHERE c.status = 'abandoned' AND c.updated_at > start_date
      GROUP BY day ORDER BY day;
      
    WHEN 12 THEN
      -- Product inventory warnings
      PERFORM id, name, stock FROM products
      WHERE stock < 10 ORDER BY stock;
      
    WHEN 13 THEN
      -- Update last login time
      UPDATE users SET last_login = NOW() WHERE id = user_id_var;
      
    WHEN 14 THEN
      -- Product popularity ranking
      PERFORM p.id, p.name, COUNT(oi.id) as order_count
      FROM products p
      JOIN order_items oi ON p.id = oi.product_id
      GROUP BY p.id, p.name
      ORDER BY order_count DESC LIMIT 20;
      
    WHEN 15 THEN
      -- Daily sales report
      PERFORM DATE_TRUNC('day', o.created_at) as day, 
             COUNT(DISTINCT o.id) as order_count,
             SUM(oi.price * oi.quantity) as revenue
      FROM orders o
      JOIN order_items oi ON o.id = oi.order_id
      WHERE o.created_at BETWEEN start_date AND end_date
      GROUP BY day ORDER BY day;
      
    WHEN 16 THEN
      -- New user registration (simulated)
      INSERT INTO users (username, email)
      VALUES (
        'new_user_' || (random() * 10000)::integer,
        'new_' || (random() * 10000)::integer || '@example.com'
      );
      
    WHEN 17 THEN
      -- Join cart with products (shopping cart page)
      PERFORM ci.cart_id, p.name, p.price, ci.quantity
      FROM cart_items ci
      JOIN products p ON ci.product_id = p.id
      JOIN shopping_carts sc ON ci.cart_id = sc.id
      WHERE sc.user_id = user_id_var AND sc.status = 'active';
      
    WHEN 18 THEN
      -- Check user session history
      PERFORM * FROM user_sessions
      WHERE user_id = user_id_var
      ORDER BY session_start DESC LIMIT 10;
      
    WHEN 19 THEN
      -- Search products by name (inefficient)
      PERFORM * FROM products
      WHERE name ILIKE '%' || (array['pro','super','deluxe','special','premium'])[(random()*4)::integer + 1] || '%'
      ORDER BY price DESC LIMIT 20;
      
    ELSE
      -- User activity timeline (complex join)
      PERFORM u.username, 
             COALESCE(o.id::text, pv.page::text) as activity,
             COALESCE(o.created_at, pv.viewed_at) as activity_time
      FROM users u
      LEFT JOIN orders o ON u.id = o.user_id
      LEFT JOIN page_views pv ON u.id = pv.user_id
      WHERE u.id = user_id_var
      ORDER BY activity_time DESC LIMIT 50;
  END CASE;
END;
$$ LANGUAGE plpgsql;

-- Run the workload multiple times
DO $$
BEGIN
  FOR i IN 1..200 LOOP -- Reduced from 1000 to 200 for demo setup speed
    PERFORM run_random_workload();
  END LOOP;
END $$;

-- Reset pg_stat_statements to get clean data
SELECT pg_stat_statements_reset();

-- Run the workload again to collect stats
DO $$
BEGIN
  FOR i IN 1..200 LOOP -- Reduced from 1000 to 200 for demo setup speed
    PERFORM run_random_workload();
  END LOOP;
END $$;

-- Create minimal indexes (but not all that are needed)
CREATE INDEX idx_orders_user_id ON orders(user_id);
CREATE INDEX idx_order_items_order_id ON order_items(order_id);
CREATE INDEX idx_page_views_user_id ON page_views(user_id);

-- Create some materialized views that could be used but aren't
CREATE MATERIALIZED VIEW product_sales_summary AS
SELECT 
  p.id,
  p.name,
  p.category,
  COUNT(DISTINCT o.id) as order_count,
  SUM(oi.quantity) as units_sold,
  SUM(oi.price * oi.quantity) as revenue
FROM products p
LEFT JOIN order_items oi ON p.id = oi.product_id
LEFT JOIN orders o ON oi.order_id = o.id
GROUP BY p.id, p.name, p.category;

-- Update statistics
ANALYZE;

-- Print summary
SELECT 'Advanced Workload Analysis demo setup completed successfully.';
SELECT 'The database contains a mix of different query patterns and workloads.';
SELECT 'Use the get_top_queries and analyze_workload_indexes tools to analyze performance.'; 
