"""The learned layer. A gradient-boosted classifier scores the evidence the
checks produce. It ships trained on synthetic genuine/tampered documents and is
meant to be retrained on analyst-confirmed outcomes (see ml/train.py)."""
from __future__ import annotations

import os

import joblib
import numpy as np

CODES = ["F_EDITOR", "F_OFFICE", "F_MODIFIED", "F_NOMETA", "F_INCREMENTAL", "F_ANNOT", "F_TEXT_ON_SCAN",
         "F_STACKED", "F_PATCH", "F_FONT", "F_ALIGN", "F_SIZE", "F_SCAN", "F_ELA",
         "C_MISSING", "C_NET_MATH", "C_DED_SUM", "C_EARN_SUM", "C_RATE_HOURS", "C_FICA_SS", "C_FICA_MED",
         "C_YTD_LT", "C_YTD_PACE", "C_DATES", "C_FUTURE", "C_STALE", "C_PERIOD", "C_PREDATED", "C_ROUND",
         "C_NAME", "C_TYPE", "B_RUNNING", "B_ENDING", "B_SUMMARY", "B_DEPOSITS"]
# F_PIXEL (the pixel-level detector) is deliberately absent: on real scanned receipts it flags about half of
# genuine pages, so it is shown to reviewers as a note and kept out of the score. F_ELA is a retired slot.
NUMERIC = ["rel_net_err", "rel_fica_err", "n_revisions", "n_money_fonts", "ocr_used", "n_findings", "is_pdf"]
FEATURES = CODES + NUMERIC
WEIGHT = {"critical": 0.9, "high": 0.55, "medium": 0.25, "low": 0.07, "info": 0.0}
PATH = os.path.join(os.path.dirname(__file__), "model.joblib")
_model = None


def featurize(findings, doc, fields):
    from .forensics import base_family
    codes = {f["code"] for f in findings}
    v = [1.0 if c in codes else 0.0 for c in CODES]
    rel = lambda code: max([f["data"].get("rel", 0.0) for f in findings if f["code"].startswith(code)] or [0.0])
    v += [min(rel("C_NET_MATH"), 2.0), min(rel("C_FICA"), 2.0),
          float(min(doc.n_eof, 6)) if doc else 0.0,
          float(len({base_family(m.font) for m in fields.money_spans if m.font})) if fields else 0.0,
          float(doc.ocr_used) if doc else 0.0,
          float(len([f for f in findings if f["severity"] != "info" and f["code"] != "F_PIXEL"])),
          float(doc.kind == "pdf") if doc else 0.0]
    return v


def load():
    global _model
    if _model is None and os.path.exists(PATH):
        try:
            _model = joblib.load(PATH)
        except Exception:
            _model = None
    return _model


def set_model(m):
    global _model
    _model = m


def rule_score(findings):
    p = 1.0
    for f in findings:
        if f["code"] != "F_PIXEL":          # reviewer note only
            p *= 1 - WEIGHT.get(f["severity"], 0)
    return 1 - p


def score(features, findings):
    """Return (risk 0-1, model details)."""
    rs = rule_score(findings)
    m = load()
    if not m:
        return rs, {"available": False, "probability": None, "rule_score": round(rs, 3)}
    x = np.array([features])
    p = float(m["clf"].predict_proba(x)[0, 1])
    drivers = []
    for i, name in enumerate(FEATURES):
        if features[i]:
            x2 = x.copy()
            x2[0, i] = 0
            drop = p - float(m["clf"].predict_proba(x2)[0, 1])
            if drop > 0.01:
                drivers.append({"feature": name, "effect": round(drop, 3)})
    drivers.sort(key=lambda d: -d["effect"])
    return max(rs, p) if rs > 0.2 else (rs + p) / 2, {
        "available": True, "probability": round(p, 4), "rule_score": round(rs, 3), "drivers": drivers[:5],
        "version": m.get("version"), "trained_on": m.get("trained_on")}


def verdict(findings, prob):
    sev = [f["severity"] for f in findings]
    crit, high, med = sev.count("critical"), sev.count("high"), sev.count("medium")
    if crit or high >= 2 or rule_score(findings) >= 0.85 or (prob is not None and prob >= 0.9 and high):
        return "fail"
    if high or med >= 2 or (prob is not None and prob >= 0.5):
        return "review"
    return "pass"
