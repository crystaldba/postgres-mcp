# Postgres Pro MCP Server Demos

This document contains examples and scripts to demonstrate the capabilities of Postgres Pro MCP Server with AI assistants like Claude, Cursor, and other MCP-compatible agents.

## Setup

Before running these demos, ensure you have:

1. Postgres Pro MCP Server installed and configured
2. A PostgreSQL database (sample databases provided below)
3. An MCP-compatible AI assistant configured to use Postgres Pro

⚠️ **Resource Requirements**: Some demos create millions of rows and may require significant disk space (up to several GB) and memory. Adjust the `generate_series()` parameters in the scripts if needed to reduce resource usage.

## Demo 1: Database Health Assessment

### Scenario
You've inherited a production database and need to understand its health and identify potential issues.

### Sample Interaction

**User:** "Can you check the health of my database and identify any issues that might impact performance?"

**Assistant Actions:**
1. Uses `analyze_db_health` to perform comprehensive health checks
2. Analyzes buffer cache hit rates, connection health, vacuum status, etc.
3. Identifies problems such as unused indexes, bloated tables, or vacuum issues
4. Presents findings in an organized format with severity ratings
5. Provides specific recommendations for each issue

### Practical Implementation

#### Step 1: Create test database with issues

```bash
# Create a test database
createdb health_demo

# Connect to the database
psql health_demo
```

```sql
-- Create tables with common issues
CREATE TABLE big_table (
  id SERIAL PRIMARY KEY,
  data TEXT,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW(),
  status TEXT,
  category TEXT,
  price NUMERIC(10,2)
);

-- Insert dummy data (1 million rows)
INSERT INTO big_table (data, status, category, price)
SELECT 
  md5(random()::text), 
  (ARRAY['active', 'inactive', 'pending', 'deleted'])[ceil(random()*4)],
  (ARRAY['A', 'B', 'C', 'D', 'E'])[ceil(random()*5)],
  random()*1000
FROM generate_series(1, 1000000);

-- Create inefficient indexes
CREATE INDEX idx_big_table_data ON big_table(data); -- Inefficient text index
CREATE INDEX idx_big_table_status ON big_table(status); -- Duplicate of another index we'll create
CREATE INDEX idx_big_table_status_category ON big_table(status, category); -- Redundant with status index

-- Create a table with a sequence that will approach its limit
CREATE SEQUENCE almost_depleted_sequence START 2147483000; -- Close to max int
CREATE TABLE high_sequence_table (
  id INTEGER PRIMARY KEY DEFAULT nextval('almost_depleted_sequence'),
  data TEXT
);

-- Insert some rows to use the sequence
INSERT INTO high_sequence_table (data)
SELECT md5(random()::text) FROM generate_series(1, 100);

-- Create bloated table by updating frequently
CREATE TABLE bloated_table (
  id SERIAL PRIMARY KEY,
  counter INTEGER,
  data TEXT
);

INSERT INTO bloated_table (counter, data)
SELECT 0, md5(random()::text) FROM generate_series(1, 10000);

-- Create many updates to cause bloat
DO $$
BEGIN
  FOR i IN 1..20 LOOP
    UPDATE bloated_table SET counter = counter + 1, data = md5(random()::text);
  END LOOP;
END $$;

-- Create tables with no stats
CREATE TABLE no_stats_table (
  id SERIAL PRIMARY KEY,
  data TEXT
);

INSERT INTO no_stats_table (data)
SELECT md5(random()::text) FROM generate_series(1, 1000);
```

#### Step 2: Connect to the database with Postgres Pro MCP

Configure your AI assistant to connect to the health_demo database.

#### Step 3: Ask for health assessment

**User:** "Can you check the health of my database and identify any issues that might impact performance?"

#### Step 4: Review findings and apply fixes

After receiving the health assessment, you can apply the recommended fixes:

```sql
-- Sample fixes based on expected findings:

-- Remove duplicate/redundant indexes
DROP INDEX idx_big_table_status;

-- Fix sequence approaching limit
ALTER SEQUENCE almost_depleted_sequence RESTART WITH 1;
-- or
ALTER TABLE high_sequence_table ALTER COLUMN id TYPE BIGINT;

-- Fix bloated table
VACUUM FULL bloated_table;

-- Update statistics
ANALYZE no_stats_table;
```

### Expected Results

The assessment should identify common issues like:
- Low buffer cache hit rates
- Connections approaching limits
- Unused or duplicate indexes
- Tables that haven't been vacuumed recently
- Bloated tables and indexes
- Sequences approaching their limits

## Demo 2: Index Recommendations for a Slow Application

### Scenario
Users are reporting that your application is running slow. You suspect database query performance is the culprit.

### Sample Interaction

**User:** "My application is slow, especially when users access the reporting dashboard. Can you help me figure out what's wrong and how to fix it?"

**Assistant Actions:**
1. Uses `get_top_queries` to identify slow queries
2. Analyzes the query patterns using `analyze_workload_indexes`
3. Identifies missing indexes or optimization opportunities
4. Recommends specific indexes to improve performance
5. Provides estimates of performance improvements for each recommendation

### Practical Implementation

#### Step 1: Create test database with performance issues

```bash
# Create a test database
createdb slow_app_demo

# Connect to the database
psql slow_app_demo
```

```sql
-- Create sample tables for a reporting application
CREATE TABLE customers (
  id SERIAL PRIMARY KEY,
  name VARCHAR(100),
  email VARCHAR(100),
  signup_date DATE,
  last_login TIMESTAMP,
  account_type VARCHAR(20)
);

CREATE TABLE reports (
  id SERIAL PRIMARY KEY,
  title VARCHAR(200),
  description TEXT,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW(),
  creator_id INTEGER REFERENCES customers(id),
  is_public BOOLEAN DEFAULT false
);

CREATE TABLE report_views (
  id SERIAL PRIMARY KEY,
  report_id INTEGER REFERENCES reports(id),
  viewer_id INTEGER REFERENCES customers(id),
  viewed_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE report_data (
  id SERIAL PRIMARY KEY,
  report_id INTEGER REFERENCES reports(id),
  data_point VARCHAR(100),
  value NUMERIC(12,2),
  entry_date DATE
);

-- Insert sample data
INSERT INTO customers (name, email, signup_date, last_login, account_type)
SELECT 
  'Customer ' || i,
  'customer' || i || '@example.com',
  CURRENT_DATE - (random() * 365 * 2)::integer,
  NOW() - (random() * 30)::integer * INTERVAL '1 hour',
  (ARRAY['free', 'basic', 'premium', 'enterprise'])[1 + (i % 4)]
FROM generate_series(1, 10000) i;

INSERT INTO reports (title, description, created_at, creator_id, is_public)
SELECT 
  'Report ' || i,
  'Description for report ' || i,
  NOW() - (random() * 180)::integer * INTERVAL '1 day',
  (random() * 10000)::integer + 1,
  random() > 0.7
FROM generate_series(1, 5000) i;

INSERT INTO report_views (report_id, viewer_id, viewed_at)
SELECT 
  (random() * 5000)::integer + 1,
  (random() * 10000)::integer + 1,
  NOW() - (random() * 90)::integer * INTERVAL '1 day'
FROM generate_series(1, 100000) i;

INSERT INTO report_data (report_id, data_point, value, entry_date)
SELECT 
  (random() * 5000)::integer + 1,
  'Metric ' || (i % 20),
  random() * 1000,
  CURRENT_DATE - (random() * 365)::integer
FROM generate_series(1, 500000) i;

-- Create basic but insufficient indexes
CREATE INDEX idx_customers_name ON customers(name);
CREATE INDEX idx_reports_creator ON reports(creator_id);
```

#### Step 2: Create slow dashboard queries

```sql
-- Create some slow queries that run periodically in the dashboard
-- This simulates the application's main performance issues

-- 1. Popular reports query with no efficient indexes
CREATE OR REPLACE FUNCTION get_popular_reports() RETURNS void AS $$
BEGIN
  PERFORM r.id, r.title, COUNT(rv.id) as view_count
  FROM reports r
  JOIN report_views rv ON r.id = rv.report_id
  WHERE r.is_public = true
  GROUP BY r.id, r.title
  ORDER BY view_count DESC
  LIMIT 20;
END;
$$ LANGUAGE plpgsql;

-- 2. User activity tracking with poor performance
CREATE OR REPLACE FUNCTION track_user_activity() RETURNS void AS $$
BEGIN
  PERFORM c.id, c.name, c.email, MAX(rv.viewed_at) as last_activity
  FROM customers c
  LEFT JOIN report_views rv ON c.id = rv.viewer_id
  WHERE c.account_type = 'premium' OR c.account_type = 'enterprise'
  GROUP BY c.id, c.name, c.email
  ORDER BY last_activity DESC NULLS LAST;
END;
$$ LANGUAGE plpgsql;

-- 3. Report data aggregation that's slow
CREATE OR REPLACE FUNCTION aggregate_report_data() RETURNS void AS $$
BEGIN
  PERFORM rd.report_id, r.title, rd.data_point, 
         AVG(rd.value) as avg_value, 
         MIN(rd.value) as min_value,
         MAX(rd.value) as max_value
  FROM report_data rd
  JOIN reports r ON rd.report_id = r.id
  WHERE rd.entry_date >= CURRENT_DATE - INTERVAL '30 days'
  GROUP BY rd.report_id, r.title, rd.data_point
  ORDER BY rd.report_id, rd.data_point;
END;
$$ LANGUAGE plpgsql;

-- Generate load by calling these functions
DO $$
BEGIN
  FOR i IN 1..20 LOOP
    PERFORM get_popular_reports();
    PERFORM track_user_activity();
    PERFORM aggregate_report_data();
  END LOOP;
END $$;
```

