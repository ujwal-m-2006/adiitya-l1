#!/usr/bin/env python3
"""
06_historical_data.py
=====================
Aditya-L1 SFF Pipeline — Historical Data Collector

Downloads NOAA GOES XRS archive data + confirmed flare events
to build a labeled training dataset for the AI ensemble.

Data sources:
  • NOAA SWPC GOES XRS 1-min averages (historical daily files)
  • NOAA SWPC confirmed flare event list (7-day rolling + archive)
  • NOAA Kp index archive
  • NOAA solar wind archive (ACE/DSCOVR)

Usage:
  python 06_historical_data.py                    # Download last 30 days
  python 06_historical_data.py --days 180         # Download last 180 days
  python 06_historical_data.py --start 2025-01-01 --end 2025-06-01

Output: data/historical/training_dataset.json
"""

import os
import sys
import json
import math
import time
import argparse
import requests
import numpy as np
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from pipeline_utils import (
    load_config, setup_logger, save_json, load_json, utc_now, classify_flux
)

cfg = load_config()
logger = setup_logger("historical", cfg["pipeline"]["log_level"])
HIST_DIR = Path("data/historical")
HIST_DIR.mkdir(parents=True, exist_ok=True)

TIMEOUT = 30
HEADERS = {"User-Agent": "Aditya-L1-SFF-Pipeline/1.0"}

# ══════════════════════════════════════════════════════════════
# PRADAN Historical Data Fetcher
# ══════════════════════════════════════════════════════════════

class PRADANHistorical:
    """Downloads and parses PRADAN historical data for training."""

    PORTAL_URL   = "https://pradan1.issdc.gov.in/al1/"
    BROWSE_URL  = "https://pradan1.issdc.gov.in/al1/protected/browse.xhtml"
    KEYCLOAK_URL = "https://idp.issdc.gov.in/auth/realms/issdc"
    USERNAME   = os.getenv("PRADAN_USERNAME", "")
    PASSWORD   = os.getenv("PRADAN_PASSWORD", "")

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Aditya-L1-SFF-Pipeline/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self.logged_in = False

    def login(self) -> bool:
        if not self.USERNAME or not self.PASSWORD:
            logger.warning("PRADAN credentials not set — skipping PRADAN data fetch.")
            return False

        try:
            import re

            r = self.session.get(self.PORTAL_URL, timeout=20, allow_redirects=True)
            logger.debug(f"Portal access: {r.status_code}, URL: {r.url[:100]}")

            # Find login form action
            action_match = re.search(
                r'<form[^>]*id=["\']kc-form-login["\'][^>]*action=["\']([^"\']+)["\']',
                r.text, re.IGNORECASE
            )
            if not action_match:
                # Check if already logged in
                if "payload" in r.url.lower() or "protected" in r.text.lower():
                    self.logged_in = True
                    logger.info("PRADAN: already logged in.")
                    return True
                logger.warning("Could not find login form in PRADAN response.")
                return False

            login_url = action_match.group(1)
            login_url = login_url.replace("&amp;", "&")

            # Post credentials
            login_data = {
                "username": self.USERNAME,
                "password": self.PASSWORD,
                "credentialId": "",
            }
            r = self.session.post(
                login_url, data=login_data, timeout=30, allow_redirects=True,
                headers={"Referer": r.url, "Content-Type": "application/x-www-form-urlencoded"}
            )

            # Verify login
            r2 = self.session.get(self.PORTAL_URL, timeout=15, allow_redirects=True)
            self.logged_in = (
                r2.status_code == 200 and
                "login" not in r2.url.lower() and
                "authenticate" not in r2.url.lower()
            )

            if self.logged_in:
                logger.info("PRADAN Keycloak login successful.")
            else:
                logger.warning("PRADAN login verification failed.")

            return self.logged_in
        except Exception as e:
            logger.error(f"PRADAN login error: {e}")
            self.logged_in = False
            return False

    def fetch_historical_files(self, instrument: str, days: int = 30) -> list[dict]:
        """Fetch historical files for a given instrument.
        Returns list of file info dicts.
        """
        if not self.logged_in:
            return []

        inst_id = {"SoLEXS": "solexs", "HEL1OS": "hel1os"}.get(instrument, "")
        if not inst_id:
            return []

        url = f"{self.BROWSE_URL}?id={inst_id}"
        try:
            r = self.session.get(url, timeout=20)
            r.raise_for_status()

            import re
            files = []
            since = datetime.now(timezone.utc) - timedelta(days=days)

            for m in re.finditer(r'<tr[^>]*>(.*?)</tr>', r.text, re.DOTALL):
                cells = re.findall(r'<td[^>]*>(.*?)</td>', m.group(1), re.DOTALL)
                if not cells:
                    continue

                text = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
                text = [t for t in text if t]
                if len(text) < 5 or not text[0].isdigit():
                    continue

                fname = text[1]
                try:
                    obs_date_str = text[2]
                    # Try different date formats
                    for fmt in ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"]:
                        try:
                            obs_date = datetime.strptime(obs_date_str, fmt).replace(tzinfo=timezone.utc)
                            break
                        except ValueError:
                            continue
                    else:
                        continue
                except (ValueError, IndexError):
                    continue

                if obs_date < since:
                    continue

                dl_match = re.search(
                    rf'href="(/[^"]*downloadData/{inst_id}/[^"]*{re.escape(fname)}[^"]*)"',
                    r.text, re.IGNORECASE
                )
                dl_url = dl_match.group(1) if dl_match else f"{self.PORTAL_URL}/protected/downloadData/{inst_id}/level1/{obs_date.year}/{obs_date.strftime("%m")}/N00_0000/{fname}?{inst_id}"
                if not dl_url.startswith("http"):
                    dl_url = f"https://pradan1.issdc.gov.in{dl_url}"

                files.append({
                    "filename": fname,
                    "url": dl_url,
                    "obs_date": obs_date_str,
                    "size_kb": float(text[4]) if text[4] else 0,
                })

            logger.info(f"PRADAN: Found {len(files)} {instrument} files from last {days} days")
            return files
        except Exception as e:
            logger.error(f"PRADAN browse error ({instrument}): {e}")
            return []

