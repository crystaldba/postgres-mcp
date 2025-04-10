-- Advanced Demo 4: Time-Series Data Management
-- This script sets up a database for IoT sensor data with high ingestion rates 
-- and demonstrates time-series specific optimizations

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS hypopg;
CREATE EXTENSION IF NOT EXISTS timescaledb; -- For time-series specific functionality

-- Create schema for IoT sensor data
CREATE SCHEMA iot;

-- Create dimension tables
CREATE TABLE iot.devices (
  device_id SERIAL PRIMARY KEY,
  device_name VARCHAR(100) NOT NULL,
  device_type VARCHAR(50) NOT NULL,
  firmware_version VARCHAR(50),
  installation_date TIMESTAMP NOT NULL,
  location_id INTEGER,
  is_active BOOLEAN DEFAULT TRUE,
  properties JSONB,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE iot.locations (
  location_id SERIAL PRIMARY KEY,
  location_name VARCHAR(100) NOT NULL,
  address TEXT,
  city VARCHAR(100),
  state VARCHAR(50),
  country VARCHAR(50),
  latitude NUMERIC(10,6),
  longitude NUMERIC(10,6),
  location_type VARCHAR(50), -- 'factory', 'warehouse', 'office', etc.
  parent_location_id INTEGER REFERENCES iot.locations(location_id),
  properties JSONB,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Add foreign key from devices to locations
ALTER TABLE iot.devices ADD CONSTRAINT fk_devices_location 
  FOREIGN KEY (location_id) REFERENCES iot.locations(location_id);

-- Create sensor measurements table
CREATE TABLE iot.sensor_data (
  time TIMESTAMPTZ NOT NULL,
  device_id INTEGER NOT NULL,
  temperature NUMERIC(10,2),
  humidity NUMERIC(10,2),
  pressure NUMERIC(10,2),
  voltage NUMERIC(10,2),
  co2_level NUMERIC(10,2),
  light_level NUMERIC(10,2),
  accelerometer_x NUMERIC(10,4),
  accelerometer_y NUMERIC(10,4),
  accelerometer_z NUMERIC(10,4),
  status_code INTEGER,
  battery_level NUMERIC(5,2),
  signal_strength NUMERIC(5,2),
  error_flags INTEGER,
  metadata JSONB
);

-- Create an unoptimized traditional index
CREATE INDEX idx_sensor_data_device_id_time ON iot.sensor_data(device_id, time);

-- Create a hypertable from the sensor_data table (TimescaleDB optimization)
SELECT create_hypertable('iot.sensor_data', 'time', 
  chunk_time_interval => INTERVAL '1 day',
  create_default_indexes => FALSE);

-- Create maintenance tables
CREATE TABLE iot.alerts (
  alert_id SERIAL PRIMARY KEY,
  device_id INTEGER NOT NULL REFERENCES iot.devices(device_id),
  alert_time TIMESTAMP NOT NULL,
  alert_type VARCHAR(50) NOT NULL,
  alert_level VARCHAR(20) NOT NULL, -- 'info', 'warning', 'critical'
  message TEXT,
  is_acknowledged BOOLEAN DEFAULT FALSE,
  acknowledged_at TIMESTAMP,
  acknowledged_by VARCHAR(100),
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE iot.maintenance_logs (
  log_id SERIAL PRIMARY KEY,
  device_id INTEGER NOT NULL REFERENCES iot.devices(device_id),
  maintenance_time TIMESTAMP NOT NULL,
  maintenance_type VARCHAR(50) NOT NULL,
  technician VARCHAR(100),
  description TEXT,
  parts_replaced TEXT[],
  next_maintenance_date TIMESTAMP,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Create aggregate tables for faster queries
CREATE TABLE iot.hourly_aggregates (
  bucket TIMESTAMPTZ NOT NULL,
  device_id INTEGER NOT NULL,
  avg_temperature NUMERIC(10,2),
  min_temperature NUMERIC(10,2),
  max_temperature NUMERIC(10,2),
  avg_humidity NUMERIC(10,2),
  min_humidity NUMERIC(10,2),
  max_humidity NUMERIC(10,2),
  avg_pressure NUMERIC(10,2),
  avg_voltage NUMERIC(10,2),
  avg_co2_level NUMERIC(10,2),
  measurements_count INTEGER
);

-- Create a TimescaleDB hypertable for the hourly aggregates
SELECT create_hypertable('iot.hourly_aggregates', 'bucket', 
  chunk_time_interval => INTERVAL '1 month');

-- Create a view for device status
CREATE VIEW iot.device_status AS
SELECT
  d.device_id,
  d.device_name,
  d.device_type,
  l.location_name,
  l.city,
  l.country,
  sd.time as last_reading_time,
  sd.temperature as last_temperature,
  sd.humidity as last_humidity,
  sd.battery_level as last_battery_level,
  sd.signal_strength as last_signal_strength,
  (NOW() - sd.time) > INTERVAL '1 hour' as is_stale,
  d.is_active
FROM iot.devices d
JOIN iot.locations l ON d.location_id = l.location_id
LEFT JOIN LATERAL (
  SELECT time, temperature, humidity, battery_level, signal_strength
  FROM iot.sensor_data
  WHERE device_id = d.device_id
  ORDER BY time DESC
  LIMIT 1
) sd ON true;

-- Create functions for simulating and managing IoT data

-- Function to generate data in the past
CREATE OR REPLACE FUNCTION iot.generate_historical_data(
  start_timestamp TIMESTAMPTZ,
  end_timestamp TIMESTAMPTZ,
  device_count INTEGER
) RETURNS void AS $$
DECLARE
  current_time TIMESTAMPTZ := start_timestamp;
  batch_size INTEGER := 1000;
  device_ids INTEGER[];
  i INTEGER;
  d INTEGER;
  temp_min NUMERIC;
  temp_max NUMERIC;
  humidity_min NUMERIC;
  humidity_max NUMERIC;
  pressure_base NUMERIC;
  voltage_base NUMERIC;
  co2_base NUMERIC;
  light_base NUMERIC;
BEGIN
  -- Get all device IDs
  SELECT array_agg(device_id) INTO device_ids FROM iot.devices LIMIT device_count;
  
  -- Loop through time periods and generate data
  WHILE current_time < end_timestamp LOOP
    -- For each device
    FOREACH d IN ARRAY device_ids
    LOOP
      -- Set baseline values for this device
      temp_min := 15.0 + (random() * 10);
      temp_max := temp_min + 15.0 + (random() * 10);
      humidity_min := 30.0 + (random() * 20);
      humidity_max := humidity_min + 20.0 + (random() * 30);
      pressure_base := 990.0 + (random() * 40);
      voltage_base := 110.0 + (random() * 20);
      co2_base := 300.0 + (random() * 200);
      light_base := 0.0 + (random() * 500);
      
      -- Insert readings every 5 minutes for this device at this time
      FOR i IN 0..11 LOOP
        INSERT INTO iot.sensor_data (
          time,
          device_id,
          temperature,
          humidity,
          pressure,
          voltage,
          co2_level,
          light_level,
          accelerometer_x,
          accelerometer_y,
          accelerometer_z,
          status_code,
          battery_level,
          signal_strength,
          error_flags,
          metadata
        ) VALUES (
          current_time + (i * INTERVAL '5 minutes'),
          d,
          -- Generate realistic patterns (daily temperature cycles, etc.)
          temp_min + (temp_max - temp_min) * (
            0.5 + 0.5 * sin(
              extract(epoch from (current_time + (i * INTERVAL '5 minutes')))::numeric / 86400 * 2 * pi()
            )
          ) + (random() * 2 - 1),
          humidity_min + (humidity_max - humidity_min) * (
            0.5 - 0.3 * sin(
              extract(epoch from (current_time + (i * INTERVAL '5 minutes')))::numeric / 86400 * 2 * pi()
            )
          ) + (random() * 5 - 2.5),
          pressure_base + (random() * 2 - 1) + sin(
            extract(epoch from (current_time + (i * INTERVAL '5 minutes')))::numeric / 43200 * 2 * pi()
          ) * 3,
          voltage_base + (random() * 1 - 0.5),
          co2_base + (random() * 50 - 25) + (
            100 * sin(
              extract(epoch from (current_time + (i * INTERVAL '5 minutes')))::numeric / 86400 * 2 * pi()
            )
          ),
          CASE 
            WHEN extract(hour from (current_time + (i * INTERVAL '5 minutes'))) BETWEEN 8 AND 18 
            THEN light_base + 500 + (random() * 300)
            ELSE (random() * 50)
          END,
          (random() * 0.1 - 0.05),
          (random() * 0.1 - 0.05),
          (random() * 0.1 - 0.05) + 1.0, -- Gravity
          CASE WHEN random() < 0.01 THEN (random() * 5)::integer ELSE 0 END,
          CASE 
            WHEN i % 288 = 0 THEN 100.0
            ELSE 100.0 - (i % 288) / 288.0 * 15.0 - (random() * 2)
          END,
          -50 - (random() * 40),
          CASE WHEN random() < 0.005 THEN (1 << (random() * 8)::integer) ELSE 0 END,
          jsonb_build_object(
            'firmware_version', (SELECT firmware_version FROM iot.devices WHERE device_id = d),
            'reading_id', md5(random()::text)
          )
        );
      END LOOP;
    END LOOP;
    
    -- Advance time by 1 hour
    current_time := current_time + INTERVAL '1 hour';
    
    -- Commit every batch to avoid massive transactions
    IF random() < 0.1 THEN
      COMMIT;
      -- Start a new transaction
      BEGIN;
    END IF;
  END LOOP;
END;
$$ LANGUAGE plpgsql;

-- Function to populate hourly aggregates
CREATE OR REPLACE FUNCTION iot.populate_hourly_aggregates() RETURNS void AS $$
BEGIN
  -- Clear existing data
  TRUNCATE TABLE iot.hourly_aggregates;
  
  -- Insert aggregated data
  INSERT INTO iot.hourly_aggregates (
    bucket,
    device_id,
    avg_temperature,
    min_temperature,
    max_temperature,
    avg_humidity,
    min_humidity,
    max_humidity,
    avg_pressure,
    avg_voltage,
    avg_co2_level,
    measurements_count
  )
  SELECT
    time_bucket('1 hour', time) AS bucket,
    device_id,
    avg(temperature) AS avg_temperature,
    min(temperature) AS min_temperature,
    max(temperature) AS max_temperature,
    avg(humidity) AS avg_humidity,
    min(humidity) AS min_humidity,
    max(humidity) AS max_humidity,
    avg(pressure) AS avg_pressure,
    avg(voltage) AS avg_voltage,
    avg(co2_level) AS avg_co2_level,
    count(*) AS measurements_count
  FROM iot.sensor_data
  GROUP BY bucket, device_id;
END;
$$ LANGUAGE plpgsql;

-- Function to generate anomalies for demo
CREATE OR REPLACE FUNCTION iot.generate_anomalies(anomaly_count INTEGER) RETURNS void AS $$
DECLARE
  device_id_var INTEGER;
  anomaly_time TIMESTAMPTZ;
  alert_types TEXT[] := ARRAY['high_temperature', 'low_temperature', 'high_humidity', 'low_humidity', 
                             'high_pressure', 'voltage_drop', 'high_co2', 'battery_low', 'connection_lost'];
  alert_type_var TEXT;
  alert_level_var TEXT;
BEGIN
  FOR i IN 1..anomaly_count LOOP
    -- Select a random device
    SELECT device_id INTO device_id_var FROM iot.devices ORDER BY random() LIMIT 1;
    
    -- Generate a random time for the anomaly
    SELECT time INTO anomaly_time FROM iot.sensor_data 
    WHERE device_id = device_id_var
    ORDER BY random()
    LIMIT 1;
    
    -- Select a random alert type
    alert_type_var := alert_types[1 + (random() * (array_length(alert_types, 1) - 1))::integer];
    
    -- Determine alert level based on type
    alert_level_var := CASE
      WHEN alert_type_var IN ('high_temperature', 'voltage_drop', 'connection_lost') THEN 'critical'
      WHEN alert_type_var IN ('high_humidity', 'high_co2', 'battery_low') THEN 'warning'
      ELSE 'info'
    END;
    
    -- Insert the alert
    INSERT INTO iot.alerts (
      device_id,
      alert_time,
      alert_type,
      alert_level,
      message,
      is_acknowledged,
      acknowledged_at,
      acknowledged_by
    ) VALUES (
      device_id_var,
      anomaly_time,
      alert_type_var,
      alert_level_var,
      'Anomaly detected: ' || alert_type_var || ' on device ' || device_id_var,
      random() < 0.7, -- 70% are acknowledged
      CASE WHEN random() < 0.7 THEN anomaly_time + (random() * INTERVAL '4 hours') ELSE NULL END,
      CASE WHEN random() < 0.7 THEN 'Operator ' || (1 + (random() * 10)::integer)::text ELSE NULL END
    );
    
    -- Also insert sensor data anomaly if applicable
    IF alert_type_var = 'high_temperature' THEN
      INSERT INTO iot.sensor_data (
        time,
        device_id,
        temperature,
        humidity,
        pressure,
        voltage,
        co2_level,
        battery_level,
        signal_strength,
        status_code
      ) VALUES (
        anomaly_time,
        device_id_var,
        85.0 + (random() * 15), -- Very high temperature
        40.0 + (random() * 10),
        1013.0 + (random() * 5),
        110.0 + (random() * 5),
        400.0 + (random() * 50),
        75.0 + (random() * 10),
        -65.0 + (random() * 10),
        2
      );
    ELSIF alert_type_var = 'voltage_drop' THEN
      INSERT INTO iot.sensor_data (
        time,
        device_id,
        temperature,
        humidity,
        pressure,
        voltage,
        co2_level,
        battery_level,
        signal_strength,
        status_code
      ) VALUES (
        anomaly_time,
        device_id_var,
        22.0 + (random() * 5),
        50.0 + (random() * 10),
        1013.0 + (random() * 5),
        70.0 + (random() * 10), -- Low voltage
        400.0 + (random() * 50),
        60.0 + (random() * 10),
        -70.0 + (random() * 10),
        3
      );
    END IF;
    
    -- Generate maintenance events for some alerts
    IF random() < 0.3 AND alert_level_var = 'critical' THEN
      INSERT INTO iot.maintenance_logs (
        device_id,
        maintenance_time,
        maintenance_type,
        technician,
        description,
        parts_replaced,
        next_maintenance_date
      ) VALUES (
        device_id_var,
        anomaly_time + (random() * INTERVAL '2 days'),
        CASE 
          WHEN alert_type_var = 'high_temperature' THEN 'emergency_cooling'
          WHEN alert_type_var = 'voltage_drop' THEN 'power_system_repair'
          ELSE 'general_maintenance'
        END,
        'Technician ' || (1 + (random() * 5)::integer)::text,
        'Emergency maintenance performed due to ' || alert_type_var,
        CASE 
          WHEN alert_type_var = 'high_temperature' THEN ARRAY['thermal_sensor', 'cooling_fan']
          WHEN alert_type_var = 'voltage_drop' THEN ARRAY['power_supply']
          ELSE ARRAY['general_parts']
        END,
        anomaly_time + INTERVAL '90 days'
      );
    END IF;
  END LOOP;
END;
$$ LANGUAGE plpgsql;

-- Create continuous aggregates for real-time analytics (TimescaleDB feature)
CREATE MATERIALIZED VIEW iot.daily_device_summary
WITH (timescaledb.continuous) AS
SELECT
  time_bucket('1 day', time) AS day,
  device_id,
  avg(temperature) AS avg_temperature,
  min(temperature) AS min_temperature,
  max(temperature) AS max_temperature,
  avg(humidity) AS avg_humidity,
  avg(pressure) AS avg_pressure,
  avg(voltage) AS avg_voltage,
  avg(co2_level) AS avg_co2_level,
  count(*) AS reading_count
FROM iot.sensor_data
GROUP BY day, device_id;

-- Add policy for automatic refresh
SELECT add_continuous_aggregate_policy('iot.daily_device_summary',
  start_offset => INTERVAL '30 days',
  end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 day');

-- Create retention policy for data older than 6 months
SELECT add_retention_policy('iot.sensor_data', INTERVAL '6 months');

-- Create compression policy to compress chunks older than 7 days
SELECT add_compression_policy('iot.sensor_data', INTERVAL '7 days');

-- Add typical problem queries for the demo

-- Inefficient query that scans all data (will be slow)
CREATE OR REPLACE FUNCTION iot.get_device_temperature_history_inefficient(
  p_device_id INTEGER,
  p_start_date TIMESTAMP,
  p_end_date TIMESTAMP
) RETURNS TABLE (
  reading_time TIMESTAMP,
  temperature NUMERIC,
  humidity NUMERIC
) AS $$
BEGIN
  RETURN QUERY
  SELECT 
    time::TIMESTAMP,
    temperature,
    humidity
  FROM iot.sensor_data
  WHERE device_id = p_device_id
  AND time BETWEEN p_start_date AND p_end_date
  ORDER BY time;
END;
$$ LANGUAGE plpgsql;

-- Efficient query using TimescaleDB time_bucket for analytics
CREATE OR REPLACE FUNCTION iot.get_device_temperature_hourly(
  p_device_id INTEGER,
  p_start_date TIMESTAMP,
  p_end_date TIMESTAMP
) RETURNS TABLE (
  hour TIMESTAMP,
  avg_temp NUMERIC,
  min_temp NUMERIC,
  max_temp NUMERIC,
  avg_humidity NUMERIC
) AS $$
BEGIN
  RETURN QUERY
  SELECT 
    time_bucket('1 hour', time)::TIMESTAMP as hour,
    avg(temperature)::NUMERIC(10,2) as avg_temp,
    min(temperature)::NUMERIC(10,2) as min_temp,
    max(temperature)::NUMERIC(10,2) as max_temp,
    avg(humidity)::NUMERIC(10,2) as avg_humidity
  FROM iot.sensor_data
  WHERE device_id = p_device_id
  AND time BETWEEN p_start_date AND p_end_date
  GROUP BY hour
  ORDER BY hour;
END;
$$ LANGUAGE plpgsql;

-- Function to find temperature anomalies (inefficient approach)
CREATE OR REPLACE FUNCTION iot.find_temperature_anomalies_inefficient(
  p_threshold NUMERIC
) RETURNS TABLE (
  device_id INTEGER,
  device_name VARCHAR,
  location_name VARCHAR,
  anomaly_time TIMESTAMP,
  temperature NUMERIC
) AS $$
BEGIN
  RETURN QUERY
  SELECT 
    d.device_id,
    d.device_name,
    l.location_name,
    s.time::TIMESTAMP,
    s.temperature
  FROM iot.sensor_data s
  JOIN iot.devices d ON s.device_id = d.device_id
  JOIN iot.locations l ON d.location_id = l.location_id
  WHERE s.temperature > p_threshold
  ORDER BY s.time DESC;
END;
$$ LANGUAGE plpgsql;

-- Function to find temperature anomalies (efficient approach with window functions)
CREATE OR REPLACE FUNCTION iot.find_temperature_anomalies_efficient(
  p_z_score NUMERIC DEFAULT 3.0
) RETURNS TABLE (
  device_id INTEGER,
  device_name VARCHAR,
  location_name VARCHAR,
  anomaly_time TIMESTAMP,
  temperature NUMERIC,
  avg_temp NUMERIC,
  stddev_temp NUMERIC,
  z_score NUMERIC
) AS $$
BEGIN
  RETURN QUERY
  WITH device_stats AS (
    SELECT 
      d.device_id,
      avg(s.temperature) as avg_temp,
      stddev(s.temperature) as stddev_temp
    FROM iot.sensor_data s
    JOIN iot.devices d ON s.device_id = d.device_id
    GROUP BY d.device_id
  ),
  anomalies AS (
    SELECT 
      s.device_id,
      s.time,
      s.temperature,
      ds.avg_temp,
      ds.stddev_temp,
      ABS((s.temperature - ds.avg_temp) / NULLIF(ds.stddev_temp, 0)) as z_score
    FROM iot.sensor_data s
    JOIN device_stats ds ON s.device_id = ds.device_id
    WHERE ABS((s.temperature - ds.avg_temp) / NULLIF(ds.stddev_temp, 0)) > p_z_score
  )
  SELECT 
    a.device_id,
    d.device_name,
    l.location_name,
    a.time::TIMESTAMP,
    a.temperature,
    a.avg_temp,
    a.stddev_temp,
    a.z_score
  FROM anomalies a
  JOIN iot.devices d ON a.device_id = d.device_id
  JOIN iot.locations l ON d.location_id = l.location_id
  ORDER BY a.z_score DESC
  LIMIT 100;
END;
$$ LANGUAGE plpgsql;

-- Populate sample data

-- Insert locations
INSERT INTO iot.locations (location_name, address, city, state, country, latitude, longitude, location_type)
VALUES 
  ('Main Factory', '123 Manufacturing Blvd', 'Detroit', 'MI', 'USA', 42.331429, -83.045753, 'factory'),
  ('Warehouse East', '456 Storage Ave', 'Cleveland', 'OH', 'USA', 41.499320, -81.694361, 'warehouse'),
  ('Warehouse West', '789 Inventory St', 'Chicago', 'IL', 'USA', 41.878113, -87.629799, 'warehouse'),
  ('R&D Center', '321 Innovation Pkwy', 'Pittsburgh', 'PA', 'USA', 40.440624, -79.995888, 'office'),
  ('European Factory', '1 Production Road', 'Manchester', 'England', 'UK', 53.483959, -2.244644, 'factory'),
  ('Asian Factory', '2 Manufacturing Center', 'Shenzhen', 'Guangdong', 'China', 22.543096, 114.057865, 'factory'),
  ('South Factory', '3 Assembly Lane', 'Austin', 'TX', 'USA', 30.267153, -97.743057, 'factory'),
  ('Distribution Center', '4 Logistics Way', 'Denver', 'CO', 'USA', 39.739235, -104.990250, 'warehouse'),
  ('Main Office', '5 Corporate Plaza', 'New York', 'NY', 'USA', 40.712776, -74.005974, 'office'),
  ('Data Center', '6 Server Road', 'Seattle', 'WA', 'USA', 47.606209, -122.332069, 'datacenter');

-- Insert sub-locations
INSERT INTO iot.locations (location_name, parent_location_id, location_type, city, state, country)
VALUES
  ('Assembly Line A', 1, 'production_line', 'Detroit', 'MI', 'USA'),
  ('Assembly Line B', 1, 'production_line', 'Detroit', 'MI', 'USA'),
  ('QA Department', 1, 'testing', 'Detroit', 'MI', 'USA'),
  ('Storage Area 1', 2, 'storage', 'Cleveland', 'OH', 'USA'),
  ('Storage Area 2', 2, 'storage', 'Cleveland', 'OH', 'USA'),
  ('Shipping Dock', 2, 'shipping', 'Cleveland', 'OH', 'USA'),
  ('Electronics Lab', 4, 'laboratory', 'Pittsburgh', 'PA', 'USA'),
  ('Software Lab', 4, 'laboratory', 'Pittsburgh', 'PA', 'USA'),
  ('Server Room', 9, 'infrastructure', 'New York', 'NY', 'USA'),
  ('Cold Storage', 3, 'refrigerated', 'Chicago', 'IL', 'USA');

-- Insert devices
INSERT INTO iot.devices (device_name, device_type, firmware_version, installation_date, location_id, properties)
SELECT 
  'Device-' || i,
  (ARRAY['temperature_sensor', 'humidity_sensor', 'pressure_sensor', 'multi_sensor', 'environmental_monitor', 'power_monitor'])[(i % 6) + 1],
  '1.' || (i % 10) || '.' || (i % 5),
  NOW() - (random() * 365 * 2)::integer * INTERVAL '1 day',
  (i % 20) + 1,
  jsonb_build_object(
    'manufacturer', (ARRAY['Acme', 'Globex', 'Initech', 'Umbrella', 'Massive Dynamic'])[(i % 5) + 1],
    'model', 'Model-' || (i % 10 + 1),
    'serial_number', 'SN-' || i,
    'maintenance_cycle', (ARRAY[30, 60, 90, 180, 365])[(i % 5) + 1],
    'indoor', (i % 3) != 0
  )
FROM generate_series(1, 100) i;

-- Begin a transaction for the data generation
BEGIN;

-- Generate historical data for the past 30 days (reduce time range if needed for performance)
SELECT iot.generate_historical_data(
  NOW() - INTERVAL '30 days',
  NOW(),
  100 -- device count
);

-- Populate hourly aggregates
SELECT iot.populate_hourly_aggregates();

-- Generate some anomalies for testing
SELECT iot.generate_anomalies(200);

-- Add some scheduled maintenance records
INSERT INTO iot.maintenance_logs (
  device_id,
  maintenance_time,
  maintenance_type,
  technician,
  description,
  parts_replaced,
  next_maintenance_date
)
SELECT 
  (random() * 99 + 1)::integer,
  NOW() - (random() * 180)::integer * INTERVAL '1 day',
  (ARRAY['routine', 'calibration', 'repair', 'upgrade', 'inspection'])[(i % 5) + 1],
  'Technician ' || (i % 10 + 1),
  'Scheduled maintenance performed',
  ARRAY['filter', 'sensor', 'battery'][(i % 3) + 1:1],
  NOW() + (random() * 180)::integer * INTERVAL '1 day'
FROM generate_series(1, 300) i;

COMMIT;

-- Update statistics
ANALYZE;

-- Create a cagg_now function for continuous aggregate demos
CREATE OR REPLACE FUNCTION iot.cagg_now() RETURNS TIMESTAMPTZ
LANGUAGE SQL STABLE AS
$BODY$
    SELECT latest_completed_ts FROM _timescaledb_catalog.continuous_aggs_invalidation_threshold 
    WHERE hypertable_id = (
        SELECT format('%I.%I', schema_name, table_name)::regclass::oid
        FROM _timescaledb_catalog.hypertable
        WHERE table_name = 'sensor_data'
        AND schema_name = 'iot'
    )
$BODY$;

-- Print summary info
SELECT 'Time-Series IoT database setup completed. Database contains:' AS message;
SELECT 
  (SELECT COUNT(*) FROM iot.locations) AS locations_count,
  (SELECT COUNT(*) FROM iot.devices) AS devices_count,
  (SELECT COUNT(*) FROM iot.sensor_data) AS sensor_readings_count,
  (SELECT COUNT(*) FROM iot.alerts) AS alerts_count,
  (SELECT COUNT(*) FROM iot.maintenance_logs) AS maintenance_logs_count,
  (SELECT COUNT(*) FROM iot.hourly_aggregates) AS hourly_aggregate_count; 