#### Step 3: Install required extensions

```sql
-- Install required extensions
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

#### Step 4: Connect to the database with Postgres Pro MCP

Configure your AI assistant to connect to the slow_app_demo database.

#### Step 5: Ask for query analysis and index recommendations

**User:** "My application is slow, especially when users access the reporting dashboard. Can you help me figure out what's wrong and how to fix it?"

#### Step 6: Implement the recommended indexes

After receiving the index recommendations, implement them and verify performance improvements:

```sql
-- Examples of indexes that might be recommended:
CREATE INDEX idx_reports_is_public ON reports(is_public);
CREATE INDEX idx_report_views_report_id ON report_views(report_id);
CREATE INDEX idx_report_data_entry_date ON report_data(entry_date);
CREATE INDEX idx_customers_account_type ON customers(account_type);

-- Test the queries again to verify improvement
DO $$
BEGIN
  FOR i IN 1..5 LOOP
    PERFORM get_popular_reports();
    PERFORM track_user_activity();
    PERFORM aggregate_report_data();
  END LOOP;
END $$;
```

### Expected Results

The analysis should provide:
- List of the slowest queries by execution time
- Recommended indexes with estimated performance improvements
- Storage requirements for the proposed indexes
- Explanation of how each index would improve specific queries

## Demo 3: Query Optimization with Hypothetical Indexes

### Scenario
You have a specific complex query that's running slow, and you want to optimize it without making immediate changes to the database.

### Sample Interaction

**User:** "This query is running slow: `SELECT o.order_id, c.customer_name, p.product_name, o.order_date 
FROM orders o 
JOIN customers c ON o.customer_id = c.id 
JOIN order_items oi ON o.order_id = oi.order_id 
JOIN products p ON oi.product_id = p.id 
WHERE o.order_date > '2022-01-01' AND c.state = 'CA' 
ORDER BY o.order_date DESC LIMIT 100;`
Can you help me optimize it?"

**Assistant Actions:**
1. Uses `explain_query` to analyze the execution plan
2. Identifies bottlenecks in the query execution
3. Tests hypothetical indexes using HypoPG
4. Compares execution plans before and after proposed indexes
5. Recommends the most effective index(es) for this specific query

### Practical Implementation

#### Step 1: Create test schema and data

```bash
# Create a test database
createdb query_demo

# Connect to the database
psql query_demo
```

```sql
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
  (ARRAY['New York', 'Chicago', 'Los Angeles', 'Houston', 'Phoenix'])[1 + (i % 5)],
  (ARRAY['NY', 'IL', 'CA', 'TX', 'AZ'])[1 + (i % 5)]
FROM generate_series(1, 10000) i;

INSERT INTO products (product_name, category, price, inventory_count)
SELECT 
  'Product ' || i,
  (ARRAY['Electronics', 'Clothing', 'Books', 'Home', 'Food'])[1 + (i % 5)],
  (random() * 500)::numeric(10,2),
  (random() * 1000)::integer
FROM generate_series(1, 1000) i;

INSERT INTO orders (customer_id, order_date, total_amount, status)
SELECT 
  (random() * 10000)::integer + 1,
  NOW() - (random() * 365 * 2)::integer * INTERVAL '1 day',
  (random() * 1000)::numeric(12,2),
  (ARRAY['Completed', 'Processing', 'Shipped', 'Cancelled'])[1 + (i % 4)]
FROM generate_series(1, 50000) i;

INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT 
  (random() * 50000)::integer + 1,
  (random() * 1000)::integer + 1,
  (random() * 5)::integer + 1,
  (random() * 500)::numeric(10,2)
FROM generate_series(1, 150000) i;

-- Create some basic indexes, but not optimal ones
CREATE INDEX idx_orders_customer_id ON orders(customer_id);
CREATE INDEX idx_order_items_product_id ON order_items(product_id);
```

#### Step 2: Install required extensions

```sql
-- Install required extensions for hypothetical indexes testing
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS hypopg;
```

#### Step 3: Connect to the database with Postgres Pro MCP

Configure your AI assistant to connect to the query_demo database.

#### Step 4: Submit slow query for optimization

**User:** "This query is running slow: `SELECT o.order_id, c.customer_name, p.product_name, o.order_date 
FROM orders o 
JOIN customers c ON o.customer_id = c.id 
JOIN order_items oi ON o.order_id = oi.order_id 
JOIN products p ON oi.product_id = p.id 
WHERE o.order_date > '2022-01-01' AND c.state = 'CA' 
ORDER BY o.order_date DESC LIMIT 100;`
Can you help me optimize it?"

#### Step 5: Implement the recommended indexes

After receiving the optimization recommendations:

```sql
-- Example optimal indexes that might be recommended
CREATE INDEX idx_orders_order_date ON orders(order_date DESC);
CREATE INDEX idx_customers_state_id ON customers(state, id);
CREATE INDEX idx_order_items_order_id_product_id ON order_items(order_id, product_id);

-- Or a covering index such as:
CREATE INDEX idx_orders_customer_date ON orders(customer_id, order_date DESC);
```

#### Step 6: Verify performance improvement

```sql
-- Compare execution times before and after index creation
EXPLAIN ANALYZE SELECT o.order_id, c.customer_name, p.product_name, o.order_date 
FROM orders o 
JOIN customers c ON o.customer_id = c.id 
JOIN order_items oi ON o.order_id = oi.order_id 
JOIN products p ON oi.product_id = p.id 
WHERE o.order_date > '2022-01-01' AND c.state = 'CA' 
ORDER BY o.order_date DESC LIMIT 100;
```

### Expected Results

The optimization should provide:
- Original execution plan with identified bottlenecks
- Several hypothetical index options
- Comparison of execution costs with each index option
- Recommendation of the most effective index(es)
- Potential SQL for creating the recommended index(es)

## Demo 4: Schema Discovery and Documentation

### Scenario
You're working with a new database and need to understand its structure, relationships, and data.

### Sample Interaction

**User:** "I'm new to this database. Can you help me understand its structure and how the tables are related?"

**Assistant Actions:**
1. Uses `list_schemas` to identify available schemas
2. Uses `list_objects` to find tables, views, and other objects
3. Uses `get_object_details` to examine table structures and relationships
4. Identifies primary keys, foreign keys, and indexing strategies
5. Creates a comprehensive overview of the database structure

### Practical Implementation

#### Step 1: Set up a sample database with complex schema

```bash
# Create a test database
createdb schema_discovery_demo

# Connect to the database
psql schema_discovery_demo
```

```sql
-- Create multiple schemas to simulate a real-world complex application
CREATE SCHEMA auth;
CREATE SCHEMA public;
CREATE SCHEMA analytics;
CREATE SCHEMA archive;

-- Auth schema objects
CREATE TABLE auth.users (
  id SERIAL PRIMARY KEY,
  username VARCHAR(50) UNIQUE NOT NULL,
  email VARCHAR(100) UNIQUE NOT NULL,
  password_hash VARCHAR(100) NOT NULL,
  created_at TIMESTAMP DEFAULT NOW(),
  last_login TIMESTAMP,
  is_active BOOLEAN DEFAULT true
);

CREATE TABLE auth.roles (
  id SERIAL PRIMARY KEY,
  name VARCHAR(50) UNIQUE NOT NULL,
  description TEXT
);

CREATE TABLE auth.user_roles (
  user_id INTEGER REFERENCES auth.users(id),
  role_id INTEGER REFERENCES auth.roles(id),
  assigned_at TIMESTAMP DEFAULT NOW(),
  PRIMARY KEY (user_id, role_id)
);

CREATE TABLE auth.permissions (
  id SERIAL PRIMARY KEY,
  name VARCHAR(100) UNIQUE NOT NULL,
  description TEXT
);

CREATE TABLE auth.role_permissions (
  role_id INTEGER REFERENCES auth.roles(id),
  permission_id INTEGER REFERENCES auth.permissions(id),
  PRIMARY KEY (role_id, permission_id)
);

