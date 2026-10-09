"""Pixel-level tamper detector for photos, scans and screenshots.

The page is cut into small cells. For every cell that contains ink we measure
how the pixels look (ink darkness, edge sharpness, edge softness, background
noise, colour cast, JPEG error levels) and how far each measure sits from the
rest of the same page. A gradient-boosted classifier, trained on images whose
edited regions are known (ml/train_pixel.py), scores each cell. Neighbouring
high-scoring cells become a highlighted region.

An indicator for a reviewer, not proof: see the metrics stored with the model.
"""
from __future__ import annotations

import io
import os

import joblib
import numpy as np
from PIL import Image
from scipy import ndimage

CELL = 24
MAX_SIDE = 2600
PATH = os.path.join(os.path.dirname(__file__), "pixel.joblib")
RAW = ["ink_frac", "dark", "bg", "contrast", "sharp", "soft", "noise", "chroma", "hp",
       "ela90_ink", "ela90_max", "ela75_ink", "ela90_bg"]
NAMES = RAW + ["rel_" + n for n in RAW] + ["ctx_ela90", "ctx_noise", "ctx_sharp", "ctx_soft", "ctx_cells"]
_model = None


def _ela(img, a, q):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=q)
    b = np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"), dtype=np.float32)
    return np.abs(a - b).sum(axis=2)


def _blocks(x, h, w):
    return x[:h, :w].reshape(h // CELL, CELL, w // CELL, CELL).transpose(0, 2, 1, 3).reshape(h // CELL, w // CELL, -1)


def features(img: Image.Image):
    """-> (F [n, len(NAMES)], rows, cols, scale) for every inked cell. scale maps cell coords back to input pixels."""
    img = img.convert("RGB")
    scale = 1.0
    if max(img.size) > MAX_SIDE:
        scale = MAX_SIDE / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    a = np.asarray(img, dtype=np.float32)
    h, w = (a.shape[0] // CELL) * CELL, (a.shape[1] // CELL) * CELL
    if h < CELL * 8 or w < CELL * 8:
        return np.zeros((0, len(NAMES)), np.float32), np.array([], int), np.array([], int), scale
    g = a.mean(axis=2)
    grad = np.abs(np.diff(g, axis=1, prepend=g[:, :1])) + np.abs(np.diff(g, axis=0, prepend=g[:1]))
    hp = np.abs(g - ndimage.uniform_filter(g, 3))
    chroma = np.abs(a[..., 0] - a[..., 1]) + np.abs(a[..., 1] - a[..., 2])
    e90, e75 = _ela(img, a, 90), _ela(img, a, 75)

    G, GR, HP, CH, E90, E75 = (_blocks(x, h, w) for x in (g, grad, hp, chroma, e90, e75))
    bg = np.percentile(G, 90, axis=2)
    dark = np.percentile(G, 5, axis=2)
    con = bg - dark
    c3 = con[..., None]
    ink = G < (bg[..., None] - 0.5 * c3)
    edge = (G < bg[..., None] - 0.1 * c3)
    mid = edge & (G > dark[..., None] + 0.25 * c3) & (G < bg[..., None] - 0.25 * c3)
    paper = G > bg[..., None] - 12
    cnt = lambda m: np.maximum(m.sum(axis=2), 1)
    mean = lambda x, m: (x * m).sum(axis=2) / cnt(m)
    ink_frac = ink.mean(axis=2)
    k = max(1, CELL * CELL // 10)
    sharp = np.sort(GR, axis=2)[..., -k:].mean(axis=2) / (con + 1)
    soft = mid.sum(axis=2) / cnt(edge)
    pm = mean(G, paper)
    noise = np.sqrt(np.maximum(mean(G * G, paper) - pm * pm, 0))
    raw = np.stack([ink_frac, dark, bg, con, sharp, soft, noise, mean(CH, edge), mean(HP, edge),
                    mean(E90, edge), (E90 * edge).max(axis=2), mean(E75, edge), mean(E90, paper)], axis=2)
    sel = (ink_frac > 0.03) & (con > 50)
    rows, cols = np.where(sel)
    X = raw[sel]
    if len(X) < 12:
        return np.zeros((0, len(NAMES)), np.float32), np.array([], int), np.array([], int), scale
    med = np.median(X, axis=0)
    mad = np.median(np.abs(X - med), axis=0) * 1.4826 + 1e-3 + 0.02 * np.abs(med)
    rel = np.clip((X - med) / mad, -30, 30)
    i = RAW.index
    ctx = np.tile([med[i("ela90_ink")], med[i("noise")], med[i("sharp")], med[i("soft")], np.log(len(X))], (len(X), 1))
    return np.hstack([X, rel, ctx]).astype(np.float32), rows, cols, scale


def page_scores(p, rows, cols):
    """Cell probabilities -> smoothed score per cell (an edit almost always spans two cells side by side)."""
    if not len(p):
        return np.zeros(0)
    grid = np.zeros((rows.max() + 1, cols.max() + 3), np.float32)
    grid[rows, cols + 1] = p
    nb = np.maximum(grid[rows, cols], grid[rows, cols + 2])
    return 0.6 * p + 0.4 * nb


def load():
    global _model
    if _model is None and os.path.exists(PATH):
        try:
            _model = joblib.load(PATH)
        except Exception:
            _model = None
    return _model


def detect(img: Image.Image, model=None):
    """-> {"score": float, "flagged": bool, "regions": [{"bbox": (x0, y0, x1, y1), "score": float}]}"""
    m = model or load()
    if not m:
        return None
    F, rows, cols, scale = features(img)
    if not len(F):
        return {"score": 0.0, "flagged": False, "regions": []}
    p = m["clf"].predict_proba(F)[:, 1]
    s = page_scores(p, rows, cols)
    out = {"score": float(s.max()), "flagged": bool(s.max() >= m["t_page"]), "regions": []}
    if out["flagged"]:
        grid = np.zeros((rows.max() + 1, cols.max() + 1), bool)
        hot = p >= m["t_cell"]
        grid[rows[hot], cols[hot]] = True
        lab, n = ndimage.label(ndimage.binary_dilation(grid, structure=np.ones((1, 3), bool)))
        best = {}
        for r, c, sc in zip(rows[hot], cols[hot], s[hot]):
            b = best.setdefault(lab[r, c], [c, r, c, r, 0.0])
            b[0], b[1], b[2], b[3], b[4] = min(b[0], c), min(b[1], r), max(b[2], c), max(b[3], r), max(b[4], sc)
        for b in sorted(best.values(), key=lambda b: -b[4])[:6]:
            if b[4] >= m["t_page"]:
                out["regions"].append({"bbox": tuple(v * CELL / scale for v in (b[0], b[1], b[2] + 1, b[3] + 1)),
                                       "score": float(b[4])})
        out["flagged"] = bool(out["regions"])
    return out
