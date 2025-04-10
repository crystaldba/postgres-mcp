-- Demo 2: Index Recommendations for a Slow Application
-- This script creates a database with performance issues caused by missing or inefficient indexes

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

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

-- Create another slow query that searches for reports by keyword
CREATE OR REPLACE FUNCTION search_reports(search_term TEXT) RETURNS TABLE (
  report_id INTEGER,
  report_title VARCHAR,
  creator_name VARCHAR,
  view_count BIGINT
) AS $$
BEGIN
  RETURN QUERY
  SELECT 
    r.id,
    r.title,
    c.name,
    COUNT(rv.id)::BIGINT
  FROM reports r
  JOIN customers c ON r.creator_id = c.id
  LEFT JOIN report_views rv ON r.id = rv.report_id
  WHERE r.title ILIKE '%' || search_term || '%' 
     OR r.description ILIKE '%' || search_term || '%'
  GROUP BY r.id, r.title, c.name
  ORDER BY COUNT(rv.id) DESC;
END;
$$ LANGUAGE plpgsql;

-- Run some sample searches
SELECT * FROM search_reports('report');
SELECT * FROM search_reports('analysis');
SELECT * FROM search_reports('data');

-- Update statistics
ANALYZE;

-- Print summary
SELECT 'Index Recommendations demo setup completed successfully.';
SELECT 'The database contains queries with missing or inefficient indexes.';
SELECT 'Use the get_top_queries and analyze_workload_indexes tools to analyze performance.'; 
