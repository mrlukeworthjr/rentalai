"""Test the pixel detector on public forgery sets it was never trained on.

  python -m ml.eval_public --findit /path/to/findit2 --payslips /path/to/sample_dataset

Find it again (ICDAR 2023, L3i): 988 scanned receipts, 163 forged by hand.
  https://l3i-share.univ-lr.fr/2023Finditagain/index.html
L3i payslip forgery sample: 6 genuine and 6 forged payslips.
  https://navidomass.univ-lr.fr/ForgeryDataset/
Neither set states a licence, so they are used here for measurement only and are not redistributed.
"""
import argparse
import ast
import csv
import glob
import json
import os
import re
from multiprocessing import get_context

from PIL import Image

from app.engine import pixel


def overlap(a, b):
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def run(args):
    path, forged, boxes = args
    try:
        r = pixel.detect(Image.open(path).convert("RGB"))
    except Exception:
        return None
    hit = any(overlap(R["bbox"], b) for R in r["regions"] for b in boxes)
    return forged, r["flagged"], hit, r["score"]


def summarise(name, res):
    res = [r for r in res if r]
    g = [r for r in res if not r[0]]
    f = [r for r in res if r[0]]
    auc = None
    if g and f:
        from sklearn.metrics import roc_auc_score
        auc = round(float(roc_auc_score([r[0] for r in res], [r[3] for r in res])), 3)
    out = {"set": name, "genuine": len(g), "forged": len(f),
           "genuine_flagged": sum(r[1] for r in g), "forged_flagged": sum(r[1] for r in f),
           "forged_flagged_on_the_edit": sum(r[2] for r in f), "page_auc": auc}
    print(json.dumps(out))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--findit")
    ap.add_argument("--payslips")
    a = ap.parse_args()
    out = []
    if a.payslips:
        jobs = []
        for f in sorted(glob.glob(a.payslips + "/**/*.tif", recursive=True)):
            x, boxes = f.replace(".tif", ".vt.xml"), []
            if os.path.exists(x):
                for m in re.finditer(r'x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)"', open(x, errors="replace").read()):
                    x0, y0, w, h = map(int, m.groups())
                    boxes.append((x0, y0, x0 + w, y0 + h))
            jobs.append((f, "-g." not in f, boxes))
        out.append(summarise("L3i payslip sample", [run(j) for j in jobs]))
    if a.findit:
        jobs = []
        for split in ("train", "val", "test"):
            for row in csv.DictReader(open(os.path.join(a.findit, split + ".txt"), encoding="utf-8", errors="replace")):
                boxes = []
                if row["forged"] == "1":
                    try:
                        for reg in ast.literal_eval(row["forgery annotations"])["regions"]:
                            s = reg["shape_attributes"]
                            if reg["region_attributes"].get("Original area") != "yes":
                                boxes.append((s["x"], s["y"], s["x"] + s["width"], s["y"] + s["height"]))
                    except Exception:
                        pass
                jobs.append((os.path.join(a.findit, split, row["image"]), row["forged"] == "1", boxes))
        with get_context("spawn").Pool() as pool:   # spawn, not fork: OpenMP threads do not survive a fork
            out.append(summarise("Find it again receipts", pool.map(run, jobs, chunksize=16)))
    return out


if __name__ == "__main__":
    main()
