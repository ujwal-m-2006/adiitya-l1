
#!/usr/bin/env python3
"""
Aditya-L1 Solar Flare Forecasting Dashboard
A simple Flask dashboard to visualize predictions and alerts
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from flask import Flask, render_template, jsonify
from pipeline_utils import load_config

# Explicitly set template folder
template_dir = project_root / "dashboard" / "templates"
app = Flask(__name__, template_folder=str(template_dir))
cfg = load_config()


def get_latest_raw_data_file():
    # First try real data directory
    raw_dir = project_root / "data" / "raw"
    if raw_dir.exists():
        raw_files = list(raw_dir.glob("raw_*.json"))
        if raw_files:
            return sorted(raw_files, key=lambda x: x.stat().st_mtime, reverse=True)[0]
    # Fallback to sample data
    sample_raw_dir = project_root / "sample_data" / "raw"
    if sample_raw_dir.exists():
        raw_files = list(sample_raw_dir.glob("raw_*.json"))
        if raw_files:
            return sorted(raw_files, key=lambda x: x.stat().st_mtime, reverse=True)[0]
    return None


def load_all_reports():
    reports = []
    # First try real data directory
    reports_dir = project_root / "data" / "reports"
    if reports_dir.exists():
        for report_file in sorted(reports_dir.glob("report_*.json"), reverse=True):
            try:
                with open(report_file, "r") as f:
                    report = json.load(f)
                    reports.append(report)
            except Exception:
                pass
    # If no real reports, use sample data
    if not reports:
        sample_reports_dir = project_root / "sample_data" / "reports"
        if sample_reports_dir.exists():
            for report_file in sorted(sample_reports_dir.glob("report_*.json"), reverse=True):
                try:
                    with open(report_file, "r") as f:
                        report = json.load(f)
                        reports.append(report)
                except Exception:
                    pass
    return reports


def load_latest_report():
    reports = load_all_reports()
    return reports[0] if reports else None


def format_prediction_from_report(report):
    if not report:
        return None
    return {
        "pred_id": report.get("run_id", "N/A"),
        "obs_time": report.get("timestamp", ""),
        "prediction_time": report.get("timestamp", ""),
        "source": report.get("data_acquisition", {}).get("source_used", ""),
        "predicted_class": report.get("predicted_flare_class", ""),
        "predicted_flux_class": report.get("predicted_flux_class", ""),
        "flare_probability": float(report.get("flare_probability", "0%").strip("%")) / 100,
        "m_class_probability": float(report.get("class_probabilities", {}).get("M", "0%").strip("%")) / 100,
        "x_class_probability": float(report.get("class_probabilities", {}).get("X", "0%").strip("%")) / 100,
        "cme_probability": float(report.get("cme_probability", "0%").strip("%")) / 100,
        "geomagnetic_risk": float(report.get("geomagnetic_risk_score", "0%").strip("%")) / 100,
        "geomagnetic_label": report.get("geomagnetic_risk", ""),
        "confidence_score": float(report.get("confidence_score", "0%").strip("%")) / 100,
        "estimated_onset_utc": report.get("estimated_onset_utc", ""),
        "class_probs_json": report.get("class_probabilities", {}),
        "model_outputs_json": report.get("ai_ensemble", {})
    }


@app.route('/')
def index():
    index_file = template_dir / "index.html"
    with open(index_file, "r", encoding="utf-8") as f:
        html_content = f.read()
    return app.response_class(html_content, mimetype="text/html")


@app.route('/hello')
def hello():
    return "Hello, World! The app is working!"


@app.route('/api/predictions')
def get_predictions():
    reports = load_all_reports()
    predictions = [format_prediction_from_report(r) for r in reports[:50]]
    return jsonify([p for p in predictions if p is not None])


@app.route('/api/alerts')
def get_alerts():
    reports = load_all_reports()
    alerts = []
    for report in reports:
        if report.get("active_alerts"):
            for alert in report["active_alerts"]:
                alerts.append({
                    "alert_id": f"ALT_{report.get('run_id', '')}",
                    "pred_id": report.get("run_id", ""),
                    "alert_time": report.get("timestamp", ""),
                    "severity": alert.get("severity", ""),
                    "threshold_name": "",
                    "threshold_value": 0,
                    "actual_value": 0,
                    "message": alert.get("message", ""),
                    "dispatched": False,
                    "predicted_class": report.get("predicted_flare_class", ""),
                    "flare_probability": float(report.get("flare_probability", "0%").strip("%")) / 100
                })
    return jsonify(alerts[:50])


@app.route('/api/latest')
def get_latest():
    latest_report = load_latest_report()
    prediction = format_prediction_from_report(latest_report)
    alerts = []
    if latest_report and latest_report.get("active_alerts"):
        for alert in latest_report["active_alerts"]:
            alerts.append({
                "alert_id": f"ALT_{latest_report.get('run_id', '')}",
                "pred_id": latest_report.get("run_id", ""),
                "alert_time": latest_report.get("timestamp", ""),
                "severity": alert.get("severity", ""),
                "threshold_name": "",
                "threshold_value": 0,
                "actual_value": 0,
                "message": alert.get("message", ""),
                "dispatched": False,
                "predicted_class": latest_report.get("predicted_flare_class", ""),
                "flare_probability": float(latest_report.get("flare_probability", "0%").strip("%")) / 100
            })
    return jsonify({
        "prediction": prediction,
        "alerts": alerts[:5]
    })


@app.route('/api/raw-noaa-data')
def get_raw_noaa_data():
    raw_file = get_latest_raw_data_file()
    if not raw_file:
        return jsonify({"error": "No raw data file found"}), 404
    try:
        with open(raw_file, "r") as f:
            raw_data = json.load(f)
        return jsonify(raw_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    # Install Flask if not available
    try:
        from flask import Flask
    except ImportError:
        print("Flask not found, installing...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "flask"])
        from flask import Flask

    app.run(debug=True, host='0.0.0.0', port=5000)
