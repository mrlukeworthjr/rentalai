"""File-level forensics: how the file was produced and whether its contents
were altered after the fact. Each check returns findings that say what was
seen, why it matters, and what evidence would clear it."""
from __future__ import annotations

import io
import re
from collections import Counter
from datetime import datetime

import numpy as np
from PIL import Image

ASK_ORIGINAL = ("Upload the original PDF downloaded directly from your payroll or bank portal — "
                "not a scan, photo, screenshot or edited copy.")
ASK_CLEAR = "Upload a clearer copy: the whole page visible, flat, in focus and well lit."
ASK_CONNECT = "If you cannot get the original file, ask the leasing office about verifying income directly with your employer or bank."

IMAGE_EDITORS = ("photoshop", "illustrator", "gimp", "canva", "inkscape", "affinity", "pixelmator",
                 "paint.net", "coreldraw", "figma", "indesign", "picsart", "snapseed")
PDF_EDITORS = ("pdfescape", "sejda", "ilovepdf", "smallpdf", "pdffiller", "dochub", "pdf-xchange",
               "phantompdf", "foxit pdf editor", "nitro", "pdfelement", "wondershare", "soda pdf",
               "pdf24", "xodo", "power pdf", "master pdf", "pdf expert", "acrobat pro", "pdfsam",
               "pdf candy", "libreoffice draw", "paystub generator", "stub creator", "check stub maker")
OFFICE = ("microsoft word", "microsoft® word", "microsoft excel", "microsoft® excel", "google docs",
          "google sheets", "libreoffice", "openoffice", "pages", "wps ", "writer", "powerpoint")


def finding(code, severity, title, what, why, fix, ask=None, refs=None, category="file", **data):
    return {"code": code, "category": category, "severity": severity, "title": title, "what": what,
            "why": why, "fix": fix, "applicant_request": ask, "evidence": refs or [], "data": data}


def base_family(font: str | None) -> str:
    if not font:
        return ""
    f = font.split("+")[-1]
    f = re.sub(r"[-,]?(Bold|Italic|Oblique|Regular|Roman|Medium|Semibold|Light|MT|PS|Black|It|Bd)+$", "", f, flags=re.I)
    f = re.sub(r"[-,](Bold|Italic|Oblique|Regular|BoldOblique|BoldItalic|Roman|Medium).*$", "", f, flags=re.I)
    return re.sub(r"(MT|PS)$", "", f).strip("-, ").lower()


def pdf_date(s):
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?", s or "")
    if not m:
        return None
    try:
        return datetime(*[int(g) if g else 0 for g in m.groups()])
    except ValueError:
        return None


def _ref(page, bbox):
    return {"page": page, "bbox": [round(x, 1) for x in bbox]}


