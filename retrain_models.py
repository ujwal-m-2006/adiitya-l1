#!/usr/bin/env python3
"""
retrain_models.py
=================
Aditya-L1 SFF Pipeline — Model Training & Retraining

Trains the 4-model ensemble on historical NOAA + Aditya-L1 data:
  • LSTM        — 3-layer, 128 hidden, sequence-to-class
  • GRU         — 2-layer, 128 hidden, sequence-to-class
  • Transformer — 4-layer encoder, 64 d_model, 8 heads
  • XGBoost     — 500 trees, scalar feature vector

Designed to run nightly via cron:
  0 2 * * * python retrain_models.py >> logs/retrain.log 2>&1

Usage:
  python retrain_models.py                          # Train on existing data
  python retrain_models.py --collect-first          # Download fresh data first
  python retrain_models.py --epochs 100 --lr 0.001  # Custom hyperparams
"""

import os
import sys
import json
import math
import time
import argparse
import numpy as np
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

from pipeline_utils import load_config, setup_logger, save_json, load_json, utc_now

cfg = load_config()
logger = setup_logger("retrain", cfg["pipeline"]["log_level"])
M_CFG = cfg["models"]
HIST_DIR = Path("data/historical")
MODEL_DIR = Path("models")
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# ══════════════════════════════════════════════════════════════
# PyTorch Setup
# ══════════════════════════════════════════════════════════════

try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from torch.utils.data import Dataset, DataLoader
    TORCH_AVAILABLE = True
    DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info(f"PyTorch {torch.__version__} on {DEVICE}")
except ImportError:
    TORCH_AVAILABLE = False
    logger.error("PyTorch not installed — cannot train neural models.")

try:
    import xgboost as xgb
    XGB_AVAILABLE = True
except ImportError:
    XGB_AVAILABLE = False
    logger.error("XGBoost not installed — cannot train XGBoost model.")


# ══════════════════════════════════════════════════════════════
# Dataset
# ══════════════════════════════════════════════════════════════

CLASSES = ["A", "B", "C", "M", "X"]

class SolarFlareDataset(Dataset):
    """PyTorch dataset wrapping training sequences + labels."""

    def __init__(self, sequences: list, labels: list):
        self.sequences = torch.tensor(sequences, dtype=torch.float32)
        self.labels    = torch.tensor(labels, dtype=torch.long)

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        return self.sequences[idx], self.labels[idx]


def load_training_data(path: str = None) -> tuple[list, list, dict]:
    """Load training dataset from JSON file."""
    if path is None:
        path = HIST_DIR / "training_dataset.json"

    path = Path(path)
    if not path.exists():
        logger.error(f"Training data not found: {path}")
        logger.info("Run 06_historical_data.py first to collect training data.")
        return [], [], {}

    data = load_json(path)
    sequences = data.get("sequences", [])
    labels    = data.get("labels", [])
    dist      = data.get("class_distribution", {})

    logger.info(f"Loaded {len(sequences)} training samples from {path}")
    logger.info(f"Class distribution: {dist}")

    return sequences, labels, dist


# ══════════════════════════════════════════════════════════════
# Model Definitions (same architecture as inference)
# ══════════════════════════════════════════════════════════════

if TORCH_AVAILABLE:

    class LSTMFlareModel(nn.Module):
        def __init__(self, input_size=17, hidden=128, layers=3, dropout=0.2):
            super().__init__()
            self.lstm = nn.LSTM(input_size, hidden, layers,
                                batch_first=True, dropout=dropout)
            self.fc = nn.Sequential(
                nn.Linear(hidden, 64),
                nn.ReLU(),
                nn.Dropout(dropout),
                nn.Linear(64, 5),
            )

        def forward(self, x):
            out, _ = self.lstm(x)
            return self.fc(out[:, -1, :])

    class GRUFlareModel(nn.Module):
        def __init__(self, input_size=17, hidden=128, layers=2, dropout=0.2):
            super().__init__()
            self.gru = nn.GRU(input_size, hidden, layers,
                              batch_first=True, dropout=dropout)
            self.fc = nn.Sequential(
                nn.Linear(hidden, 64),
                nn.ReLU(),
                nn.Linear(64, 5),
            )

        def forward(self, x):
            out, _ = self.gru(x)
            return self.fc(out[:, -1, :])

    class TransformerFlareModel(nn.Module):
        def __init__(self, d_model=64, nhead=8, n_layers=4, ff=256,
                     dropout=0.1, seq=60, feat=17):
            super().__init__()
            self.input_proj = nn.Linear(feat, d_model)
            self.pos_enc = nn.Parameter(torch.randn(1, seq, d_model) * 0.02)
            encoder_layer = nn.TransformerEncoderLayer(
                d_model, nhead, ff, dropout, batch_first=True
            )
            self.encoder = nn.TransformerEncoder(encoder_layer, n_layers)
            self.fc = nn.Sequential(
                nn.Linear(d_model, 32),
                nn.ReLU(),
                nn.Linear(32, 5),
            )

        def forward(self, x):
            x = self.input_proj(x) + self.pos_enc[:, :x.size(1), :]
            x = self.encoder(x)
            return self.fc(x[:, -1, :])


