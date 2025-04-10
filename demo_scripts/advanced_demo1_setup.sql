-- Advanced Demo 1: E-commerce Performance Optimization Setup
-- This script sets up a simulated e-commerce database with performance issues

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS hypopg;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Create tables for e-commerce schema
CREATE TABLE categories (
  id SERIAL PRIMARY KEY,
  category_name VARCHAR(100) NOT NULL,
  description TEXT,
  parent_id INTEGER REFERENCES categories(id),
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  name VARCHAR(200) NOT NULL,
  description TEXT,
  price NUMERIC(10,2) NOT NULL,
  cost NUMERIC(10,2),
  sku VARCHAR(50) UNIQUE,
  is_available BOOLEAN DEFAULT TRUE,
  category_id INTEGER REFERENCES categories(id),
  popularity_score INTEGER DEFAULT 0,
  inventory_count INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE customers (
  id SERIAL PRIMARY KEY,
  email VARCHAR(150) UNIQUE NOT NULL,
  password_hash VARCHAR(100) NOT NULL,
  first_name VARCHAR(50),
  last_name VARCHAR(50),
  phone VARCHAR(20),
  created_at TIMESTAMP DEFAULT NOW(),
  last_login TIMESTAMP,
  is_active BOOLEAN DEFAULT TRUE
);

CREATE TABLE customer_addresses (
  id SERIAL PRIMARY KEY,
  customer_id INTEGER REFERENCES customers(id),
  address_line1 VARCHAR(100) NOT NULL,
  address_line2 VARCHAR(100),
  city VARCHAR(50) NOT NULL,
  state VARCHAR(50),
  country VARCHAR(50) NOT NULL,
  postal_code VARCHAR(20) NOT NULL,
  is_default BOOLEAN DEFAULT FALSE,
  address_type VARCHAR(20) NOT NULL -- 'billing' or 'shipping'
);

CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  customer_id INTEGER REFERENCES customers(id),
  order_date TIMESTAMP DEFAULT NOW(),
  status VARCHAR(20) NOT NULL, -- 'pending', 'processing', 'shipped', 'delivered', 'cancelled'
  shipping_address_id INTEGER REFERENCES customer_addresses(id),
  billing_address_id INTEGER REFERENCES customer_addresses(id),
  shipping_fee NUMERIC(10,2) DEFAULT 0,
  total_amount NUMERIC(12,2) NOT NULL,
  payment_method VARCHAR(50), -- 'credit_card', 'paypal', etc.
  notes TEXT,
  updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE order_lines (
  id SERIAL PRIMARY KEY,
  order_id INTEGER REFERENCES orders(id),
  product_id INTEGER REFERENCES products(id),
  quantity INTEGER NOT NULL,
  unit_price NUMERIC(10,2) NOT NULL,
  discount_amount NUMERIC(10,2) DEFAULT 0
);

CREATE TABLE product_views (
  id SERIAL PRIMARY KEY,
  product_id INTEGER REFERENCES products(id),
  customer_id INTEGER REFERENCES customers(id),
  viewed_at TIMESTAMP DEFAULT NOW(),
  source VARCHAR(50) -- 'search', 'category_page', 'recommendation', etc.
);

CREATE TABLE reviews (
  id SERIAL PRIMARY KEY,
  product_id INTEGER REFERENCES products(id),
  customer_id INTEGER REFERENCES customers(id),
  rating INTEGER NOT NULL, -- 1-5
  title VARCHAR(100),
  comment TEXT,
  created_at TIMESTAMP DEFAULT NOW(),
  is_verified_purchase BOOLEAN DEFAULT FALSE
);

CREATE TABLE shopping_carts (
  id SERIAL PRIMARY KEY,
  customer_id INTEGER REFERENCES customers(id),
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW(),
  is_active BOOLEAN DEFAULT TRUE
);

CREATE TABLE cart_items (
  id SERIAL PRIMARY KEY,
  cart_id INTEGER REFERENCES shopping_carts(id),
  product_id INTEGER REFERENCES products(id),
  quantity INTEGER NOT NULL,
  added_at TIMESTAMP DEFAULT NOW()
);

-- Insert sample data

-- Categories (create a hierarchy)
INSERT INTO categories (category_name, description, parent_id)
VALUES 
  ('Electronics', 'Electronic devices and accessories', NULL),
  ('Computers', 'Desktops, laptops, and computer equipment', 1),
  ('Smartphones', 'Mobile phones and accessories', 1),
  ('Audio', 'Headphones, speakers, and audio equipment', 1),
  ('Clothing', 'Apparel and fashion items', NULL),
  ('Men''s', 'Men''s clothing and accessories', 5),
  ('Women''s', 'Women''s clothing and accessories', 5),
  ('Children''s', 'Children''s clothing and accessories', 5),
  ('Home & Kitchen', 'Home goods and kitchen equipment', NULL),
  ('Furniture', 'Tables, chairs, and other furniture', 9),
  ('Kitchenware', 'Kitchen utensils and appliances', 9),
  ('Books', 'Books and literature', NULL),
  ('Fiction', 'Novels and fiction literature', 12),
  ('Non-fiction', 'Educational and non-fiction books', 12);

-- Products (100,000 products)
-- First create a function to generate random text
CREATE OR REPLACE FUNCTION random_text(min_length INTEGER, max_length INTEGER) RETURNS TEXT AS $$
DECLARE
  result TEXT := '';
  possible_chars TEXT := 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ';
  length INTEGER;
BEGIN
  length := min_length + floor(random() * (max_length - min_length + 1))::INTEGER;
  FOR i IN 1..length LOOP
    result := result || substr(possible_chars, 1 + floor(random() * length(possible_chars))::INTEGER, 1);
  END LOOP;
  RETURN result;
END;
$$ LANGUAGE plpgsql;

-- Insert products
INSERT INTO products (name, description, price, cost, sku, is_available, category_id, popularity_score, inventory_count)
SELECT 
  'Product ' || i,
  'Description for product ' || i || ': ' || random_text(50, 300),
  (random() * 990 + 10)::NUMERIC(10,2),
  (random() * 400 + 5)::NUMERIC(10,2),
  'SKU-' || to_char(i, 'FM000000'),
  random() > 0.1, -- 90% are available
  1 + floor(random() * 14)::INTEGER, -- Random category id between 1-14
  floor(random() * 100)::INTEGER, -- Popularity between 0-99
  floor(random() * 1000)::INTEGER -- Inventory between 0-999
FROM generate_series(1, 100000) i;

-- Customers (50,000 customers)
INSERT INTO customers (email, password_hash, first_name, last_name, phone, created_at, last_login, is_active)
SELECT 
  'customer' || i || '@example.com',
  'hashed_password_' || md5(i::TEXT),
  'FirstName' || i,
  'LastName' || i,
  '555-' || lpad(i::TEXT, 7, '0'),
  NOW() - (random() * 365 * 3)::INTEGER * INTERVAL '1 day', -- Registration date in last 3 years
  NOW() - (random() * 30)::INTEGER * INTERVAL '1 day', -- Last login in last 30 days
  random() > 0.05 -- 95% are active
FROM generate_series(1, 50000) i;

-- Customer addresses (80,000 addresses, some customers have multiple)
INSERT INTO customer_addresses (customer_id, address_line1, address_line2, city, state, country, postal_code, is_default, address_type)
SELECT 
  1 + floor(random() * 50000)::INTEGER,
  random_text(10, 30) || ' ' || random_text(5, 15) || ' St',
  CASE WHEN random() > 0.7 THEN 'Apt ' || floor(random() * 999 + 1)::TEXT ELSE NULL END,
  (ARRAY['New York', 'Los Angeles', 'Chicago', 'Houston', 'Phoenix', 'Philadelphia', 'San Antonio', 'San Diego', 'Dallas', 'San Jose'])[(i % 10) + 1],
  (ARRAY['NY', 'CA', 'IL', 'TX', 'AZ', 'PA', 'TX', 'CA', 'TX', 'CA'])[(i % 10) + 1],
  'USA',
  (10000 + floor(random() * 89999))::TEXT,
  i % 2 = 0, -- 50% are default addresses
  CASE WHEN i % 3 = 0 THEN 'billing' ELSE 'shipping' END
FROM generate_series(1, 80000) i;

-- Orders (500,000 orders)
INSERT INTO orders (customer_id, order_date, status, shipping_address_id, billing_address_id, shipping_fee, total_amount, payment_method, updated_at)
SELECT 
  1 + floor(random() * 50000)::INTEGER,
  NOW() - (random() * 365 * 2)::INTEGER * INTERVAL '1 day', -- Order date in last 2 years
  (ARRAY['pending', 'processing', 'shipped', 'delivered', 'cancelled'])[(1 + floor(random() * 5))::INTEGER],
  1 + floor(random() * 80000)::INTEGER,
  1 + floor(random() * 80000)::INTEGER,
  (random() * 20)::NUMERIC(10,2),
  (random() * 500 + 10)::NUMERIC(12,2),
  (ARRAY['credit_card', 'paypal', 'bank_transfer', 'apple_pay', 'google_pay'])[(1 + floor(random() * 5))::INTEGER],
  NOW() - (random() * 365)::INTEGER * INTERVAL '1 day'
FROM generate_series(1, 500000) i;

-- Order lines (2,000,000 order lines)
INSERT INTO order_lines (order_id, product_id, quantity, unit_price, discount_amount)
SELECT 
  1 + floor(random() * 500000)::INTEGER,
  1 + floor(random() * 100000)::INTEGER,
  1 + floor(random() * 5)::INTEGER,
  (random() * 990 + 10)::NUMERIC(10,2),
  CASE WHEN random() > 0.7 THEN (random() * 100)::NUMERIC(10,2) ELSE 0 END
FROM generate_series(1, 2000000) i;

-- Product views (5,000,000 views)
INSERT INTO product_views (product_id, customer_id, viewed_at, source)
SELECT 
  1 + floor(random() * 100000)::INTEGER,
  1 + floor(random() * 50000)::INTEGER,
  NOW() - (random() * 30)::INTEGER * INTERVAL '1 day' - (random() * 24)::INTEGER * INTERVAL '1 hour',
  (ARRAY['search', 'category_page', 'recommendation', 'social_media', 'direct'])[(1 + floor(random() * 5))::INTEGER]
FROM generate_series(1, 5000000) i;

-- Reviews (200,000 reviews)
INSERT INTO reviews (product_id, customer_id, rating, title, comment, created_at, is_verified_purchase)
SELECT 
  1 + floor(random() * 100000)::INTEGER,
  1 + floor(random() * 50000)::INTEGER,
  1 + floor(random() * 5)::INTEGER,
  'Review title ' || i,
  'Comment about the product: ' || random_text(20, 200),
  NOW() - (random() * 365)::INTEGER * INTERVAL '1 day',
  random() > 0.3
FROM generate_series(1, 200000) i;

-- Shopping carts (10,000 active carts)
INSERT INTO shopping_carts (customer_id, created_at, updated_at, is_active)
SELECT 
  1 + floor(random() * 50000)::INTEGER,
  NOW() - (random() * 7)::INTEGER * INTERVAL '1 day',
  NOW() - (random() * 1)::INTEGER * INTERVAL '1 day',
  TRUE
FROM generate_series(1, 10000) i;

-- Cart items (30,000 items in carts)
INSERT INTO cart_items (cart_id, product_id, quantity, added_at)
SELECT 
  1 + floor(random() * 10000)::INTEGER,
  1 + floor(random() * 100000)::INTEGER,
  1 + floor(random() * 3)::INTEGER,
  NOW() - (random() * 3)::INTEGER * INTERVAL '1 day'
FROM generate_series(1, 30000) i;

-- Create some inefficient indexes to demonstrate optimization
CREATE INDEX idx_products_name ON products(name);
CREATE INDEX idx_products_price ON products(price);
CREATE INDEX idx_products_category_id ON products(category_id);
CREATE INDEX idx_order_update_time ON orders(updated_at);
CREATE INDEX idx_order_customer ON orders(customer_id);

-- Create some bloated tables by repeated updates
DO $$
BEGIN
  FOR i IN 1..50 LOOP
    -- Update some product popularity scores
    UPDATE products 
    SET popularity_score = popularity_score + floor(random() * 3)::INTEGER,
        updated_at = NOW()
    WHERE id % 100 = i % 100;
    
    -- Update some order status
    UPDATE orders
    SET status = (ARRAY['pending', 'processing', 'shipped', 'delivered'])[(1 + floor(random() * 4))::INTEGER],
        updated_at = NOW()
    WHERE id % 100 = i % 100;
  END LOOP;
END $$;

-- Create some stored procedures and functions that will be used in the demo
CREATE OR REPLACE FUNCTION get_product_details(p_product_id INTEGER)
RETURNS TABLE (
  product_id INTEGER,
  product_name VARCHAR,
  price NUMERIC,
  category_name VARCHAR,
  inventory INTEGER,
  review_count BIGINT,
  avg_rating NUMERIC
) AS $$
BEGIN
  RETURN QUERY
  SELECT 
    p.id,
    p.name,
    p.price,
    c.category_name,
    p.inventory_count,
    COUNT(r.id),
    COALESCE(AVG(r.rating), 0)
  FROM products p
  LEFT JOIN categories c ON p.category_id = c.id
  LEFT JOIN reviews r ON p.id = r.product_id
  WHERE p.id = p_product_id
  GROUP BY p.id, p.name, p.price, c.category_name, p.inventory_count;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION search_products(search_term TEXT, sort_field TEXT DEFAULT 'popularity', limit_number INTEGER DEFAULT 20)
RETURNS TABLE (
  product_id INTEGER,
  product_name VARCHAR,
  price NUMERIC,
  category_name VARCHAR,
  popularity INTEGER,
  is_available BOOLEAN
) AS $$
BEGIN
  IF sort_field = 'popularity' THEN
    RETURN QUERY
    SELECT 
      p.id,
      p.name,
      p.price,
      c.category_name,
      p.popularity_score,
      p.is_available
    FROM products p
    JOIN categories c ON p.category_id = c.id
    WHERE p.is_available = true 
      AND (p.name ILIKE '%' || search_term || '%' OR p.description ILIKE '%' || search_term || '%')
    ORDER BY p.popularity_score DESC
    LIMIT limit_number;
  ELSIF sort_field = 'price_asc' THEN
    RETURN QUERY
    SELECT 
      p.id,
      p.name,
      p.price,
      c.category_name,
      p.popularity_score,
      p.is_available
    FROM products p
    JOIN categories c ON p.category_id = c.id
    WHERE p.is_available = true 
      AND (p.name ILIKE '%' || search_term || '%' OR p.description ILIKE '%' || search_term || '%')
    ORDER BY p.price ASC
    LIMIT limit_number;
  ELSIF sort_field = 'price_desc' THEN
    RETURN QUERY
    SELECT 
      p.id,
      p.name,
      p.price,
      c.category_name,
      p.popularity_score,
      p.is_available
    FROM products p
    JOIN categories c ON p.category_id = c.id
    WHERE p.is_available = true 
      AND (p.name ILIKE '%' || search_term || '%' OR p.description ILIKE '%' || search_term || '%')
    ORDER BY p.price DESC
    LIMIT limit_number;
  ELSE
    RETURN QUERY
    SELECT 
      p.id,
      p.name,
      p.price,
      c.category_name,
      p.popularity_score,
      p.is_available
    FROM products p
    JOIN categories c ON p.category_id = c.id
    WHERE p.is_available = true 
      AND (p.name ILIKE '%' || search_term || '%' OR p.description ILIKE '%' || search_term || '%')
    ORDER BY p.name
    LIMIT limit_number;
  END IF;
END;
$$ LANGUAGE plpgsql;

-- Update statistics 
ANALYZE;

-- Done
SELECT 'E-commerce database setup completed. Database contains:' AS message;
SELECT 
  (SELECT COUNT(*) FROM categories) AS categories_count,
  (SELECT COUNT(*) FROM products) AS products_count,
  (SELECT COUNT(*) FROM customers) AS customers_count,
  (SELECT COUNT(*) FROM orders) AS orders_count,
  (SELECT COUNT(*) FROM order_lines) AS order_lines_count,
  (SELECT COUNT(*) FROM product_views) AS product_views_count; 
