-- Advanced Demo 3: Database Migration Validation Setup
-- This script sets up a simulated database that looks like it was migrated from MySQL to PostgreSQL
-- It intentionally contains common migration issues

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS hypopg;

-- Create schema structure with issues typical to MySQL -> PostgreSQL migrations

-- Issue 1: Missing primary keys on some tables
CREATE TABLE blog_posts (
  post_id INTEGER, -- Missing PRIMARY KEY constraint
  title VARCHAR(200) NOT NULL,
  content TEXT,
  author_id INTEGER,
  status VARCHAR(20) DEFAULT 'draft',
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW(),
  published_at TIMESTAMP,
  view_count INTEGER DEFAULT 0
);

-- Issue 2: Using 'id' vs 'table_name_id' convention inconsistently (from different database conventions)
CREATE TABLE users (
  id SERIAL PRIMARY KEY, -- Using "id" here
  username VARCHAR(50) NOT NULL,
  email VARCHAR(100) NOT NULL,
  password_hash VARCHAR(100) NOT NULL,
  first_name VARCHAR(50),
  last_name VARCHAR(50),
  user_status VARCHAR(20) DEFAULT 'active',
  created_at TIMESTAMP DEFAULT NOW(),
  last_login TIMESTAMP
);

-- Issue 3: Table using MyISAM style naming conventions and no constraints
CREATE TABLE tbl_categories (
  categoryID INTEGER PRIMARY KEY, -- Non-standard naming from MySQL
  name VARCHAR(50) NOT NULL,
  parent_id INTEGER, -- No foreign key constraint
  level INTEGER,
  description TEXT
);

-- Issue 4: Foreign keys without indexes (common after migrations)
CREATE TABLE comments (
  comment_id SERIAL PRIMARY KEY,
  post_id INTEGER, -- Foreign key without index
  user_id INTEGER, -- Foreign key without index
  content TEXT,
  status VARCHAR(20) DEFAULT 'pending',
  created_at TIMESTAMP DEFAULT NOW()
);