-- Public schema (application data)
CREATE TABLE public.products (
  id SERIAL PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  description TEXT,
  price NUMERIC(10,2) NOT NULL,
  inventory INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE public.categories (
  id SERIAL PRIMARY KEY,
  name VARCHAR(50) NOT NULL,
  parent_id INTEGER REFERENCES public.categories(id)
);

CREATE TABLE public.product_categories (
  product_id INTEGER REFERENCES public.products(id),
  category_id INTEGER REFERENCES public.categories(id),
  PRIMARY KEY (product_id, category_id)
);

CREATE TABLE public.orders (
  id SERIAL PRIMARY KEY,
  user_id INTEGER REFERENCES auth.users(id),
  status VARCHAR(20) NOT NULL,
  total_amount NUMERIC(12,2) NOT NULL,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE public.order_items (
  id SERIAL PRIMARY KEY,
  order_id INTEGER REFERENCES public.orders(id),
  product_id INTEGER REFERENCES public.products(id),
  quantity INTEGER NOT NULL,
  unit_price NUMERIC(10,2) NOT NULL
);

-- Analytics schema
CREATE TABLE analytics.page_views (
  id SERIAL PRIMARY KEY,
  user_id INTEGER REFERENCES auth.users(id),
  page_url VARCHAR(200) NOT NULL,
  viewed_at TIMESTAMP DEFAULT NOW(),
  session_id VARCHAR(100),
  user_agent TEXT
);

CREATE TABLE analytics.product_views (
  id SERIAL PRIMARY KEY,
  user_id INTEGER REFERENCES auth.users(id),
  product_id INTEGER REFERENCES public.products(id),
  viewed_at TIMESTAMP DEFAULT NOW(),
  session_id VARCHAR(100)
);

-- Archive schema
CREATE TABLE archive.old_orders (
  id INTEGER,
  user_id INTEGER,
  status VARCHAR(20),
  total_amount NUMERIC(12,2),
  created_at TIMESTAMP,
  archived_at TIMESTAMP DEFAULT NOW()
);

-- Create views
CREATE VIEW public.active_products AS
  SELECT * FROM public.products WHERE inventory > 0;

CREATE VIEW public.order_summary AS
  SELECT o.id, o.user_id, u.username, o.total_amount, o.created_at
  FROM public.orders o
  JOIN auth.users u ON o.user_id = u.id;

-- Create a stored procedure
CREATE OR REPLACE PROCEDURE public.create_order(
  p_user_id INTEGER,
  p_status VARCHAR(20),
  p_total_amount NUMERIC(12,2)
)
LANGUAGE plpgsql
AS $$
BEGIN
  INSERT INTO public.orders (user_id, status, total_amount)
  VALUES (p_user_id, p_status, p_total_amount);
END;
$$;

-- Insert some sample data
INSERT INTO auth.roles (name, description) VALUES
  ('admin', 'Administrator with full access'),
  ('manager', 'Can manage products and view orders'),
  ('customer', 'Regular customer');

INSERT INTO auth.permissions (name, description) VALUES
  ('view_products', 'Can view products'),
  ('edit_products', 'Can edit products'),
  ('view_orders', 'Can view orders'),
  ('create_orders', 'Can create orders'),
  ('manage_users', 'Can manage users');

INSERT INTO auth.users (username, email, password_hash) VALUES
  ('admin', 'admin@example.com', 'hashed_password'),
  ('manager1', 'manager1@example.com', 'hashed_password'),
  ('customer1', 'customer1@example.com', 'hashed_password');

-- Create some indexes
CREATE INDEX idx_products_name ON public.products(name);
CREATE INDEX idx_orders_user_id ON public.orders(user_id);
CREATE INDEX idx_page_views_user_id ON analytics.page_views(user_id);
```

#### Step 2: Connect to the database with Postgres Pro MCP

Configure your AI assistant to connect to the schema_discovery_demo database.

#### Step 3: Explore the database schema

**User:** "I'm new to this database. Can you help me understand its structure and how the tables are related?"

### Expected Results

The assistant should provide:
- List of schemas and their purpose
- Major tables and their relationships
- Primary and foreign key structure
- Existing indexing strategy
- Entity-relationship description in natural language
- Recommendations for better documentation if needed

## Demo 5: Storage Optimization

### Scenario
Your database is growing rapidly and you're concerned about storage usage.

### Sample Interaction

**User:** "My database is growing too large. Can you help me identify what's taking up space and how I can optimize storage?"

**Assistant Actions:**
1. Analyzes table and index sizes
2. Identifies bloated tables and indexes
3. Finds unused indexes that can be safely removed
4. Recommends partitioning strategies for large tables
5. Suggests VACUUM and maintenance strategies

### Practical Implementation

#### Step 1: Create a database with storage issues

```bash
# Create a test database
createdb storage_optimization_demo

# Connect to the database
psql storage_optimization_demo
```

```sql
-- Enable extensions
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Create sample tables with storage issues
CREATE TABLE customer_data (
  id SERIAL PRIMARY KEY,
  first_name VARCHAR(50),
  last_name VARCHAR(50),
  email VARCHAR(100),
  full_address TEXT,
  phone VARCHAR(20),
  notes TEXT,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE transactions (
  id SERIAL PRIMARY KEY,
  customer_id INTEGER REFERENCES customer_data(id),
  amount NUMERIC(12,2),
  transaction_date TIMESTAMP DEFAULT NOW(),
  status VARCHAR(20),
  details TEXT
);

CREATE TABLE transaction_history (
  id SERIAL PRIMARY KEY,
  transaction_id INTEGER REFERENCES transactions(id),
  status VARCHAR(20),
  changed_at TIMESTAMP DEFAULT NOW(),
  changed_by VARCHAR(50)
);

CREATE TABLE logs (
  id SERIAL PRIMARY KEY,
  log_time TIMESTAMP DEFAULT NOW(),
  source VARCHAR(50),
  message TEXT,
  level VARCHAR(10)
);

-- Insert sample data
-- Add customers with large notes fields
INSERT INTO customer_data (first_name, last_name, email, full_address, phone, notes)
SELECT 
  'FirstName' || i,
  'LastName' || i,
  'customer' || i || '@example.com',
  'Full Address for customer ' || i || ', with city, state and ZIP code information included here for completeness',
  '555-' || lpad(i::text, 7, '0'),
  repeat('This is a sample note with lots of redundant information that takes up space. ', 10)
FROM generate_series(1, 100000) i;

-- Add transactions
INSERT INTO transactions (customer_id, amount, transaction_date, status, details)
SELECT 
  (random() * 100000)::integer + 1,
  (random() * 1000)::numeric(12,2),
  NOW() - (random() * 365 * 3)::integer * INTERVAL '1 day',
  (ARRAY['pending', 'completed', 'failed', 'refunded'])[1 + (i % 4)],
  repeat('Transaction details with verbose description that is mostly redundant. ', 5)
FROM generate_series(1, 500000) i;

-- Add transaction history (creating a lot of history for some transactions)
INSERT INTO transaction_history (transaction_id, status, changed_at, changed_by)
SELECT 
  (i % 100000) + 1,
  (ARRAY['created', 'pending', 'processing', 'completed', 'failed', 'refunded'])[1 + (i % 6)],
  NOW() - (random() * 365)::integer * INTERVAL '1 day',
  'User' || (random() * 100)::integer
FROM generate_series(1, 1000000) i;

-- Add log entries (many will be redundant)
INSERT INTO logs (log_time, source, message, level)
SELECT 
  NOW() - (random() * 90)::integer * INTERVAL '1 day',
  (ARRAY['app', 'database', 'system', 'network'])[1 + (i % 4)],
  CASE i % 10
    WHEN 0 THEN 'User login attempt'
    WHEN 1 THEN 'Transaction processed'
    WHEN 2 THEN 'Database connection established'
    WHEN 3 THEN 'Cache miss'
    WHEN 4 THEN 'API request received'
    ELSE 'Generic log message with additional text to take up space: ' || i::text
  END,
  (ARRAY['INFO', 'WARNING', 'ERROR', 'DEBUG'])[1 + (i % 4)]
FROM generate_series(1, 2000000) i;

-- Create excessive indexes
CREATE INDEX idx_customer_first_name ON customer_data(first_name);
CREATE INDEX idx_customer_last_name ON customer_data(last_name);
CREATE INDEX idx_customer_full_name ON customer_data(first_name, last_name); -- Redundant
CREATE INDEX idx_customer_email ON customer_data(email);
CREATE INDEX idx_customer_email_lower ON customer_data(lower(email)); -- Mostly redundant

CREATE INDEX idx_transactions_customer_id ON transactions(customer_id);
CREATE INDEX idx_transactions_date ON transactions(transaction_date);
CREATE INDEX idx_transactions_amount ON transactions(amount);
CREATE INDEX idx_transactions_status ON transactions(status);
CREATE INDEX idx_transactions_cust_date ON transactions(customer_id, transaction_date); -- Partially redundant

CREATE INDEX idx_logs_time ON logs(log_time);
CREATE INDEX idx_logs_source ON logs(source);
CREATE INDEX idx_logs_level ON logs(level);
CREATE INDEX idx_logs_source_level ON logs(source, level); -- Could be redundant

-- Create bloated table through updates
DO $$
BEGIN
  FOR i IN 1..50 LOOP
    UPDATE customer_data 
    SET notes = notes || ' Additional note added in update ' || i || '. '
    WHERE id % 100 = i % 100;
  END LOOP;
END $$;

DO $$
BEGIN
  FOR i IN 1..20 LOOP
    UPDATE transactions 
    SET details = details || ' Updated details for transaction. '
    WHERE id % 50 = i % 50;
  END LOOP;
END $$;
```

#### Step 2: Connect to the database with Postgres Pro MCP

Configure your AI assistant to connect to the storage_optimization_demo database.

#### Step 3: Ask for storage optimization suggestions

**User:** "My database is growing too large. Can you help me identify what's taking up space and how I can optimize storage?"

#### Step 4: Implement the recommended optimizations

After receiving the optimization recommendations, implement them:

```sql
-- Examples of optimizations that might be recommended:

-- Remove redundant indexes
DROP INDEX idx_customer_full_name;
DROP INDEX idx_customer_email_lower;
DROP INDEX idx_transactions_cust_date;
DROP INDEX idx_logs_source_level;

-- Fix bloated tables
VACUUM FULL customer_data;
VACUUM FULL transactions;

-- Add table partitioning for logs
CREATE TABLE logs_partitioned (
  id SERIAL,
  log_time TIMESTAMP DEFAULT NOW(),
  source VARCHAR(50),
  message TEXT,
  level VARCHAR(10)
) PARTITION BY RANGE (log_time);

-- Create monthly partitions
CREATE TABLE logs_y2023m01 PARTITION OF logs_partitioned
  FOR VALUES FROM ('2023-01-01') TO ('2023-02-01');
CREATE TABLE logs_y2023m02 PARTITION OF logs_partitioned
  FOR VALUES FROM ('2023-02-01') TO ('2023-03-01');
-- Add more partitions as needed

-- Create retention policy function
CREATE OR REPLACE FUNCTION delete_old_logs() RETURNS void AS $$
BEGIN
  DELETE FROM logs WHERE log_time < NOW() - INTERVAL '90 days';
END;
$$ LANGUAGE plpgsql;
```

### Expected Results

The analysis should provide:
- List of largest tables and indexes
- Identification of bloated objects
- Unused indexes that can be removed
- Partitioning recommendations for large tables
- Vacuum and maintenance schedule recommendations
- Estimated storage savings from implementing recommendations

## Demo 6: Advanced Workload Analysis

### Scenario
You want to understand your database workload patterns to plan for scaling and optimization.

### Sample Interaction

**User:** "We're planning to scale our application. Can you analyze our database workload patterns to help us understand what we need to optimize?"

**Assistant Actions:**
1. Analyzes query patterns from pg_stat_statements
2. Identifies peak usage times and resource-intensive operations
3. Categorizes queries by type (read/write, tables accessed)
4. Recommends caching strategies, connection pooling optimizations
5. Suggests application-level and database-level improvements

### Practical Implementation

#### Step 1: Set up a simulated production workload

```bash
# Create a test database
createdb workload_demo

# Connect to the database
psql workload_demo
```

```sql
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
  (ARRAY['Electronics', 'Clothing', 'Home', 'Books', 'Food'])[1 + (i % 5)]
FROM generate_series(1, 1000) i;

INSERT INTO orders (user_id, created_at, status)
SELECT 
  (random() * 10000)::integer + 1,
  NOW() - (random() * 180)::integer * INTERVAL '1 day',
  (ARRAY['completed', 'processing', 'shipped', 'cancelled'])[1 + (i % 4)]
FROM generate_series(1, 50000) i;

INSERT INTO order_items (order_id, product_id, quantity, price)
SELECT 
  (random() * 50000)::integer + 1,
  (random() * 1000)::integer + 1,
  (random() * 5)::integer + 1,
  (random() * 1000)::numeric(10,2)
FROM generate_series(1, 100000) i;

INSERT INTO page_views (user_id, page, viewed_at)
SELECT 
  (random() * 10000)::integer + 1,
  (ARRAY['home', 'product', 'cart', 'checkout', 'profile'])[1 + (i % 5)],
  NOW() - (random() * 30)::integer * INTERVAL '1 day' - (random() * 24)::integer * INTERVAL '1 hour'
FROM generate_series(1, 500000) i;
```

#### Step 2: Run a mixed workload simulation

```sql
-- Create a function to run random queries
CREATE OR REPLACE FUNCTION run_random_workload() RETURNS void AS $$
DECLARE
  user_id_var INTEGER;
  product_id_var INTEGER;
  order_id_var INTEGER;
  category_var VARCHAR;
BEGIN
  -- Select random values to use in queries
  SELECT id INTO user_id_var FROM users ORDER BY random() LIMIT 1;
  SELECT id INTO product_id_var FROM products ORDER BY random() LIMIT 1;
  SELECT id INTO order_id_var FROM orders ORDER BY random() LIMIT 1;
  SELECT category INTO category_var FROM products ORDER BY random() LIMIT 1;
  
  -- Run a random mix of queries
  CASE (random() * 10)::integer
    WHEN 0 THEN
      -- User profile query
      PERFORM * FROM users WHERE id = user_id_var;
    WHEN 1 THEN
      -- Product search
      PERFORM * FROM products WHERE category = category_var ORDER BY price LIMIT 10;
    WHEN 2 THEN
      -- Order history
      PERFORM o.id, o.created_at, sum(oi.price * oi.quantity) 
      FROM orders o JOIN order_items oi ON o.id = oi.order_id 
      WHERE o.user_id = user_id_var
      GROUP BY o.id, o.created_at ORDER BY o.created_at DESC;
    WHEN 3 THEN
      -- Product detail with stock
      PERFORM p.*, sum(oi.quantity) as total_ordered
      FROM products p LEFT JOIN order_items oi ON p.id = oi.product_id
      WHERE p.id = product_id_var
      GROUP BY p.id;
    WHEN 4 THEN
      -- Dashboard summary
      PERFORM count(*), sum(oi.price * oi.quantity)
      FROM orders o JOIN order_items oi ON o.id = oi.order_id
      WHERE o.created_at > (NOW() - INTERVAL '7 days');
    WHEN 5 THEN
      -- Insert page view
      INSERT INTO page_views (user_id, page)
      VALUES (user_id_var, (ARRAY['home', 'product', 'cart', 'checkout', 'profile'])[1 + (random() * 4)::integer]);
    WHEN 6 THEN
      -- Update product stock
      UPDATE products SET stock = stock - 1 WHERE id = product_id_var AND stock > 0;
    WHEN 7 THEN
      -- Order analysis
      PERFORM p.category, count(*), sum(oi.quantity)
      FROM order_items oi JOIN products p ON oi.product_id = p.id
      JOIN orders o ON oi.order_id = o.id
      WHERE o.created_at > (NOW() - INTERVAL '30 days')
      GROUP BY p.category ORDER BY sum(oi.quantity) DESC;
    WHEN 8 THEN
      -- User session analysis
      PERFORM user_id, count(*), min(viewed_at), max(viewed_at)
      FROM page_views
      WHERE viewed_at > (NOW() - INTERVAL '1 day')
      GROUP BY user_id HAVING count(*) > 5
      ORDER BY count(*) DESC LIMIT 10;
    ELSE
      -- Complex join for reporting
      PERFORM u.id, u.username, count(o.id) as order_count, sum(oi.price * oi.quantity) as total_spent
      FROM users u
      JOIN orders o ON u.id = o.user_id
      JOIN order_items oi ON o.id = oi.order_id
      WHERE u.created_at > (NOW() - INTERVAL '90 days')
      GROUP BY u.id, u.username
      ORDER BY total_spent DESC LIMIT 100;
  END CASE;
END;
$$ LANGUAGE plpgsql;

-- Run the workload multiple times
DO $$
BEGIN
  FOR i IN 1..1000 LOOP
    PERFORM run_random_workload();
  END LOOP;
END $$;

-- Reset pg_stat_statements to get clean data
SELECT pg_stat_statements_reset();

-- Run the workload again to collect stats
DO $$
BEGIN
  FOR i IN 1..1000 LOOP
    PERFORM run_random_workload();
  END LOOP;
END $$;
```

#### Step 3: Connect to the database with Postgres Pro MCP

Configure your AI assistant to connect to the workload_demo database.

#### Step 4: Analyze workload patterns

**User:** "We're planning to scale our application. Can you analyze our database workload patterns to help us understand what we need to optimize?"

#### Step 5: Implement the recommendations

After receiving the analysis and recommendations, you might implement changes such as:

```sql
-- Example implementations based on common recommendations:

-- Create targeted indexes for the most frequent queries
CREATE INDEX idx_page_views_user_viewed_at ON page_views(user_id, viewed_at);
CREATE INDEX idx_orders_created_at ON orders(created_at);
CREATE INDEX idx_products_category_price ON products(category, price);

-- Add materialized view for dashboard summary
CREATE MATERIALIZED VIEW mv_sales_summary AS
SELECT 
  date_trunc('day', o.created_at) as day,
  p.category,
  count(distinct o.id) as order_count,
  count(distinct o.user_id) as user_count,
  sum(oi.quantity) as items_sold,
  sum(oi.price * oi.quantity) as total_sales
FROM orders o 
JOIN order_items oi ON o.id = oi.order_id
JOIN products p ON oi.product_id = p.id
WHERE o.status = 'completed'
GROUP BY 1, 2;

-- Create refresh function
CREATE OR REPLACE FUNCTION refresh_sales_summary()
RETURNS void AS $$
BEGIN
  REFRESH MATERIALIZED VIEW mv_sales_summary;
END
$$ LANGUAGE plpgsql;

-- Add table partitioning for large tables
CREATE TABLE page_views_partitioned (
  id SERIAL,
  user_id INTEGER,
  page VARCHAR(100),
  viewed_at TIMESTAMP DEFAULT NOW()
) PARTITION BY RANGE (viewed_at);

-- Create partitions
CREATE TABLE page_views_y2023m01 PARTITION OF page_views_partitioned
  FOR VALUES FROM ('2023-01-01') TO ('2023-02-01');
CREATE TABLE page_views_y2023m02 PARTITION OF page_views_partitioned
  FOR VALUES FROM ('2023-02-01') TO ('2023-03-01');
-- Add more partitions as needed

-- Add connection pooling configuration example (for pgbouncer)
-- This would be added to your pgbouncer.ini file
/*
[databases]
workload_demo = host=127.0.0.1 port=5432 dbname=workload_demo

[pgbouncer]
pool_mode = transaction
max_client_conn = 1000
default_pool_size = 50
*/
```

### Expected Results

The analysis should provide:
- Breakdown of query types (SELECT, INSERT, UPDATE, DELETE)
- Time distribution of database activity
- Resource consumption patterns
- Tables and operations that are most resource-intensive
- Recommendations for scaling strategies
- Specific application and database optimization suggestions

## Sample Databases for Demo

To run these demos effectively, you can use these sample databases:

1. **Pagila** - A sample DVD rental database
   - [GitHub Repository](https://github.com/devrimgunduz/pagila)
   - Good for demonstrating JOIN optimizations and complex queries

2. **Northwind** - A classic sample database
   - [GitHub Repository](https://github.com/pthom/northwind_psql)
   - Simple structure that's easy to understand

3. **DVD Rental** - Another sample database from PostgreSQL tutorials
   - [Download Link](https://www.postgresqltutorial.com/postgresql-getting-started/postgresql-sample-database/)
   - Includes rental, inventory, and customer data

4. **PgBench** - For performance testing
   - Built into PostgreSQL
   - Run `pgbench -i -s 10` to create a test database with scaling factor 10

## Tips for Effective Demos

1. **Pre-warm the database**: Run some queries before the demo to ensure the cache is warm
2. **Create some issues**: Deliberately create some issues (like missing indexes) to demonstrate problem detection
3. **Use real-world queries**: Use realistic queries that demonstrate complex relationships and calculations
4. **Prepare failure cases**: Show how the system handles errors and edge cases
5. **Demonstrate progressive optimization**: Show how incremental improvements can significantly boost performance

## Advanced Multi-Step Demo Scenarios

### Setup Prerequisites

To run the advanced multi-step demo scenarios (Advanced Demos 1-3), you'll need:

1. A more powerful PostgreSQL instance (at least 4GB RAM recommended)
2. The following extensions installed:
   - pg_stat_statements
   - hypopg
   - pg_trgm (for text search demonstrations)

For each advanced demo, create a database with the following prefixes:
- Advanced Demo 1: `ecommerce_optimization`
- Advanced Demo 2: `datawarehouse_optimization`
- Advanced Demo 3: `migration_validation`

The setup scripts for these advanced demos are significantly larger and would be best run from script files. The example commands below reference script files that you'll need to create based on the demo descriptions or download from the repository:

```bash
# Example for setting up the E-commerce Performance Optimization demo
createdb ecommerce_optimization
psql ecommerce_optimization -f demo_scripts/advanced_demo1_setup.sql

# Example for setting up the Data Warehouse Optimization demo
createdb datawarehouse_optimization
psql datawarehouse_optimization -f demo_scripts/advanced_demo2_setup.sql

# Example for setting up the Migration Validation demo
# (This would typically involve a MySQL to PostgreSQL migration process)
# The script below just sets up the PostgreSQL side for demonstration
createdb migration_validation
psql migration_validation -f demo_scripts/advanced_demo3_setup.sql
```

⚠️ **Resource Warning**: These advanced demos involve large datasets and complex operations. Ensure your system has adequate resources (8GB+ RAM and 10GB+ free disk space recommended) before attempting to run them.

Note: For the Migration Validation demo (Advanced Demo 3), in a real-world scenario, you would be working with a database that was actually migrated from MySQL to PostgreSQL. The setup script creates a simulated post-migration PostgreSQL database that exhibits typical issues seen after migrations.

## Advanced Demo 1: E-commerce Performance Optimization Journey

### Scenario
You're the lead developer at a growing e-commerce site experiencing periodic slowdowns during peak hours. Customer complaints are increasing, and the marketing team plans a major promotion next week that will significantly increase traffic.

### Multi-step Interaction Flow

#### Step 1: Initial Assessment

**User:** "Our e-commerce site has been slowing down, especially during peak hours. We've got a major sales event next week and need to make sure the database can handle the load."

**Assistant:**
"I'll help you optimize your database for the upcoming sales event. Let's start with a comprehensive health check to establish baseline metrics and identify any immediate issues."

*The assistant runs:*
```
analyze_db_health with all components
```

"I've analyzed your database health and found several issues that could be affecting performance:

1. Your buffer cache hit rate is 82%, which is below the recommended 99%
2. I've identified 3 unused indexes consuming 4.2GB of storage
3. Several tables haven't been vacuumed in over 30 days
4. The 'orders' table appears to be bloated (43% bloat ratio)

Let's examine your slow queries before making optimization recommendations."

#### Step 2: Workload Analysis

*The assistant runs:*
```
get_top_queries
analyze_workload_indexes
```

"Looking at your query patterns, I see that:

1. The product search query accounts for 42% of your database load:
   ```sql
   SELECT p.*, c.category_name 
   FROM products p 
   JOIN categories c ON p.category_id = c.id 
   WHERE p.is_available = true 
   AND (p.name ILIKE $1 OR p.description ILIKE $1) 
   ORDER BY p.popularity_score DESC LIMIT 20;
   ```

2. Order history lookups are taking 2.3 seconds on average:
   ```sql
   SELECT o.*, ol.*, p.name, p.image_url
   FROM orders o
   JOIN order_lines ol ON o.id = ol.order_id
   JOIN products p ON ol.product_id = p.id
   WHERE o.customer_id = $1
   ORDER BY o.created_at DESC
   LIMIT 10;
   ```

3. Your checkout process involves 12 separate queries, several of which could be optimized

Based on the workload analysis, I recommend these specific optimizations:

1. Create a partial index for product searches
2. Add a covering index for order history
3. Drop the 3 unused indexes I identified
4. Run VACUUM FULL on the bloated orders table
5. Add an index on customer_id + created_at for the orders table"

**User:** "Can you explain how these changes will improve our product search specifically? That's the most critical part of our application."

#### Step 3: Testing Hypothetical Indexes

*The assistant runs:*
```
explain_query with hypothetical indexes for the product search query
```

"I've simulated several index combinations for your product search query. Here's what I found:

**Current execution plan (cost: 15403.28, estimated time: 570ms):**
- Performs a bitmap heap scan on products
- Uses a sequential scan on categories
- Uses a nested loop join

**With a multicolumn index on (is_available, popularity_score) including name, description:**
```sql
CREATE INDEX idx_product_search ON products (is_available, popularity_score DESC) 
INCLUDE (name, description, category_id);
```

This would reduce the query cost by 93% to 1078.42 (estimated time: ~40ms) by:
- Enabling an index-only scan instead of bitmap scan
- Eliminating the need for sorting (ORDER BY)
- Reducing the amount of data read from disk

This is the optimal index configuration for this specific query pattern. Would you like me to implement this change or simulate any other indexes?"

**User:** "That sounds promising. Let's implement those changes and verify the improvements."

#### Step 4: Implementing and Verifying Changes

*The assistant provides SQL statements for each change:*
```sql
-- Create optimal product search index
CREATE INDEX idx_product_search ON products (is_available, popularity_score DESC) 
INCLUDE (name, description, category_id);

-- Add covering index for order history
CREATE INDEX idx_orders_customer_history ON orders (customer_id, created_at DESC);

-- Drop unused indexes
DROP INDEX idx_products_price;
DROP INDEX idx_order_update_time;
DROP INDEX idx_rarely_used_column;

-- Run VACUUM FULL on bloated table
VACUUM FULL orders;

-- Adjust configuration (if you have server access)
ALTER SYSTEM SET shared_buffers = '2GB';
ALTER SYSTEM SET work_mem = '16MB';
```

*After implementation, the assistant verifies:*
```
explain_query for the product search with the new index
get_top_queries to confirm improvements
```

"I've verified the improvements:

1. The product search query is now running 14x faster, down from 570ms to 41ms
2. Order history lookups improved from 2.3s to 320ms
3. Overall database load has decreased by approximately 35%
4. We've reclaimed 4.2GB of storage by removing unused indexes
5. Buffer cache hit rate has improved to 96% after configuration changes

These optimizations should provide significant headroom for your upcoming sales event."

#### Step 5: Load Testing Simulation

**User:** "How can we verify the database will handle our projected load for the sales event?"

**Assistant:**
"To verify your database will handle the increased load, I recommend simulating your peak traffic patterns. Here's how we can set up a test:

1. First, create a custom pgbench script that mirrors your actual workload:

```sql
-- Save as custom_workload.sql
\set customer_id random(1, 10000)
\set search_term 'product' || random(1, 100)
\set product_id random(1, 5000)

BEGIN;
-- Product search (most common operation)
SELECT p.*, c.category_name 
FROM products p 
JOIN categories c ON p.category_id = c.id 
WHERE p.is_available = true 
AND (p.name ILIKE '%' || :search_term || '%') 
ORDER BY p.popularity_score DESC LIMIT 20;

-- View product details
SELECT * FROM products WHERE id = :product_id;

-- Customer order history (less frequent)
SELECT o.*, ol.*, p.name, p.image_url
FROM orders o
JOIN order_lines ol ON o.id = ol.order_id
JOIN products p ON ol.product_id = p.id
WHERE o.customer_id = :customer_id
ORDER BY o.created_at DESC
LIMIT 10;
COMMIT;
```

2. Run the load test with your projected traffic (for example, 5x current load):

```bash
pgbench -h localhost -U username -d dbname -f custom_workload.sql -c 50 -j 4 -T 300
```

This simulates 50 concurrent connections running your custom workload for 5 minutes.

3. During the test, monitor these key metrics:
   - Transaction throughput (TPS)
   - Average latency
   - 95th percentile latency
   - Connection utilization
   - CPU and memory usage

With these optimizations implemented, you should be able to handle the increased load for your sales event. The load test will confirm this and identify any remaining bottlenecks."

### Expected Results

After completing this multi-step optimization process, you should achieve:

- 80-95% reduction in query execution time for time-bounded queries
- 80-90% reduction in storage requirements through compression
- Automatic data lifecycle management without manual intervention
- Near real-time analytics even as data volumes continue to grow
- Ability to scale to billions of data points without degradation

## Advanced Demo 2: Data Warehouse Query Optimization

### Scenario
You're managing a data warehouse that analysts use for business intelligence. Reports that were fast with smaller data volumes now take hours to complete after a year of data collection. Your analytics team needs these reports to run much faster to support weekly instead of quarterly business reviews.

### Multi-Step Interactions

#### Step 1: Problem Assessment

**User:** "Our quarterly sales report query used to take 5 minutes but now runs for over 3 hours. We need to generate these reports weekly instead of quarterly, so this performance is unacceptable."

**Assistant:**
"I'll help you optimize your data warehouse queries. Can you share the problematic query so I can analyze it?"

**User:**
```sql
SELECT 
  date_trunc('month', o.order_date) as month,
  c.category_name,
  r.region_name,
  SUM(ol.quantity) as units_sold,
  SUM(ol.quantity * ol.unit_price) as revenue,
  COUNT(DISTINCT o.customer_id) as unique_customers
FROM orders o
JOIN order_lines ol ON o.id = ol.order_id
JOIN products p ON ol.product_id = p.id
JOIN categories c ON p.category_id = c.id
JOIN customers cust ON o.customer_id = cust.id
JOIN regions r ON cust.region_id = r.id
WHERE o.order_date >= '2022-01-01'
  AND o.status = 'completed'
GROUP BY 1, 2, 3
ORDER BY 1, 3, 2;
```

#### Step 2: Schema and Data Volume Analysis

*The assistant examines the schema and data volumes:*
```
list_schemas
list_objects in public schema
get_object_details for tables: orders, order_lines, products, categories, customers, regions
```

"I've analyzed your schema and found:

1. The `orders` table has 8.5 million rows (245GB)
2. The `order_lines` table has 42 million rows (512GB)
3. The `products` table has 250,000 rows
4. Your tables are not partitioned by date
5. There's no index on orders.order_date which is causing a sequential scan
6. The query involves 5 joins without optimal join indexes

These factors explain why your query performance has degraded as data volumes increased."

#### Step 3: Query Plan Analysis

*The assistant runs:*
```
explain_query with ANALYZE for the report query
```

"The execution plan reveals several bottlenecks:

1. **Sequential scan on orders table** (245GB) - Cost: 1,245,832.45, actual time: 1532.4ms
2. **Hash join between orders and order_lines** - Cost: 3,852,421.12, actual time: 5284.7ms
3. **Sort operation for GROUP BY** - Cost: 854,372.18, actual time: 3842.5ms
4. **Additional nested loop joins** with smaller tables - Cost: 452,471.85, actual time: 2143.2ms

The total execution time is 186 minutes, with most time spent on disk I/O and sorting operations. Let's implement a series of optimizations to address each bottleneck."

#### Step 4: Progressive Optimization

**Assistant:** "I recommend a progressive optimization approach where we implement changes in stages and measure the improvement at each step."

##### Stage 1: Basic Indexing

*Assistant provides SQL for initial improvements:*
```sql
-- Add index for the WHERE clause
CREATE INDEX idx_orders_date_status ON orders (order_date, status);

-- Add index for the GROUP BY
CREATE INDEX idx_orderlines_order_id ON order_lines (order_id);
```

"After implementing these basic indexes, the query execution time reduced from 186 minutes to 78 minutes - a 58% improvement, but still not enough."

##### Stage 2: Materialized View Creation

```sql
-- Create a materialized view for monthly sales aggregations
CREATE MATERIALIZED VIEW mv_monthly_sales AS
SELECT 
  date_trunc('month', o.order_date) as month,
  p.category_id,
  cust.region_id,
  SUM(ol.quantity) as units_sold,
  SUM(ol.quantity * ol.unit_price) as revenue,
  COUNT(DISTINCT o.customer_id) as unique_customers
FROM orders o
JOIN order_lines ol ON o.id = ol.order_id
JOIN products p ON ol.product_id = p.id
JOIN customers cust ON o.customer_id = cust.id
WHERE o.order_date >= '2022-01-01'
  AND o.status = 'completed'
GROUP BY 1, 2, 3;

-- Add indexes to the materialized view
CREATE INDEX idx_mv_monthly_sales_month ON mv_monthly_sales(month);
CREATE INDEX idx_mv_monthly_sales_category ON mv_monthly_sales(category_id);
CREATE INDEX idx_mv_monthly_sales_region ON mv_monthly_sales(region_id);

-- Create a refresh function
CREATE OR REPLACE FUNCTION refresh_sales_mv()
RETURNS void AS $$
BEGIN
  REFRESH MATERIALIZED VIEW mv_monthly_sales;
END
$$ LANGUAGE plpgsql;
```

"Now let's rewrite the query to use the materialized view:"

```sql
SELECT 
  mv.month,
  c.category_name,
  r.region_name,
  SUM(mv.units_sold) as units_sold,
  SUM(mv.revenue) as revenue,
  SUM(mv.unique_customers) as unique_customers
FROM mv_monthly_sales mv
JOIN categories c ON mv.category_id = c.id
JOIN regions r ON mv.region_id = r.id
GROUP BY 1, 2, 3
ORDER BY 1, 3, 2;
```

"This rewritten query now executes in 12 seconds - a 930x improvement over the original 3-hour runtime."

##### Stage 3: Table Partitioning Strategy

"For a long-term solution, I recommend implementing table partitioning on your orders table by date range:"

```sql
-- Create partitioned orders table
CREATE TABLE orders_partitioned (
  -- Same schema as original orders table
) PARTITION BY RANGE (order_date);

-- Create partitions by quarter
CREATE TABLE orders_y2022q1 PARTITION OF orders_partitioned
  FOR VALUES FROM ('2022-01-01') TO ('2022-04-01');
  
CREATE TABLE orders_y2022q2 PARTITION OF orders_partitioned
  FOR VALUES FROM ('2022-04-01') TO ('2022-07-01');
  
-- Add additional partitions as needed
-- ...

-- Create similar partitioning for order_lines if needed
```

"Partitioning would give you additional benefits:
1. More efficient pruning of irrelevant data
2. Faster vacuum and maintenance operations
3. Ability to archive older partitions to lower-cost storage
4. Improved query planning"

#### Step 5: Maintenance and Automation

"To maintain optimal performance as your data continues to grow:

1. Schedule regular refreshes of the materialized view:
   ```sql
   -- Add to crontab or pg_cron
   SELECT refresh_sales_mv();
   ```

2. Automate the creation of new partitions as needed

3. Set up a monitoring query to track the execution time trends:
   ```sql
   SELECT query, calls, mean_exec_time, total_exec_time
   FROM pg_stat_statements
   WHERE query ILIKE '%category%region%revenue%'
   ORDER BY total_exec_time DESC
   LIMIT 10;
   ```

4. Schedule statistics updates for optimal query planning:
   ```sql
   ANALYZE mv_monthly_sales;
   ```

With these optimizations in place, your reports will complete in seconds instead of hours, enabling weekly business reviews."

### Expected Results

After completing this data warehouse optimization:
- Query execution time reduced from 3+ hours to seconds
- Automated maintenance ensures consistent performance
- Data growth is managed through partitioning
- Analysts can run reports on demand rather than scheduling them overnight

## Advanced Demo 3: Database Migration Validation

### Scenario
You're migrating from a legacy database to a new PostgreSQL instance. You need to verify data integrity, optimize for the new workload, and ensure performance meets expectations.

### Multi-Step Interactions

#### Step 1: Initial Assessment

**User:** "We've just migrated our application database from MySQL to PostgreSQL. I need to validate the migration, ensure data integrity, and optimize for our workload patterns. Can you help?"

**Assistant:**
"I'll help you validate your migration and optimize your new PostgreSQL database. Let's start with a systematic assessment of the new database structure and health."

*The assistant runs:*
```
list_schemas
list_objects in public schema
analyze_db_health
```

"I've completed an initial assessment of your PostgreSQL database:

1. Schema structure: 42 tables, 18 views, 87 indexes, 6 triggers
2. Database health findings:
   - Missing primary keys on 3 tables
   - Foreign key constraints without indexes: 8
   - No indexes on frequently queried timestamp columns
   - Several tables missing ANALYZE statistics
   - No extensions currently installed

Let's continue with more detailed validation checks."

#### Step 2: Data Integrity Validation

**User:** "What checks should we run to ensure all data migrated correctly?"

**Assistant:**
"To validate data integrity after migration, let's run these checks:

1. **Row count validation for all tables:**
```sql
-- Save this as rowcount_validation.sql
SELECT 'SELECT ''' || table_name || ''', COUNT(*) FROM ' || table_name || ';'
FROM information_schema.tables 
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name;
```

2. **Primary key uniqueness check:**
```sql
-- For each table with a primary key
SELECT COUNT(*) as total_rows, COUNT(DISTINCT id) as unique_ids
FROM users;
-- Repeat for all tables with primary keys
```

3. **Foreign key validation:**
```sql
-- Example for order_items table
SELECT COUNT(*) 
FROM order_items oi 
LEFT JOIN orders o ON oi.order_id = o.id
WHERE o.id IS NULL;
```

4. **Data type conversion issues check:**
```sql
-- Check for truncated values in text fields
SELECT COUNT(*) FROM products WHERE LENGTH(description) = 255;
```

5. **Statistical distribution comparison:**
```sql
-- Check value distributions match expected patterns
SELECT 
  MIN(amount), MAX(amount), AVG(amount), 
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) as median
FROM transactions;
```

Would you like me to implement any of these checks now?"

**User:** "Let's run the row count validation first and then check for foreign key issues."

*The assistant runs the row count validation and foreign key checks*

"I've found several integrity issues:

1. The `order_lines` table has 3,842 orphaned records with no matching order_id
2. The `customer_addresses` table has 275 fewer rows than expected
3. The `product_attributes` table has duplicate entries for some products

These need to be addressed before proceeding with optimization."

#### Step 3: Index and Query Optimization

**User:** "We've fixed those data issues. Now let's focus on optimizing the database for our workload."

*The assistant runs:*
```
get_top_queries
analyze_workload_indexes
```

"Based on your query patterns, I've identified these optimization opportunities:

1. **Missing indexes on foreign keys:**
```sql
CREATE INDEX idx_order_lines_order_id ON order_lines(order_id);
CREATE INDEX idx_order_lines_product_id ON order_lines(product_id);
CREATE INDEX idx_transactions_customer_id ON transactions(customer_id);
```

2. **Timestamp query optimization:**
```sql
CREATE INDEX idx_orders_created_at ON orders(created_at);
CREATE INDEX idx_user_events_timestamp ON user_events(event_timestamp);
```

3. **Text search improvements:**
```sql
-- For product search functionality
CREATE INDEX idx_products_name_gin ON products USING gin(name gin_trgm_ops);
CREATE EXTENSION pg_trgm;
```

4. **Missing primary keys:**
```sql
ALTER TABLE log_entries ADD PRIMARY KEY (id);
ALTER TABLE temporary_uploads ADD PRIMARY KEY (id);
```

5. **Query rewriting opportunities:**
Several queries are using `COUNT(*)` over entire tables which can be optimized by using smaller indexed columns.

Would you like me to implement these optimizations?"

#### Step 4: Configuration Tuning

**User:** "Yes, please implement those index changes. What about PostgreSQL-specific configuration tuning we should consider?"

**Assistant:**
"Now let's tune your PostgreSQL configuration to match your workload characteristics:

1. **Memory configuration:**
```sql
-- Check current settings
SHOW shared_buffers;
SHOW work_mem;
SHOW maintenance_work_mem;
SHOW effective_cache_size;
```

Given your server has 32GB RAM and this is a dedicated database server, I recommend:

```
shared_buffers = 8GB         # 25% of RAM
work_mem = 64MB              # Higher for complex sorts and joins
maintenance_work_mem = 512MB # Higher for VACUUM operations
effective_cache_size = 24GB  # 75% of RAM
```

2. **Write performance:**
```
wal_buffers = 16MB           # Helps with write-heavy workloads
checkpoint_completion_target = 0.9  # Spreads out checkpoint writes
```

3. **Query planning:**
```
random_page_cost = 1.1       # For SSD storage
effective_io_concurrency = 200  # For SSD storage
```

4. **Autovacuum tuning:**
```
autovacuum_vacuum_scale_factor = 0.05  # More aggressive vacuuming
autovacuum_analyze_scale_factor = 0.02  # More frequent statistics updates
```

These settings should be added to your postgresql.conf file."

#### Step 5: Migration-Specific Optimizations

**User:** "Are there any MySQL-to-PostgreSQL specific optimizations we should consider?"

**Assistant:**
"Yes, there are several MySQL-to-PostgreSQL migration-specific optimizations:

1. **Update application query patterns:**
   - Replace `LIMIT x,y` with PostgreSQL's `OFFSET x LIMIT y`
   - Replace `IF()` with `CASE WHEN` expressions
   - Update `GROUP_CONCAT()` functions with PostgreSQL's `string_agg()`

2. **Review and update data types:**
   - Replace `UNSIGNED` integer types with appropriate PostgreSQL types
   - Migrate `ENUM` types to PostgreSQL's native ENUM type or CHECK constraints
   - Update any `DATETIME` fields to use PostgreSQL's richer timestamp types

3. **Indexing differences:**
   - MySQL's implicit primary key indexes vs. PostgreSQL's explicit indexing
   - Consider using PostgreSQL-specific index types (GIN, BRIN, etc.) where appropriate

4. **Transaction isolation:**
   - MySQL's default isolation level (REPEATABLE READ) vs. PostgreSQL's default (READ COMMITTED)
   - Review transaction boundaries in your application code

5. **Text search functionality:**
   - Migrate from MySQL's fulltext search to PostgreSQL's more powerful full-text search
   ```sql
   CREATE INDEX idx_product_search ON products USING gin(to_tsvector('english', name || ' ' || description));
   ```

Would you like me to elaborate on any of these areas?"

### Expected Results

After completing this migration validation and optimization process:
- Verified data integrity with no loss during migration
- Optimized schema and indexes for PostgreSQL
- Configured server parameters for your specific workload
- Updated queries to leverage PostgreSQL-specific features
- Performance equal to or better than the original MySQL database

## Advanced Demo 4: Time-Series Data Management

### Scenario
You're a data engineer responsible for an IoT platform collecting sensor data from manufacturing facilities. The system ingests millions of readings daily from thousands of devices, and performance has been steadily declining as data volumes grow. Operations teams need real-time dashboards and alerting, while data science teams require efficient access to historical trends.

### Multi-step Interaction Flow

#### Step 1: Initial Assessment

**User:** "Our IoT database collects temperature, humidity, and pressure readings from factory sensors every 5 minutes. Reports and dashboards are getting slower each month as we add more devices. How can we optimize this for both real-time monitoring and historical analysis?"

**Assistant:**
"I'll help you implement time-series specific optimizations for your IoT database. Let's first analyze your current database structure and query patterns."

*The assistant runs:*
```
list_schemas
list_objects in iot schema
get_object_details for iot.sensor_data
analyze_db_health
```

"I've analyzed your IoT database and identified several optimization opportunities:

1. Your `sensor_data` table contains 143 million rows spanning 2 years with no partitioning
2. Queries scanning more than a week of data take 45+ seconds to complete
3. The database uses standard B-tree indexes, which aren't optimal for time-series access patterns
4. You're storing raw data indefinitely, which isn't necessary for older historical analysis
5. Most of your analytical queries aggregate data but still scan raw readings

Let's implement a comprehensive time-series optimization strategy using TimescaleDB's specialized features."

#### Step 2: Implementing TimescaleDB Hypertables

**User:** "That sounds promising. What specific changes would you recommend to improve performance?"

*The assistant runs:*
```
execute_sql to examine typical queries
```

"I recommend converting your main sensor_data table to a TimescaleDB hypertable with time-based partitioning. This will significantly improve query performance by enabling chunk exclusion, where queries only scan relevant time partitions."

*The assistant provides SQL statements:*
```sql
-- First, install the TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Convert the existing table to a hypertable
SELECT create_hypertable('iot.sensor_data', 'time', 
  chunk_time_interval => INTERVAL '1 day',
  migrate_data => true);
  
-- Create an optimized index for time-series queries
CREATE INDEX idx_sensor_data_time_device ON iot.sensor_data(device_id, time DESC);
```

"This change alone should provide a 10-50x speedup for time-bounded queries. The hypertable automatically partitions your data into chunks based on time, optimizing both insert and query performance."

#### Step 3: Implementing Continuous Aggregates for Analytics

**User:** "Our operations team needs to see hourly averages for the last 7 days and daily averages for the last 90 days. Would that require separate tables?"

*The assistant analyzes query patterns further:*
```
explain_query with analyze for dashboard queries
```

"Instead of maintaining separate aggregate tables manually, I recommend implementing TimescaleDB's continuous aggregates. These are automatically refreshed materialized views that are specifically optimized for time-series aggregation."

*The assistant provides implementation SQL:*
```sql
-- Create an hourly rollup view
CREATE MATERIALIZED VIEW iot.hourly_metrics
WITH (timescaledb.continuous) AS
SELECT 
  time_bucket('1 hour', time) AS bucket,
  device_id,
  AVG(temperature) AS avg_temp,
  MIN(temperature) AS min_temp,
  MAX(temperature) AS max_temp,
  AVG(humidity) AS avg_humidity,
  AVG(pressure) AS avg_pressure
FROM iot.sensor_data
GROUP BY bucket, device_id;

-- Create a daily rollup view
CREATE MATERIALIZED VIEW iot.daily_metrics
WITH (timescaledb.continuous) AS
SELECT 
  time_bucket('1 day', time) AS bucket,
  device_id,
  AVG(temperature) AS avg_temp,
  MIN(temperature) AS min_temp,
  MAX(temperature) AS max_temp,
  AVG(humidity) AS avg_humidity,
  AVG(pressure) AS avg_pressure
FROM iot.sensor_data
GROUP BY bucket, device_id;

-- Add automated refresh policies
SELECT add_continuous_aggregate_policy('iot.hourly_metrics',
  start_offset => INTERVAL '1 week',
  end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 hour');
  
SELECT add_continuous_aggregate_policy('iot.daily_metrics',
  start_offset => INTERVAL '3 months',
  end_offset => INTERVAL '1 day',
  schedule_interval => INTERVAL '1 day');
```

"These continuous aggregates will dramatically improve dashboard performance. For example, a query that previously scanned millions of raw readings to calculate a daily average will now just retrieve pre-computed aggregates from these views."

#### Step 4: Implementing Data Retention and Compression

**User:** "We need to keep raw data for 90 days for troubleshooting, but after that, we only need aggregated data for trends. How do we manage that without manual purging?"

*The assistant runs:*
```
get_object_details for iot.sensor_data to check current size
analyze_db_health with health_type=storage
```

"I recommend implementing an automated data lifecycle policy with TimescaleDB. We can compress older data to reduce storage requirements and automatically drop data beyond your retention period."

*The assistant provides SQL implementation:*
```sql
-- Compress chunks older than 7 days (reduces storage by ~70-90%)
SELECT add_compression_policy('iot.sensor_data', INTERVAL '7 days');

-- Automatically drop raw data older than 90 days
-- (since we have continuous aggregates for historical analysis)
SELECT add_retention_policy('iot.sensor_data', INTERVAL '90 days');
```

"With these policies:
1. Data older than 7 days will be automatically compressed, reducing your storage needs by approximately 80%
2. Raw sensor data older than 90 days will be automatically removed
3. Your pre-computed aggregates in the continuous aggregate views will be retained indefinitely
4. These processes happen automatically in the background

This approach gives you the best of both worlds - detailed recent data for troubleshooting and aggregated historical data for trend analysis, all while optimizing storage costs."

#### Step 5: Implementing Anomaly Detection

**User:** "Our team currently runs a nightly job to detect sensor anomalies by calculating standard deviations, but it's getting very slow. Can we make this more efficient and possibly real-time?"

*The assistant analyzes the current anomaly detection query:*
```
execute_sql to examine the current anomaly detection queries
explain_query with analyze for anomaly detection
```

"Your current anomaly detection query is inefficient because it recalculates statistics across all historical data. Let's create a more efficient real-time approach using TimescaleDB's hyperfunctions."

*The assistant provides the optimized implementation:*
```sql
-- Create a view that detects anomalies efficiently
CREATE OR REPLACE VIEW iot.temperature_anomalies AS
WITH device_stats AS (
  -- Calculate per-device statistics using the daily aggregate
  SELECT 
    device_id,
    AVG(avg_temp) as avg_temperature,
    STDDEV(avg_temp) as stddev_temperature
  FROM iot.daily_metrics
  WHERE bucket >= NOW() - INTERVAL '30 days'
  GROUP BY device_id
),
recent_data AS (
  -- Use the hourly aggregate for recent data instead of raw readings
  SELECT 
    s.bucket as time,
    s.device_id,
    s.avg_temp as temperature,
    ds.avg_temperature,
    ds.stddev_temperature,
    (s.avg_temp - ds.avg_temperature) / NULLIF(ds.stddev_temperature, 0) as z_score
  FROM iot.hourly_metrics s
  JOIN device_stats ds ON s.device_id = ds.device_id
  WHERE s.bucket >= NOW() - INTERVAL '24 hours'
)
SELECT 
  rd.device_id,
  d.device_name,
  l.location_name,
  rd.time,
  rd.temperature,
  rd.avg_temperature as baseline_avg,
  rd.stddev_temperature as baseline_stddev,
  rd.z_score
FROM recent_data rd
JOIN iot.devices d ON rd.device_id = d.device_id
JOIN iot.locations l ON d.location_id = l.location_id
WHERE ABS(rd.z_score) > 3.0  -- 3 standard deviations
ORDER BY ABS(rd.z_score) DESC;
```

"This approach provides several advantages:
1. Anomaly detection now completes in milliseconds instead of minutes
2. It uses pre-aggregated data instead of scanning raw readings
3. The baseline statistics are calculated from a relevant historical window
4. The view can be queried in real-time for dashboard alerts

Since this uses your continuous aggregates, the anomaly detection automatically accounts for new data as it's collected."

### Expected Results

After implementing these time-series optimizations, you should achieve:
- 10-50x faster query performance for time-bounded queries
- 80-90% reduction in storage requirements through compression
- Automatic data lifecycle management without manual intervention
- Near real-time analytics even as data volumes continue to grow
- Ability to scale to billions of data points without degradation

## Cleanup After Demos

After running the demos, you may want to clean up your system to free resources:

```bash
# List all test databases
psql -c "\l" | grep demo

# Drop each test database
dropdb health_demo
dropdb slow_app_demo
dropdb query_demo
dropdb schema_discovery_demo
dropdb storage_optimization_demo
dropdb workload_demo
dropdb ecommerce_optimization
dropdb datawarehouse_optimization
dropdb migration_validation
```
