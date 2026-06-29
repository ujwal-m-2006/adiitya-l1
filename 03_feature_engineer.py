#!/usr/bin/env python3
"""
03_feature_engineer.py
======================
Aditya-L1 Solar Flare Forecasting Pipeline — Step 3

Extracts 17 AI-ready features from preprocessed SoLEXS + HEL1OS data.

Feature vector (ordered):
 [0]  log10_soft_flux          — SoLEXS 1–8 Å log10 normalised
 [1]  log10_soft_peak_60min    — Peak in last 60 min
 [2]  log10_soft_0_4A          — SoLEXS 0.5–4 Å
 [3]  flux_ratio_short_long    — 0.5-4A / 1-8A ratio (spectral hardness)
 [4]  dF_dt_norm               — Flux rise rate (normalised)
 [5]  d2F_dt2_norm             — Flux acceleration
 [6]  log10_hard_20_60         — HEL1OS 20–60 keV (log10 normalised)
 [7]  log10_hard_60_100        — HEL1OS 60–100 keV
 [8]  flux_ratio_hard_soft     — HEL1OS/SoLEXS ratio
 [9]  spectral_gamma           — Hard X-ray photon spectral index
 [10] kp_index_norm            — Kp / 9  (normalised to 0–1)
 [11] solar_wind_speed_norm    — Speed / 1000
 [12] solar_wind_density_norm  — Density / 50
 [13] imf_bz_norm              — IMF Bz normalised (-1 to +1)
 [14] flux_percentile_24h      — Current flux vs 24h distribution
 [15] rolling_mean_norm_15min  — 15-min rolling mean (normalised)
 [16] rolling_std_15min        — 15-min rolling std dev
"""

import json
import math
import numpy as np
from pathlib import Path
from scipy import stats as scipy_stats

from pipeline_utils import (
    load_config, setup_logger, PipelineState,
    save_json, load_json, utc_now
)

cfg    = load_config()
logger = setup_logger("features", cfg["pipeline"]["log_level"])
FEAT   = Path(cfg["data"]["storage"]["features_dir"])
FEAT.mkdir(parents=True, exist_ok=True)

SEQ_LEN = cfg["models"]["sequence_length"]   # 60 time steps


# ══════════════════════════════════════════════════════════════
# Feature extractor
# ══════════════════════════════════════════════════════════════

