-- Demo 4: Schema Discovery and Documentation
-- This script creates a database with multiple schemas and complex relationships
-- to demonstrate PostgreSQL schema exploration capabilities

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

CREATE TABLE analytics.conversion_funnel (
  id SERIAL PRIMARY KEY,
  session_id VARCHAR(100),
  user_id INTEGER REFERENCES auth.users(id),
  step VARCHAR(50) NOT NULL,
  occurred_at TIMESTAMP DEFAULT NOW(),
  metadata JSONB
);

CREATE TABLE analytics.marketing_campaigns (
  id SERIAL PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  description TEXT,
  start_date TIMESTAMP NOT NULL,
  end_date TIMESTAMP,
  budget NUMERIC(12,2),
  target_audience TEXT,
  metrics JSONB
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

CREATE TABLE archive.audit_log (
  id SERIAL PRIMARY KEY,
  table_name VARCHAR(100) NOT NULL,
  record_id INTEGER NOT NULL,
  action VARCHAR(20) NOT NULL,
  changed_by INTEGER REFERENCES auth.users(id),
  changed_at TIMESTAMP DEFAULT NOW(),
  old_values JSONB,
  new_values JSONB
);

-- Create views
CREATE VIEW public.active_products AS
  SELECT * FROM public.products WHERE inventory > 0;

CREATE VIEW public.order_summary AS
  SELECT o.id, o.user_id, u.username, o.total_amount, o.created_at
  FROM public.orders o
  JOIN auth.users u ON o.user_id = u.id;

CREATE VIEW analytics.user_activity AS
  SELECT 
    u.id, 
    u.username, 
    COUNT(DISTINCT pv.session_id) as session_count,
    COUNT(DISTINCT o.id) as order_count,
    MAX(pv.viewed_at) as last_activity,
    SUM(o.total_amount) as total_spent
  FROM auth.users u
  LEFT JOIN analytics.page_views pv ON u.id = pv.user_id
  LEFT JOIN public.orders o ON u.id = o.user_id
  GROUP BY u.id, u.username;

CREATE MATERIALIZED VIEW analytics.product_performance AS
  SELECT 
    p.id,
    p.name,
    COUNT(DISTINCT pv.id) as view_count,
    COUNT(DISTINCT oi.order_id) as order_count,
    SUM(oi.quantity) as units_sold,
    SUM(oi.quantity * oi.unit_price) as revenue,
    SUM(oi.quantity * oi.unit_price) / NULLIF(COUNT(DISTINCT pv.id), 0) as conversion_rate
  FROM public.products p
  LEFT JOIN analytics.product_views pv ON p.id = pv.product_id
  LEFT JOIN public.order_items oi ON p.id = oi.product_id
  GROUP BY p.id, p.name;

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

-- Create a function
CREATE OR REPLACE FUNCTION analytics.get_user_conversion_rate(user_id INTEGER)
RETURNS NUMERIC AS $$
DECLARE
  view_count INTEGER;
  purchase_count INTEGER;
  conversion_rate NUMERIC;
BEGIN
  SELECT COUNT(*) INTO view_count
  FROM analytics.product_views
  WHERE user_id = $1;
  
  SELECT COUNT(*) INTO purchase_count
  FROM public.orders
  WHERE user_id = $1;
  
  IF view_count = 0 THEN
    RETURN 0;
  END IF;
  
  conversion_rate := purchase_count::NUMERIC / view_count::NUMERIC;
  RETURN conversion_rate;
END;
$$ LANGUAGE plpgsql;

-- Create triggers for audit logging
CREATE OR REPLACE FUNCTION archive.create_audit_log()
RETURNS TRIGGER AS $$
BEGIN
  IF (TG_OP = 'DELETE') THEN
    INSERT INTO archive.audit_log (table_name, record_id, action, old_values)
    VALUES (TG_TABLE_NAME, OLD.id, 'DELETE', to_jsonb(OLD));
    RETURN OLD;
  ELSIF (TG_OP = 'UPDATE') THEN
    INSERT INTO archive.audit_log (table_name, record_id, action, old_values, new_values)
    VALUES (TG_TABLE_NAME, NEW.id, 'UPDATE', to_jsonb(OLD), to_jsonb(NEW));
    RETURN NEW;
  ELSIF (TG_OP = 'INSERT') THEN
    INSERT INTO archive.audit_log (table_name, record_id, action, new_values)
    VALUES (TG_TABLE_NAME, NEW.id, 'INSERT', to_jsonb(NEW));
    RETURN NEW;
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER products_audit
AFTER INSERT OR UPDATE OR DELETE ON public.products
FOR EACH ROW EXECUTE FUNCTION archive.create_audit_log();

CREATE TRIGGER orders_audit
AFTER INSERT OR UPDATE OR DELETE ON public.orders
FOR EACH ROW EXECUTE FUNCTION archive.create_audit_log();

-- Add some data types
CREATE TYPE public.shipping_status AS ENUM ('pending', 'processing', 'shipped', 'delivered', 'returned');

CREATE TABLE public.shipments (
  id SERIAL PRIMARY KEY,
  order_id INTEGER REFERENCES public.orders(id),
  tracking_number VARCHAR(100),
  carrier VARCHAR(50),
  status public.shipping_status DEFAULT 'pending',
  shipped_at TIMESTAMP,
  estimated_delivery TIMESTAMP,
  actual_delivery TIMESTAMP
);

-- Add a domain constraint
CREATE DOMAIN public.valid_email AS VARCHAR(100)
  CHECK (VALUE ~ '^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$');

CREATE TABLE public.newsletter_subscribers (
  id SERIAL PRIMARY KEY,
  email public.valid_email NOT NULL UNIQUE,
  subscribed_at TIMESTAMP DEFAULT NOW(),
  is_active BOOLEAN DEFAULT TRUE
);

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

INSERT INTO auth.role_permissions (role_id, permission_id)
VALUES
  (1, 1), (1, 2), (1, 3), (1, 4), (1, 5), -- admin gets all permissions
  (2, 1), (2, 2), (2, 3),                  -- manager gets some permissions
  (3, 1), (3, 4);                          -- customer gets few permissions

INSERT INTO auth.users (username, email, password_hash) VALUES
  ('admin', 'admin@example.com', 'hashed_password'),
  ('manager1', 'manager1@example.com', 'hashed_password'),
  ('customer1', 'customer1@example.com', 'hashed_password');

INSERT INTO auth.user_roles (user_id, role_id)
VALUES
  (1, 1),  -- admin user gets admin role
  (2, 2),  -- manager gets manager role
  (3, 3);  -- customer gets customer role

INSERT INTO public.categories (name, parent_id) VALUES
  ('Electronics', NULL),
  ('Computers', 1),
  ('Laptops', 2),
  ('Desktop PCs', 2),
  ('Accessories', 1),
  ('Clothing', NULL),
  ('Men''s', 6),
  ('Women''s', 6);

INSERT INTO public.products (name, description, price, inventory) VALUES
  ('Laptop Pro', 'High performance laptop', 1299.99, 50),
  ('Desktop Ultra', 'Powerful desktop computer', 999.99, 25),
  ('Wireless Mouse', 'Bluetooth wireless mouse', 29.99, 100),
  ('T-shirt', 'Cotton t-shirt', 19.99, 200),
  ('Jeans', 'Blue denim jeans', 49.99, 100);

INSERT INTO public.product_categories (product_id, category_id) VALUES
  (1, 3),  -- Laptop Pro in Laptops
  (2, 4),  -- Desktop Ultra in Desktop PCs
  (3, 5),  -- Wireless Mouse in Accessories
  (4, 7),  -- T-shirt in Men's
  (5, 7);  -- Jeans in Men's

-- Create some indexes
CREATE INDEX idx_products_name ON public.products(name);
CREATE INDEX idx_orders_user_id ON public.orders(user_id);
CREATE INDEX idx_page_views_user_id ON analytics.page_views(user_id);
CREATE INDEX idx_page_views_viewed_at ON analytics.page_views(viewed_at);
CREATE INDEX idx_product_views_product_id ON analytics.product_views(product_id);

COMMENT ON TABLE auth.users IS 'Stores user accounts and authentication information';
COMMENT ON TABLE public.products IS 'Product catalog with inventory information';
COMMENT ON TABLE analytics.page_views IS 'Tracks user page view activity for analytics';

-- Update statistics
ANALYZE;

-- Print summary
SELECT 'Schema Discovery demo setup completed successfully.';
SELECT 'The database contains multiple schemas, tables, views, and relationships to explore.';
SELECT 'Use the list_schemas, list_objects, and get_object_details tools to explore the database structure.'; 