# ══════════════════════════════════════════════════════════════
# NOAA Historical Data Fetcher
# ══════════════════════════════════════════════════════════════

class NOAAHistorical:
    """Downloads and parses NOAA SWPC historical data for training."""

    # Current real-time endpoints (also serve recent history)
    XRAY_6H   = cfg["data"]["noaa_fallback"]["xray_6h"]
    XRAY_1D   = cfg["data"]["noaa_fallback"]["xray_1d"]
    FLARES_7D = cfg["data"]["noaa_fallback"]["flares_7d"]
    KP_INDEX  = cfg["data"]["noaa_fallback"]["kp_index"]
    SW_PLASMA = cfg["data"]["noaa_fallback"]["solar_wind_plasma"]
    SW_MAG    = cfg["data"]["noaa_fallback"]["solar_wind_mag"]
    SCALES    = cfg["data"]["noaa_fallback"]["scales"]

    # NOAA archived daily XRS: https://services.swpc.noaa.gov/json/goes/primary/
    XRAY_DAILY_URL = "https://services.swpc.noaa.gov/json/goes/primary/xrays-1-day.json"

    def _get(self, url: str, retries: int = 2) -> Optional[list | dict]:
        for attempt in range(retries + 1):
            try:
                r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                logger.warning(f"Fetch attempt {attempt+1} failed [{url}]: {e}")
                if attempt < retries:
                    time.sleep(2)
        return None

    def fetch_xray_timeseries(self) -> dict:
        """
        Fetch 1-day GOES XRS data (1-min cadence, ~1440 points).
        Returns parsed long + short channel timeseries.
        """
        data = self._get(self.XRAY_1D)
        if not data:
            return {"long_ch": [], "short_ch": [], "n_records": 0}

        long_ch  = [d for d in data if d.get("energy") in ("1.0-8.0A", "0.1-0.8nm")]
        short_ch = [d for d in data if d.get("energy") in ("0.5-4.0A", "0.05-0.4nm")]

        def parse_channel(records):
            points = []
            for d in records:
                flux = d.get("flux")
                if flux and float(flux) > 0:
                    points.append({
                        "time": d.get("time_tag", ""),
                        "flux": float(flux),
                    })
            return points

        long_pts  = parse_channel(long_ch)
        short_pts = parse_channel(short_ch)
        logger.info(f"XRS: {len(long_pts)} long-ch, {len(short_pts)} short-ch points")

        return {
            "long_ch":  long_pts,
            "short_ch": short_pts,
            "n_records": len(long_pts),
        }

    def fetch_flare_events(self) -> list[dict]:
        """Fetch last 7 days of confirmed GOES flare events."""
        data = self._get(self.FLARES_7D)
        if not data:
            return []

        events = []
        for ev in data:
            cls = ev.get("max_class", "")
            peak_flux = ev.get("max_xrlong", None)
            if not cls:
                continue

            # Parse class letter and magnitude
            class_letter = cls[0] if cls else "C"
            try:
                class_val = float(cls[1:]) if len(cls) > 1 else 1.0
            except ValueError:
                class_val = 1.0

            events.append({
                "begin_time":    ev.get("begin_time"),
                "peak_time":     ev.get("peak_time"),
                "end_time":      ev.get("end_time"),
                "flare_class":   class_letter,
                "class_value":   class_val,
                "peak_flux_Wm2": float(peak_flux) if peak_flux else None,
                "region":        ev.get("linked_region"),
            })

        logger.info(f"Flare events: {len(events)} confirmed in last 7 days")
        return events

    def fetch_kp_history(self) -> list[dict]:
        """Fetch Kp index history (3-hour cadence)."""
        data = self._get(self.KP_INDEX)
        if not data:
            return []

        records = []
        for r in data:
            # Handle both dict and list formats from NOAA
            if isinstance(r, dict):
                kp_val = r.get("Kp") or r.get("kp_index") or r.get("kp")
                time_val = r.get("time_tag") or r.get("time")
            elif isinstance(r, (list, tuple)) and len(r) > 1:
                time_val, kp_val = r[0], r[1]
            else:
                continue

            if kp_val is not None:
                try:
                    records.append({
                        "time": str(time_val),
                        "kp":   float(kp_val),
                    })
                except (ValueError, TypeError):
                    pass

        logger.info(f"Kp index: {len(records)} records")
        return records

    def fetch_solar_wind_history(self) -> dict:
        """Fetch recent solar wind plasma + IMF data."""
        plasma = self._get(self.SW_PLASMA)
        mag    = self._get(self.SW_MAG)

        result = {"plasma": [], "mag": []}

        if plasma and len(plasma) > 1:
            for row in plasma[1:]:
                try:
                    result["plasma"].append({
                        "time":       row[0],
                        "density":    float(row[1]) if len(row) > 1 and row[1] else None,
                        "speed":      float(row[2]) if len(row) > 2 and row[2] else None,
                        "temperature": float(row[3]) if len(row) > 3 and row[3] else None,
                    })
                except (ValueError, TypeError, IndexError):
                    pass

        if mag and len(mag) > 1:
            for row in mag[1:]:
                try:
                    result["mag"].append({
                        "time":   row[0],
                        "bz_nT":  float(row[3]) if len(row) > 3 and row[3] else None,
                        "bt_nT":  float(row[6]) if len(row) > 6 and row[6] else None,
                    })
                except (ValueError, TypeError, IndexError):
                    pass

        logger.info(f"Solar wind: {len(result['plasma'])} plasma, {len(result['mag'])} mag records")
        return result