class FeatureEngineer:

    # Normalisation constants (physics-based ranges)
    LOG_FLUX_MIN = -9.0    # log10(1e-9) — A-class floor
    LOG_FLUX_MAX = -3.0    # log10(1e-3) — extreme X-class ceiling
    LOG_HARD_MIN = 0.0     # log10(1) cts/s
    LOG_HARD_MAX = 4.0     # log10(10000) cts/s

    def safe_log10(self, v: float, floor: float = 1e-12) -> float:
        return math.log10(max(v, floor)) if v and not math.isnan(v) else self.LOG_FLUX_MIN

    def norm_log_flux(self, v: float) -> float:
        lv = self.safe_log10(v)
        return max(0.0, min(1.0, (lv - self.LOG_FLUX_MIN) / (self.LOG_FLUX_MAX - self.LOG_FLUX_MIN)))

    def norm_log_hard(self, v: float) -> float:
        lv = math.log10(max(v, 1.0)) if v and v > 0 else 0.0
        return max(0.0, min(1.0, (lv - self.LOG_HARD_MIN) / (self.LOG_HARD_MAX - self.LOG_HARD_MIN)))

    def compute_percentile_rank(self, current: float, series: list) -> float:
        """Where does current flux sit in the 24-h distribution? Returns 0–1."""
        s = [v for v in series if v and not math.isnan(v)]
        if len(s) < 5:
            return 0.5
        return float(scipy_stats.percentileofscore(s, current) / 100.0)

    def rolling_stats(self, series: list, window: int = 15) -> tuple[float, float]:
        """Rolling mean and std dev over the last `window` points."""
        tail = [v for v in series[-window:] if v and not math.isnan(v)]
        if not tail:
            return 0.5, 0.0
        arr  = np.array(tail)
        mean = float(np.mean(arr))
        std  = float(np.std(arr))
        norm_mean = max(0.0, min(1.0,
            (self.safe_log10(mean) - self.LOG_FLUX_MIN) / (self.LOG_FLUX_MAX - self.LOG_FLUX_MIN)
        ))
        norm_std  = min(1.0, std / max(mean, 1e-12))
        return norm_mean, norm_std

    def extract(self, proc_record: dict) -> dict:
        """
        Extract the 17-dimensional feature vector from a preprocessed record.
        Returns {vector: [...], metadata: {...}, feature_names: [...]}
        """
        sol   = proc_record.get("solexs", {}) or {}
        hel   = proc_record.get("hel1os", {}) or {}
        anc   = proc_record.get("ancillary", {}) or {}
        ts    = sol.get("timeseries_raw", [])

        # ── Soft X-ray features (SoLEXS) ──────────────────────
        soft_flux  = sol.get("band_1_8A_Wm2",  1e-8) or 1e-8
        soft_peak  = sol.get("peak_flux_60min_Wm2", soft_flux) or soft_flux
        soft_0_4   = sol.get("band_0_4A_Wm2",  soft_flux * 0.32) or soft_flux * 0.32
        ratio_sl   = sol.get("flux_ratio_short_long", 0.32) or 0.32
        dFdt       = sol.get("dF_dt_Wm2s", 0.0) or 0.0
        d2Fdt2     = sol.get("d2F_dt2_Wm2s2", 0.0) or 0.0

        f0  = self.norm_log_flux(soft_flux)       # log10 soft flux
        f1  = self.norm_log_flux(soft_peak)       # log10 peak 60min
        f2  = self.norm_log_flux(soft_0_4)        # log10 0.5–4A band
        f3  = max(0.0, min(1.0, ratio_sl))        # short/long ratio (already 0–1 range)

        # Rise rate: normalise dF/dt to [-1, 1]
        dFdt_scale = 1e-6     # typical rapid-rise rate W/m²/min
        f4 = max(-1.0, min(1.0, dFdt / dFdt_scale))
        f5 = max(-1.0, min(1.0, d2Fdt2 / (dFdt_scale * 0.1)))

        # ── Hard X-ray features (HEL1OS) ──────────────────────
        h20_60  = hel.get("band_20_60keV",  1.0) or 1.0
        h60_100 = hel.get("band_60_100keV", 0.1) or 0.1
        f6  = self.norm_log_hard(h20_60)          # log10 20-60 keV
        f7  = self.norm_log_hard(h60_100)         # log10 60-100 keV
        # Hard/soft ratio — high ratio = energetic non-thermal flare
        hard_soft = h20_60 / max(soft_flux * 1e8, 0.1)  # dimensionless
        f8  = max(0.0, min(1.0, math.log10(max(hard_soft, 0.01) + 1) / 3.0))
        # Spectral index
        gamma = hel.get("spectral_gamma", 4.0) or 4.0
        f9  = max(0.0, min(1.0, (gamma - 1.5) / 5.0))  # 1.5–6.5 → 0–1

        # ── Ancillary / space environment ─────────────────────
        kp      = anc.get("kp_index", 2.0) or 2.0
        sw_spd  = anc.get("solar_wind_speed_km_s", 450.0) or 450.0
        sw_den  = anc.get("solar_wind_density_n_cc", 5.0) or 5.0
        bz      = anc.get("imf_bz_nT", 0.0) or 0.0

        f10 = max(0.0, min(1.0, kp / 9.0))
        f11 = max(0.0, min(1.0, sw_spd / 1000.0))
        f12 = max(0.0, min(1.0, sw_den / 50.0))
        f13 = max(-1.0, min(1.0, bz / 20.0))   # ±20 nT → ±1

        # ── Temporal statistics ────────────────────────────────
        f14 = self.compute_percentile_rank(soft_flux, ts)
        f15, f16 = self.rolling_stats(ts, window=15)

        vector = [f0, f1, f2, f3, f4, f5, f6, f7, f8, f9,
                  f10, f11, f12, f13, f14, f15, f16]

        # ── Sequence tensor (60 steps × 17 features) ──────────
        # For LSTM/GRU/Transformer: replicate scalar features along time axis
        # Real deployment: maintain a rolling buffer of 60 consecutive observations
        n  = min(len(ts), SEQ_LEN)
        ts_norm = [(self.safe_log10(v) - self.LOG_FLUX_MIN) /
                   (self.LOG_FLUX_MAX - self.LOG_FLUX_MIN) for v in ts[-SEQ_LEN:]]
        # Pad front with earliest value if short
        while len(ts_norm) < SEQ_LEN:
            ts_norm.insert(0, ts_norm[0] if ts_norm else 0.5)

        # Build (60, 17) sequence: channel 0 = timeseries, rest = repeated scalars
        sequence = []
        for i in range(SEQ_LEN):
            step = list(vector)
            step[0] = ts_norm[i]   # Override f0 with actual time-varying flux
            sequence.append(step)

        return {
            "obs_time":      proc_record.get("obs_time"),
            "source":        proc_record.get("source"),
            "vector":        [round(v, 6) for v in vector],
            "sequence":      sequence,            # (60, 17) for LSTM/GRU/Transformer
            "feature_names": [
                "log10_soft_flux", "log10_soft_peak_60min", "log10_soft_0_4A",
                "flux_ratio_short_long", "dFdt_norm", "d2Fdt2_norm",
                "log10_hard_20_60keV", "log10_hard_60_100keV",
                "flux_ratio_hard_soft", "spectral_gamma_norm",
                "kp_index_norm", "solar_wind_speed_norm",
                "solar_wind_density_norm", "imf_bz_norm",
                "flux_percentile_24h", "rolling_mean_15min_norm",
                "rolling_std_15min",
            ],
            "raw_scalars": {
                "soft_flux_Wm2":    soft_flux,
                "soft_peak_Wm2":    soft_peak,
                "hard_20_60_cts_s": h20_60,
                "kp_index":         kp,
                "flux_ratio":       ratio_sl,
                "spectral_gamma":   gamma,
                "dFdt":             dFdt,
                "hel1os_source":    hel.get("source", "unknown"),
            },
        }


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════

