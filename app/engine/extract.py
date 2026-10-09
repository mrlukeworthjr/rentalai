"""Turn an uploaded PDF or image into positioned text plus the raw structural
facts the forensic checks need. Nothing in here decides whether a document is
genuine; it only reports what is physically in the file."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pymupdf
import pytesseract
from PIL import Image

MAX_PAGES = 12
OCR_DPI = 200


@dataclass
class Span:
    text: str
    bbox: tuple
    page: int
    font: str | None = None
    size: float | None = None


@dataclass
class Row:
    text: str
    bbox: tuple
    page: int
    spans: list


@dataclass
class Page:
    number: int
    width: float
    height: float
    spans: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    ocr: bool = False
    image_cover: float = 0.0        # share of the page covered by raster images
    visible_text_spans: int = 0     # real (non-OCR-layer) text objects
    invisible_text_spans: int = 0   # hidden OCR text layer objects
    patches: list = field(default_factory=list)   # white filled rectangles
    annots: list = field(default_factory=list)
    jpeg_images: list = field(default_factory=list)  # raw bytes of large JPEGs
    overlay: list = field(default_factory=list)      # live text found on top of a scanned page


@dataclass
class Doc:
    kind: str                       # "pdf" | "image"
    pages: list
    meta: dict = field(default_factory=dict)
    n_eof: int = 1
    linearized: bool = False
    encrypted: bool = False
    exif_software: str | None = None
    image_format: str | None = None
    raw: bytes = b""

    @property
    def text(self):
        return "\n".join(r.text for p in self.pages for r in p.rows)

    @property
    def ocr_used(self):
        return any(p.ocr for p in self.pages)


class Unreadable(Exception):
    pass


def sniff(data: bytes) -> str | None:
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def build_rows(spans, page_no):
    """Group spans that sit on the same baseline into reading-order rows."""
    rows, cur, cur_y = [], [], None
    for s in sorted(spans, key=lambda s: ((s.bbox[1] + s.bbox[3]) / 2, s.bbox[0])):
        yc = (s.bbox[1] + s.bbox[3]) / 2
        h = max(s.bbox[3] - s.bbox[1], 1)
        if cur and abs(yc - cur_y) > 0.55 * h:
            rows.append(cur)
            cur = []
        if not cur:
            cur_y = yc
        cur.append(s)
    if cur:
        rows.append(cur)
    out = []
    for r in rows:
        r.sort(key=lambda s: s.bbox[0])
        bbox = (min(s.bbox[0] for s in r), min(s.bbox[1] for s in r),
                max(s.bbox[2] for s in r), max(s.bbox[3] for s in r))
        out.append(Row(" ".join(s.text for s in r), bbox, page_no, r))
    return out


def ocr_spans(img: Image.Image, page_no: int, scale: float = 1.0):
    try:
        d = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT,
                                      config="--psm 6")
    except Exception as e:  # tesseract missing or crashed
        raise Unreadable(f"OCR failed: {e}")
    spans = []
    for i, t in enumerate(d["text"]):
        t = t.strip()
        if not t or float(d["conf"][i]) < 30:
            continue
        x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
        spans.append(Span(t, (x * scale, y * scale, (x + w) * scale, (y + h) * scale),
                          page_no, None, h * scale))
    return spans


def _load_pdf(data: bytes) -> Doc:
    try:
        pdf = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise Unreadable(f"The PDF could not be opened ({e}).")
    if pdf.needs_pass:
        raise Unreadable("The PDF is password-protected.")
    if pdf.page_count == 0:
        raise Unreadable("The PDF has no pages.")
    doc = Doc("pdf", [], dict(pdf.metadata or {}), raw=data)
    doc.n_eof = len(re.findall(rb"%%EOF", data))
    doc.linearized = b"/Linearized" in data[:2048]
    doc.encrypted = bool(pdf.is_encrypted)

    for i, pg in enumerate(pdf):
        if i >= MAX_PAGES:
            break
        page = Page(i + 1, pg.rect.width, pg.rect.height)
        area = max(pg.rect.width * pg.rect.height, 1)
        for b in pg.get_text("dict")["blocks"]:
            if b["type"] == 1:
                x0, y0, x1, y1 = b["bbox"]
                page.image_cover += max(0, x1 - x0) * max(0, y1 - y0) / area
                continue
            for ln in b["lines"]:
                for sp in ln["spans"]:
                    if sp["text"].strip():
                        page.spans.append(Span(sp["text"].strip(), tuple(sp["bbox"]),
                                               i + 1, sp["font"], round(sp["size"], 2)))
        try:
            for tr in pg.get_texttrace():
                if tr.get("type") == 3 or tr.get("opacity", 1) == 0:
                    page.invisible_text_spans += 1
                else:
                    page.visible_text_spans += 1
        except Exception:
            page.visible_text_spans = len(page.spans)
        try:
            for dr in pg.get_drawings():
                fill, r = dr.get("fill"), dr.get("rect")
                if fill and r and min(fill) > 0.97 and dr.get("color") is None:
                    if 20 < r.width * r.height < 0.03 * area:
                        page.patches.append(tuple(r))
        except Exception:
            pass
        for a in pg.annots() or []:
            page.annots.append({"type": a.type[1], "bbox": tuple(a.rect)})
        for im in pg.get_images(full=True):
            try:
                info = pdf.extract_image(im[0])
                if info["ext"] in ("jpeg", "jpg") and info["width"] * info["height"] > 250_000:
                    page.jpeg_images.append(info["image"])
            except Exception:
                pass

        chars = sum(len(s.text) for s in page.spans)
        if chars < 40 and page.image_cover > 0.3:
            # A scan or photo wrapped in a PDF: read it with OCR instead.
            pix = pg.get_pixmap(dpi=OCR_DPI)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            page.overlay = list(page.spans)
            page.spans = ocr_spans(img, i + 1, 72 / OCR_DPI)
            page.ocr = True
        elif page.image_cover > 0.6 and page.visible_text_spans:
            page.overlay = list(page.spans)
        page.rows = build_rows(page.spans, i + 1)
        doc.pages.append(page)
    return doc


def _load_image(data: bytes, fmt: str) -> Doc:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise Unreadable(f"The image could not be opened ({e}).")
    software = None
    try:
        software = img.getexif().get(0x0131)
    except Exception:
        pass
    rgb = img.convert("RGB")
    if max(rgb.size) > 3200:
        rgb.thumbnail((3200, 3200))
    k = rgb.width / img.width
    page = Page(1, img.width, img.height, ocr=True, image_cover=1.0)
    page.spans = ocr_spans(rgb, 1, 1 / k)
    page.rows = build_rows(page.spans, 1)
    if fmt == "jpeg":
        page.jpeg_images.append(data)
    return Doc("image", [page], {}, exif_software=str(software) if software else None,
               image_format=fmt, raw=data)


def load(data: bytes) -> Doc:
    fmt = sniff(data)
    if fmt is None:
        raise Unreadable("This is not a PDF, JPEG, PNG, TIFF or WebP file.")
    return _load_pdf(data) if fmt == "pdf" else _load_image(data, fmt)


def render_page(data: bytes, page_no: int, max_width: int = 1100) -> bytes:
    """PNG preview of one page, used by the evidence viewer."""
    fmt = sniff(data)
    if fmt == "pdf":
        pdf = pymupdf.open(stream=data, filetype="pdf")
        pg = pdf[page_no - 1]
        return pg.get_pixmap(matrix=pymupdf.Matrix(*(max_width / pg.rect.width,) * 2)).tobytes("png")
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
