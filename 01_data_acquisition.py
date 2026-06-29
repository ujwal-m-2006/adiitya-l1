#!/usr/bin/env python3
"""
01_data_acquisition.py
======================
Aditya-L1 SoLEXS / HEL1OS Data Acquisition
ISRO Solar Flare Forecasting Pipeline — Step 1

Fetches newly available data on each cron trigger.
Primary:  ISRO PRADAN portal (Level-1 FITS files)
Fallback: NOAA SWPC public JSON feeds (real-time proxy)

Called by crontab every 5 minutes:
  */5 * * * * /opt/aditya_l1/venv/bin/python /opt/aditya_l1/scripts/01_data_acquisition.py
"""

import os
import json
import time
import hashlib
import logging
import requests
import numpy as np
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── Optional: astropy for real FITS parsing ────────────────────
try:
    from astropy.io import fits
    FITS_AVAILABLE = True
except ImportError:
    FITS_AVAILABLE = False

from pipeline_utils import (
    load_config, setup_logger, PipelineState,
    save_json, load_json, utc_now, STATE_FILE
)

# ── Setup ──────────────────────────────────────────────────────
cfg    = load_config()
logger = setup_logger("acquisition", cfg["pipeline"]["log_level"])
RAW    = Path(cfg["data"]["storage"]["raw_dir"])
RAW.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════════════════════
# PRADAN Client — ISRO native Aditya-L1 data
# ══════════════════════════════════════════════════════════════