def run(proc_result: dict) -> dict:
    logger.info("=" * 60)
    logger.info("STEP 3 — FEATURE ENGINEERING")

    result = {
        "step":       "feature_engineering",
        "timestamp":  utc_now(),
        "status":     "PENDING",
        "n_features": 17,
        "feature_sets": [],
    }

    records = proc_result.get("processed") or proc_result.get("records", [])
    if not records:
        result["status"] = "FAILED"
        result["error"]  = "No preprocessed records."
        return result

    fe  = FeatureEngineer()
    out = []
    for rec in records:
        try:
            feat = fe.extract(rec)
            out.append(feat)
            logger.info(
                f"Features extracted: flux={feat['raw_scalars']['soft_flux_Wm2']:.2e} "
                f"| ratio={feat['raw_scalars']['flux_ratio']:.3f} "
                f"| kp={feat['raw_scalars']['kp_index']:.1f} "
                f"| gamma={feat['raw_scalars']['spectral_gamma']:.2f}"
            )
        except Exception as e:
            logger.error(f"Feature extraction error: {e}")
            result["warnings"] = result.get("warnings", []) + [str(e)]

    out_path = FEAT / f"features_{utc_now().replace(':','-').replace(' ','T')}.json"
    save_json({"step": "feature_engineering", "timestamp": utc_now(),
               "n_feature_sets": len(out), "feature_sets": out}, out_path)

    state = PipelineState.load()
    state["last_features_file"] = str(out_path)
    PipelineState.save(state)

    result.update({
        "status":       "SUCCESS",
        "n_sets":       len(out),
        "output_file":  str(out_path),
        "feature_sets": out,
    })

    logger.info(f"Feature engineering complete — {len(out)} feature set(s) ready")
    return result


if __name__ == "__main__":
    import sys
    state = PipelineState.load()
    pf = state.get("last_processed_file")
    if not pf:
        print("No processed file in state. Run 02_preprocess.py first.")
        sys.exit(1)
    proc = load_json(Path(pf))
    out  = run(proc)
    if out["feature_sets"]:
        fs = out["feature_sets"][0]
        print("Feature vector:", [round(v, 4) for v in fs["vector"]])
        print("Sequence shape:", f"({len(fs['sequence'])}, {len(fs['sequence'][0])})")
