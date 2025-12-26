-- Sample Data Initialization Script
-- This creates a simple e-commerce database with customers, products, and orders

-- Create tables
CREATE TABLE IF NOT EXISTS customers (
    customer_id SERIAL PRIMARY KEY,
    first_name VARCHAR(50) NOT NULL,
    last_name VARCHAR(50) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    phone VARCHAR(20),
    city VARCHAR(50),
    country VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    category_id SERIAL PRIMARY KEY,
    category_name VARCHAR(100) NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS products (
    product_id SERIAL PRIMARY KEY,
    product_name VARCHAR(200) NOT NULL,
    category_id INTEGER REFERENCES categories(category_id),
    price DECIMAL(10, 2) NOT NULL,
    stock_quantity INTEGER DEFAULT 0,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    order_id SERIAL PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(customer_id),
    order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_amount DECIMAL(10, 2),
    status VARCHAR(20) DEFAULT 'pending',
    shipping_address TEXT
);

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id SERIAL PRIMARY KEY,
    order_id INTEGER REFERENCES orders(order_id),
    product_id INTEGER REFERENCES products(product_id),
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(10, 2) NOT NULL
);

-- Insert sample data

-- Customers
INSERT INTO customers (first_name, last_name, email, phone, city, country) VALUES
    ('John', 'Doe', 'john.doe@email.com', '+1-555-0101', 'New York', 'USA'),
    ('Jane', 'Smith', 'jane.smith@email.com', '+1-555-0102', 'Los Angeles', 'USA'),
    ('Bob', 'Johnson', 'bob.johnson@email.com', '+1-555-0103', 'Chicago', 'USA'),
    ('Alice', 'Williams', 'alice.williams@email.com', '+44-20-1234', 'London', 'UK'),
    ('Charlie', 'Brown', 'charlie.brown@email.com', '+49-30-5678', 'Berlin', 'Germany'),
    ('Diana', 'Davis', 'diana.davis@email.com', '+33-1-9876', 'Paris', 'France'),
    ('Eve', 'Martinez', 'eve.martinez@email.com', '+34-91-5432', 'Madrid', 'Spain'),
    ('Frank', 'Garcia', 'frank.garcia@email.com', '+1-555-0104', 'Miami', 'USA'),
    ('Grace', 'Lee', 'grace.lee@email.com', '+81-3-1234', 'Tokyo', 'Japan'),
    ('Henry', 'Wilson', 'henry.wilson@email.com', '+61-2-5678', 'Sydney', 'Australia');

-- Categories
INSERT INTO categories (category_name, description) VALUES
    ('Electronics', 'Electronic devices and accessories'),
    ('Books', 'Physical and digital books'),
    ('Clothing', 'Apparel and fashion items'),
    ('Home & Garden', 'Home improvement and garden supplies'),
    ('Sports & Outdoors', 'Sports equipment and outdoor gear'),
    ('Toys & Games', 'Toys, games, and entertainment'),
    ('Health & Beauty', 'Health products and beauty supplies');

-- Products
INSERT INTO products (product_name, category_id, price, stock_quantity, description) VALUES
    ('Wireless Bluetooth Headphones', 1, 79.99, 150, 'High-quality wireless headphones with noise cancellation'),
    ('Laptop Stand', 1, 49.99, 200, 'Ergonomic aluminum laptop stand'),
    ('USB-C Cable 6ft', 1, 12.99, 500, 'Fast charging USB-C cable'),
    ('The Great Gatsby', 2, 14.99, 100, 'Classic American novel by F. Scott Fitzgerald'),
    ('Clean Code', 2, 39.99, 75, 'A Handbook of Agile Software Craftsmanship'),
    ('Mens Cotton T-Shirt', 3, 19.99, 300, 'Comfortable 100% cotton t-shirt'),
    ('Womens Running Shoes', 3, 89.99, 120, 'Lightweight running shoes with arch support'),
    ('Yoga Mat', 5, 24.99, 180, 'Non-slip exercise yoga mat'),
    ('Dumbbell Set', 5, 99.99, 50, '20lb adjustable dumbbell set'),
    ('LED Desk Lamp', 4, 34.99, 90, 'Adjustable brightness LED desk lamp'),
    ('Indoor Plant Pot', 4, 15.99, 250, 'Ceramic plant pot with drainage'),
    ('Board Game - Strategy', 6, 44.99, 60, 'Family-friendly strategy board game'),
    ('Vitamin D Supplement', 7, 18.99, 200, '1000 IU vitamin D3 supplements'),
    ('Face Moisturizer', 7, 29.99, 150, 'Hydrating face moisturizer with SPF'),
    ('Smart Watch', 1, 199.99, 80, 'Fitness tracking smart watch');