# ══════════════════════════════════════════════════════════════
# Training Dataset Builder
# ══════════════════════════════════════════════════════════════

class TrainingDatasetBuilder:
    """
    Converts raw historical data into labeled training samples.

    Each sample = 60-step sequence of 17 features + label (flare class).
    Labels are derived from confirmed flare events:
      - "positive" sample: 60 min window leading up to a flare peak
      - "negative" sample: 60 min window during quiet periods
    """

    SEQ_LEN = cfg["models"]["sequence_length"]  # 60
    FEAT_DIM = cfg["models"]["feature_dim"]      # 17
    LOG_FLUX_MIN = -9.0
    LOG_FLUX_MAX = -3.0

    def safe_log10(self, v: float) -> float:
        return math.log10(max(v, 1e-12)) if v and v > 0 else self.LOG_FLUX_MIN

    def norm_log_flux(self, v: float) -> float:
        lv = self.safe_log10(v)
        return max(0.0, min(1.0, (lv - self.LOG_FLUX_MIN) / (self.LOG_FLUX_MAX - self.LOG_FLUX_MIN)))

    def build_sequences(self, xray_data: dict, flare_events: list,
                        kp_data: list, sw_data: dict) -> dict:
        """
        Build labeled training sequences from historical data.
        Returns {sequences: [...], labels: [...], metadata: {...}}
        """
        long_ch = xray_data.get("long_ch", [])
        short_ch = xray_data.get("short_ch", [])

        if len(long_ch) < self.SEQ_LEN:
            logger.warning(f"Only {len(long_ch)} XRS points — need at least {self.SEQ_LEN}")
            return {"sequences": [], "labels": [], "n_samples": 0}

        # Build flux array with timestamps
        flux_series = [(p["time"], p["flux"]) for p in long_ch]
        short_map = {}
        for p in short_ch:
            # Match short channel to nearest long channel time
            short_map[p["time"]] = p["flux"]

        # Build Kp lookup (nearest 3-hour value)
        kp_map = {}
        for r in kp_data:
            kp_map[r["time"][:13]] = r["kp"]  # hour-level key

        # Build solar wind lookup
        sw_speed_map = {}
        sw_den_map = {}
        bz_map = {}
        for r in sw_data.get("plasma", []):
            key = r["time"][:13] if r.get("time") else None
            if key:
                sw_speed_map[key] = r.get("speed")
                sw_den_map[key] = r.get("density")
        for r in sw_data.get("mag", []):
            key = r["time"][:13] if r.get("time") else None
            if key:
                bz_map[key] = r.get("bz_nT")

        # Identify flare windows (positive samples)
        flare_windows = set()
        label_map = {}
        for ev in flare_events:
            peak = ev.get("peak_time", "")
            if peak:
                label_map[peak[:16]] = ev["flare_class"]  # minute-level key
                flare_windows.add(peak[:16])

        # Class encoding
        class_to_idx = {"A": 0, "B": 1, "C": 2, "M": 3, "X": 4}

        sequences = []
        labels = []
        sample_meta = []
        n_positive = 0
        n_negative = 0

        # Slide window across the timeseries
        step = max(1, len(flux_series) // 200)  # ~200 samples max from one day
        for i in range(self.SEQ_LEN, len(flux_series), step):
            window = flux_series[i - self.SEQ_LEN:i]
            current_time = window[-1][0]
            time_key = current_time[:16]

            # Determine label
            if time_key in flare_windows:
                label_class = label_map.get(time_key, "C")
                label_idx = class_to_idx.get(label_class, 2)
                is_positive = True
                n_positive += 1
            else:
                # Check if any flare in next 60 min → "pre-flare" positive
                upcoming = False
                for ev in flare_events:
                    peak = ev.get("peak_time", "")
                    if peak:
                        try:
                            fmt = "%Y-%m-%dT%H:%M:%SZ"
                            t_now = datetime.strptime(current_time, fmt)
                            t_peak = datetime.strptime(peak, fmt)
                            delta_min = (t_peak - t_now).total_seconds() / 60
                            if 0 < delta_min <= 60:
                                upcoming = True
                                label_class = ev["flare_class"]
                                label_idx = class_to_idx.get(label_class, 2)
                                n_positive += 1
                                is_positive = True
                                break
                        except (ValueError, TypeError):
                            pass

                if not upcoming:
                    # Classify by current flux level for quiet-time labels
                    current_flux = window[-1][1]
                    cls, _ = classify_flux(current_flux)
                    label_class = cls
                    label_idx = class_to_idx.get(cls, 2)
                    is_positive = False
                    n_negative += 1

            # Build 17D feature vector for each timestep
            seq = []
            for t_idx, (t, flux) in enumerate(window):
                short_flux = short_map.get(t, flux * 0.32)
                ratio = short_flux / max(flux, 1e-12)

                # Derivatives (approximate)
                if t_idx > 0:
                    prev_flux = window[t_idx - 1][1]
                    dFdt = (flux - prev_flux)
                else:
                    dFdt = 0.0

                if t_idx > 1:
                    prev2 = window[t_idx - 2][1]
                    prev1 = window[t_idx - 1][1]
                    d2Fdt2 = flux - 2 * prev1 + prev2
                else:
                    d2Fdt2 = 0.0

                # Derived HEL1OS (spectral model)
                cls_cur, _ = classify_flux(flux)
                gamma = {"X": 2.5, "M": 3.0, "C": 3.8, "B": 4.5, "A": 5.0}.get(cls_cur, 4.0)
                hardness = max(0.5, 1.5 - ratio * 3.0)
                K, alpha = 5.2e9, 0.72
                base_hxr = K * (flux ** alpha) * hardness

                # Ancillary lookups
                hour_key = t[:13]
                kp = kp_map.get(hour_key, 2.0) or 2.0
                sw_speed = sw_speed_map.get(hour_key, 450.0) or 450.0
                sw_den = sw_den_map.get(hour_key, 5.0) or 5.0
                bz = bz_map.get(hour_key, 0.0) or 0.0

                # Rolling stats over window so far
                recent = [f for _, f in window[max(0, t_idx - 15):t_idx + 1]]
                roll_mean = np.mean(recent) if recent else flux
                roll_std = np.std(recent) if recent else 0.0

                # Percentile
                all_fluxes = [f for _, f in window]
                pct = float(np.searchsorted(sorted(all_fluxes), flux)) / max(len(all_fluxes), 1)

                # Build 17D vector
                f0  = self.norm_log_flux(flux)
                f1  = self.norm_log_flux(max(all_fluxes))
                f2  = self.norm_log_flux(short_flux)
                f3  = max(0.0, min(1.0, ratio))
                f4  = max(-1.0, min(1.0, dFdt / 1e-6))
                f5  = max(-1.0, min(1.0, d2Fdt2 / 1e-7))
                f6  = max(0.0, min(1.0, math.log10(max(base_hxr, 1.0)) / 4.0))
                f7  = max(0.0, min(1.0, math.log10(max(base_hxr * 0.28, 1.0)) / 4.0))
                f8  = max(0.0, min(1.0, math.log10(max(base_hxr / max(flux * 1e8, 0.1), 0.01) + 1) / 3.0))
                f9  = max(0.0, min(1.0, (gamma - 1.5) / 5.0))
                f10 = max(0.0, min(1.0, kp / 9.0))
                f11 = max(0.0, min(1.0, sw_speed / 1000.0))
                f12 = max(0.0, min(1.0, sw_den / 50.0))
                f13 = max(-1.0, min(1.0, bz / 20.0))
                f14 = pct
                f15 = self.norm_log_flux(max(roll_mean, 1e-12))
                f16 = min(1.0, roll_std / max(roll_mean, 1e-12))

                seq.append([f0, f1, f2, f3, f4, f5, f6, f7, f8, f9,
                            f10, f11, f12, f13, f14, f15, f16])

            sequences.append(seq)
            labels.append(label_idx)
            sample_meta.append({
                "time": current_time,
                "class": label_class,
                "positive": is_positive,
                "flux": window[-1][1],
            })

        logger.info(f"Built {len(sequences)} training sequences "
                     f"({n_positive} positive, {n_negative} negative)")

        return {
            "sequences": sequences,
            "labels":    labels,
            "metadata":  sample_meta,
            "n_samples": len(sequences),
            "class_distribution": {
                cls: labels.count(idx) for cls, idx in class_to_idx.items()
            },
        }


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════

def run(days: int = 7) -> dict:
    logger.info("=" * 60)
    logger.info("HISTORICAL DATA COLLECTION & TRAINING SET BUILDER")
    logger.info(f"Collecting last {days} days of NOAA data")

    noaa = NOAAHistorical()
    builder = TrainingDatasetBuilder()

    # Fetch all data sources
    xray = noaa.fetch_xray_timeseries()
    events = noaa.fetch_flare_events()
    kp = noaa.fetch_kp_history()
    sw = noaa.fetch_solar_wind_history()

    if xray["n_records"] == 0:
        logger.error("No XRS data available — cannot build training set.")
        return {"status": "FAILED", "n_samples": 0}

    # Build training dataset
    dataset = builder.build_sequences(xray, events, kp, sw)

    # Save
    out_path = HIST_DIR / "training_dataset.json"
    save_json({
        "created":     utc_now(),
        "source":      "NOAA_SWPC_historical",
        "days":        days,
        "n_samples":   dataset["n_samples"],
        "class_distribution": dataset.get("class_distribution", {}),
        "sequences":   dataset["sequences"],
        "labels":      dataset["labels"],
        "metadata":    dataset["metadata"],
    }, out_path)

    logger.info(f"Training dataset saved: {out_path}")
    logger.info(f"  Samples: {dataset['n_samples']}")
    logger.info(f"  Distribution: {dataset.get('class_distribution', {})}")

    return {
        "status":     "SUCCESS",
        "n_samples":  dataset["n_samples"],
        "output_file": str(out_path),
        "class_distribution": dataset.get("class_distribution", {}),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download NOAA historical data for training")
    parser.add_argument("--days", type=int, default=7, help="Number of days to fetch (default: 7)")
    args = parser.parse_args()

    result = run(days=args.days)
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("sequences",)}, indent=2))
