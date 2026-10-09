"""Train the fraud model.

  python -m ml.train                # bootstrap on synthetic documents
  python -m ml.train --n 800

The synthetic feature matrix is cached inside the model file so the running app
can retrain quickly when analysts confirm real outcomes (app.main: /api/model/retrain).
"""
from __future__ import annotations

import argparse
import random
import time
from datetime import datetime

import joblib
import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split

from app.engine import analyze, model
from ml import synth

REAL_WEIGHT = 8.0     # one analyst-confirmed document counts as much as eight synthetic ones


def build(n: int, seed: int = 42):
    rng = random.Random(seed)
    X, y, kinds, verdicts = [], [], [], []
    model.set_model(None)
    saved, model.PATH = model.PATH, "/nonexistent"     # featurise with rules only
    try:
        for i in range(n):
            s = synth.make(rng, fraud=i % 2 == 1)
            r = analyze(s["data"], f"s.{s['ext']}", s["applicant"], s["today"])
            if r["features"] is None:
                continue
            X.append(r["features"]); y.append(s["label"]); kinds.append(s["kind"]); verdicts.append(r["verdict"])
    finally:
        model.PATH = saved
    return np.array(X), np.array(y), kinds, verdicts


def fit(X, y, w=None):
    clf = GradientBoostingClassifier(n_estimators=160, max_depth=3, learning_rate=0.08, subsample=0.9, random_state=0)
    clf.fit(X, y, sample_weight=w)
    return clf


def retrain(base: dict, real_X, real_y):
    """Refit on cached synthetic data plus analyst-labelled real documents."""
    X, y = base["X_synth"], base["y_synth"]
    w = np.ones(len(y))
    if len(real_y):
        X = np.vstack([X, np.array(real_X)])
        y = np.concatenate([y, np.array(real_y)])
        w = np.concatenate([w, np.full(len(real_y), REAL_WEIGHT)])
    out = dict(base)
    out.update(clf=fit(X, y, w), version=datetime.utcnow().strftime("%Y%m%d-%H%M%S"),
               trained_on={"synthetic": int(len(base["y_synth"])), "real_labelled": int(len(real_y))})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=600)
    a = ap.parse_args()
    t0 = time.time()
    X, y, kinds, verdicts = build(a.n)
    idx = np.arange(len(y))
    tr, te = train_test_split(idx, test_size=0.25, random_state=1, stratify=y)
    clf = fit(X[tr], y[tr])
    p = clf.predict_proba(X[te])[:, 1]
    v = np.array(verdicts)
    by_kind = {}
    for k in sorted(set(kinds)):
        for lab in (0, 1):
            m = [i for i in idx if kinds[i] == k and y[i] == lab]
            if m:
                by_kind[f"{'tampered' if lab else 'genuine'}:{k}"] = {
                    "n": len(m), **{x: int((v[m] == x).sum()) for x in ("pass", "review", "fail")}}
    g, f = idx[y == 0], idx[y == 1]
    metrics = {
        "holdout_auc": round(float(roc_auc_score(y[te], p)), 4),
        "holdout_accuracy": round(float(((p > 0.5) == y[te]).mean()), 4),
        "genuine_passed": round(float((v[g] == "pass").mean()), 4),
        "genuine_failed": round(float((v[g] == "fail").mean()), 4),
        "tampered_caught": round(float((v[f] != "pass").mean()), 4),
        "tampered_passed": round(float((v[f] == "pass").mean()), 4),
        "by_kind": by_kind, "n": int(len(y)),
        "note": "Measured on synthetic documents only. Not an estimate of real-world accuracy."}
    obj = {"clf": fit(X, y), "features": model.FEATURES, "metrics": metrics, "X_synth": X, "y_synth": y,
           "version": datetime.utcnow().strftime("%Y%m%d-%H%M%S"),
           "trained_on": {"synthetic": int(len(y)), "real_labelled": 0}}
    joblib.dump(obj, model.PATH, compress=3)
    import json
    print(json.dumps(metrics, indent=1))
    print(f"saved {model.PATH} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