# ══════════════════════════════════════════════════════════════
# Training Loop
# ══════════════════════════════════════════════════════════════

def compute_class_weights(labels: list, n_classes: int = 5) -> torch.Tensor:
    """Inverse frequency class weights for imbalanced datasets."""
    counts = np.bincount(labels, minlength=n_classes).astype(float)
    counts = np.maximum(counts, 1.0)  # Avoid division by zero
    weights = len(labels) / (n_classes * counts)
    logger.info(f"Class weights: {[round(w, 2) for w in weights.tolist()]}")
    return torch.tensor(weights, dtype=torch.float32).to(DEVICE)


def train_neural_model(model, train_loader, val_loader, class_weights,
                       epochs: int = 50, lr: float = 0.001,
                       model_name: str = "model") -> dict:
    """
    Train a neural model with early stopping.
    Returns training history and best metrics.
    """
    model = model.to(DEVICE)
    criterion = nn.CrossEntropyLoss(weight=class_weights)
    optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    best_val_acc = 0.0
    best_epoch = 0
    patience = 10
    no_improve = 0
    history = {"train_loss": [], "val_acc": [], "val_f1": []}

    for epoch in range(epochs):
        # ── Train ──────────────────────────────────────────
        model.train()
        total_loss = 0.0
        n_batches = 0

        for X_batch, y_batch in train_loader:
            X_batch = X_batch.to(DEVICE)
            y_batch = y_batch.to(DEVICE)

            optimizer.zero_grad()
            logits = model(X_batch)
            loss = criterion(logits, y_batch)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()

            total_loss += loss.item()
            n_batches += 1

        scheduler.step()
        avg_loss = total_loss / max(n_batches, 1)
        history["train_loss"].append(round(avg_loss, 4))

        # ── Validate ───────────────────────────────────────
        model.eval()
        correct = 0
        total = 0
        class_correct = [0] * 5
        class_total = [0] * 5

        with torch.no_grad():
            for X_batch, y_batch in val_loader:
                X_batch = X_batch.to(DEVICE)
                y_batch = y_batch.to(DEVICE)

                logits = model(X_batch)
                probs = torch.softmax(logits, dim=-1)
                preds = probs.argmax(dim=-1)

                correct += (preds == y_batch).sum().item()
                total += y_batch.size(0)

                for i in range(y_batch.size(0)):
                    true_cls = y_batch[i].item()
                    class_total[true_cls] += 1
                    if preds[i].item() == true_cls:
                        class_correct[true_cls] += 1

        val_acc = correct / max(total, 1)
        history["val_acc"].append(round(val_acc, 4))

        # Per-class recall
        recalls = []
        for c in range(5):
            r = class_correct[c] / max(class_total[c], 1)
            recalls.append(round(r, 3))
        history["val_f1"].append(recalls)

        if (epoch + 1) % 5 == 0 or epoch == 0:
            logger.info(
                f"  [{model_name}] Epoch {epoch+1:3d}/{epochs} | "
                f"loss={avg_loss:.4f} | val_acc={val_acc:.3f} | "
                f"recalls={dict(zip(CLASSES, recalls))}"
            )

        # Early stopping
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_epoch = epoch + 1
            no_improve = 0
        else:
            no_improve += 1

        if no_improve >= patience and epoch > 20:
            logger.info(f"  Early stop at epoch {epoch+1} (best={best_val_acc:.3f} at epoch {best_epoch})")
            break

    logger.info(f"  {model_name} best: val_acc={best_val_acc:.3f} at epoch {best_epoch}")

    return {
        "best_val_acc": round(best_val_acc, 4),
        "best_epoch":   best_epoch,
        "final_loss":   round(avg_loss, 4),
        "per_class_recall": dict(zip(CLASSES, recalls)),
        "history":      history,
    }


