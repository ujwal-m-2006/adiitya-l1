# Aditya-L1 Solar Flare Forecasting Pipeline

ISRO Space Weather Monitoring Platform — SoLEXS + HEL1OS Data Pipeline

---

## Directory Structure

```
aditya_l1_pipeline/
├── config/
│   └── config.yaml              # All settings — instruments, models, thresholds, DB
├── scripts/
│   ├── pipeline_utils.py        # Shared helpers, logging, state management
│   ├── 00_run_pipeline.py       # ← Master entry point (called by cron)
│   ├── 01_data_acquisition.py   # PRADAN FITS fetch + NOAA SWPC fallback
│   ├── 02_preprocess.py         # Validation, QC, normalisation, HEL1OS derivation
│   ├── 03_feature_engineer.py   # 17-dimensional AI feature vector extraction
│   ├── 04_ai_predict.py         # LSTM / GRU / Transformer / XGBoost ensemble
│   └── 05_save_alert_report.py  # PostgreSQL write, alerts, dashboard, JSON report
├── models/                      # Saved model weights (place .pt / .json files here)
│   ├── lstm_v1.pt
│   ├── gru_v1.pt
│   ├── transformer_v1.pt
│   └── xgboost_v1.json
├── data/
│   ├── raw/                     # Raw JSON from PRADAN/NOAA (retained 90 days)
│   ├── processed/               # Validated + normalised records
│   ├── features/                # Feature vectors and sequences
│   └── reports/                 # Structured JSON reports
└── logs/                        # Daily rotating logs per module
```

---

## Quick Start

### 1. Install Dependencies

```bash
python3 -m venv venv
source venv/bin/activate

pip install \
    requests \
    numpy \
    scipy \
    pyyaml \
    astropy \
    psycopg2-binary \
    torch \
    xgboost
```

For GPU training (optional):
```bash
pip install torch --index-url https://download.pytorch.org/whl/cu118
```

---

### 2. Configure Environment Variables

Create a `.env` file (never commit this):

```bash
# ISRO PRADAN Portal (register at https://pradan.issdc.gov.in)
export PRADAN_USERNAME="your_username"
export PRADAN_PASSWORD="your_password"

# PostgreSQL
export DB_HOST="localhost"
export DB_PORT="5432"
export DB_NAME="aditya_l1_sff"
export DB_USER="sff_pipeline"
export DB_PASSWORD="your_db_password"

# Optional alerts
export SMTP_HOST="smtp.isro.gov.in"
export ALERT_WEBHOOK_URL="https://your-dashboard/api/alerts"
```

Source before running: `source .env`

---

### 3. Set Up PostgreSQL

```sql
CREATE DATABASE aditya_l1_sff;
CREATE USER sff_pipeline WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE aditya_l1_sff TO sff_pipeline;
```

Tables are created automatically on first pipeline run (idempotent).

---

### 4. Run Manually

```bash
# Full pipeline (all 8 steps)
python scripts/00_run_pipeline.py

# Individual steps (for debugging)
python scripts/01_data_acquisition.py
python scripts/02_preprocess.py
python scripts/03_feature_engineer.py
python scripts/04_ai_predict.py
```

---

### 5. Install Cron Job

```bash
crontab -e
```

Add this line:

```cron
*/5 * * * * source /opt/aditya_l1/.env && \
    /opt/aditya_l1/venv/bin/python \
    /opt/aditya_l1/scripts/00_run_pipeline.py \
    >> /opt/aditya_l1/logs/cron.log 2>&1

# Nightly model retrain at 2 AM
0 2 * * * source /opt/aditya_l1/.env && \
    /opt/aditya_l1/venv/bin/python \
    /opt/aditya_l1/scripts/retrain_models.py \
    >> /opt/aditya_l1/logs/retrain.log 2>&1
```

---

## Data Sources

| Source | What it provides | Access |
|---|---|---|
| **ISRO PRADAN** | Native SoLEXS L1 FITS + HEL1OS L1 FITS | Register: pradan.issdc.gov.in |
| **NOAA SWPC GOES XRS** | 1–8 Å soft X-ray (SoLEXS Band 1 proxy) | Public JSON, no auth |
| **NOAA Kp index** | Planetary geomagnetic index | Public JSON |
| **NOAA Solar Wind** | ACE/DSCOVR plasma + IMF | Public JSON |

**When PRADAN credentials are set**, the pipeline uses native Aditya-L1 data.
**When not**, it falls back automatically to NOAA SWPC (real-time, no auth required).

---

## Feature Vector (17 dimensions)

| Index | Name | Source |
|---|---|---|
| 0 | log10_soft_flux | SoLEXS 1–8 Å |
| 1 | log10_soft_peak_60min | SoLEXS |
| 2 | log10_soft_0_4A | SoLEXS 0.5–4 Å |
| 3 | flux_ratio_short_long | SoLEXS ratio |
| 4 | dFdt_norm | Flux rise rate |
| 5 | d2Fdt2_norm | Flux acceleration |
| 6 | log10_hard_20_60keV | HEL1OS |
| 7 | log10_hard_60_100keV | HEL1OS |
| 8 | flux_ratio_hard_soft | HEL1OS / SoLEXS |
| 9 | spectral_gamma_norm | Spectral index |
| 10 | kp_index_norm | Kp / 9 |
| 11 | solar_wind_speed_norm | km/s / 1000 |
| 12 | solar_wind_density_norm | n/cc / 50 |
| 13 | imf_bz_norm | IMF Bz / 20 nT |
| 14 | flux_percentile_24h | Rank in 24h distribution |
| 15 | rolling_mean_15min_norm | 15-min rolling mean |
| 16 | rolling_std_15min | 15-min rolling std dev |

---

## Alert Thresholds

| Condition | Threshold | Severity |
|---|---|---|
| X-Class probability | > 50% | 🔴 CRITICAL |
| M-Class probability | > 70% | 🟠 WARNING |
| CME probability | > 60% | 🟠 HIGH RISK |
| Geomagnetic storm | > 55% | 🟡 STORM WATCH |
| General flare probability | > 40% | 🟡 WATCH |

Thresholds are configurable in `config/config.yaml`.

---

## Adding Trained Models

Place saved weights in `models/`:

```python
# LSTM / GRU / Transformer: PyTorch state_dict
torch.save(model.state_dict(), "models/lstm_v1.pt")

# XGBoost: native JSON format
bst.save_model("models/xgboost_v1.json")
```

The pipeline auto-detects and loads weights. Falls back to physics
surrogate models if weights are absent (safe for initial deployment).

---

## JSON Output Schema

Every cron run writes a structured JSON report:

```json
{
  "run_id": "RUN-A1B2C3D4",
  "timestamp": "2026-06-25T12:00:00Z",
  "pipeline_status": "SUCCESS",
  "data_points_processed": 312,
  "flare_probability": "73.4%",
  "predicted_flare_class": "M",
  "predicted_flux_class": "M2.3",
  "class_probabilities": {"A": "3.1%", "B": "8.2%", "C": "15.3%", "M": "52.1%", "X": "21.3%"},
  "cme_probability": "64.2%",
  "geomagnetic_risk": "HIGH (G3)",
  "confidence_score": "81.0%",
  "estimated_onset_utc": "2026-06-25T14:23:00Z",
  "alert_status": "WARNING",
  "recommended_action": "..."
}
```
