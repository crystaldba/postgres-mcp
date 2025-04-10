-- Demo 1: Database Health Assessment Setup
-- This script creates a database with various health issues to demonstrate
-- the database health assessment features of Postgres Pro MCP

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

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

-- Create a large unvacuumed table
CREATE TABLE unvacuumed_table (
  id SERIAL PRIMARY KEY,
  data TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);

INSERT INTO unvacuumed_table (data, created_at)
SELECT 
  md5(random()::text),
  NOW() - (random() * 365)::INTEGER * INTERVAL '1 day'
FROM generate_series(1, 500000);

-- Create many updates without vacuum
DO $$
BEGIN
  FOR i IN 1..10 LOOP
    DELETE FROM unvacuumed_table WHERE id % 10 = i % 10;
    INSERT INTO unvacuumed_table (data, created_at)
    SELECT 
      md5(random()::text),
      NOW() - (random() * 30)::INTEGER * INTERVAL '1 day'
    FROM generate_series(1, 50000);
  END LOOP;
END $$;

-- Create tables with non-indexed foreign keys
CREATE TABLE parent_table (
  id SERIAL PRIMARY KEY,
  name TEXT
);

CREATE TABLE child_table (
  id SERIAL PRIMARY KEY,
  parent_id INTEGER REFERENCES parent_table(id), -- Foreign key without index
  data TEXT
);

INSERT INTO parent_table (name)
SELECT 'Parent ' || i FROM generate_series(1, 1000) i;

INSERT INTO child_table (parent_id, data)
SELECT 
  (random() * 999 + 1)::INTEGER,
  'Child data ' || i
FROM generate_series(1, 10000) i;

-- Create table with duplicate data
CREATE TABLE duplicate_data (
  id SERIAL PRIMARY KEY,
  code VARCHAR(10) NOT NULL,
  description TEXT
);

INSERT INTO duplicate_data (code, description)
SELECT
  'CODE-' || (i % 100), -- Only 100 unique codes
  'Description for code ' || (i % 100)
FROM generate_series(1, 10000) i;

-- Update statistics
ANALYZE;

-- Print summary
SELECT 'Database Health Assessment demo setup completed successfully.';
SELECT 'The database contains various health issues that can be identified and fixed.';
SELECT 'Use the analyze_db_health tool to assess the database health.'; 