# ══════════════════════════════════════════════════════════════
# XGBoost Training
# ══════════════════════════════════════════════════════════════

def train_xgboost(sequences: list, labels: list,
                  val_split: float = 0.2) -> dict:
    """Train XGBoost on the last timestep's feature vector (scalar)."""
    if not XGB_AVAILABLE:
        logger.warning("XGBoost not available — skipping.")
        return {"status": "SKIPPED"}

    # Extract scalar features (last timestep of each sequence)
    X = np.array([seq[-1] for seq in sequences])
    y = np.array(labels)

    # Train/val split
    n = len(X)
    n_val = max(1, int(n * val_split))
    n_train = n - n_val

    # Shuffle
    idx = np.random.permutation(n)
    X_train, y_train = X[idx[:n_train]], y[idx[:n_train]]
    X_val, y_val     = X[idx[n_train:]], y[idx[n_train:]]

    logger.info(f"XGBoost: {n_train} train, {n_val} val samples")

    xgb_cfg = M_CFG["XGBoost"]
    dtrain = xgb.DMatrix(X_train, label=y_train)
    dval   = xgb.DMatrix(X_val, label=y_val)

    params = {
        "objective":       "multi:softprob",
        "num_class":       5,
        "max_depth":       xgb_cfg["max_depth"],
        "learning_rate":   xgb_cfg["learning_rate"],
        "subsample":       xgb_cfg["subsample"],
        "colsample_bytree": xgb_cfg["colsample_bytree"],
        "eval_metric":     "mlogloss",
        "verbosity":       0,
    }

    bst = xgb.train(
        params, dtrain,
        num_boost_round=xgb_cfg["n_estimators"],
        evals=[(dval, "val")],
        early_stopping_rounds=30,
        verbose_eval=50,
    )

    # Evaluate
    val_preds = bst.predict(dval)
    val_pred_classes = np.argmax(val_preds, axis=1)
    val_acc = float(np.mean(val_pred_classes == y_val))

    # Per-class recall
    recalls = {}
    for c_idx, c_name in enumerate(CLASSES):
        mask = y_val == c_idx
        if mask.sum() > 0:
            recalls[c_name] = round(float(np.mean(val_pred_classes[mask] == c_idx)), 3)
        else:
            recalls[c_name] = 0.0

    # Save model
    model_path = MODEL_DIR / "xgboost_v1.json"
    bst.save_model(str(model_path))
    logger.info(f"XGBoost saved: {model_path} | val_acc={val_acc:.3f}")

    return {
        "status":        "SUCCESS",
        "val_accuracy":  round(val_acc, 4),
        "n_trees":       bst.num_boosted_rounds(),
        "per_class_recall": recalls,
        "model_path":    str(model_path),
    }


# ══════════════════════════════════════════════════════════════
# Main Training Orchestrator
# ══════════════════════════════════════════════════════════════