-- Issue 5: Using ENUM type from MySQL (simulated with check constraint, but not exactly the same)
CREATE TABLE orders (
  order_id SERIAL PRIMARY KEY,
  user_id INTEGER,
  order_status VARCHAR(20) CHECK (order_status IN ('new', 'processing', 'shipped', 'delivered', 'cancelled')),
  total_amount NUMERIC(10,2) NOT NULL,
  shipping_address TEXT,
  billing_address TEXT,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

-- Issue 6: Different date handling (MySQL allows zero dates, PostgreSQL doesn't)
CREATE TABLE events (
  event_id SERIAL PRIMARY KEY,
  title VARCHAR(100) NOT NULL,
  description TEXT,
  start_date TIMESTAMP NOT NULL,
  end_date TIMESTAMP NOT NULL, -- In MySQL, this might have allowed '0000-00-00'
  location VARCHAR(200),
  organizer_id INTEGER,
  max_attendees INTEGER,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Issue 7: Text length limitations are different (simulated by creating too many char columns)
CREATE TABLE product_details (
  product_id INTEGER PRIMARY KEY,
  sku VARCHAR(50),
  name VARCHAR(200) NOT NULL,
  description_short VARCHAR(500),
  description_long TEXT,
  specification TEXT,
  features TEXT,
  meta_title VARCHAR(255), -- Many text fields that might be improperly sized
  meta_description VARCHAR(255),
  meta_keywords VARCHAR(255),
  custom_field1 VARCHAR(255),
  custom_field2 VARCHAR(255),
  custom_field3 VARCHAR(255),
  custom_field4 VARCHAR(255),
  custom_field5 VARCHAR(255),
  custom_field6 VARCHAR(255),
  custom_field7 VARCHAR(255),
  custom_field8 VARCHAR(255),
  custom_field9 VARCHAR(255),
  custom_field10 VARCHAR(255)
);

-- Issue 8: Auto-increment issues (MySQL will restart at 1, PostgreSQL continues sequence)
CREATE SEQUENCE user_addresses_seq START 1;
CREATE TABLE user_addresses (
  address_id INTEGER PRIMARY KEY DEFAULT nextval('user_addresses_seq'),
  user_id INTEGER,
  address_line1 VARCHAR(100) NOT NULL,
  address_line2 VARCHAR(100),
  city VARCHAR(50) NOT NULL,
  state VARCHAR(50),
  postal_code VARCHAR(20) NOT NULL,
  country VARCHAR(50) NOT NULL,
  is_default BOOLEAN DEFAULT FALSE,
  address_type VARCHAR(20) NOT NULL -- 'billing' or 'shipping'
);

-- Issue 9: INTEGER vs BIGINT for IDs (in MySQL, often INT is used for IDs)
CREATE TABLE products (
  product_id INTEGER PRIMARY KEY, -- Should be BIGINT for large tables
  category_id INTEGER,
  name VARCHAR(200) NOT NULL,
  price NUMERIC(10,2) NOT NULL,
  cost NUMERIC(10,2),
  stock INTEGER NOT NULL DEFAULT 0,
  sku VARCHAR(50) UNIQUE,
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

-- Issue 10: ON DELETE CASCADE missing (common in migrations)
CREATE TABLE order_items (
  item_id SERIAL PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(order_id), -- Missing ON DELETE CASCADE
  product_id INTEGER NOT NULL REFERENCES products(product_id), -- Missing ON DELETE CASCADE
  quantity INTEGER NOT NULL,
  unit_price NUMERIC(10,2) NOT NULL,
  total_price NUMERIC(10,2) NOT NULL,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Issue 11: Missing indexes on timestamp columns (common query pattern in web apps)
CREATE TABLE user_activity_logs (
  log_id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL,
  activity_type VARCHAR(50) NOT NULL,
  ip_address VARCHAR(45),
  user_agent TEXT,
  activity_data JSONB,
  created_at TIMESTAMP DEFAULT NOW() -- Missing index on this frequently queried column
);

-- Issue 12: Issues with boolean type conversion (MySQL uses TINYINT, PostgreSQL uses BOOLEAN)
CREATE TABLE newsletter_subscriptions (
  subscription_id SERIAL PRIMARY KEY,
  email VARCHAR(100) NOT NULL,
  is_active INTEGER DEFAULT 1, -- Should be BOOLEAN but kept as INTEGER from MySQL
  subscription_date TIMESTAMP DEFAULT NOW(),
  unsubscribe_date TIMESTAMP,
  source VARCHAR(50)
);

-- Issue 13: Missing constraints that were enforced by application logic
CREATE TABLE coupons (
  coupon_id SERIAL PRIMARY KEY,
  code VARCHAR(50) NOT NULL, -- Missing UNIQUE constraint
  discount_amount NUMERIC(10,2),
  discount_percent NUMERIC(5,2),
  valid_from TIMESTAMP NOT NULL,
  valid_to TIMESTAMP NOT NULL,
  usage_limit INTEGER,
  usage_count INTEGER DEFAULT 0
);

-- Issue 14: Using TEXT where VARCHAR would be more appropriate (common in migrations)
CREATE TABLE shipping_methods (
  method_id SERIAL PRIMARY KEY,
  name TEXT NOT NULL, -- Should be VARCHAR with appropriate length
  description TEXT,
  cost NUMERIC(10,2) NOT NULL,
  is_active BOOLEAN DEFAULT TRUE
);

-- Issue 15: Duplicate indexes or missing indexes on foreign keys
CREATE TABLE wishlist_items (
  wishlist_item_id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL,
  product_id INTEGER NOT NULL,
  added_at TIMESTAMP DEFAULT NOW(),
  note TEXT
);

-- Now create constraints to link tables (but with intentional issues)

-- Add foreign key constraints
ALTER TABLE blog_posts ADD CONSTRAINT fk_blog_posts_author 
  FOREIGN KEY (author_id) REFERENCES users(id);

ALTER TABLE comments ADD CONSTRAINT fk_comments_post 
  FOREIGN KEY (post_id) REFERENCES blog_posts(post_id);
  
ALTER TABLE comments ADD CONSTRAINT fk_comments_user 
  FOREIGN KEY (user_id) REFERENCES users(id);

ALTER TABLE orders ADD CONSTRAINT fk_orders_user 
  FOREIGN KEY (user_id) REFERENCES users(id);

ALTER TABLE events ADD CONSTRAINT fk_events_organizer 
  FOREIGN KEY (organizer_id) REFERENCES users(id);

ALTER TABLE user_addresses ADD CONSTRAINT fk_user_addresses_user 
  FOREIGN KEY (user_id) REFERENCES users(id);

ALTER TABLE products ADD CONSTRAINT fk_products_category 
  FOREIGN KEY (category_id) REFERENCES tbl_categories(categoryID);

ALTER TABLE user_activity_logs ADD CONSTRAINT fk_user_activity_logs_user 
  FOREIGN KEY (user_id) REFERENCES users(id);

ALTER TABLE wishlist_items ADD CONSTRAINT fk_wishlist_items_user 
  FOREIGN KEY (user_id) REFERENCES users(id);
  
ALTER TABLE wishlist_items ADD CONSTRAINT fk_wishlist_items_product 
  FOREIGN KEY (product_id) REFERENCES products(product_id);

-- Insert sample data with issues

-- Insert users
INSERT INTO users (username, email, password_hash, first_name, last_name, user_status, last_login)
SELECT 
  'user' || i,
  'user' || i || '@example.com',
  md5('password' || i),
  'FirstName' || i,
  'LastName' || i,
  CASE WHEN i % 20 = 0 THEN 'inactive' ELSE 'active' END,
  NOW() - (random() * 30)::INTEGER * INTERVAL '1 day'
FROM generate_series(1, 1000) i;

-- Insert categories
INSERT INTO tbl_categories (categoryID, name, parent_id, level, description)
VALUES 
  (1, 'Electronics', NULL, 1, 'Electronic devices and gadgets'),
  (2, 'Computers', 1, 2, 'Laptops, desktops, and components'),
  (3, 'Phones', 1, 2, 'Mobile phones and accessories'),
  (4, 'Clothing', NULL, 1, 'Apparel and fashion items'),
  (5, 'Men''s', 4, 2, 'Men''s clothing'),
  (6, 'Women''s', 4, 2, 'Women''s clothing'),
  (7, 'Home & Kitchen', NULL, 1, 'Home goods and appliances'),
  (8, 'Furniture', 7, 2, 'Household furniture'),
  (9, 'Appliances', 7, 2, 'Kitchen and household appliances'),
  (10, 'Books', NULL, 1, 'Books and publications');

-- Insert products
INSERT INTO products (product_id, category_id, name, price, cost, stock, sku, is_active)
SELECT 
  i,
  (i % 10) + 1, -- Cycle through categories
  'Product ' || i,
  (random() * 990 + 10)::NUMERIC(10,2),
  (random() * 500 + 5)::NUMERIC(10,2),
  floor(random() * 100)::INTEGER,
  'SKU-' || to_char(i, 'FM000000'),
  i % 10 != 0 -- 90% are active
FROM generate_series(1, 5000) i;

-- Insert product details
INSERT INTO product_details (product_id, sku, name, description_short, description_long)
SELECT 
  i,
  'SKU-' || to_char(i, 'FM000000'),
  'Product ' || i,
  'Short description for product ' || i,
  'Detailed description for product ' || i || '. This is a high-quality product with many features and benefits. Customers love this product for its durability and design.'
FROM generate_series(1, 5000) i;

-- Insert blog posts
INSERT INTO blog_posts (post_id, title, content, author_id, status, published_at, view_count)
SELECT 
  i,
  'Blog Post Title ' || i,
  'Content for blog post ' || i || '. This is a sample blog post with enough content to be meaningful in a demonstration.',
  (random() * 999 + 1)::INTEGER,
  CASE 
    WHEN i % 10 = 0 THEN 'draft'
    WHEN i % 20 = 0 THEN 'deleted'
    ELSE 'published'
  END,
  CASE 
    WHEN i % 10 = 0 THEN NULL 
    ELSE NOW() - (random() * 365)::INTEGER * INTERVAL '1 day'
  END,
  floor(random() * 10000)::INTEGER
FROM generate_series(1, 2000) i;

-- Issue 16: NULL row in a join table (bug after migration)
INSERT INTO blog_posts (post_id, title, content, author_id, status, published_at, view_count)
VALUES (2001, 'Post with NULL author', 'This post has no author, which should be caught by foreign key constraint but somehow it got inserted.', NULL, 'published', NOW(), 0);

-- Insert comments
INSERT INTO comments (post_id, user_id, content, status)
SELECT 
  (random() * 1999 + 1)::INTEGER, -- post_id between 1-2000
  (random() * 999 + 1)::INTEGER, -- user_id between 1-1000
  'This is comment ' || i || ' on the blog post. It contains user feedback or questions.',
  CASE 
    WHEN i % 5 = 0 THEN 'pending'
    WHEN i % 20 = 0 THEN 'rejected'
    ELSE 'approved'
  END
FROM generate_series(1, 10000) i;

-- Issue 17: Orphaned records (data integrity issue)
INSERT INTO comments (post_id, user_id, content, status)
VALUES 
  (9999, 1, 'This comment references a post that does not exist.', 'approved'),
  (1, 9999, 'This comment references a user that does not exist.', 'approved');

-- Insert orders
INSERT INTO orders (user_id, order_status, total_amount, shipping_address, billing_address)
SELECT 
  (random() * 999 + 1)::INTEGER, -- user_id between 1-1000
  (ARRAY['new', 'processing', 'shipped', 'delivered', 'cancelled'])[(i % 5) + 1],
  (random() * 990 + 10)::NUMERIC(10,2),
  i || ' Shipping Street, Shipping City, SC ' || (10000 + i)::TEXT,
  i || ' Billing Street, Billing City, BC ' || (10000 + i)::TEXT
FROM generate_series(1, 3000) i;

-- Insert order items
INSERT INTO order_items (order_id, product_id, quantity, unit_price, total_price)
SELECT 
  (random() * 2999 + 1)::INTEGER, -- order_id between 1-3000
  (random() * 4999 + 1)::INTEGER, -- product_id between 1-5000
  (random() * 4 + 1)::INTEGER, -- quantity between 1-5
  (random() * 990 + 10)::NUMERIC(10,2),
  (random() * 4 + 1) * (random() * 990 + 10)::NUMERIC(10,2) -- quantity * unit_price
FROM generate_series(1, 10000) i;

-- Insert events
INSERT INTO events (title, description, start_date, end_date, location, organizer_id, max_attendees)
SELECT 
  'Event ' || i,
  'Description for event ' || i,
  NOW() + (random() * 90)::INTEGER * INTERVAL '1 day',
  NOW() + (random() * 90 + 1)::INTEGER * INTERVAL '1 day', -- end_date is after start_date
  (ARRAY['New York', 'Los Angeles', 'Chicago', 'Houston', 'Phoenix'])[(i % 5) + 1],
  (random() * 999 + 1)::INTEGER, -- organizer_id between 1-1000
  (random() * 100 + 20)::INTEGER -- max_attendees between 20-120
FROM generate_series(1, 500) i;

-- Issue 18: Invalid date ranges (end before start)
INSERT INTO events (title, description, start_date, end_date, location, organizer_id, max_attendees)
VALUES (
  'Event with Invalid Date Range',
  'This event ends before it starts, which is a data issue.',
  NOW() + INTERVAL '10 days',
  NOW() + INTERVAL '5 days',
  'Error City',
  1,
  50
);

-- Insert user addresses
INSERT INTO user_addresses (user_id, address_line1, address_line2, city, state, postal_code, country, is_default, address_type)
SELECT 
  (random() * 999 + 1)::INTEGER, -- user_id between 1-1000
  i || ' Main Street',
  CASE WHEN i % 3 = 0 THEN 'Apt ' || i ELSE NULL END,
  (ARRAY['New York', 'Los Angeles', 'Chicago', 'Houston', 'Phoenix'])[(i % 5) + 1],
  (ARRAY['NY', 'CA', 'IL', 'TX', 'AZ'])[(i % 5) + 1],
  '1' || lpad((i % 9999)::TEXT, 4, '0'),
  'USA',
  i % 5 = 0, -- 20% are default
  CASE WHEN i % 2 = 0 THEN 'billing' ELSE 'shipping' END
FROM generate_series(1, 2000) i;

-- Insert user activity logs
INSERT INTO user_activity_logs (user_id, activity_type, ip_address, user_agent, activity_data)
SELECT 
  (random() * 999 + 1)::INTEGER, -- user_id between 1-1000
  (ARRAY['login', 'logout', 'purchase', 'view_product', 'view_category', 'add_to_cart', 'remove_from_cart', 'checkout'])[(i % 8) + 1],
  '192.168.' || (i % 255) || '.' || (i % 255),
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/' || (i % 15 + 80) || '.0.' || (i % 1000 + 4000) || '.' || (i % 100 + 100) || ' Safari/537.36',
  json_build_object(
    'details', 'Activity ' || i,
    'timestamp', NOW() - (random() * 30)::INTEGER * INTERVAL '1 day',
    'session_id', md5(i::TEXT)
  )
FROM generate_series(1, 50000) i;

-- Insert newsletter subscriptions
INSERT INTO newsletter_subscriptions (email, is_active, subscription_date, unsubscribe_date, source)
SELECT 
  'subscriber' || i || '@example.com',
  CASE WHEN i % 10 = 0 THEN 0 ELSE 1 END, -- 90% active (using integer for boolean)
  NOW() - (random() * 365)::INTEGER * INTERVAL '1 day',
  CASE WHEN i % 10 = 0 THEN NOW() - (random() * 30)::INTEGER * INTERVAL '1 day' ELSE NULL END,
  (ARRAY['website', 'social_media', 'referral', 'email_campaign'])[(i % 4) + 1]
FROM generate_series(1, 5000) i;

-- Insert coupons
INSERT INTO coupons (code, discount_amount, discount_percent, valid_from, valid_to, usage_limit, usage_count)
SELECT 
  'COUPON' || i,
  CASE WHEN i % 2 = 0 THEN (random() * 50)::NUMERIC(10,2) ELSE NULL END,
  CASE WHEN i % 2 = 1 THEN (random() * 50)::NUMERIC(5,2) ELSE NULL END,
  NOW() - (random() * 90)::INTEGER * INTERVAL '1 day',
  NOW() + (random() * 90)::INTEGER * INTERVAL '1 day',
  (random() * 100 + 1)::INTEGER,
  (random() * 50)::INTEGER
FROM generate_series(1, 100) i;

-- Issue 19: Duplicate coupon codes (missing unique constraint)
INSERT INTO coupons (code, discount_amount, discount_percent, valid_from, valid_to, usage_limit, usage_count)
VALUES 
  ('DUPLICATE10', 10.00, NULL, NOW(), NOW() + INTERVAL '30 days', 100, 0),
  ('DUPLICATE10', 10.00, NULL, NOW(), NOW() + INTERVAL '30 days', 100, 0);

-- Insert shipping methods
INSERT INTO shipping_methods (name, description, cost, is_active)
VALUES 
  ('Standard Shipping', 'Delivery within 5-7 business days', 5.99, TRUE),
  ('Express Shipping', 'Delivery within 2-3 business days', 12.99, TRUE),
  ('Overnight Shipping', 'Next day delivery', 19.99, TRUE),
  ('Free Shipping', 'Free shipping on orders over $50', 0.00, TRUE),
  ('International Standard', 'International delivery within 7-14 business days', 15.99, TRUE),
  ('International Express', 'International delivery within 3-5 business days', 29.99, TRUE),
  ('Store Pickup', 'Pick up your order at our store', 0.00, TRUE),
  ('Freight Shipping', 'For large items requiring special handling', 49.99, FALSE);

-- Insert wishlist items
INSERT INTO wishlist_items (user_id, product_id, added_at, note)
SELECT 
  (random() * 999 + 1)::INTEGER, -- user_id between 1-1000
  (random() * 4999 + 1)::INTEGER, -- product_id between 1-5000
  NOW() - (random() * 180)::INTEGER * INTERVAL '1 day',
  CASE WHEN i % 5 = 0 THEN 'Note for wishlist item ' || i ELSE NULL END
FROM generate_series(1, 3000) i;

-- Create some problematic indexes
CREATE INDEX idx_blog_posts_author ON blog_posts(author_id); -- Partial duplication with foreign key index
CREATE INDEX idx_blog_posts_author_id ON blog_posts(author_id); -- Duplicate index
CREATE INDEX idx_wishlist_user ON wishlist_items(user_id); -- Missing product_id index

-- Update statistics
ANALYZE;

-- Create some typical problematic queries from the migrated application
-- These will be slow and need optimization

-- Problematic query 1: Using LIKE on unindexed text fields
CREATE OR REPLACE FUNCTION search_products(search_term TEXT) RETURNS SETOF products AS $$
BEGIN
  RETURN QUERY
  SELECT * FROM products
  WHERE name LIKE '%' || search_term || '%'
  OR sku LIKE '%' || search_term || '%';
END;
$$ LANGUAGE plpgsql;

-- Problematic query 2: MySQL-style pagination causing sequential scans
CREATE OR REPLACE FUNCTION get_recent_orders(p_user_id INTEGER, p_limit INTEGER, p_offset INTEGER) RETURNS SETOF orders AS $$
BEGIN
  RETURN QUERY
  SELECT * FROM orders
  WHERE user_id = p_user_id
  ORDER BY created_at DESC
  LIMIT p_limit OFFSET p_offset;
END;
$$ LANGUAGE plpgsql;

-- Problematic query 3: MySQL-style LEFT JOIN without proper indexes
CREATE OR REPLACE FUNCTION get_user_report() RETURNS TABLE(
  user_id INTEGER,
  username VARCHAR,
  email VARCHAR,
  order_count BIGINT,
  total_spent NUMERIC
) AS $$
BEGIN
  RETURN QUERY
  SELECT 
    u.id,
    u.username,
    u.email,
    COUNT(o.order_id),
    COALESCE(SUM(o.total_amount), 0) as total_spent
  FROM users u
  LEFT JOIN orders o ON u.id = o.user_id
  GROUP BY u.id, u.username, u.email
  ORDER BY total_spent DESC;
END;
$$ LANGUAGE plpgsql;

-- Create views that would be common in a migrated application

-- Problematic view 1: Using MySQL-style string concatenation vs PostgreSQL
CREATE VIEW user_orders_view AS
SELECT 
  o.order_id,
  (u.first_name || ' ' || u.last_name) as customer_name, -- MySQL would use CONCAT()
  o.total_amount,
  o.order_status,
  o.created_at,
  COUNT(i.item_id) as item_count
FROM orders o
JOIN users u ON o.user_id = u.id
LEFT JOIN order_items i ON o.order_id = i.order_id
GROUP BY o.order_id, u.first_name, u.last_name, o.total_amount, o.order_status, o.created_at;

-- Problematic view 2: Using boolean logic differently than MySQL
CREATE VIEW active_products_view AS
SELECT 
  p.*,
  c.name as category_name
FROM products p
JOIN tbl_categories c ON p.category_id = c.categoryID
WHERE p.is_active = TRUE -- MySQL would use "WHERE is_active = 1"
AND p.stock > 0;

-- Problematic view 3: Using date functions differently than MySQL
CREATE VIEW recent_posts_view AS
SELECT 
  p.post_id,
  p.title,
  p.status,
  p.view_count,
  u.username as author,
  date_trunc('day', p.published_at) as publish_date, -- MySQL would use DATE() function
  COUNT(c.comment_id) as comment_count
FROM blog_posts p
LEFT JOIN users u ON p.author_id = u.id
LEFT JOIN comments c ON p.post_id = c.post_id
WHERE p.status = 'published'
GROUP BY p.post_id, p.title, p.status, p.view_count, u.username, p.published_at
ORDER BY p.published_at DESC;

-- Print summary information
SELECT 'Migration validation database setup completed. Database contains:' AS message;
SELECT 
  (SELECT COUNT(*) FROM users) AS users_count,
  (SELECT COUNT(*) FROM tbl_categories) AS categories_count,
  (SELECT COUNT(*) FROM products) AS products_count,
  (SELECT COUNT(*) FROM blog_posts) AS blog_posts_count,
  (SELECT COUNT(*) FROM comments) AS comments_count,
  (SELECT COUNT(*) FROM orders) AS orders_count,
  (SELECT COUNT(*) FROM order_items) AS order_items_count; 
