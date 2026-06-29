
-- Aditya-L1 Solar Flare Forecasting Database Initialization
-- Run this script first to create the database and user

-- Create database
CREATE DATABASE IF NOT EXISTS aditya_l1_sff;

-- Connect to the database
\c aditya_l1_sff;

-- Create user if not exists
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'sff_pipeline') THEN
        CREATE USER sff_pipeline WITH PASSWORD 'your_secure_password_here';
    END IF;
END
$$;

-- Grant privileges
GRANT ALL PRIVILEGES ON DATABASE aditya_l1_sff TO sff_pipeline;