def run(epochs: int = 50, lr: float = 0.001, collect_first: bool = False) -> dict:
    logger.info("=" * 60)
    logger.info("MODEL RETRAINING — ADITYA-L1 SFF ENSEMBLE")
    logger.info(f"Timestamp: {utc_now()}")
    logger.info(f"Device: {DEVICE if TORCH_AVAILABLE else 'CPU (no torch)'}")

    # ── Optionally collect fresh data first ─────────────────
    if collect_first:
        logger.info("Collecting fresh historical data...")
        from importlib import import_module
        hist_mod = import_module("06_historical_data")
        hist_result = hist_mod.run(days=7)
        if hist_result["status"] != "SUCCESS":
            logger.warning("Data collection failed — using existing data.")

    # ── Load training data ──────────────────────────────────
    sequences, labels, dist = load_training_data()

    if len(sequences) < 10:
        logger.error("Not enough training data. Run 06_historical_data.py first.")
        logger.info("Generating synthetic training data for initial model...")
        sequences, labels = generate_synthetic_data()
        dist = {CLASSES[i]: labels.count(i) for i in range(5)}

    logger.info(f"Training on {len(sequences)} samples | Distribution: {dist}")

    results = {"timestamp": utc_now(), "n_samples": len(sequences),
               "models": {}}

    # ── Train/Val split (80/20) ─────────────────────────────
    n = len(sequences)
    n_val = max(1, int(n * 0.2))
    idx = np.random.permutation(n).tolist()
    train_idx, val_idx = idx[:n - n_val], idx[n - n_val:]

    train_seqs = [sequences[i] for i in train_idx]
    train_labs = [labels[i] for i in train_idx]
    val_seqs   = [sequences[i] for i in val_idx]
    val_labs   = [labels[i] for i in val_idx]

    # ── Neural Models ──────────────────────────────────────
    if TORCH_AVAILABLE and len(train_seqs) >= 5:
        class_weights = compute_class_weights(train_labs)

        train_ds = SolarFlareDataset(train_seqs, train_labs)
        val_ds   = SolarFlareDataset(val_seqs, val_labs)
        train_loader = DataLoader(train_ds, batch_size=min(32, len(train_ds)),
                                  shuffle=True, drop_last=False)
        val_loader   = DataLoader(val_ds, batch_size=min(64, len(val_ds)),
                                  shuffle=False)

        # LSTM
        logger.info("-" * 40)
        logger.info("Training LSTM...")
        lstm = LSTMFlareModel(
            hidden=M_CFG["LSTM"]["hidden_size"],
            layers=M_CFG["LSTM"]["num_layers"],
            dropout=M_CFG["LSTM"]["dropout"],
        )
        lstm_metrics = train_neural_model(
            lstm, train_loader, val_loader, class_weights,
            epochs=epochs, lr=lr, model_name="LSTM"
        )
        torch.save(lstm.state_dict(), MODEL_DIR / "lstm_v1.pt")
        results["models"]["LSTM"] = lstm_metrics
        logger.info(f"LSTM saved: models/lstm_v1.pt")

        # GRU
        logger.info("-" * 40)
        logger.info("Training GRU...")
        gru = GRUFlareModel(
            hidden=M_CFG["GRU"]["hidden_size"],
            layers=M_CFG["GRU"]["num_layers"],
            dropout=M_CFG["GRU"]["dropout"],
        )
        gru_metrics = train_neural_model(
            gru, train_loader, val_loader, class_weights,
            epochs=epochs, lr=lr, model_name="GRU"
        )
        torch.save(gru.state_dict(), MODEL_DIR / "gru_v1.pt")
        results["models"]["GRU"] = gru_metrics
        logger.info(f"GRU saved: models/gru_v1.pt")

        # Transformer
        logger.info("-" * 40)
        logger.info("Training Transformer...")
        trans = TransformerFlareModel(
            d_model=M_CFG["Transformer"]["d_model"],
            nhead=M_CFG["Transformer"]["nhead"],
            n_layers=M_CFG["Transformer"]["num_encoder_layers"],
            ff=M_CFG["Transformer"]["dim_feedforward"],
            dropout=M_CFG["Transformer"]["dropout"],
        )
        trans_metrics = train_neural_model(
            trans, train_loader, val_loader, class_weights,
            epochs=epochs, lr=lr, model_name="Transformer"
        )
        torch.save(trans.state_dict(), MODEL_DIR / "transformer_v1.pt")
        results["models"]["Transformer"] = trans_metrics
        logger.info(f"Transformer saved: models/transformer_v1.pt")

    # ── XGBoost ─────────────────────────────────────────────
    logger.info("-" * 40)
    logger.info("Training XGBoost...")
    xgb_result = train_xgboost(sequences, labels)
    results["models"]["XGBoost"] = xgb_result

    # ── Save training report ────────────────────────────────
    report_path = Path("data/reports") / f"retrain_{utc_now().replace(':','-').replace(' ','T')}.json"
    save_json(results, report_path)

    logger.info("=" * 60)
    logger.info("RETRAINING COMPLETE")
    for name, m in results["models"].items():
        acc = m.get("best_val_acc") or m.get("val_accuracy", "N/A")
        logger.info(f"  {name:15s} -> val_acc={acc}")
    logger.info("=" * 60)

    return results


# ══════════════════════════════════════════════════════════════
# Synthetic Data Generator (for initial bootstrap)
# ══════════════════════════════════════════════════════════════

