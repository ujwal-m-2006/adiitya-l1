#!/usr/bin/env python3
"""
05_save_alert_report.py
=======================
Aditya-L1 SFF Pipeline — Steps 5, 6, 7, 8

  Step 5 — Save predictions to PostgreSQL
  Step 6 — Evaluate alert thresholds and fire alerts
  Step 7 — Update monitoring dashboard (WebSocket push)
  Step 8 — Generate structured JSON report

PostgreSQL schema is created on first run (idempotent).
"""

import json
import uuid
import time
import logging
import smtplib
import requests
from pathlib import Path
from datetime import datetime, timezone
from email.mime.text import MIMEText

# ── Optional: psycopg2 for real PostgreSQL ────────────────────
try:
    import psycopg2
    import psycopg2.extras
    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

from pipeline_utils import (
    load_config, setup_logger, PipelineState,
    save_json, utc_now, classify_flux, geo_storm_label
)

cfg    = load_config()
logger = setup_logger("save_alert", cfg["pipeline"]["log_level"])
DB_CFG = cfg["database"]
ALERT_CFG = cfg["alerts"]


# ══════════════════════════════════════════════════════════════
# STEP 5 — PostgreSQL
# ══════════════════════════════════════════════════════════════

class PostgresWriter:

    CREATE_TABLES = """
    -- Run-level tracking
    CREATE TABLE IF NOT EXISTS pipeline_runs (
        run_id          TEXT PRIMARY KEY,
        run_time        TIMESTAMPTZ NOT NULL,
        source_used     TEXT,
        records_fetched INTEGER,
        pipeline_status TEXT,
        elapsed_s       REAL,
        warnings        JSONB
    );

    -- Raw L1 observations (SoLEXS + HEL1OS)
    CREATE TABLE IF NOT EXISTS solexs_hel1os_raw (
        obs_id              TEXT PRIMARY KEY,
        obs_time            TIMESTAMPTZ NOT NULL,
        source              TEXT,
        solexs_1_8A_Wm2     DOUBLE PRECISION,
        solexs_0_4A_Wm2     DOUBLE PRECISION,
        solexs_peak_60min   DOUBLE PRECISION,
        solexs_dFdt         DOUBLE PRECISION,
        flux_ratio          DOUBLE PRECISION,
        hel1os_20_60_cts    DOUBLE PRECISION,
        hel1os_60_100_cts   DOUBLE PRECISION,
        spectral_gamma      DOUBLE PRECISION,
        kp_index            REAL,
        solar_wind_speed    REAL,
        imf_bz              REAL,
        raw_json            JSONB
    );

    -- AI predictions
    CREATE TABLE IF NOT EXISTS flare_predictions (
        pred_id                 TEXT PRIMARY KEY,
        obs_time                TIMESTAMPTZ NOT NULL,
        prediction_time         TIMESTAMPTZ NOT NULL,
        source                  TEXT,
        predicted_class         TEXT,
        predicted_flux_class    TEXT,
        flare_probability       REAL,
        m_class_probability     REAL,
        x_class_probability     REAL,
        cme_probability         REAL,
        geomagnetic_risk        REAL,
        geomagnetic_label       TEXT,
        confidence_score        REAL,
        estimated_onset_utc     TIMESTAMPTZ,
        class_probs_json        JSONB,
        model_outputs_json      JSONB
    );

    -- Fired alerts
    CREATE TABLE IF NOT EXISTS flare_alerts (
        alert_id        TEXT PRIMARY KEY,
        pred_id         TEXT REFERENCES flare_predictions(pred_id),
        alert_time      TIMESTAMPTZ NOT NULL,
        severity        TEXT,
        threshold_name  TEXT,
        threshold_value REAL,
        actual_value    REAL,
        message         TEXT,
        dispatched      BOOLEAN DEFAULT FALSE
    );

    -- Indexes for time-range queries
    CREATE INDEX IF NOT EXISTS idx_pred_obs_time  ON flare_predictions(obs_time DESC);
    CREATE INDEX IF NOT EXISTS idx_alert_severity ON flare_alerts(severity);
    """

    def __init__(self):
        self.conn = None

    def connect(self) -> bool:
        if not PG_AVAILABLE:
            logger.info("psycopg2 not installed — PostgreSQL write skipped (simulation mode).")
            return False
        try:
            self.conn = psycopg2.connect(
                host     = DB_CFG["host"],
                port     = DB_CFG["port"],
                dbname   = DB_CFG["name"],
                user     = DB_CFG["user"],
                password = DB_CFG["password"],
                connect_timeout = 5,
            )
            with self.conn.cursor() as cur:
                cur.execute(self.CREATE_TABLES)
            self.conn.commit()
            logger.info("PostgreSQL connected — schema ready.")
            return True
        except Exception as e:
            logger.error(f"PostgreSQL connection failed: {e}")
            return False

    def insert_prediction(self, pred: dict, proc_rec: dict = None) -> str:
        pred_id = "PRED-" + str(uuid.uuid4())[:8].upper()
        if not self.conn:
            logger.info(f"[SIM] INSERT flare_predictions: {pred_id}")
            return pred_id

        try:
            obs_time  = pred.get("obs_time",  utc_now())
            onset_str = pred.get("estimated_onset_utc", utc_now())

            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO flare_predictions
                    (pred_id, obs_time, prediction_time, source,
                     predicted_class, predicted_flux_class,
                     flare_probability, m_class_probability, x_class_probability,
                     cme_probability, geomagnetic_risk, geomagnetic_label,
                     confidence_score, estimated_onset_utc,
                     class_probs_json, model_outputs_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (pred_id) DO NOTHING
                """, (
                    pred_id,
                    obs_time,
                    utc_now(),
                    pred.get("source"),
                    pred.get("predicted_flare_class"),
                    pred.get("predicted_flux_class"),
                    pred.get("flare_probability"),
                    pred.get("m_class_probability"),
                    pred.get("x_class_probability"),
                    pred.get("cme_probability"),
                    pred.get("geomagnetic_risk"),
                    pred.get("geomagnetic_storm_label"),
                    pred.get("confidence_score"),
                    onset_str,
                    json.dumps(pred.get("class_probabilities", {})),
                    json.dumps(pred.get("model_outputs", {})),
                ))
            self.conn.commit()
            logger.info(f"Inserted prediction: {pred_id}")
        except Exception as e:
            logger.error(f"Insert prediction error: {e}")
            self.conn.rollback()

        return pred_id

    def insert_alert(self, alert: dict) -> None:
        if not self.conn:
            logger.info(f"[SIM] INSERT flare_alerts: {alert['alert_id']}")
            return
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO flare_alerts
                    (alert_id, pred_id, alert_time, severity,
                     threshold_name, threshold_value, actual_value, message)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (alert_id) DO NOTHING
                """, (
                    alert["alert_id"], alert["pred_id"], utc_now(),
                    alert["severity"], alert["threshold_name"],
                    alert["threshold_value"], alert["actual_value"],
                    alert["message"],
                ))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Insert alert error: {e}")
            self.conn.rollback()

    def close(self):
        if self.conn:
            self.conn.close()


