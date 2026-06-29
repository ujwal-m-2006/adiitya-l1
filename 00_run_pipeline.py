#!/usr/bin/env python3
"""
00_run_pipeline.py
==================
Aditya-L1 Solar Flare Forecasting Pipeline — Master Entry Point

This is the script called directly by crontab:

    */5 * * * * /opt/aditya_l1/venv/bin/python \
        /opt/aditya_l1/scripts/00_run_pipeline.py \
        >> /opt/aditya_l1/logs/cron.log 2>&1

Orchestrates all 8 pipeline steps in sequence:
  1. Data Acquisition      (01_data_acquisition.py)
  2. Validation & QC       (02_preprocess.py)
  3. Feature Engineering   (03_feature_engineer.py)
  4. AI Ensemble Predict   (04_ai_predict.py)
  5. PostgreSQL Save        ─┐
  6. Alert Evaluation        ├─ (05_save_alert_report.py)
  7. Dashboard Update        │
  8. JSON Report             ┘

On failure at any step: logs error, saves partial state, exits cleanly.
"""

import sys
import json
import time
import traceback
from pathlib import Path

# Add pipeline root to path
sys.path.insert(0, str(Path(__file__).parent))

from pipeline_utils import setup_logger, load_config, utc_now, PipelineState

cfg    = load_config()
logger = setup_logger("pipeline", cfg["pipeline"]["log_level"])


def step(name: str, fn, *args, **kwargs):
    """Run a pipeline step with timing, retry, and error handling."""
    max_retries = cfg["pipeline"]["max_retries"]
    delay       = cfg["pipeline"]["retry_delay_seconds"]

    for attempt in range(1, max_retries + 1):
        try:
            t0     = time.time()
            result = fn(*args, **kwargs)
            elapsed = round(time.time() - t0, 2)
            logger.info(f"[{name}] completed in {elapsed}s (attempt {attempt})")
            return result, elapsed
        except Exception as e:
            logger.error(f"[{name}] attempt {attempt}/{max_retries} failed: {e}")
            logger.debug(traceback.format_exc())
            if attempt < max_retries:
                logger.info(f"Retrying in {delay}s...")
                time.sleep(delay)
            else:
                raise RuntimeError(f"Step [{name}] failed after {max_retries} attempts: {e}")


def main():
    run_start = time.time()
    logger.info("=" * 70)
    logger.info(f"ADITYA-L1 SFF PIPELINE — CRON TRIGGER — {utc_now()}")
    logger.info("=" * 70)

    total_elapsed = 0.0

    try:
        # ── Step 1: Data Acquisition ───────────────────────────
        import importlib
        acq_mod  = importlib.import_module("01_data_acquisition")
        proc_mod = importlib.import_module("02_preprocess")
        feat_mod = importlib.import_module("03_feature_engineer")
        pred_mod = importlib.import_module("04_ai_predict")
        rept_mod = importlib.import_module("05_save_alert_report")

        acq_result, t1 = step("ACQUISITION", acq_mod.run)
        total_elapsed += t1

        if acq_result.get("status") == "NO_NEW_DATA":
            logger.info("No new data since last run — pipeline exits cleanly.")
            return

        if acq_result.get("status") == "FAILED":
            logger.error("Acquisition failed — aborting pipeline.")
            sys.exit(1)

        # ── Step 2: Preprocessing ──────────────────────────────
        proc_result, t2 = step("PREPROCESSING", proc_mod.run, acq_result)
        total_elapsed += t2

        if proc_result.get("records_out", 0) == 0:
            logger.error("No valid records after preprocessing — aborting.")
            sys.exit(1)

        # ── Step 3: Feature Engineering ────────────────────────
        feat_result, t3 = step("FEATURES", feat_mod.run, proc_result)
        total_elapsed += t3

        if feat_result.get("n_sets", 0) == 0:
            logger.error("Feature extraction produced no outputs — aborting.")
            sys.exit(1)

        # ── Step 4: AI Ensemble Prediction ────────────────────
        pred_result, t4 = step("AI_PREDICT", pred_mod.run, feat_result)
        total_elapsed += t4

        # ── Steps 5–8: Save, Alert, Dashboard, Report ──────────
        report, t5 = step("REPORT", rept_mod.run,
                           acq_result, proc_result, feat_result, pred_result,
                           total_elapsed)
        total_elapsed = round(time.time() - run_start, 2)

        # ── Final output (stdout captured by cron) ─────────────
        print(json.dumps(report, indent=2))

        logger.info("=" * 70)
        logger.info(f"PIPELINE COMPLETE — {report.get('alert_status')} — {total_elapsed}s total")
        logger.info("=" * 70)

    except Exception as e:
        logger.critical(f"PIPELINE ABORT: {e}")
        logger.debug(traceback.format_exc())

        # Save failure state so next cron run knows
        state = PipelineState.load()
        state["last_failure"]      = utc_now()
        state["last_failure_msg"]  = str(e)
        PipelineState.save(state)

        # Minimal failure report
        failure_report = {
            "timestamp":        utc_now(),
            "pipeline_status":  "FAILED",
            "error":            str(e),
            "alert_status":     "PIPELINE_ERROR",
            "recommended_action": "Check logs. Verify PRADAN/NOAA connectivity. Restart cron if needed.",
        }
        print(json.dumps(failure_report, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
