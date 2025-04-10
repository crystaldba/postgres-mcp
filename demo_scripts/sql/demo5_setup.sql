-- Demo 5: Storage Optimization
-- This script creates a database with various storage issues that can be optimized

-- Enable required extensions
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
  (random() * 99999 + 1)::integer,
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
CREATE INDEX idx_logs_message ON logs(message); -- Very inefficient on TEXT

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

-- Create duplicate data table
CREATE TABLE duplicate_data (
  id SERIAL PRIMARY KEY,
  description TEXT,
  category VARCHAR(50),
  data_value TEXT
);

-- Insert duplicate data
INSERT INTO duplicate_data (description, category, data_value)
SELECT
  'Description for item ' || (i % 100), -- Only 100 unique descriptions
  (ARRAY['A', 'B', 'C', 'D', 'E'])[1 + (i % 5)],
  'Data value ' || (i % 20) -- Only 20 unique data values
FROM generate_series(1, 50000) i;

-- Create a table that could benefit from compression
CREATE TABLE sensor_readings (
  id SERIAL PRIMARY KEY,
  device_id INTEGER,
  reading_time TIMESTAMP,
  temperature NUMERIC(5,2),
  humidity NUMERIC(5,2),
  pressure NUMERIC(7,2),
  voltage NUMERIC(5,2)
);

-- Add time-series data that repeats patterns (good for compression)
INSERT INTO sensor_readings (device_id, reading_time, temperature, humidity, pressure, voltage)
SELECT
  (i % 100) + 1, -- 100 devices
  NOW() - (30 * 24 * 60 - i) * INTERVAL '1 minute', -- One reading per minute for 30 days
  20 + (10 * sin(i::float / 60 / 12))::numeric(5,2) + (random() * 2)::numeric(5,2), -- Temperature follows daily pattern
  50 + (10 * sin(i::float / 60 / 12 + 3))::numeric(5,2) + (random() * 5)::numeric(5,2), -- Humidity follows daily pattern
  1013 + (5 * sin(i::float / 60 / 24))::numeric(7,2) + (random() * 2)::numeric(7,2), -- Pressure follows daily pattern
  120 + (random() * 5)::numeric(5,2) -- Voltage mostly stable
FROM generate_series(1, 5000) i; -- Reduced number for demo setup speed

-- Create a table with TOAST issues (large TEXT fields)
CREATE TABLE documents (
  id SERIAL PRIMARY KEY,
  title VARCHAR(200),
  content TEXT,
  metadata JSONB,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Insert documents with large content fields
INSERT INTO documents (title, content, metadata)
SELECT
  'Document Title ' || i,
  repeat('This is paragraph ' || i || ' of the document. It contains sample text that will be stored in the database. ', 500),
  jsonb_build_object(
    'author', 'Author ' || (i % 20 + 1),
    'category', (ARRAY['Technical', 'Business', 'Legal', 'Marketing', 'Research'])[1 + (i % 5)],
    'tags', array[(i % 10)::text, ((i+1) % 10)::text, ((i+2) % 10)::text],
    'version', (i % 5) + 1,
    'status', (ARRAY['draft', 'published', 'archived'])[1 + (i % 3)]
  )
FROM generate_series(1, 1000) i;

-- Create a table with a poor choice of data types
CREATE TABLE inventory (
  id SERIAL PRIMARY KEY,
  product_code VARCHAR(100), -- Oversized for typical product codes
  description TEXT, -- Even for short descriptions
  quantity INTEGER,
  price NUMERIC(12,2),
  last_updated TIMESTAMP DEFAULT NOW()
);

-- Insert inventory data
INSERT INTO inventory (product_code, description, quantity, price)
SELECT
  'PROD-' || i, -- Typically less than 20 chars
  'Product ' || i, -- Typically short descriptions
  (random() * 1000)::integer,
  (random() * 500)::numeric(12,2)
FROM generate_series(1, 10000) i;

-- Update statistics
ANALYZE;

-- Print summary
SELECT 'Storage Optimization demo setup completed successfully.';
SELECT 'The database contains tables with various storage inefficiencies.';
SELECT 'Use the analyze_db_health tool to identify storage issues.'; 