# ══════════════════════════════════════════════════════════════
# STEP 6 — Alert Engine
# ══════════════════════════════════════════════════════════════

class AlertEngine:

    THRESHOLDS = ALERT_CFG["thresholds"]

    def evaluate(self, pred: dict, pred_id: str) -> list[dict]:
        alerts = []

        checks = [
            ("x_class_probability",  "x_class_critical_pct",  "CRITICAL",
             f"X-Class probability {pred['x_class_probability']*100:.1f}% exceeds "
             f"{self.THRESHOLDS['x_class_critical_pct']}% threshold — Immediate action required."),
            ("m_class_probability",  "m_class_warning_pct",   "WARNING",
             f"M-Class probability {pred['m_class_probability']*100:.1f}% exceeds "
             f"{self.THRESHOLDS['m_class_warning_pct']}% threshold — Heightened monitoring."),
            ("cme_probability",      "cme_high_risk_pct",     "HIGH RISK",
             f"CME probability {pred['cme_probability']*100:.1f}% exceeds "
             f"{self.THRESHOLDS['cme_high_risk_pct']}% — Notify satellite operators."),
            ("geomagnetic_risk",     "geomag_storm_pct",      "STORM WATCH",
             f"Geomagnetic storm risk {pred['geomagnetic_risk']*100:.1f}% — "
             f"{pred['geomagnetic_storm_label']}. Advise power grid operators."),
            ("flare_probability",    "flare_watch_pct",       "WATCH",
             f"Flare probability {pred['flare_probability']*100:.1f}% — general watch active."),
        ]

        for field, thresh_key, severity, msg in checks:
            actual_pct  = pred.get(field, 0.0) * 100
            thresh_pct  = self.THRESHOLDS.get(thresh_key, 100)
            if actual_pct > thresh_pct:
                alert = {
                    "alert_id":       f"ALT-{str(uuid.uuid4())[:8].upper()}",
                    "pred_id":        pred_id,
                    "severity":       severity,
                    "threshold_name": thresh_key,
                    "threshold_value":thresh_pct / 100,
                    "actual_value":   round(actual_pct / 100, 4),
                    "message":        msg,
                }
                alerts.append(alert)
                logger.warning(f"ALERT [{severity}]: {msg}")

        if not alerts:
            logger.info("No thresholds breached — NOMINAL conditions.")

        return alerts

    def dispatch(self, alert: dict) -> None:
        """Send alert to configured channels (log, email, webhook)."""
        for ch in ALERT_CFG["channels"]:
            if not ch.get("enabled"):
                continue
            try:
                if ch["type"] == "email":
                    self._send_email(alert, ch)
                elif ch["type"] == "webhook":
                    self._send_webhook(alert, ch)
            except Exception as e:
                logger.error(f"Alert dispatch [{ch['type']}] failed: {e}")

    def _send_email(self, alert: dict, ch: dict) -> None:
        msg = MIMEText(
            f"Aditya-L1 Space Weather Alert\n\n"
            f"Severity:  {alert['severity']}\n"
            f"Time:      {utc_now()}\n"
            f"Message:   {alert['message']}\n"
            f"Alert ID:  {alert['alert_id']}\n"
        )
        msg["Subject"] = f"[Aditya-L1 SFF] {alert['severity']} — Solar Flare Alert"
        msg["From"]    = ch.get("from_email", "solexs_pipeline@isro.gov.in")
        msg["To"]      = ", ".join(ch.get("recipients", []))

        smtp_host = ch.get("smtp_host", "localhost")
        smtp_port = ch.get("smtp_port", 587)
        smtp_user = ch.get("smtp_username", os.getenv("SMTP_USERNAME", ""))
        smtp_pass = ch.get("smtp_password", os.getenv("SMTP_PASSWORD", ""))

        with smtplib.SMTP(smtp_host, smtp_port) as s:
            if smtp_user and smtp_pass:
                s.starttls()
                s.login(smtp_user, smtp_pass)
            s.sendmail(msg["From"], ch["recipients"], msg.as_string())
        logger.info(f"Alert email sent to {ch['recipients']}")

    def _send_webhook(self, alert: dict, ch: dict) -> None:
        url = ch["url"]
        headers = ch.get("headers", {"Content-Type": "application/json"})
        max_retries = ch.get("max_retries", 3)
        timeout = ch.get("timeout", 10)

        for attempt in range(max_retries):
            try:
                response = requests.post(url, json=alert, headers=headers, timeout=timeout)
                response.raise_for_status()
                logger.info(f"Alert webhook posted to {url} (attempt {attempt+1}/{max_retries})")
                return
            except Exception as e:
                logger.warning(f"Webhook attempt {attempt+1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff
                else:
                    logger.error(f"Webhook failed after {max_retries} attempts")


# ══════════════════════════════════════════════════════════════
# STEP 7 — Dashboard update (stub for WebSocket push)
# ══════════════════════════════════════════════════════════════

def update_dashboard(pred: dict, alerts: list) -> dict:
    """
    In production: push to dashboard via WebSocket or Redis pub/sub.
    This implementation logs and returns the payload.
    Connect to your FastAPI / WebSocket server here.
    """
    payload = {
        "type":       "prediction_update",
        "timestamp":  utc_now(),
        "prediction": {
            "flare_class":     pred.get("predicted_flare_class"),
            "flare_prob_pct":  round(pred.get("flare_probability", 0) * 100, 1),
            "x_prob_pct":      round(pred.get("x_class_probability", 0) * 100, 1),
            "m_prob_pct":      round(pred.get("m_class_probability", 0) * 100, 1),
            "cme_prob_pct":    round(pred.get("cme_probability", 0) * 100, 1),
            "geo_risk":        pred.get("geomagnetic_storm_label"),
            "confidence_pct":  round(pred.get("confidence_score", 0) * 100, 1),
            "onset_utc":       pred.get("estimated_onset_utc"),
        },
        "alert_count": len(alerts),
        "top_severity": alerts[0]["severity"] if alerts else "NOMINAL",
    }

    # TODO: replace with actual WebSocket/Redis push
    # import redis; r = redis.Redis(); r.publish("sff:dashboard", json.dumps(payload))
    # import websockets; await ws.send(json.dumps(payload))

    logger.info(f"Dashboard payload ready: {pred.get('predicted_flare_class')}-class "
                f"| {len(alerts)} alert(s)")
    return payload


# ══════════════════════════════════════════════════════════════
# STEP 8 — Structured JSON Report
# ══════════════════════════════════════════════════════════════

def generate_report(
    run_id:      str,
    acq_result:  dict,
    proc_result: dict,
    feat_result: dict,
    pred_result: dict,
    pred_id:     str,
    alerts:      list,
    elapsed_s:   float,
) -> dict:
    """
    Produces the canonical structured output for automated processing.
    Suitable for: ISRO ops, satellite operators, downstream systems.
    """
    pred = pred_result["predictions"][0] if pred_result.get("predictions") else {}

    report = {
        "run_id":                run_id,
        "timestamp":             utc_now(),
        "pipeline_version":      cfg["pipeline"]["version"],
        "elapsed_seconds":       round(elapsed_s, 2),
        "pipeline_status":       pred_result.get("status", "UNKNOWN"),

        # ── Data acquisition ────────────────────────────────
        "data_acquisition": {
            "source_used":          acq_result.get("source_used"),
            "data_points_processed":acq_result.get("n_records", 0),
            "status":               acq_result.get("status"),
        },

        # ── QC ──────────────────────────────────────────────
        "data_quality": {
            "records_validated":    proc_result.get("records_in", 0),
            "records_passed":       proc_result.get("records_out", 0),
            "warnings":             proc_result.get("warnings", []),
        },

        # ── Core prediction output ───────────────────────────
        "timestamp":             pred.get("obs_time", utc_now()),
        "data_points_processed": acq_result.get("n_records", 0),
        "flare_probability":     f"{pred.get('flare_probability', 0)*100:.1f}%",
        "predicted_flare_class": pred.get("predicted_flare_class", "N/A"),
        "predicted_flux_class":  pred.get("predicted_flux_class", "N/A"),
        "class_probabilities": {
            k: f"{v*100:.1f}%"
            for k, v in pred.get("class_probabilities", {}).items()
        },
        "cme_probability":       f"{pred.get('cme_probability', 0)*100:.1f}%",
        "geomagnetic_risk":      pred.get("geomagnetic_storm_label", "N/A"),
        "geomagnetic_risk_score":f"{pred.get('geomagnetic_risk', 0)*100:.1f}%",
        "confidence_score":      f"{pred.get('confidence_score', 0)*100:.1f}%",
        "estimated_onset_utc":   pred.get("estimated_onset_utc"),
        "onset_window_minutes":  pred.get("onset_window_minutes"),

        # ── AI ensemble ──────────────────────────────────────
        "ai_ensemble": {
            "models": list(pred.get("model_outputs", {}).keys()),
            "weights": pred.get("ensemble_weights", {}),
        },

        # ── Alerts ───────────────────────────────────────────
        "alert_status": alerts[0]["severity"] if alerts else "NOMINAL",
        "active_alerts": [
            {"severity": a["severity"], "message": a["message"]}
            for a in alerts
        ],
        "recommended_action": _recommend(pred, alerts),

        # ── Thresholds ───────────────────────────────────────
        "threshold_evaluation": {
            "x_class_critical_50pct":  pred.get("x_class_probability", 0) > 0.50,
            "m_class_warning_70pct":   pred.get("m_class_probability", 0) > 0.70,
            "cme_high_risk_60pct":     pred.get("cme_probability", 0) > 0.60,
            "geomag_storm_55pct":      pred.get("geomagnetic_risk", 0) > 0.55,
        },

        # ── System health ────────────────────────────────────
        "system_health": {
            "pipeline_ok":   True,
            "prediction_id": pred_id,
            "db_write":      "SIMULATED" if not PG_AVAILABLE else "POSTGRES",
            "dashboard":     "PAYLOAD_READY",
        },
    }

    return report


def _recommend(pred: dict, alerts: list) -> str:
    if not alerts:
        return ("Continue standard 5-minute cron monitoring cadence. "
                "No immediate action required.")
    top = alerts[0]["severity"]
    if top == "CRITICAL":
        return ("IMMEDIATE ACTION: Initiate satellite safe-mode protocols. "
                "Notify all LEO/GEO satellite operators. Issue ISRO public advisory. "
                "Mobilise Udaipur Solar Observatory backup observations.")
    if top in ("WARNING", "HIGH RISK"):
        return ("ELEVATED WATCH: Increase sampling to 1-min cadence. "
                "Brief satellite operations team. Standby for SEP event protocol. "
                "Notify power grid operators for potential geomagnetic disturbance.")
    if top == "STORM WATCH":
        return ("STORM WATCH: Alert power grid operators and GNSS service providers. "
                "Monitor Kp index continuously. Prepare geomagnetic storm contingency.")
    return ("WATCH: Monitor more frequently. No satellite action needed yet. "
            "Brief on-call space weather duty officer.")


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════

def run(
    acq_result:  dict,
    proc_result: dict,
    feat_result: dict,
    pred_result: dict,
    elapsed_s:   float,
) -> dict:
    import time
    logger.info("=" * 60)
    logger.info("STEPS 5–8 — SAVE · ALERT · DASHBOARD · REPORT")

    run_id = "RUN-" + str(uuid.uuid4())[:8].upper()
    db     = PostgresWriter()
    db_ok  = db.connect()
    engine = AlertEngine()

    all_alerts = []
    pred_ids   = []

    for pred in pred_result.get("predictions", []):
        pred_id = db.insert_prediction(pred)
        pred_ids.append(pred_id)

        alerts = engine.evaluate(pred, pred_id)
        for a in alerts:
            db.insert_alert(a)
            engine.dispatch(a)
        all_alerts.extend(alerts)

    dash_payload = update_dashboard(
        pred_result["predictions"][0] if pred_result.get("predictions") else {},
        all_alerts
    )

    report = generate_report(
        run_id, acq_result, proc_result, feat_result,
        pred_result, pred_ids[0] if pred_ids else "N/A",
        all_alerts, elapsed_s
    )

    out_path = Path("data") / "reports" / f"report_{utc_now().replace(':','-').replace(' ','T')}.json"
    save_json(report, out_path)

    db.close()
    state = PipelineState.load()
    state["last_report_file"] = str(out_path)
    state["last_run_id"]      = run_id
    PipelineState.save(state)

    logger.info(f"Run {run_id} complete — status: {report['alert_status']}")
    return report


if __name__ == "__main__":
    print("Run via 00_run_pipeline.py")