-- Orders
INSERT INTO orders (customer_id, order_date, total_amount, status, shipping_address) VALUES
    (1, '2024-12-01 10:30:00', 92.98, 'delivered', '123 Main St, New York, NY 10001'),
    (1, '2024-12-15 14:20:00', 49.99, 'shipped', '123 Main St, New York, NY 10001'),
    (2, '2024-12-03 09:15:00', 134.97, 'delivered', '456 Oak Ave, Los Angeles, CA 90001'),
    (3, '2024-12-05 16:45:00', 79.99, 'delivered', '789 Pine Rd, Chicago, IL 60601'),
    (4, '2024-12-08 11:00:00', 54.98, 'delivered', '10 Downing St, London, UK'),
    (5, '2024-12-10 13:30:00', 199.99, 'shipped', '20 Unter den Linden, Berlin, Germany'),
    (2, '2024-12-12 15:00:00', 89.99, 'processing', '456 Oak Ave, Los Angeles, CA 90001'),
    (6, '2024-12-14 10:45:00', 44.99, 'pending', '30 Champs Elysees, Paris, France'),
    (7, '2024-12-16 12:20:00', 124.98, 'processing', '40 Gran Via, Madrid, Spain'),
    (8, '2024-12-18 14:00:00', 149.98, 'pending', '50 Ocean Dr, Miami, FL 33139');

-- Order Items
INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES
    -- Order 1
    (1, 1, 1, 79.99),
    (1, 3, 1, 12.99),
    -- Order 2
    (2, 2, 1, 49.99),
    -- Order 3
    (3, 7, 1, 89.99),
    (3, 4, 1, 14.99),
    (3, 6, 2, 19.99),
    -- Order 4
    (4, 1, 1, 79.99),
    -- Order 5
    (5, 4, 1, 14.99),
    (5, 5, 1, 39.99),
    -- Order 6
    (6, 15, 1, 199.99),
    -- Order 7
    (7, 7, 1, 89.99),
    -- Order 8
    (8, 12, 1, 44.99),
    -- Order 9
    (9, 8, 1, 24.99),
    (9, 9, 1, 99.99),
    -- Order 10
    (10, 10, 1, 34.99),
    (10, 11, 2, 15.99),
    (10, 1, 1, 79.99);

-- Create some indexes for better query performance
CREATE INDEX idx_products_category ON products(category_id);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);

-- Create a view for order summaries
CREATE VIEW order_summary AS
SELECT 
    o.order_id,
    c.first_name || ' ' || c.last_name AS customer_name,
    c.email,
    o.order_date,
    o.status,
    o.total_amount,
    COUNT(oi.order_item_id) AS total_items
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
LEFT JOIN order_items oi ON o.order_id = oi.order_id
GROUP BY o.order_id, c.first_name, c.last_name, c.email, o.order_date, o.status, o.total_amount
ORDER BY o.order_date DESC;

-- Grant necessary permissions
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO postgres;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO postgres;

-- Display summary
DO $$
BEGIN
    RAISE NOTICE '===========================================';
    RAISE NOTICE 'Sample Database Initialized Successfully!';
    RAISE NOTICE '===========================================';
    RAISE NOTICE 'Tables created:';
    RAISE NOTICE '  - customers (% rows)', (SELECT COUNT(*) FROM customers);
    RAISE NOTICE '  - categories (% rows)', (SELECT COUNT(*) FROM categories);
    RAISE NOTICE '  - products (% rows)', (SELECT COUNT(*) FROM products);
    RAISE NOTICE '  - orders (% rows)', (SELECT COUNT(*) FROM orders);
    RAISE NOTICE '  - order_items (% rows)', (SELECT COUNT(*) FROM order_items);
    RAISE NOTICE '===========================================';
END $$;
