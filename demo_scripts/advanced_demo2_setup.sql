-- Advanced Demo 2: Data Warehouse Query Optimization Setup
-- This script sets up a simulated data warehouse with performance issues for analytical queries

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS hypopg;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Create dimension and fact tables for a star schema data warehouse
-- Dimension tables
CREATE TABLE regions (
  id SERIAL PRIMARY KEY,
  region_name VARCHAR(50) NOT NULL,
  region_code VARCHAR(10) NOT NULL,
  continent VARCHAR(30) NOT NULL,
  country VARCHAR(50) NOT NULL,
  country_code VARCHAR(5) NOT NULL,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE categories (
  id SERIAL PRIMARY KEY,
  category_name VARCHAR(100) NOT NULL,
  category_code VARCHAR(20) NOT NULL,
  parent_id INTEGER REFERENCES categories(id),
  description TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  product_name VARCHAR(200) NOT NULL,
  product_code VARCHAR(30) NOT NULL,
  category_id INTEGER REFERENCES categories(id),
  description TEXT,
  base_price NUMERIC(12,2) NOT NULL,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW(),
  is_active BOOLEAN DEFAULT TRUE,
  vendor_id INTEGER, -- Will be linked to vendors table
  tags TEXT[]
);

CREATE TABLE vendors (
  id SERIAL PRIMARY KEY,
  vendor_name VARCHAR(100) NOT NULL,
  contact_name VARCHAR(100),
  contact_email VARCHAR(100),
  contact_phone VARCHAR(30),
  region_id INTEGER REFERENCES regions(id),
  created_at TIMESTAMP DEFAULT NOW(),
  is_active BOOLEAN DEFAULT TRUE
);

-- Reference from products to vendors
ALTER TABLE products ADD CONSTRAINT fk_products_vendor FOREIGN KEY (vendor_id) REFERENCES vendors(id);

CREATE TABLE time_dim (
  date_id INTEGER PRIMARY KEY, -- YYYYMMDD format
  date DATE NOT NULL,
  day INTEGER NOT NULL,
  month INTEGER NOT NULL,
  quarter INTEGER NOT NULL,
  year INTEGER NOT NULL,
  day_of_week INTEGER NOT NULL,
  day_name VARCHAR(10) NOT NULL,
  month_name VARCHAR(10) NOT NULL,
  is_weekend BOOLEAN NOT NULL,
  is_holiday BOOLEAN NOT NULL,
  fiscal_year INTEGER NOT NULL,
  fiscal_quarter INTEGER NOT NULL
);

CREATE TABLE customers (
  id SERIAL PRIMARY KEY,
  first_name VARCHAR(50),
  last_name VARCHAR(50),
  email VARCHAR(100) UNIQUE NOT NULL,
  phone VARCHAR(30),
  address_line1 VARCHAR(100),
  address_line2 VARCHAR(100),
  city VARCHAR(50),
  state VARCHAR(50),
  postal_code VARCHAR(20),
  country VARCHAR(50),
  region_id INTEGER REFERENCES regions(id),
  customer_since DATE NOT NULL,
  last_purchase_date DATE,
  customer_segment VARCHAR(30) NOT NULL, -- 'individual', 'business', 'government', etc.
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

-- Fact tables
CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  order_date DATE NOT NULL,
  date_id INTEGER REFERENCES time_dim(date_id),
  customer_id INTEGER REFERENCES customers(id),
  region_id INTEGER REFERENCES regions(id),
  order_status VARCHAR(20) NOT NULL, -- 'completed', 'cancelled', 'pending', etc.
  shipping_method VARCHAR(30),
  order_channel VARCHAR(30), -- 'online', 'phone', 'in-store', etc.
  order_priority VARCHAR(20), -- 'high', 'medium', 'low'
  shipping_cost NUMERIC(12,2),
  tax_amount NUMERIC(12,2),
  total_amount NUMERIC(12,2) NOT NULL,
  discount_amount NUMERIC(12,2) DEFAULT 0,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE order_lines (
  id SERIAL PRIMARY KEY,
  order_id INTEGER REFERENCES orders(id),
  product_id INTEGER REFERENCES products(id),
  vendor_id INTEGER REFERENCES vendors(id),
  quantity INTEGER NOT NULL,
  unit_price NUMERIC(12,2) NOT NULL,
  discount_percent NUMERIC(5,2) DEFAULT 0,
  tax_rate NUMERIC(5,2) DEFAULT 0,
  total_price NUMERIC(12,2) NOT NULL, -- quantity * unit_price * (1 - discount_percent/100) * (1 + tax_rate/100)
  is_promotion BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE inventory_snapshots (
  id SERIAL PRIMARY KEY,
  date_id INTEGER REFERENCES time_dim(date_id),
  product_id INTEGER REFERENCES products(id),
  vendor_id INTEGER REFERENCES vendors(id),
  region_id INTEGER REFERENCES regions(id),
  quantity_on_hand INTEGER NOT NULL,
  quantity_on_order INTEGER NOT NULL,
  quantity_committed INTEGER NOT NULL,
  quantity_available INTEGER NOT NULL, -- quantity_on_hand - quantity_committed
  reorder_point INTEGER,
  standard_cost NUMERIC(12,2) NOT NULL,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE sales_targets (
  id SERIAL PRIMARY KEY,
  date_id INTEGER REFERENCES time_dim(date_id),
  product_id INTEGER REFERENCES products(id) DEFAULT NULL,
  category_id INTEGER REFERENCES categories(id) DEFAULT NULL,
  region_id INTEGER REFERENCES regions(id) DEFAULT NULL,
  target_amount NUMERIC(12,2) NOT NULL,
  target_units INTEGER,
  target_type VARCHAR(30) NOT NULL -- 'monthly', 'quarterly', 'annual'
);

-- Create indexes (not optimized by intention)
CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_order_lines_product ON order_lines(product_id);
CREATE INDEX idx_inventory_product ON inventory_snapshots(product_id);

-- Generate data for the data warehouse

-- Function to generate a date_id from a date (YYYYMMDD format)
CREATE OR REPLACE FUNCTION get_date_id(input_date DATE) RETURNS INTEGER AS $$
BEGIN
  RETURN (EXTRACT(YEAR FROM input_date) * 10000 + 
         EXTRACT(MONTH FROM input_date) * 100 + 
         EXTRACT(DAY FROM input_date))::INTEGER;
END;
$$ LANGUAGE plpgsql;

-- Populate time_dim for 5 years (from 3 years ago to 2 years in the future)
INSERT INTO time_dim (date_id, date, day, month, quarter, year, day_of_week, day_name, month_name, is_weekend, is_holiday, fiscal_year, fiscal_quarter)
SELECT 
  get_date_id(d) AS date_id,
  d AS date,
  EXTRACT(DAY FROM d) AS day,
  EXTRACT(MONTH FROM d) AS month,
  EXTRACT(QUARTER FROM d) AS quarter,
  EXTRACT(YEAR FROM d) AS year,
  EXTRACT(DOW FROM d) AS day_of_week,
  TO_CHAR(d, 'Day') AS day_name,
  TO_CHAR(d, 'Month') AS month_name,
  CASE WHEN EXTRACT(DOW FROM d) IN (0, 6) THEN TRUE ELSE FALSE END AS is_weekend,
  CASE WHEN TO_CHAR(d, 'MMDD') IN ('0101', '1225', '0704', '1231') THEN TRUE ELSE FALSE END AS is_holiday,
  CASE WHEN EXTRACT(MONTH FROM d) >= 7 THEN EXTRACT(YEAR FROM d) ELSE EXTRACT(YEAR FROM d) - 1 END AS fiscal_year,
  CASE 
    WHEN EXTRACT(MONTH FROM d) BETWEEN 7 AND 9 THEN 1
    WHEN EXTRACT(MONTH FROM d) BETWEEN 10 AND 12 THEN 2
    WHEN EXTRACT(MONTH FROM d) BETWEEN 1 AND 3 THEN 3
    ELSE 4
  END AS fiscal_quarter
FROM generate_series(
  (CURRENT_DATE - INTERVAL '3 years')::date, 
  (CURRENT_DATE + INTERVAL '2 years')::date, 
  INTERVAL '1 day'
) AS d;

-- Populate regions
INSERT INTO regions (region_name, region_code, continent, country, country_code)
VALUES 
  ('North America - US East', 'NAE', 'North America', 'United States', 'US'),
  ('North America - US West', 'NAW', 'North America', 'United States', 'US'),
  ('North America - US Central', 'NAC', 'North America', 'United States', 'US'),
  ('North America - Canada', 'CAN', 'North America', 'Canada', 'CA'),
  ('Europe - Western', 'EUW', 'Europe', 'Various', 'EU'),
  ('Europe - Eastern', 'EUE', 'Europe', 'Various', 'EU'),
  ('Asia - East', 'ASE', 'Asia', 'Various', 'AS'),
  ('Asia - South', 'ASS', 'Asia', 'Various', 'AS'),
  ('Asia - Central', 'ASC', 'Asia', 'Various', 'AS'),
  ('South America', 'SAM', 'South America', 'Various', 'SA'),
  ('Africa', 'AFR', 'Africa', 'Various', 'AF'),
  ('Oceania', 'OCE', 'Oceania', 'Various', 'OC');

-- Populate categories (hierarchical)
INSERT INTO categories (category_name, category_code, parent_id, description)
VALUES 
  ('Electronics', 'ELEC', NULL, 'Electronic devices and accessories'),
  ('Computers', 'COMP', 1, 'Desktop and laptop computers'),
  ('Laptops', 'LAPT', 2, 'Portable computers'),
  ('Desktops', 'DESK', 2, 'Stationary computers'),
  ('Computer Accessories', 'COMA', 2, 'Accessories for computers'),
  ('Smartphones', 'SMAR', 1, 'Mobile phones'),
  ('Audio', 'AUDI', 1, 'Audio equipment'),
  ('Headphones', 'HEAD', 7, 'Headphones and earbuds'),
  ('Speakers', 'SPEA', 7, 'Speaker systems'),
  ('Clothing', 'CLOT', NULL, 'Apparel and fashion items'),
  ('Men''s', 'MENS', 10, 'Men''s clothing'),
  ('Women''s', 'WMNS', 10, 'Women''s clothing'),
  ('Children''s', 'CHLD', 10, 'Children''s clothing'),
  ('Footwear', 'FOOT', 10, 'Shoes and boots'),
  ('Home & Kitchen', 'HOME', NULL, 'Home goods and appliances'),
  ('Furniture', 'FURN', 15, 'Household furniture'),
  ('Kitchen', 'KTCH', 15, 'Kitchen appliances and tools'),
  ('Bedding', 'BEDD', 15, 'Bedding and linens'),
  ('Food & Grocery', 'FOOD', NULL, 'Food and grocery items'),
  ('Fresh Food', 'FRES', 19, 'Perishable food items'),
  ('Packaged Food', 'PACK', 19, 'Non-perishable food items'),
  ('Beverages', 'BEVR', 19, 'Drinks and liquid refreshments'),
  ('Beauty & Health', 'BEAU', NULL, 'Beauty and health products'),
  ('Skincare', 'SKIN', 23, 'Skin treatment products'),
  ('Makeup', 'MAKE', 23, 'Cosmetic products'),
  ('Healthcare', 'HEAL', 23, 'Health and wellness items'),
  ('Sports & Outdoors', 'SPOR', NULL, 'Sports equipment and outdoor gear'),
  ('Exercise', 'EXER', 27, 'Exercise equipment'),
  ('Outdoor Recreation', 'OUTD', 27, 'Outdoor activity gear'),
  ('Books & Media', 'BOOK', NULL, 'Books, movies, and other media'),
  ('Books', 'BOOKS', 30, 'Printed books'),
  ('E-books', 'EBKS', 30, 'Electronic books'),
  ('Movies', 'MOVI', 30, 'Films and documentaries'),
  ('Music', 'MUSC', 30, 'Audio recordings');

-- Populate vendors
INSERT INTO vendors (vendor_name, contact_name, contact_email, contact_phone, region_id, is_active)
SELECT 
  'Vendor ' || i,
  'Contact Person ' || i,
  'contact' || i || '@vendor' || i || '.com',
  '555-' || lpad(i::TEXT, 7, '0'),
  1 + (i % 12), -- Random region ID (1-12)
  random() > 0.05 -- 5% inactive vendors
FROM generate_series(1, 100) i;

-- Populate products
INSERT INTO products (product_name, product_code, category_id, description, base_price, vendor_id, tags, is_active)
SELECT 
  'Product ' || i,
  'PROD-' || lpad(i::TEXT, 6, '0'),
  1 + floor(random() * 34)::INTEGER, -- Random category ID (1-34)
  'Description for product ' || i || '. This is a high-quality product designed for maximum customer satisfaction.',
  (random() * 990 + 10)::NUMERIC(12,2),
  1 + floor(random() * 100)::INTEGER, -- Random vendor ID (1-100)
  ARRAY[
    (ARRAY['premium', 'standard', 'economy', 'budget'])[(floor(random() * 4) + 1)::INTEGER],
    (ARRAY['new', 'bestseller', 'limited', 'clearance'])[(floor(random() * 4) + 1)::INTEGER]
  ],
  random() > 0.1 -- 10% inactive products
FROM generate_series(1, 50000) i;

-- Populate customers
INSERT INTO customers (first_name, last_name, email, phone, address_line1, city, state, postal_code, country, region_id, customer_since, customer_segment)
SELECT 
  'FirstName' || i,
  'LastName' || i,
  'customer' || i || '@example.com',
  '555-' || lpad(i::TEXT, 7, '0'),
  i || ' Main Street',
  (ARRAY['New York', 'Los Angeles', 'Chicago', 'Houston', 'Phoenix', 'London', 'Paris', 'Berlin', 'Tokyo', 'Sydney'])[(i % 10) + 1],
  (ARRAY['NY', 'CA', 'IL', 'TX', 'AZ', 'UK', 'FR', 'DE', 'JP', 'AU'])[(i % 10) + 1],
  (10000 + floor(random() * 89999))::TEXT,
  (ARRAY['United States', 'United States', 'United States', 'United States', 'United States', 'United Kingdom', 'France', 'Germany', 'Japan', 'Australia'])[(i % 10) + 1],
  1 + (i % 12), -- Random region ID (1-12)
  (CURRENT_DATE - ((random() * 365 * 5)::INTEGER || ' days')::INTERVAL)::DATE, -- Customer since date within last 5 years
  (ARRAY['individual', 'business', 'government', 'education'])[(floor(random() * 4) + 1)::INTEGER] -- Random segment
FROM generate_series(1, 100000) i;

-- Update the last_purchase_date for some customers
UPDATE customers
SET last_purchase_date = customer_since + ((random() * (CURRENT_DATE - customer_since))::INTEGER || ' days')::INTERVAL
WHERE random() > 0.1; -- 90% of customers have made a purchase

-- Create a function to generate random orders
CREATE OR REPLACE FUNCTION generate_random_orders(start_date DATE, end_date DATE, num_orders INTEGER) RETURNS VOID AS $$
DECLARE
  current_date DATE;
  random_date DATE;
  random_customer_id INTEGER;
  random_region_id INTEGER;
  random_order_status VARCHAR;
  random_shipping_method VARCHAR;
  random_channel VARCHAR;
  random_priority VARCHAR;
  shipping_cost NUMERIC;
  tax_amount NUMERIC;
  order_id INTEGER;
  date_id INTEGER;
  order_items_count INTEGER;
  random_product_id INTEGER;
  random_vendor_id INTEGER;
  quantity INTEGER;
  unit_price NUMERIC;
  discount_percent NUMERIC;
  tax_rate NUMERIC;
  total_price NUMERIC;
  order_total NUMERIC;
BEGIN
  FOR i IN 1..num_orders LOOP
    -- Generate a random date within the range
    random_date := start_date + (random() * (end_date - start_date))::INTEGER;
    
    -- Get a random customer
    SELECT id INTO random_customer_id FROM customers ORDER BY random() LIMIT 1;
    
    -- Get customer's region
    SELECT region_id INTO random_region_id FROM customers WHERE id = random_customer_id;
    
    -- Generate random order details
    random_order_status := (ARRAY['completed', 'cancelled', 'pending', 'shipped', 'returned'])[(floor(random() * 5) + 1)::INTEGER];
    random_shipping_method := (ARRAY['standard', 'express', 'overnight', 'pickup'])[(floor(random() * 4) + 1)::INTEGER];
    random_channel := (ARRAY['online', 'phone', 'in-store', 'mobile-app', 'partner'])[(floor(random() * 5) + 1)::INTEGER];
    random_priority := (ARRAY['high', 'medium', 'low'])[(floor(random() * 3) + 1)::INTEGER];
    
    shipping_cost := (random() * 50)::NUMERIC(12,2);
    
    -- Calculate date_id
    date_id := get_date_id(random_date);
    
    -- Insert order
    INSERT INTO orders (
      order_date, date_id, customer_id, region_id, order_status, 
      shipping_method, order_channel, order_priority, shipping_cost, 
      tax_amount, total_amount, discount_amount, created_at
    ) VALUES (
      random_date, date_id, random_customer_id, random_region_id, random_order_status,
      random_shipping_method, random_channel, random_priority, shipping_cost,
      0, 0, 0, random_date::TIMESTAMP + (random() * 60 * 60 * 24)::INTEGER * INTERVAL '1 second'
    ) RETURNING id INTO order_id;
    
    -- Generate 1-10 order items for this order
    order_items_count := 1 + floor(random() * 10)::INTEGER;
    order_total := 0;
    
    FOR j IN 1..order_items_count LOOP
      -- Get a random product
      SELECT id, vendor_id, base_price INTO random_product_id, random_vendor_id, unit_price 
      FROM products 
      WHERE is_active = TRUE 
      ORDER BY random() 
      LIMIT 1;
      
      -- Generate random item details
      quantity := 1 + floor(random() * 5)::INTEGER;
      discount_percent := floor(random() * 30)::NUMERIC(5,2); -- 0-30% discount
      tax_rate := 5 + floor(random() * 10)::NUMERIC(5,2); -- 5-15% tax
      
      -- Calculate total price
      total_price := quantity * unit_price * (1 - discount_percent/100) * (1 + tax_rate/100);
      
      -- Add to order total
      order_total := order_total + total_price;
      
      -- Insert order item
      INSERT INTO order_lines (
        order_id, product_id, vendor_id, quantity, unit_price,
        discount_percent, tax_rate, total_price, is_promotion, created_at
      ) VALUES (
        order_id, random_product_id, random_vendor_id, quantity, unit_price,
        discount_percent, tax_rate, total_price, random() > 0.8, -- 20% are promotions
        random_date::TIMESTAMP + (random() * 60 * 60 * 24)::INTEGER * INTERVAL '1 second'
      );
    END LOOP;
    
    -- Update order total and tax
    tax_amount := order_total * 0.15; -- Simplified tax calculation for the example
    
    UPDATE orders 
    SET total_amount = order_total,
        tax_amount = tax_amount,
        discount_amount = order_total * 0.05 -- 5% overall discount
    WHERE id = order_id;
    
    -- Occasionally update customer's last purchase date
    IF random() > 0.5 AND random_order_status = 'completed' THEN
      UPDATE customers 
      SET last_purchase_date = GREATEST(last_purchase_date, random_date)
      WHERE id = random_customer_id AND (last_purchase_date IS NULL OR last_purchase_date < random_date);
    END IF;
  END LOOP;
END;
$$ LANGUAGE plpgsql;

-- Create a function to generate inventory snapshots
CREATE OR REPLACE FUNCTION generate_inventory_snapshots(start_date DATE, end_date DATE) RETURNS VOID AS $$
DECLARE
  current_date DATE;
  date_id INTEGER;
BEGIN
  -- For each month in the range
  current_date := date_trunc('month', start_date)::DATE;
  
  WHILE current_date <= end_date LOOP
    date_id := get_date_id(current_date);
    
    -- Insert inventory snapshot for all active products
    INSERT INTO inventory_snapshots (
      date_id, product_id, vendor_id, region_id,
      quantity_on_hand, quantity_on_order, quantity_committed, quantity_available,
      reorder_point, standard_cost, created_at
    )
    SELECT 
      date_id,
      p.id,
      p.vendor_id,
      v.region_id,
      floor(random() * 1000)::INTEGER, -- quantity_on_hand
      floor(random() * 200)::INTEGER,  -- quantity_on_order
      floor(random() * 300)::INTEGER,  -- quantity_committed
      0, -- Will update below
      floor(random() * 100 + 50)::INTEGER, -- reorder_point
      p.base_price * 0.7, -- standard_cost (70% of base price)
      current_date + INTERVAL '1 day' -- created_at (day after the month starts)
    FROM products p
    JOIN vendors v ON p.vendor_id = v.id
    WHERE p.is_active = TRUE;
    
    -- Update quantity_available
    UPDATE inventory_snapshots
    SET quantity_available = GREATEST(0, quantity_on_hand - quantity_committed)
    WHERE date_id = get_date_id(current_date);
    
    -- Move to next month
    current_date := (current_date + INTERVAL '1 month')::DATE;
  END LOOP;
END;
$$ LANGUAGE plpgsql;

-- Create a function to generate sales targets
CREATE OR REPLACE FUNCTION generate_sales_targets(start_date DATE, end_date DATE) RETURNS VOID AS $$
DECLARE
  current_date DATE;
  date_id INTEGER;
BEGIN
  -- For each month in the range
  current_date := date_trunc('month', start_date)::DATE;
  
  WHILE current_date <= end_date LOOP
    date_id := get_date_id(current_date);
    
    -- Product-level targets (only for a random subset of products)
    INSERT INTO sales_targets (
      date_id, product_id, category_id, region_id,
      target_amount, target_units, target_type
    )
    SELECT 
      date_id,
      id,
      NULL, -- category_id
      NULL, -- region_id
      (random() * 100000 + 10000)::NUMERIC(12,2), -- target_amount
      floor(random() * 1000 + 100)::INTEGER, -- target_units
      'monthly'
    FROM products
    WHERE is_active = TRUE AND random() < 0.1; -- Only for 10% of products
    
    -- Category-level targets
    INSERT INTO sales_targets (
      date_id, product_id, category_id, region_id,
      target_amount, target_units, target_type
    )
    SELECT 
      date_id,
      NULL, -- product_id
      id,
      NULL, -- region_id
      (random() * 1000000 + 100000)::NUMERIC(12,2), -- target_amount
      floor(random() * 10000 + 1000)::INTEGER, -- target_units
      'monthly'
    FROM categories;
    
    -- Region-level targets
    INSERT INTO sales_targets (
      date_id, product_id, category_id, region_id,
      target_amount, target_units, target_type
    )
    SELECT 
      date_id,
      NULL, -- product_id
      NULL, -- category_id
      id,
      (random() * 5000000 + 500000)::NUMERIC(12,2), -- target_amount
      floor(random() * 50000 + 5000)::INTEGER, -- target_units
      'monthly'
    FROM regions;
    
    -- Move to next month
    current_date := (current_date + INTERVAL '1 month')::DATE;
  END LOOP;
  
  -- Add quarterly targets
  INSERT INTO sales_targets (
    date_id, product_id, category_id, region_id,
    target_amount, target_units, target_type
  )
  SELECT 
    get_date_id(date_trunc('quarter', t.date)::DATE),
    NULL, -- product_id
    NULL, -- category_id
    r.id, -- region_id
    sum(t.target_amount) * 1.1, -- 10% higher than sum of monthly targets
    sum(t.target_units) * 1.1, -- 10% higher than sum of monthly targets
    'quarterly'
  FROM sales_targets t
  CROSS JOIN regions r
  WHERE t.region_id = r.id AND t.target_type = 'monthly'
  GROUP BY r.id, date_trunc('quarter', t.date);
  
  -- Add annual targets
  INSERT INTO sales_targets (
    date_id, product_id, category_id, region_id,
    target_amount, target_units, target_type
  )
  SELECT 
    get_date_id(date_trunc('year', t.date)::DATE),
    NULL, -- product_id
    NULL, -- category_id
    r.id, -- region_id
    sum(t.target_amount) * 1.2, -- 20% higher than sum of quarterly targets
    sum(t.target_units) * 1.2, -- 20% higher than sum of quarterly targets
    'annual'
  FROM sales_targets t
  CROSS JOIN regions r
  WHERE t.region_id = r.id AND t.target_type = 'quarterly'
  GROUP BY r.id, date_trunc('year', t.date);
END;
$$ LANGUAGE plpgsql;

-- Generate data for the past 2 years
DO $$
BEGIN
  -- Generate 5 million orders
  PERFORM generate_random_orders(
    CURRENT_DATE - INTERVAL '2 years',
    CURRENT_DATE,
    5000000
  );
  
  -- Generate inventory snapshots
  PERFORM generate_inventory_snapshots(
    CURRENT_DATE - INTERVAL '2 years',
    CURRENT_DATE
  );
  
  -- Generate sales targets
  PERFORM generate_sales_targets(
    CURRENT_DATE - INTERVAL '2 years',
    CURRENT_DATE + INTERVAL '1 year' -- Includes future targets
  );
END $$;

-- Create views to make reporting easier
CREATE VIEW order_summary AS
SELECT 
  t.year,
  t.quarter,
  t.month,
  t.month_name,
  r.region_name,
  c.category_name,
  COUNT(DISTINCT o.id) AS order_count,
  COUNT(DISTINCT o.customer_id) AS customer_count,
  SUM(o.total_amount) AS total_sales,
  SUM(ol.quantity) AS total_units
FROM orders o
JOIN order_lines ol ON o.id = ol.order_id
JOIN time_dim t ON o.date_id = t.date_id
JOIN regions r ON o.region_id = r.id
JOIN products p ON ol.product_id = p.id
JOIN categories c ON p.category_id = c.id
WHERE o.order_status = 'completed'
GROUP BY t.year, t.quarter, t.month, t.month_name, r.region_name, c.category_name;

-- Create a problematic view that will be slow
CREATE VIEW sales_vs_targets AS
SELECT 
  t.year,
  t.quarter,
  t.month,
  r.region_name,
  c.category_name,
  SUM(ol.total_price) AS actual_sales,
  SUM(ol.quantity) AS actual_units,
  SUM(CASE WHEN st.target_type = 'monthly' THEN st.target_amount ELSE 0 END) AS monthly_target,
  SUM(CASE WHEN st.target_type = 'monthly' THEN st.target_units ELSE 0 END) AS monthly_target_units,
  SUM(ol.total_price) / NULLIF(SUM(CASE WHEN st.target_type = 'monthly' THEN st.target_amount ELSE 0 END), 0) * 100 AS percent_of_target
FROM orders o
JOIN order_lines ol ON o.id = ol.order_id
JOIN time_dim t ON o.date_id = t.date_id
JOIN regions r ON o.region_id = r.id
JOIN products p ON ol.product_id = p.id
JOIN categories c ON p.category_id = c.id
LEFT JOIN sales_targets st ON 
  t.date_id = st.date_id AND 
  ((st.product_id IS NULL AND st.category_id = p.category_id) OR (st.product_id = p.id AND st.category_id IS NULL)) AND 
  (st.region_id = o.region_id OR st.region_id IS NULL)
WHERE o.order_status = 'completed'
GROUP BY t.year, t.quarter, t.month, r.region_name, c.category_name;

-- Update stats
ANALYZE;

-- Create the slow problematic query that will be optimized in the demo
-- This is the query referenced in Demo 2
-- It's intentionally slow due to lack of proper indexes and inefficient joins
CREATE OR REPLACE FUNCTION get_sales_report(start_date DATE, end_date DATE) RETURNS TABLE(
  month DATE,
  category_name VARCHAR,
  region_name VARCHAR,
  units_sold BIGINT,
  revenue NUMERIC,
  unique_customers BIGINT
) AS $$
BEGIN
  RETURN QUERY
  SELECT 
    date_trunc('month', o.order_date)::DATE as month,
    c.category_name,
    r.region_name,
    SUM(ol.quantity) as units_sold,
    SUM(ol.total_price) as revenue,
    COUNT(DISTINCT o.customer_id) as unique_customers
  FROM orders o
  JOIN order_lines ol ON o.id = ol.order_id
  JOIN products p ON ol.product_id = p.id
  JOIN categories c ON p.category_id = c.id
  JOIN customers cust ON o.customer_id = cust.id
  JOIN regions r ON cust.region_id = r.id
  WHERE o.order_date >= start_date
    AND o.order_date <= end_date
    AND o.order_status = 'completed'
  GROUP BY 1, 2, 3
  ORDER BY 1, 3, 2;
END;
$$ LANGUAGE plpgsql;

-- Print summary info
SELECT 'Data warehouse setup completed. Database contains:' AS message;
SELECT 
  (SELECT COUNT(*) FROM time_dim) AS time_dimensions,
  (SELECT COUNT(*) FROM regions) AS regions_count,
  (SELECT COUNT(*) FROM categories) AS categories_count,
  (SELECT COUNT(*) FROM vendors) AS vendors_count,
  (SELECT COUNT(*) FROM products) AS products_count,
  (SELECT COUNT(*) FROM customers) AS customers_count,
  (SELECT COUNT(*) FROM orders) AS orders_count,
  (SELECT COUNT(*) FROM order_lines) AS order_lines_count,
  (SELECT COUNT(*) FROM inventory_snapshots) AS inventory_snapshots_count,
  (SELECT COUNT(*) FROM sales_targets) AS sales_targets_count; 