def ela_hotspot(jpeg: bytes):
    """Error-level analysis. Regions pasted or retyped into a JPEG recompress
    differently from the rest of the picture. An indicator, never proof."""
    try:
        im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    except Exception:
        return None
    scale = 1.0
    if max(im.size) > 1800:
        scale = 1800 / max(im.size)
        im = im.resize((int(im.width * scale), int(im.height * scale)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    a = np.asarray(im, dtype=np.int16)
    b = np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"), dtype=np.int16)
    diff = np.abs(a - b).sum(axis=2).astype(np.float32)
    gray = a.mean(axis=2)
    edge = (np.abs(np.diff(gray, axis=1, prepend=gray[:, :1])) + np.abs(np.diff(gray, axis=0, prepend=gray[:1]))) > 40
    B = 24
    h, w = (gray.shape[0] // B) * B, (gray.shape[1] // B) * B
    if h < B * 6 or w < B * 6:
        return None
    e = edge[:h, :w].reshape(h // B, B, w // B, B)
    d = (diff[:h, :w] * edge[:h, :w]).reshape(h // B, B, w // B, B)
    cnt = e.sum(axis=(1, 3))
    score = d.sum(axis=(1, 3)) / np.maximum(cnt, 1)
    ok = cnt > B * B * 0.04
    if ok.sum() < 20:
        return None
    vals = score[ok]
    med = np.median(vals)
    mad = np.median(np.abs(vals - med)) * 1.4826 + 0.5
    z = np.where(ok, (score - med) / mad, 0)
    hot = z > 7
    n = int(hot.sum())
    if n < 2 or n > 0.12 * ok.sum():
        return None
    ys, xs = np.where(hot)
    box = (xs.min() * B / scale, ys.min() * B / scale, (xs.max() + 1) * B / scale, (ys.max() + 1) * B / scale)
    return {"bbox": box, "z": float(z.max()), "blocks": n}


def file_checks(doc, fields):
    out = []
    meta = doc.meta or {}
    producer = " ".join(str(meta.get(k) or "") for k in ("producer", "creator")).strip()
    low = producer.lower()

    if doc.kind == "pdf":
        hit = next((e for e in IMAGE_EDITORS if e in low), None)
        hit2 = next((e for e in PDF_EDITORS if e in low), None)
        hit3 = next((e for e in OFFICE if e in low), None)
        if hit or hit2:
            out.append(finding(
                "F_EDITOR", "high", "File was saved by editing software",
                f"The PDF's metadata says it was produced by “{producer}”.",
                "Payroll and bank systems generate PDFs with their own server software. A file last written by "
                f"{'an image editor' if hit else 'a PDF editing tool'} has been opened and re-saved by a person, "
                "which is the step where figures get changed.",
                "The untouched file exported from the payroll or bank portal, whose metadata shows the provider's "
                "own generator rather than an editor.", ASK_ORIGINAL, producer=producer))
        elif hit3:
            out.append(finding(
                "F_OFFICE", "high", "Document was made in an office program",
                f"The PDF was produced by “{producer}”.",
                "Large payroll providers and banks do not issue statements from word processors or spreadsheets. "
                "Small employers sometimes do, so this needs corroboration rather than rejection.",
                "Independent confirmation of the income: a direct payroll connection, employer verification, or "
                "bank statements showing the matching deposits.", ASK_CONNECT, producer=producer))
        c, m = pdf_date(meta.get("creationDate")), pdf_date(meta.get("modDate"))
        if c and m and (m - c).total_seconds() > 120:
            out.append(finding(
                "F_MODIFIED", "medium", "File was changed after it was created",
                f"Created {c:%b %d, %Y %H:%M}, last modified {m:%b %d, %Y %H:%M} "
                f"({_gap(m - c)} later).",
                "A statement exported by a provider is written once. A later modification time means the file was "
                "re-saved — sometimes innocently (printing to PDF, merging), sometimes after edits.",
                "A fresh download from the provider, where the created and modified times match.",
                ASK_ORIGINAL, created=str(c), modified=str(m)))
        if not producer and not meta.get("creationDate"):
            out.append(finding(
                "F_NOMETA", "low", "File metadata has been stripped",
                "The PDF carries no producer, creator or creation date.",
                "Provider-generated PDFs normally identify the software that made them. Empty metadata is what "
                "remains after a file passes through a converter or a metadata scrubber.",
                "The original export from the provider.", ASK_ORIGINAL))
        saves = doc.n_eof - (1 if doc.linearized else 0)
        if saves > 1:
            out.append(finding(
                "F_INCREMENTAL", "medium", "File contains later revisions layered on the original",
                f"The file holds {saves} saved revisions. Changes were appended after the document was first written.",
                "PDF editors append edits to the end of a file instead of rewriting it. A machine-generated "
                "statement has one revision.",
                "The single-revision file as exported by the provider.", ASK_ORIGINAL, revisions=saves))

        for p in doc.pages:
            marks = [a for a in p.annots if a["type"] in ("FreeText", "Stamp", "Redact", "Square", "Ink", "Line", "Polygon")]
            if marks:
                out.append(finding(
                    "F_ANNOT", "high", "Text or shapes were added on top of the page",
                    f"Page {p.number} has {len(marks)} added annotation(s) of type "
                    f"{', '.join(sorted({a['type'] for a in marks}))}.",
                    "Annotations are objects a person places over a finished page with a PDF tool. They can hide "
                    "or replace the printed figures while the original content stays underneath.",
                    "A copy of the statement with no annotations or overlays.", ASK_ORIGINAL,
                    [_ref(p.number, a["bbox"]) for a in marks]))
            if p.image_cover > 0.6 and p.visible_text_spans >= 1 and p.overlay:
                out.append(finding(
                    "F_TEXT_ON_SCAN", "high", "Typed text sits on top of a scanned image",
                    f"Page {p.number} is a picture of a document with {p.visible_text_spans} live text object(s) "
                    "placed over it.",
                    "A scan is one flat picture. Live text over it means characters were typed onto the image "
                    "afterwards — the standard way to overwrite a figure on a scanned statement.",
                    "The original digital file, or an unaltered scan with nothing layered on it.", ASK_ORIGINAL,
                    [_ref(s.page, s.bbox) for s in p.overlay[:8]],
                    typed=[s.text for s in p.overlay[:8]]))
            # Different text objects occupying the same spot.
            stacked = []
            sp = p.spans if not p.ocr else []
            for i, a in enumerate(sp):
                for b in sp[i + 1:]:
                    ix = min(a.bbox[2], b.bbox[2]) - max(a.bbox[0], b.bbox[0])
                    iy = min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])
                    if ix <= 0 or iy <= 0 or a.text == b.text:
                        continue
                    small = min((a.bbox[2] - a.bbox[0]) * (a.bbox[3] - a.bbox[1]),
                                (b.bbox[2] - b.bbox[0]) * (b.bbox[3] - b.bbox[1]))
                    if small > 0 and ix * iy / small > 0.45:
                        stacked.append((a, b))
            if stacked:
                a, b = stacked[0]
                out.append(finding(
                    "F_STACKED", "critical", "A value was covered and retyped",
                    f"Page {p.number} has {len(stacked)} place(s) where two different pieces of text occupy the same "
                    f"position — for example “{a.text}” and “{b.text}”.",
                    "The earlier text is still in the file beneath the visible one. That only happens when someone "
                    "covers a value and types a new one over it.",
                    "A statement in which each figure exists once. The hidden values shown here are likely the "
                    "true ones.", ASK_ORIGINAL, [_ref(p.number, a.bbox) for a, _ in stacked[:6]],
                    pairs=[[a.text, b.text] for a, b in stacked[:6]]))
            elif 0 < len(p.patches) <= 4 and not p.ocr:
                covered = [r for r in p.patches if any(
                    r[0] <= (s.bbox[0] + s.bbox[2]) / 2 <= r[2] and r[1] <= (s.bbox[1] + s.bbox[3]) / 2 <= r[3]
                    for s in p.spans)]
                if covered:
                    out.append(finding(
                        "F_PATCH", "high", "White boxes were drawn behind specific values",
                        f"Page {p.number} has {len(covered)} isolated white rectangle(s) sitting directly under text.",
                        "A blank box under a single figure is how a value is whited-out before a new number is "
                        "typed on top. Layout elements repeat across a page; these do not.",
                        "The original export, without cover-up shapes.", ASK_ORIGINAL,
                        [_ref(p.number, r) for r in covered]))

        # Fonts used for figures.
        ms = [v for v in fields.money_spans if v.font]
        if len(ms) >= 5:
            fam = Counter(base_family(v.font) for v in ms)
            dom, n = fam.most_common(1)[0]
            odd = [v for v in ms if base_family(v.font) != dom]
            if odd and n >= 0.6 * len(ms) and len(odd) <= 6:
                out.append(finding(
                    "F_FONT", "high", "Some figures use a different typeface from the rest",
                    f"{n} of {len(ms)} amounts are set in {dom.title()}; {len(odd)} are set in "
                    f"{', '.join(sorted({base_family(v.font).title() or 'an unknown font' for v in odd}))} "
                    f"({', '.join(_m(v.value) for v in odd[:4])}).",
                    "Payroll software prints every amount with one font. A figure in another typeface was placed "
                    "by a different tool than the one that made the document.",
                    "A statement where every amount is rendered in the same font, as the provider's system "
                    "produces it.", ASK_ORIGINAL, [v.ref() for v in odd],
                    dominant=dom, outliers=[str(v.value) for v in odd]))
        # Right-edge alignment of the main amounts column.
        cur = [l.current for l in (fields.earnings + fields.deductions) if l.current is not None]
        if len(cur) >= 4 and not doc.ocr_used:
            edges = Counter(round(v.bbox[2]) for v in cur)
            edge, k = edges.most_common(1)[0]
            off = [v for v in cur if 0.9 < abs(v.bbox[2] - edge) < 14]
            if k >= len(cur) - 2 and off and len(off) <= 2:
                out.append(finding(
                    "F_ALIGN", "medium", "A figure is out of line with its column",
                    f"{k} amounts end at the same right edge; {', '.join(_m(v.value) for v in off)} sits "
                    f"{abs(off[0].bbox[2] - edge):.1f} pt off it.",
                    "Generated statements align every amount in a column to the same edge. A hand-placed value "
                    "is almost never positioned exactly.",
                    "A statement whose amounts align as the provider prints them.", ASK_ORIGINAL,
                    [v.ref() for v in off]))
        for l in fields.earnings + fields.deductions + [x for x in (fields.gross, fields.net, fields.total_deductions) if x]:
            if doc.ocr_used:
                break
            if l.current and l.ytd and l.current.size and l.ytd.size and abs(l.current.size - l.ytd.size) > 0.3:
                out.append(finding(
                    "F_SIZE", "medium", "Two amounts on one line are different sizes",
                    f"On “{l.label}”, {_m(l.current.value)} is {l.current.size} pt and {_m(l.ytd.value)} is "
                    f"{l.ytd.size} pt.",
                    "Amounts on the same line are printed by the same instruction at the same size. A mismatch "
                    "means one was replaced.",
                    "A statement with consistent type sizes on each line.", ASK_ORIGINAL,
                    [l.current.ref(), l.ytd.ref()]))
                break

    # Scans, photos and screenshots.
    scanned = doc.kind == "image" or all(
        p.ocr or (p.image_cover > 0.6 and not p.visible_text_spans) for p in doc.pages)
    if scanned:
        kind = "a photo or image file" if doc.kind == "image" else "a scan wrapped in a PDF"
        out.append(finding(
            "F_SCAN", "low", "This is a picture of a document, not the original file",
            f"The upload is {kind}. Its text had to be read with OCR.",
            "A picture carries none of the internal structure — fonts, metadata, revision history — that lets "
            "an original be authenticated, and pictures are easy to retouch. It can be read but not fully verified.",
            "The original PDF from the payroll or bank portal. That file can be checked completely.",
            ASK_ORIGINAL))
    if doc.exif_software and any(e in doc.exif_software.lower() for e in IMAGE_EDITORS):
        out.append(finding(
            "F_EDITOR", "high", "Image was saved by editing software",
            f"The image's embedded data names “{doc.exif_software}” as the software that last saved it.",
            "A camera or scanner writes its own name there. An image editor's name means the picture was opened "
            "and re-saved in a tool built for altering images.",
            "The original PDF from the provider, or an unedited photo straight from the phone or scanner.",
            ASK_ORIGINAL, producer=doc.exif_software))
    for p in doc.pages:
        for jp in p.jpeg_images[:1]:
            hot = ela_hotspot(jp)
            if hot:
                box = hot["bbox"]
                if doc.kind == "pdf":       # image pixels -> page points
                    try:
                        w, h = Image.open(io.BytesIO(jp)).size
                        box = (box[0] * p.width / w, box[1] * p.height / h, box[2] * p.width / w, box[3] * p.height / h)
                    except Exception:
                        pass
                out.append(finding(
                    "F_ELA", "medium", "Part of the image was compressed differently from the rest",
                    f"On page {p.number}, {hot['blocks']} small region(s) show a compression signature far from the "
                    "rest of the picture.",
                    "When something is pasted or retyped into a JPEG and saved again, the new area carries a "
                    "different compression history. This is an indicator that warrants a look, not proof by itself.",
                    "The original PDF from the provider, which removes the question entirely.", ASK_ORIGINAL,
                    [_ref(p.number, box)], z=round(hot["z"], 1)))
    return out


def _gap(td):
    s = int(td.total_seconds())
    if s < 3600:
        return f"{s // 60} minutes"
    if s < 172800:
        return f"{s // 3600} hours"
    return f"{s // 86400} days"


def _m(v):
    return f"${v:,.2f}"
