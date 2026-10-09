"""Train the pixel-level tamper detector on images with known edits.

  python -m ml.train_pixel --n 3000

Each image is a generated pay stub or bank statement pushed through a simulated
capture (screenshot, scan or phone photo). Half are then edited in a known
region the way people do it — paint over and retype, copy a figure from
elsewhere on the page, splice in a figure from another capture — and saved again.
Because the edit is ours, every cell has a true label.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import time
from multiprocessing import Pool

import joblib
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from app.engine import pixel
from ml import synth

FONT_DIRS = ["/usr/share/fonts", "/usr/local/share/fonts"]
WANT = ("DejaVuSans.ttf", "DejaVuSerif.ttf", "DejaVuSansMono.ttf", "FreeSans.ttf", "FreeSerif.ttf", "FreeMono.ttf",
        "Carlito-Regular.ttf", "Caladea-Regular.ttf", "LiberationSans-Regular.ttf", "LiberationSerif-Regular.ttf")
_fonts = None


def fonts():
    global _fonts
    if _fonts is None:
        _fonts = [os.path.join(r, f) for d in FONT_DIRS if os.path.isdir(d) for r, _, fs in os.walk(d) for f in fs if f in WANT]
    return _fonts


def jpeg(img, q):
    b = io.BytesIO()
    img.save(b, "JPEG", quality=int(q))
    return Image.open(io.BytesIO(b.getvalue())).convert("RGB")


def capture(img, rng, mode=None):
    """Simulate how the document became a picture."""
    mode = mode or rng.choices(["screenshot", "scan", "photo"], [25, 40, 35])[0]
    if mode == "screenshot":
        return img, mode
    a = np.asarray(img, dtype=np.float32)
    nrng = np.random.default_rng(rng.randrange(1 << 30))
    if mode == "photo":
        h, w = a.shape[:2]
        ang = rng.uniform(0, 6.283)
        yy, xx = np.mgrid[0:h, 0:w]
        ramp = (np.cos(ang) * xx / w + np.sin(ang) * yy / h)
        a = a * (rng.uniform(0.82, 0.97) + rng.uniform(0.03, 0.14) * ramp)[..., None]
        a = a * np.array([1.0, rng.uniform(0.96, 1.0), rng.uniform(0.9, 1.0)])
        blur, sigma = rng.uniform(0.6, 1.3), rng.uniform(2, 6)
    else:
        a = a * rng.uniform(0.93, 1.0)
        blur, sigma = rng.uniform(0.3, 0.8), rng.uniform(0.8, 3)
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(blur))
    a = np.asarray(img, dtype=np.float32) + nrng.normal(0, sigma, (*img.size[::-1], 1))
    return jpeg(Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)), rng.uniform(60, 92)), mode


def _bg(img, box):
    x0, y0, x1, y1 = [int(v) for v in box]
    a = np.asarray(img.crop((max(x0 - 6, 0), max(y0 - 6, 0), x1 + 6, y1 + 6)), dtype=np.float32).reshape(-1, 3)
    return tuple(int(v) for v in np.percentile(a, 85, axis=0))


def paint(img, box, text, rng, careful):
    """White-out a figure and type a new one."""
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    d = ImageDraw.Draw(img)
    d.rectangle([x0 - 3, y0 - 2, x1 + 3, y1 + 2], fill=_bg(img, box) if careful or rng.random() < 0.5 else (255, 255, 255))
    f = ImageFont.truetype(rng.choice(fonts()), max(8, int((y1 - y0) * rng.uniform(0.74, 0.9))))
    ink = int(rng.uniform(0, 60)) if careful else 0
    d.text((x1 - d.textlength(text, font=f), y0 + rng.uniform(-1, 2)), text, fill=(ink, ink, ink), font=f)
    if careful:        # blend the patch so it is less crisp than the page around it
        reg = (x0 - 4, y0 - 3, x1 + 4, y1 + 3)
        img.paste(img.crop(reg).filter(ImageFilter.GaussianBlur(rng.uniform(0.4, 0.9))), reg)
    return (x0 - 3, y0 - 2, x1 + 3, y1 + 2)


def paste(img, box, src_img, src_box):
    """Copy a figure from elsewhere (same page or another capture) over this one."""
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    crop = src_img.crop([int(round(v)) for v in src_box]).resize((x1 - x0, y1 - y0), Image.BICUBIC)
    ImageDraw.Draw(img).rectangle([x0 - 2, y0 - 1, x1 + 2, y1 + 1], fill=_bg(img, box))
    img.paste(crop, (x0, y0))
    return (x0 - 2, y0 - 1, x1 + 2, y1 + 1)


def decorate(img, rng):
    """Genuine layout variety: rules, boxes, reversed-out bands and odd-sized text in other fonts.
    Real documents are full of these and none of them is an edit."""
    d = ImageDraw.Draw(img)
    W, H = img.size
    for _ in range(rng.choice([0, 0, 1, 2, 4])):
        x0, y0 = rng.uniform(0.03, 0.5) * W, rng.uniform(0.05, 0.85) * H
        d.rectangle([x0, y0, x0 + rng.uniform(0.2, 0.45) * W, y0 + rng.uniform(0.03, 0.3) * H],
                    outline=(0, 0, 0), width=rng.choice([1, 2, 3, 4]))
    for _ in range(rng.choice([0, 1, 3])):
        y = rng.uniform(0.05, 0.95) * H
        d.line([0.04 * W, y, 0.96 * W, y], fill=(0, 0, 0), width=rng.choice([1, 2, 3]))
    words = ["EMPLOYEE COPY", "Statement of Earnings", "CONFIDENTIAL", "Page 1 of 1", "Non-negotiable", "Advice of Deposit",
             "Retain for your records", "Member FDIC", "Year to date summary", "Thank you for banking with us"]
    for _ in range(rng.choice([0, 1, 2, 3])):
        f = ImageFont.truetype(rng.choice(fonts()), int(H * rng.uniform(0.009, 0.022)))
        d.text((rng.uniform(0.05, 0.6) * W, rng.uniform(0.72, 0.9) * H), rng.choice(words), fill=(0, 0, 0), font=f)
    if rng.random() < 0.35:
        y = rng.choice([0.015, 0.95]) * H
        d.rectangle([0.04 * W, y, 0.96 * W, y + 0.028 * H], fill=(0, 0, 0))
        f = ImageFont.truetype(rng.choice(fonts()), int(H * 0.016))
        d.text((rng.uniform(0.3, 0.45) * W, y + 0.005 * H), rng.choice(words).upper(), fill=(255, 255, 255), font=f)


def make_image(seed: int, fraud: bool):
    """-> (PIL image, [edited boxes in px], kind)"""
    rng = random.Random(seed)
    if rng.random() < 0.25:
        t = synth.bank_truth(rng)
        pdf = synth.render_bank(t)
        vals = [x[2] for x in t["txns"]] + [x[3] for x in t["txns"]]
    else:
        t = synth.stub_truth(rng)
        pdf = synth.render_stub(t)
        vals = [t["gross"], t["net"], t["total"], t["gross_ytd"], t["net_ytd"]] + [e[3] for e in t["earnings"]] + \
               [d[1] for d in t["deductions"]] + [d[2] for d in t["deductions"]]
    dpi = rng.choice([110, 130, 150, 170, 200])
    base, pg = synth.rasterize(pdf, dpi)
    decorate(base, rng)
    img, mode = capture(base, rng)
    kind, boxes = mode, []
    if fraud:
        k = dpi / 72
        found = {}
        for v in vals:
            hits = pg.search_for(synth.fm(v))
            if hits and v not in found:
                r = hits[0]
                found[v] = (r.x0 * k, r.y0 * k, r.x1 * k, r.y1 * k)
        targets = rng.sample(list(found), min(len(found), rng.choice([1, 1, 2, 3])))
        how = rng.choices(["paint", "paint_careful", "copy_move", "splice"], [30, 30, 20, 20])[0]
        other = None
        if how == "splice":
            other, _ = capture(base, rng, rng.choice(["scan", "photo", "screenshot"]))
        for v in targets:
            if how.startswith("paint"):
                new = synth.fm(synth.c2(v * synth.D(str(round(rng.uniform(1.2, 2.6), 2)))))
                boxes.append(paint(img, found[v], new, rng, how == "paint_careful"))
            else:
                src = rng.choice([s for s in found if s != v] or [v])
                boxes.append(paste(img, found[v], other if how == "splice" else img.copy(), found[src]))
        kind = f"{mode}+{how}"
    # Final save. Genuine files get re-saved too, so double compression alone is not a fraud signal.
    if rng.random() < (0.6 if fraud else 0.5):
        img = jpeg(img, rng.uniform(72, 95))
    return img, boxes, kind


def cell_labels(rows, cols, scale, boxes):
    """1 = edited, 0 = untouched, -1 = barely overlaps an edit (left out of training)."""
    y = np.zeros(len(rows), np.int8)
    C = pixel.CELL / scale
    for i, (r, c) in enumerate(zip(rows, cols)):
        cx0, cy0 = c * C, r * C
        for x0, y0, x1, y1 in boxes:
            ov = max(0, min(cx0 + C, x1) - max(cx0, x0)) * max(0, min(cy0 + C, y1) - max(cy0, y0)) / (C * C)
            if ov >= 0.2:
                y[i] = 1
            elif ov > 0 and y[i] == 0:
                y[i] = -1
    return y


def work(args):
    seed, fraud = args
    img, boxes, kind = make_image(seed, fraud)
    F, rows, cols, scale = pixel.features(img)
    return F, rows, cols, cell_labels(rows, cols, scale, boxes), int(fraud), kind, boxes, scale


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--procs", type=int, default=os.cpu_count() or 1)
    a = ap.parse_args()
    t0 = time.time()
    with Pool(a.procs) as pool:
        pages = pool.map(work, [(1000 + i, i % 2 == 1) for i in range(a.n)], chunksize=8)
    pages = [p for p in pages if len(p[0])]
    n = len(pages)
    fit, cal, test = pages[:int(n * .7)], pages[int(n * .7):int(n * .8)], pages[int(n * .8):]
    rs = np.random.default_rng(0)
    Xs, ys = [], []
    for F, _, _, y, *_ in fit:
        pos, neg = np.where(y == 1)[0], np.where(y == 0)[0]
        keep = np.concatenate([pos, rs.choice(neg, min(len(neg), 60), replace=False)])
        Xs.append(F[keep]); ys.append(y[keep])
    X, y = np.vstack(Xs), np.concatenate(ys)
    clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08, max_leaf_nodes=31, l2_regularization=1.0,
                                         class_weight="balanced", random_state=0).fit(X, y)

    def score(p):
        pr = clf.predict_proba(p[0])[:, 1]
        return pr, pixel.page_scores(pr, p[1], p[2])
    gen_cal = np.array([score(p)[1].max() for p in cal if not p[4]])
    t_page = float(max(0.5, np.quantile(gen_cal, 0.98)))
    t_cell = float(min(0.5, t_page * 0.6))
    model = {"clf": clf, "t_page": t_page, "t_cell": t_cell, "features": pixel.NAMES}

    ps, ls = [], []
    by, hit_loc, det = {}, 0, 0
    for p in test:
        pr, s = score(p)
        m = p[3] >= 0
        ps.append(pr[m]); ls.append(p[3][m])
        flagged = s.max() >= t_page
        b = by.setdefault(p[5], {"n": 0, "flagged": 0})
        b["n"] += 1; b["flagged"] += int(flagged)
        if p[4] and flagged:
            det += 1
            top = int(np.argmax(s))
            hit_loc += int(p[3][top] != 0)
    g = [v for k, v in by.items() if "+" not in k]
    f = [v for k, v in by.items() if "+" in k]
    rate = lambda vs: round(sum(v["flagged"] for v in vs) / max(1, sum(v["n"] for v in vs)), 4)
    metrics = {"pages": n, "training_cells": int(len(y)), "cell_auc": round(float(roc_auc_score(np.concatenate(ls), np.concatenate(ps))), 4),
               "genuine_pages_flagged": rate(g), "edited_pages_flagged": rate(f),
               "top_region_on_the_edit": round(hit_loc / max(det, 1), 4), "t_page": round(t_page, 3),
               "by_kind": {k: by[k] for k in sorted(by)},
               "note": "Measured on generated images with simulated capture and edits. Not real-world accuracy."}
    model["metrics"] = metrics
    joblib.dump(model, pixel.PATH, compress=3)
    print(json.dumps(metrics, indent=1))
    print(f"saved {pixel.PATH} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