class PRADANClient:
    """
    Downloads Level-1 SoLEXS and HEL1OS FITS files from ISRO's
    PRADAN data dissemination portal.

    Authentication: Keycloak SSO at idp.issdc.gov.in
    Registration required: https://pradan.issdc.gov.in
    Credentials must be set in environment variables:
        PRADAN_USERNAME, PRADAN_PASSWORD
    """

    PORTAL_URL   = "https://pradan1.issdc.gov.in/al1/"
    PROTECTED_URL = "https://pradan1.issdc.gov.in/al1/protected/payload.xhtml"
    BROWSE_URL   = "https://pradan1.issdc.gov.in/al1/protected/browse.xhtml"
    KEYCLOAK_URL = "https://idp.issdc.gov.in/auth/realms/issdc"
    CLIENT_ID    = "al1-pradan-1"
    USERNAME     = os.getenv("PRADAN_USERNAME", "")
    PASSWORD     = os.getenv("PRADAN_PASSWORD", "")

    def __init__(self):
        self.session   = requests.Session()
        self.session.headers.update({
            "User-Agent": "Aditya-L1-SFF-Pipeline/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self.logged_in = False
        self.LOOK_BACK = self._determine_look_back_hours()

    def _determine_look_back_hours(self) -> int:
        """Determine look-back hours based on config: auto-detect if needed."""
        look_back_config = cfg["data"]["pradan"]["look_back_hours"]
        if look_back_config != "auto":
            return int(look_back_config)
        
        min_hours = cfg["data"]["pradan"].get("look_back_auto_min_hours", 24)
        max_hours = cfg["data"]["pradan"].get("look_back_auto_max_hours", 720)
        
        # Check pipeline state for last acquisition time
        state = PipelineState.load()
        last_acq = state.get("last_acquisition")
        
        if last_acq:
            try:
                last_dt = datetime.fromisoformat(last_acq.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                delta_hours = (now - last_dt).total_seconds() / 3600
                # Add some buffer
                look_back = int(delta_hours + 6)  # +6 hours buffer
                # Clamp to min/max
                return max(min_hours, min(max_hours, look_back))
            except Exception as e:
                logger.warning(f"Error calculating auto look-back, using default: {e}")
                return min_hours
        return min_hours

    def login(self) -> bool:
        if not self.USERNAME or not self.PASSWORD:
            logger.warning("PRADAN credentials not set -- skipping native Aditya data fetch.")
            return False
    
        try:
            import re
    
            # Step 1: Access a protected page to trigger Keycloak SSO redirect
            r = self.session.get(self.PROTECTED_URL, timeout=20, allow_redirects=True)
            logger.debug(f"Protected page GET: status={r.status_code}, url={r.url[:100]}")
    
            # Step 2: Find the Keycloak login form action URL
            action_match = re.search(
                r'<form[^>]*id=["\']kc-form-login["\'][^>]*action=["\']([^"\']+)["\']',
                r.text, re.IGNORECASE
            )
            if not action_match:
                action_match = re.search(
                    r'action=["\']([^"\']*authenticate[^"\']*)["\']',
                    r.text, re.IGNORECASE
                )
    
            if not action_match:
                # Check if we're already logged in (no login form = session still valid)
                if "payload" in r.url.lower() or "protected" in r.text.lower():
                    self.logged_in = True
                    logger.info("PRADAN: session still valid, no re-login needed.")
                    return True
                logger.warning("Could not find Keycloak login form in portal response.")
                return False
    
            login_url = action_match.group(1)
            # Decode HTML entities
            login_url = login_url.replace("&amp;", "&").replace("&#38;", "&")
            logger.debug(f"Keycloak login URL: {login_url[:120]}")
    
            # Step 3: POST credentials to the Keycloak form
            login_data = {
                "username": self.USERNAME,
                "password": self.PASSWORD,
                "credentialId": "",
            }
            r = self.session.post(
                login_url,
                data=login_data,
                timeout=30,
                allow_redirects=True,
                headers={
                    "Referer": r.url,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            logger.debug(f"After login POST: status={r.status_code}, url={r.url[:100]}")
    
            # Step 4: Check for error messages
            if "invalid" in r.text.lower() or "Invalid username or password" in r.text:
                logger.warning("PRADAN login: invalid credentials.")
                return False
    
            # Step 5: Verify by accessing a protected page
            r2 = self.session.get(self.PROTECTED_URL, timeout=15, allow_redirects=True)
            self.logged_in = (
                r2.status_code == 200
                and "login" not in r2.url.lower()
                and "authenticate" not in r2.url.lower()
            )
    
            if self.logged_in:
                logger.info("PRADAN Keycloak login successful.")
            else:
                logger.warning(f"PRADAN login verification failed: url={r2.url[:80]}")
    
        except Exception as e:
            logger.error(f"PRADAN login error: {e}")
            self.logged_in = False
    
        return self.logged_in

    def fetch_instrument_files(self, instrument: str) -> list[dict]:
        """
        Browse PRADAN for Level-1 FITS files for SoLEXS or HEL1OS.
        Parses the browse page to find downloadable ZIP files.

        Returns list of dicts: {filename, url, obs_date, size_kb}
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
            since = datetime.now(timezone.utc) - timedelta(hours=self.LOOK_BACK)

            # Parse table rows: [index, filename, obs_start, obs_end, size_kb]
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
                    obs_start = datetime.fromisoformat(text[2].replace("Z", "+00:00"))
                    size_kb = float(text[4]) if text[4] else 0
                except (ValueError, IndexError):
                    continue

                # Filter by look-back window
                if obs_start < since:
                    continue

                # Find the download link for this file
                dl_pattern = re.compile(
                    rf'href="(/[^"]*downloadData/{inst_id}/[^"]*{re.escape(fname)}[^"]*)"',
                    re.IGNORECASE
                )
                dl_match = dl_pattern.search(r.text)
                if dl_match:
                    dl_url = dl_match.group(1)
                    if not dl_url.startswith("http"):
                        dl_url = f"https://pradan1.issdc.gov.in{dl_url}"
                else:
                    # Construct URL from pattern
                    year = obs_start.strftime("%Y")
                    month = obs_start.strftime("%m")
                    dl_url = (
                        f"https://pradan1.issdc.gov.in/al1/protected/downloadData/"
                        f"{inst_id}/level1/{year}/{month}/N00_0000/{fname}?{inst_id}"
                    )

                files.append({
                    "filename": fname,
                    "url":      dl_url,
                    "obs_date": text[2],
                    "size_kb":  size_kb,
                })

            logger.info(f"PRADAN: {len(files)} {instrument} files within look-back window.")
            return files

        except Exception as e:
            logger.error(f"PRADAN browse error ({instrument}): {e}")
            return []

    def download_fits_zip(self, file_info: dict, dest_dir: Path) -> Optional[Path]:
        """
        Download a single FITS ZIP file. Returns local path on success.
        """
        fname = file_info["filename"]
        dest  = dest_dir / fname

        if dest.exists():
            logger.debug(f"Already downloaded: {fname}")
            return dest

        try:
            r = self.session.get(file_info["url"], stream=True, timeout=120)
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info(f"Downloaded: {fname} ({dest.stat().st_size} bytes)")
            return dest
        except Exception as e:
            logger.error(f"Download failed [{fname}]: {e}")
            return None

    def parse_fits(self, fits_path: Path, instrument: str) -> Optional[dict]:
        """
        Parse a Level-1 FITS file into a structured dict.

        Actual PRADAN file structure:
        SoLEXS (*.lc.gz): HDU 'RATE' with cols [TIME, COUNTS] — 1-sec cadence
        HEL1OS (lightcurve_*.fits): HDUs 'CZT1_LC_BAND_*KEV_TO_*KEV'
            with cols [MJD, ISOT, CTR, STAT_ERR] — 1-sec cadence
        """
        if not FITS_AVAILABLE:
            logger.warning("astropy not installed -- cannot parse FITS.")
            return None

        try:
            # Handle gzipped files
            import gzip as gz_mod
            import shutil
            is_gzipped = str(fits_path).endswith(".gz")

            if is_gzipped:
                # Decompress to temp file for astropy
                import tempfile
                tmp_path = Path(tempfile.mktemp(suffix=".fits"))
                try:
                    with gz_mod.open(str(fits_path), "rb") as f_in:
                        with open(tmp_path, "wb") as f_out:
                            shutil.copyfileobj(f_in, f_out)
                    hdul = fits.open(str(tmp_path), memmap=False)
                except Exception:
                    if tmp_path.exists():
                        tmp_path.unlink()
                    raise
            else:
                hdul = fits.open(str(fits_path))
                tmp_path = None

            try:
                primary_hdr = hdul[0].header
                obs_time = primary_hdr.get("DATE-OBS", utc_now())
                exposure = primary_hdr.get("EXPOSURE", 1.0)

                if instrument == "SoLEXS":
                    # SoLEXS light curve: HDU 'RATE' with [TIME, COUNTS]
                    if "RATE" not in [h.name for h in hdul]:
                        logger.warning(f"SoLEXS FITS missing RATE HDU: {fits_path.name}")
                        return None

                    lc = hdul["RATE"].data
                    times = lc["TIME"]
                    counts = lc["COUNTS"]

                    # Filter valid sunlit data: remove NaN, zero, and very low counts
                    # (nighttime/eclipse periods produce NaN or 0 counts)
                    valid = ~np.isnan(counts) & (counts > 1.0)
                    n_valid = int(np.sum(valid))
                    n_total = len(counts)
                    valid_pct = (n_valid / max(n_total, 1)) * 100

                    if n_valid < 600:  # Need at least 10 minutes of valid data
                        logger.warning(
                            f"SoLEXS: only {n_valid}/{n_total} valid samples "
                            f"({valid_pct:.0f}%) in {fits_path.name} -- likely nighttime"
                        )
                        return None

                    valid_counts = counts[valid]
                    mean_counts = float(np.nanmean(valid_counts))
                    peak_counts = float(np.nanmax(valid_counts))

                    # Initial flux estimate using SDD2 calibration
                    # SDD2 large aperture (7.1 mm2): ~1 count/sec per 1e-9 W/m2 (rough)
                    # This will be refined by GOES cross-calibration in run()
                    rough_calib = 1e-10  # W/m2 per count/sec (approximate)
                    flux_1_8a = np.where(valid, counts * rough_calib, np.nan)

                    # Sunlit-only timeseries (last 60 minutes = 3600 sec)
                    sunlit_flux = flux_1_8a[valid]
                    last_60min = sunlit_flux[-3600:] if len(sunlit_flux) > 3600 else sunlit_flux

                    return {
                        "instrument":    "SoLEXS",
                        "source":        "PRADAN_L1_FITS",
                        "obs_time":      obs_time,
                        "exposure_s":    exposure,
                        # Initial flux estimates (will be recalibrated via GOES)
                        "band_1_8A":     float(np.nanmean(last_60min)),
                        "band_0_4A":     float(np.nanmean(last_60min) * 0.10),
                        "band_8_20A":    float(np.nanmean(last_60min) * 0.05),
                        "peak_flux":     float(np.nanmax(last_60min)),
                        "timeseries":    last_60min.tolist(),
                        "quality_flags": [],
                        # Raw count statistics for cross-calibration
                        "_mean_counts":       mean_counts,
                        "_peak_counts":       peak_counts,
                        "_mean_counts_short": mean_counts * 0.10,  # 0.4-1 A estimate
                        "_mean_counts_long":  mean_counts * 0.05,  # 8-20 A estimate
                        "n_samples":     n_valid,
                        "n_total":       n_total,
                        "valid_pct":     round(valid_pct, 1),
                    }

                elif instrument == "HEL1OS":
                    # HEL1OS: Multiple HDUs per energy band
                    # CZT1_LC_BAND_20.00KEV_TO_40.00KEV
                    # CZT1_LC_BAND_40.00KEV_TO_60.00KEV
                    # CZT1_LC_BAND_60.00KEV_TO_80.00KEV
                    # CZT1_LC_BAND_80.00KEV_TO_150.00KEV

                    def get_band_rate(hdu_name_pattern: str) -> float:
                        for hdu in hdul:
                            if hdu_name_pattern.upper() in hdu.name.upper() and hasattr(hdu, "data") and hdu.data is not None:
                                ctr = hdu.data["CTR"]
                                valid = ~np.isnan(ctr)
                                return float(np.nanmean(ctr[valid])) if np.any(valid) else 0.0
                        return 0.0

                    rate_20_40  = get_band_rate("20.00KEV_TO_40.00KEV")
                    rate_40_60  = get_band_rate("40.00KEV_TO_60.00KEV")
                    rate_60_80  = get_band_rate("60.00KEV_TO_80.00KEV")
                    rate_80_150 = get_band_rate("80.00KEV_TO_150.00KEV")

                    # Get timestamps from first band
                    isot_times = []
                    for hdu in hdul:
                        if hasattr(hdu, "data") and hdu.data is not None and "ISOT" in hdu.columns.names:
                            isot_times = hdu.data["ISOT"]
                            break

                    # Combine rates into pipeline-standard bands
                    # HEL1OS CZT covers 20-150 keV in 4 sub-bands
                    # CdTe covers 10-40 keV (not always present in lightcurve files)
                    # Pipeline expects: 20-60, 60-100, 100-300, 300-1000 keV
                    #
                    # Mapping:
                    #   20-60 keV  = rate_20_40 + rate_40_60 (direct measurement)
                    #   60-100 keV = rate_60_80 + partial from 80-150 (interpolated)
                    #   100-300 keV = extrapolated from 80-150 keV spectral slope
                    #   300-1000 keV = NOT AVAILABLE (outside HEL1OS range)

                    # Estimate spectral index from adjacent bands
                    if rate_40_60 > 0 and rate_60_80 > 0:
                        # Power-law spectral index between 40-80 keV
                        spectral_index = np.log(rate_60_80 / max(rate_40_60, 0.01)) / np.log(60.0 / 40.0)
                    else:
                        spectral_index = -2.0  # Typical thermal spectrum

                    # Extrapolate 100-300 keV from 80-150 keV using spectral slope
                    # Assume power-law: rate(E) ~ E^gamma
                    if rate_80_150 > 0:
                        mid_80_150 = 105.0  # Geometric mean
                        mid_100_300 = 173.0
                        rate_100_300 = rate_80_150 * (mid_100_300 / mid_80_150) ** spectral_index
                        rate_100_300 = max(0, rate_100_300)  # Floor at 0
                    else:
                        rate_100_300 = 0.0

                    # 60-100 keV: combine 60-80 keV + portion of 80-150 keV
                    band_60_100 = rate_60_80 + rate_80_150 * (40.0 / 70.0)  # 40/70 of 80-150 is in 80-100

                    return {
                        "instrument":      "HEL1OS",
                        "source":          "PRADAN_L1_FITS",
                        "obs_time":        obs_time,
                        "exposure_s":      exposure,
                        "band_20_60keV":   rate_20_40 + rate_40_60,
                        "band_60_100keV":  band_60_100,
                        "band_100_300keV": rate_100_300,
                        "band_300_1000keV": 0.0,  # Outside HEL1OS range (10-150 keV)
                        "rate_20_40keV":   rate_20_40,
                        "rate_40_60keV":   rate_40_60,
                        "rate_60_80keV":   rate_60_80,
                        "rate_80_150keV":  rate_80_150,
                        "timeseries_20_60": isot_times[:1000].tolist() if len(isot_times) > 0 else [],
                        "quality_flags":   [],
                        "n_samples":       len(isot_times),
                    }
                return None
            finally:
                hdul.close()
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink()

        except Exception as e:
            logger.error(f"FITS parse error [{fits_path.name}]: {e}")
            return None


# ══════════════════════════════════════════════════════════════
# NOAA SWPC Fallback — public JSON feeds
# ══════════════════════════════════════════════════════════════

class NOAAFallback:
    """
    Real-time NOAA SWPC data as a proxy for Aditya-L1 observations.

    SoLEXS Band 1 (1–8 Å)    ↔  GOES XRS Long  Channel (identical band)
    SoLEXS Band 2 (0.5–4 Å)  ↔  GOES XRS Short Channel (identical band)
    HEL1OS (hard X-ray)           → Spectral model derived from XRS ratio
    Kp index                      → Direct NOAA product
    """

    ENDPOINTS = cfg["data"]["noaa_fallback"]
    TIMEOUT   = 20

    def _get(self, url: str) -> Optional[list | dict]:
        try:
            r = requests.get(url, timeout=self.TIMEOUT,
                             headers={"User-Agent": "Aditya-L1-SFF-Pipeline/1.0"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning(f"NOAA fetch failed [{url}]: {e}")
            return None

    def fetch_xray(self) -> Optional[dict]:
        """Fetch 6-hour GOES XRS 1-min averages. Returns latest + timeseries."""
        data = self._get(self.ENDPOINTS["xray_6h"])
        if not data:
            return None

        long_ch  = [d for d in data if d.get("energy") in ("1.0-8.0A", "0.1-0.8nm")]
        short_ch = [d for d in data if d.get("energy") in ("0.5-4.0A", "0.05-0.4nm")]

        def extract(ch):
            vals = [float(d["flux"]) for d in ch if d.get("flux") and float(d["flux"]) > 0]
            if not vals:
                return None
            ts   = [d.get("time_tag", "") for d in ch if d.get("flux") and float(d["flux"]) > 0]
            peak = max(vals)
            mean = sum(vals) / len(vals)
            latest = vals[-1]
            dFdt = (vals[-1] - vals[-10]) / 10 if len(vals) >= 10 else 0.0
            d2Fdt2 = (vals[-1] - 2*vals[-5] + vals[-10]) / 25 if len(vals) >= 10 else 0.0
            return {
                "timeseries": vals[-120:],   # Last 2h at 1-min cadence
                "timestamps": ts[-120:],
                "latest":     latest,
                "peak_60min": max(vals[-60:]) if len(vals) >= 60 else peak,
                "mean":       mean,
                "peak":       peak,
                "dFdt":       dFdt,
                "d2Fdt2":     d2Fdt2,
                "n_records":  len(vals),
            }

        long_stats  = extract(long_ch)
        short_stats = extract(short_ch)
        if not long_stats:
            return None

        ratio = (short_stats["latest"] / long_stats["latest"]
                 if short_stats and long_stats["latest"] > 0 else 0.32)

        return {
            "instrument":   "SoLEXS_PROXY",
            "source":       "NOAA_SWPC_GOES_XRS",
            "obs_time":     utc_now(),
            "band_1_8A":    long_stats,
            "band_0_4A":    short_stats,
            "flux_ratio_short_long": ratio,
        }

    def fetch_kp(self) -> Optional[dict]:
        """Fetch latest planetary Kp index."""
        data = self._get(self.ENDPOINTS["kp_index"])
        if not data:
            return None

        # Parse Kp records — handle both dict and list formats
        records = []
        for r in data:
            if isinstance(r, dict):
                kp_val = r.get("Kp") or r.get("kp_index") or r.get("kp")
                time_val = r.get("time_tag") or r.get("time")
            elif isinstance(r, (list, tuple)) and len(r) > 1:
                time_val, kp_val = r[0], r[1]
            else:
                continue
            if kp_val is not None:
                try:
                    records.append({"time": str(time_val), "kp": float(kp_val)})
                except (ValueError, TypeError):
                    pass

        if not records:
            return None

        latest = records[-1]
        return {
            "source":       "NOAA_SWPC",
            "obs_time":     latest["time"],
            "kp_index":     latest["kp"],
            "kp_index_24h": [r["kp"] for r in records[-8:]],
        }

    def fetch_solar_wind(self) -> Optional[dict]:
        """Fetch ACE/DSCOVR solar wind plasma + magnetic field."""
        plasma = self._get(self.ENDPOINTS["solar_wind_plasma"])
        mag    = self._get(self.ENDPOINTS["solar_wind_mag"])

        result = {"source": "NOAA_SWPC_SOLAR_WIND", "obs_time": utc_now()}

        # Filter to only data rows (lists with numeric-ish content)
        def is_data_row(row):
            return (isinstance(row, list) and len(row) > 1
                    and not isinstance(row[1], str) or (isinstance(row[1], str)
                    and row[1].replace('.', '').replace('-', '').replace(' ', '').isdigit()))

        if plasma and len(plasma) > 1:
            # Skip header row if present
            data_rows = [r for r in plasma if isinstance(r, list) and len(r) > 2
                         and r[0] != "time_tag"]
            if data_rows:
                latest_p = data_rows[-1]
                try:
                    result.update({
                        "speed_km_s":   float(latest_p[2]) if latest_p[2] else None,
                        "density_n_cc": float(latest_p[1]) if latest_p[1] else None,
                        "temperature_K":float(latest_p[3]) if len(latest_p) > 3 and latest_p[3] else None,
                    })
                except (ValueError, TypeError, IndexError):
                    pass

        if mag and len(mag) > 1:
            data_rows = [r for r in mag if isinstance(r, list) and len(r) > 3
                         and r[0] != "time_tag"]
            if data_rows:
                latest_m = data_rows[-1]
                try:
                    result.update({
                        "imf_bz_nT": float(latest_m[3]) if latest_m[3] else None,
                        "imf_bt_nT": float(latest_m[6]) if len(latest_m) > 6 and latest_m[6] else None,
                    })
                except (ValueError, TypeError, IndexError):
                    pass

        return result if len(result) > 2 else None

    def fetch_flare_list(self) -> list[dict]:
        """Fetch last 7 days of confirmed flare events."""
        data = self._get(self.ENDPOINTS["flares_7d"])
        if not data:
            return []
        events = []
        for ev in data:
            events.append({
                "begin_time":  ev.get("begin_time"),
                "peak_time":   ev.get("peak_time"),
                "end_time":    ev.get("end_time"),
                "flare_class": ev.get("max_class", ""),
                "peak_flux":   ev.get("max_xrlong", None),
                "region":      ev.get("linked_region", None),
            })
        return events


# ══════════════════════════════════════════════════════════════
# Deduplication — skip already-processed records
# ══════════════════════════════════════════════════════════════

def compute_checksum(data: dict) -> str:
    key = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(key.encode()).hexdigest()[:16]

def is_duplicate(checksum: str, state: PipelineState) -> bool:
    seen = state.get("seen_checksums", [])
    return checksum in seen

def record_checksum(checksum: str, state: PipelineState) -> None:
    seen = state.get("seen_checksums", [])
    seen.append(checksum)
    seen = seen[-500:]          # Keep last 500 only
    state["seen_checksums"] = seen


# ══════════════════════════════════════════════════════════════
# Main acquisition entry point
# ══════════════════════════════════════════════════════════════

def run() -> dict:
    logger.info("=" * 60)
    logger.info("STEP 1 — DATA ACQUISITION")
    logger.info(f"Cron trigger: {utc_now()}")

    state   = PipelineState.load()
    result  = {
        "step":       "acquisition",
        "timestamp":  utc_now(),
        "source_used": None,
        "records":    [],
        "status":     "PENDING",
        "warnings":   [],
    }

    # ── 1a. Try PRADAN (native Aditya-L1) ─────────────────────
    pradan = PRADANClient()
    pradan_ok = pradan.login()
    acquired_native = []

    if pradan_ok:
        import zipfile
        for instrument in ["SoLEXS", "HEL1OS"]:
            files = pradan.fetch_instrument_files(instrument)
            for fi in files:
                dest = pradan.download_fits_zip(fi, RAW)
                if dest:
                    # Extract FITS from ZIP (handles both .fits and .gz files)
                    fits_files = []
                    try:
                        if dest.suffix == ".zip":
                            with zipfile.ZipFile(dest, "r") as zf:
                                for name in zf.namelist():
                                    is_fits = name.lower().endswith(".fits")
                                    is_gz = name.lower().endswith(".gz") and "lc" in name.lower()
                                    if is_fits or is_gz:
                                        extracted = dest.parent / name
                                        if not extracted.exists():
                                            zf.extract(name, dest.parent)
                                        fits_files.append(extracted)
                        else:
                            fits_files = [dest]
                    except Exception as e:
                        logger.error(f"ZIP extraction error [{dest.name}]: {e}")
                        continue

                    for fits_path in fits_files:
                        parsed = pradan.parse_fits(fits_path, instrument)
                        if parsed:
                            chk = compute_checksum(parsed)
                            if is_duplicate(chk, state):
                                logger.debug(f"Duplicate skipped: {fits_path.name}")
                                continue
                            record_checksum(chk, state)
                            parsed["_checksum"] = chk
                            parsed["_source_file"] = fi["filename"]
                            acquired_native.append(parsed)

        if acquired_native:
            result["source_used"] = "PRADAN_L1_FITS"
            result["records"]     = acquired_native
            logger.info(f"PRADAN: {len(acquired_native)} new records acquired.")

    # ── 1b. NOAA supplements (always fetch, even with PRADAN) ─────────
    # Kp index, solar wind, and GOES XRS for SoLEXS cross-calibration
    logger.info("Fetching NOAA supplements (Kp, solar wind, GOES XRS)...")
    noaa = NOAAFallback()
    noaa_xray = noaa.fetch_xray()
    noaa_kp   = noaa.fetch_kp()
    noaa_wind = noaa.fetch_solar_wind()
    noaa_flare = noaa.fetch_flare_list()

    # Attach NOAA supplements to each PRADAN record
    if acquired_native:
        for rec in acquired_native:
            rec["noaa_supplements"] = {
                "kp":         noaa_kp,
                "solar_wind": noaa_wind,
                "goes_xray":  noaa_xray,
                "recent_flares": noaa_flare[-10:] if noaa_flare else [],
            }
            # Cross-calibrate SoLEXS using GOES XRS if available
            if noaa_xray and rec.get("instrument") == "SoLEXS":
                goes_flux = noaa_xray.get("band_1_8A", {}).get("latest")
                solexs_counts_mean = rec.get("_mean_counts", 0)
                if goes_flux and solexs_counts_mean > 0:
                    # Derive calibration factor: flux = counts * k
                    calib_k = goes_flux / solexs_counts_mean
                    rec["calibration_factor"] = calib_k
                    # Re-calibrate the flux values
                    rec["band_1_8A"] = solexs_counts_mean * calib_k
                    rec["band_0_4A"] = rec.get("_mean_counts_short", solexs_counts_mean * 0.1) * calib_k
                    rec["band_8_20A"] = rec.get("_mean_counts_long", solexs_counts_mean * 0.05) * calib_k
                    rec["peak_flux"] = rec.get("_peak_counts", solexs_counts_mean) * calib_k
                    logger.debug(f"SoLEXS calibrated: k={calib_k:.2e}, flux={rec['band_1_8A']:.2e} W/m2")

    # ── 1c. NOAA-only fallback (if PRADAN unavailable) ──────────────
    if not acquired_native:
        logger.info("Falling back to NOAA SWPC public feeds...")

        xray     = noaa_xray
        kp       = noaa_kp
        wind     = noaa_wind
        flare_ev = noaa_flare

        if not xray:
            result["status"]  = "FAILED"
            result["error"]   = "Both PRADAN and NOAA feeds unavailable."
            logger.error("All data sources failed.")
            PipelineState.save(state)
            return result

        # Combine into one observation record
        obs = {
            "instrument":      "SoLEXS_PROXY + HEL1OS_DERIVED",
            "source":          "NOAA_SWPC",
            "obs_time":        utc_now(),
            "xray":            xray,
            "kp":              kp,
            "solar_wind":      wind,
            "recent_flares":   flare_ev[-10:],    # Last 10 confirmed events
        }
        chk = compute_checksum({"ts": obs["obs_time"][:14]})  # 1-min granularity
        if is_duplicate(chk, state):
            logger.info("No new data since last cron run.")
            result["status"] = "NO_NEW_DATA"
            PipelineState.save(state)
            return result

        record_checksum(chk, state)
        obs["_checksum"] = chk
        result["source_used"] = "NOAA_SWPC_FALLBACK"
        result["records"]     = [obs]
        result["warnings"].append(
            "Using NOAA GOES XRS as SoLEXS proxy. "
            "HEL1OS hard X-ray will be derived via spectral model in preprocessing."
        )

        if not wind:
            result["warnings"].append("Solar wind data unavailable — IMF Bz feature will be imputed.")
        if not kp:
            result["warnings"].append("Kp index unavailable — using last known value.")

    # ── Save raw output ────────────────────────────────────────
    out_path = RAW / f"raw_{utc_now().replace(':','-').replace(' ','T')}.json"
    save_json(result, out_path)
    state["last_acquisition"] = utc_now()
    state["last_raw_file"]    = str(out_path)
    PipelineState.save(state)

    result["status"]     = "SUCCESS"
    result["output_file"] = str(out_path)
    result["n_records"]   = len(result["records"])

    logger.info(f"Acquisition complete — {result['n_records']} record(s) from {result['source_used']}")
    return result


if __name__ == "__main__":
    out = run()
    print(json.dumps({k: v for k, v in out.items() if k != "records"}, indent=2))