def generate_synthetic_data(n_samples: int = 500) -> tuple[list, list]:
    """
    Generate physics-realistic synthetic training data.
    Used only when no historical data is available (first run).
    """
    logger.info(f"Generating {n_samples} synthetic training samples...")
    SEQ_LEN = M_CFG["sequence_length"]
    FEAT_DIM = M_CFG["feature_dim"]

    sequences = []
    labels = []

    # Class distribution: realistic flare frequency (Crosby et al. 1993)
    # A:B:C:M:X ≈ 50:30:15:4:1
    class_counts = {0: 250, 1: 150, 2: 75, 3: 20, 4: 5}

    for cls_idx, count in class_counts.items():
        # Base flux level for each class (log10 W/m²)
        base_log_flux = {0: -7.8, 1: -7.2, 2: -6.2, 3: -5.2, 4: -4.3}[cls_idx]
        base_flux = 10 ** base_log_flux

        for _ in range(count):
            seq = []
            # Simulate a 60-step timeseries
            flux = base_flux * np.random.uniform(0.5, 2.0)
            for t in range(SEQ_LEN):
                # Gradual rise for M/X, flat for A/B/C
                if cls_idx >= 3:
                    rise = 1.0 + (t / SEQ_LEN) * np.random.uniform(0.5, 3.0)
                else:
                    rise = 1.0 + np.random.normal(0, 0.05)

                step_flux = flux * rise * np.random.lognormal(0, 0.1)

                # Build 17D feature vector
                log_f = math.log10(max(step_flux, 1e-12))
                f0  = max(0.0, min(1.0, (log_f + 9.0) / 6.0))
                f1  = max(0.0, min(1.0, f0 + np.random.uniform(-0.05, 0.1)))
                f2  = max(0.0, min(1.0, f0 - np.random.uniform(0, 0.15)))
                f3  = max(0.0, min(1.0, np.random.uniform(0.2, 0.5)))
                f4  = max(-1.0, min(1.0, np.random.normal(0.1 * cls_idx, 0.3)))
                f5  = max(-1.0, min(1.0, np.random.normal(0.05 * cls_idx, 0.2)))
                f6  = max(0.0, min(1.0, f0 * 0.7 + np.random.normal(0, 0.1)))
                f7  = max(0.0, min(1.0, f6 * 0.6 + np.random.normal(0, 0.1)))
                f8  = max(0.0, min(1.0, 0.2 + cls_idx * 0.1 + np.random.normal(0, 0.1)))
                f9  = max(0.0, min(1.0, (5.0 - cls_idx * 0.5) / 5.0 + np.random.normal(0, 0.1)))
                f10 = max(0.0, min(1.0, np.random.uniform(0.1, 0.5 + cls_idx * 0.1)))
                f11 = max(0.0, min(1.0, np.random.uniform(0.3, 0.6)))
                f12 = max(0.0, min(1.0, np.random.uniform(0.05, 0.2)))
                f13 = max(-1.0, min(1.0, np.random.normal(0, 0.3)))
                f14 = max(0.0, min(1.0, f0 + np.random.normal(0, 0.1)))
                f15 = max(0.0, min(1.0, f0 + np.random.normal(-0.05, 0.05)))
                f16 = max(0.0, min(1.0, np.random.uniform(0.01, 0.1 + cls_idx * 0.05)))

                seq.append([f0, f1, f2, f3, f4, f5, f6, f7, f8, f9,
                            f10, f11, f12, f13, f14, f15, f16])

            sequences.append(seq)
            labels.append(cls_idx)

    logger.info(f"Generated {len(sequences)} synthetic samples")
    return sequences, labels


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retrain AI ensemble models")
    parser.add_argument("--epochs", type=int, default=50, help="Training epochs (default: 50)")
    parser.add_argument("--lr", type=float, default=0.001, help="Learning rate (default: 0.001)")
    parser.add_argument("--collect-first", action="store_true",
                        help="Download fresh NOAA data before training")
    args = parser.parse_args()

    results = run(epochs=args.epochs, lr=args.lr, collect_first=args.collect_first)

    # Print summary
    print("\n" + "=" * 50)
    print("TRAINING SUMMARY")
    print("=" * 50)
    for name, m in results.get("models", {}).items():
        acc = m.get("best_val_acc") or m.get("val_accuracy", "N/A")
        print(f"  {name:15s} -> val_acc={acc}")
    print("=" * 50)
