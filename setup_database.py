
#!/usr/bin/env python3
"""
Database Setup Script
Initializes the PostgreSQL database and creates all required tables
"""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from pipeline_utils import load_config, setup_logger

cfg = load_config()
logger = setup_logger("database_setup", cfg["pipeline"]["log_level"])

# Try to import psycopg2, install if missing
try:
    import psycopg2
    from psycopg2 import sql
except ImportError:
    logger.info("psycopg2-binary not found, installing...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psycopg2-binary"])
    import psycopg2
    from psycopg2 import sql


def main():
    db_config = cfg["database"]

    logger.info("=" * 60)
    logger.info("DATABASE SETUP")
    logger.info("=" * 60)

    # For Supabase, skip admin stuff and just connect directly to the database
    logger.info("Connecting directly to target database...")

    # Now connect to target database and create tables
    try:
        conn = psycopg2.connect(
            host=db_config["host"],
            port=db_config["port"],
            dbname=db_config["name"],
            user=db_config["user"],
            password=db_config["password"]
        )
        conn.autocommit = True
        cur = conn.cursor()

        # Create tables
        tables_sql = [
            """
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                run_id TEXT PRIMARY KEY,
                run_time TIMESTAMPTZ NOT NULL,
                source_used TEXT,
                records_fetched INTEGER,
                pipeline_status TEXT,
                elapsed_s REAL,
                warnings JSONB
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS solexs_hel1os_raw (
                obs_id TEXT PRIMARY KEY,
                obs_time TIMESTAMPTZ NOT NULL,
                source TEXT,
                solexs_1_8A_Wm2 DOUBLE PRECISION,
                solexs_0_4A_Wm2 DOUBLE PRECISION,
                solexs_peak_60min DOUBLE PRECISION,
                solexs_dFdt DOUBLE PRECISION,
                flux_ratio DOUBLE PRECISION,
                hel1os_20_60_cts DOUBLE PRECISION,
                hel1os_60_100_cts DOUBLE PRECISION,
                spectral_gamma DOUBLE PRECISION,
                kp_index REAL,
                solar_wind_speed REAL,
                imf_bz REAL,
                raw_json JSONB
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS solar_features (
                feature_id TEXT PRIMARY KEY,
                obs_time TIMESTAMPTZ NOT NULL,
                vector JSONB,
                sequence JSONB,
                raw_scalars JSONB
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS flare_predictions (
                pred_id TEXT PRIMARY KEY,
                obs_time TIMESTAMPTZ NOT NULL,
                prediction_time TIMESTAMPTZ NOT NULL,
                source TEXT,
                predicted_class TEXT,
                predicted_flux_class TEXT,
                flare_probability REAL,
                m_class_probability REAL,
                x_class_probability REAL,
                cme_probability REAL,
                geomagnetic_risk REAL,
                geomagnetic_label TEXT,
                confidence_score REAL,
                estimated_onset_utc TIMESTAMPTZ,
                class_probs_json JSONB,
                model_outputs_json JSONB
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS flare_alerts (
                alert_id TEXT PRIMARY KEY,
                pred_id TEXT REFERENCES flare_predictions(pred_id),
                alert_time TIMESTAMPTZ NOT NULL,
                severity TEXT,
                threshold_name TEXT,
                threshold_value REAL,
                actual_value REAL,
                message TEXT,
                dispatched BOOLEAN DEFAULT FALSE
            )
            """
        ]

        for sql_stmt in tables_sql:
            cur.execute(sql_stmt)

        # Create indexes
        indexes_sql = [
            "CREATE INDEX IF NOT EXISTS idx_pred_obs_time ON flare_predictions(obs_time DESC)",
            "CREATE INDEX IF NOT EXISTS idx_alert_severity ON flare_alerts(severity)",
            "CREATE INDEX IF NOT EXISTS idx_raw_obs_time ON solexs_hel1os_raw(obs_time DESC)",
            "CREATE INDEX IF NOT EXISTS idx_features_obs_time ON solar_features(obs_time DESC)"
        ]

        for sql_stmt in indexes_sql:
            cur.execute(sql_stmt)

        cur.close()
        conn.close()

        logger.info("All tables and indexes created successfully!")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"Database setup failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
