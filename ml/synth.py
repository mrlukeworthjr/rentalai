"""Synthetic pay stubs and bank statements, genuine and tampered.

Used to bootstrap the model and to test the engine. Tampering is done the way
people actually do it: retyping values in a PDF editor, whiting-out and typing
over, rebuilding the document with inflated numbers, or editing a picture.
Synthetic data is a starting point only — see README, "Making the model better".
"""
from __future__ import annotations

import io
import os
import random
import tempfile
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal as D

import pymupdf
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

FIRST = ["Maria", "James", "Aisha", "Daniel", "Priya", "Marcus", "Elena", "Tyler", "Grace", "Omar", "Hannah", "Luis"]
LAST = ["Alvarez", "Bennett", "Chen", "Dawson", "Ellis", "Foster", "Gupta", "Hughes", "Ito", "Jensen", "Khan", "Lopez"]
EMPLOYERS = ["Northgate Logistics LLC", "Desert Bloom Dental", "Saguaro Building Supply", "Pinecrest Medical Group",
             "Harbor Light Foods Inc", "Copper State Electric", "Blue Mesa Software", "Valley Transit Services"]
BANKS = ["First Canyon Bank", "Sonoran Community Credit Union", "Meridian National Bank"]
FONTS = [("Helvetica", "Helvetica-Bold"), ("Times-Roman", "Times-Bold"), ("Courier", "Courier-Bold")]
PROVIDERS = [("ADP Workforce Now", "ADP PDF Engine 4.2"), ("Paychex Flex", "iText 7.2.5 (Paychex Inc.)"),
             ("Gusto", "PDFKit"), ("QuickBooks Payroll", "Intuit PDF Library 3.1"),
             ("Workday", "Apache FOP Version 2.6"), ("Paylocity", "Aspose.PDF for .NET 23.4")]
EDITORS = ["Adobe Photoshop 25.0", "Canva", "iLovePDF", "Sejda 3.2", "PDFescape Online", "Foxit PhantomPDF 12",
           "Wondershare PDFelement 10", "Smallpdf"]
PERIODS = {"weekly": (7, 52, D("40")), "biweekly": (14, 26, D("80")), "semimonthly": (15, 24, D("86.67")),
           "monthly": (30, 12, D("173.33"))}
FREQ_LABEL = {"weekly": "Weekly", "biweekly": "Bi-Weekly", "semimonthly": "Semi-Monthly", "monthly": "Monthly"}


def c2(v):
    return D(v).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def fm(v):
    return f"{v:,.2f}"


def pdf_ts(d: date, h=9, mi=30):
    return f"D:{d:%Y%m%d}{h:02d}{mi:02d}00-07'00'"


def stub_truth(rng: random.Random, pay_date: date | None = None, scale=D(1), base=None):
    """Internally consistent pay stub values."""
    if base:
        t = dict(base)
    else:
        freq = rng.choice(["weekly", "biweekly", "biweekly", "semimonthly", "monthly"])
        t = {"name": f"{rng.choice(FIRST)} {rng.choice(LAST)}", "employer": rng.choice(EMPLOYERS), "freq": freq,
             "rate": c2(rng.uniform(17, 62)), "salaried": rng.random() < 0.3, "ot": rng.random() < 0.3,
             "fed": D(str(round(rng.uniform(0.07, 0.16), 4))), "state": D(str(round(rng.choice([0, 0.025, 0.04]), 4))),
             "health": c2(rng.choice([0, 0, 45.5, 88.25, 131.4])), "k401": D(str(rng.choice([0, 0, 0.03, 0.05]))),
             "font": rng.choice(FONTS), "layout": rng.choice("AB"), "provider": rng.choice(PROVIDERS),
             "emp_id": rng.randint(10000, 99999)}
        t["pay_date"] = pay_date or date(2026, rng.randint(2, 9), rng.randint(1, 28))
    if pay_date:
        t["pay_date"] = pay_date
    days, per_year, hours = PERIODS[t["freq"]]
    t["period_end"] = t["pay_date"] - timedelta(days=5)
    t["period_start"] = t["period_end"] - timedelta(days=days - 1)
    rate = c2(t["rate"] * scale)
    earn = []
    if t["salaried"]:
        earn.append(["Salary", None, None, c2(rate * hours)])
    else:
        earn.append(["Regular", rate, hours, c2(rate * hours)])
        if t["ot"]:
            oth = D(str(t.get("ot_hours") or rng.choice([2, 4.5, 6, 8])))
            t["ot_hours"] = oth
            earn.append(["Overtime", c2(rate * D("1.5")), oth, c2(c2(rate * D("1.5")) * oth)])
    gross = sum(e[3] for e in earn)
    taxable = gross - t["health"]
    ded = [["Federal Income Tax", c2(taxable * t["fed"])], ["Social Security", c2(taxable * D("0.062"))],
           ["Medicare", c2(taxable * D("0.0145"))]]
    if t["state"]:
        ded.append(["State Income Tax", c2(taxable * t["state"])])
    if t["health"]:
        ded.append(["Health Insurance", t["health"]])
    if t["k401"]:
        ded.append(["401(k)", c2(gross * t["k401"])])
    total = sum(d[1] for d in ded)
    elapsed = max(1, int(t["pay_date"].timetuple().tm_yday / 365 * per_year))
    k = t.get("k") or rng.randint(max(1, elapsed - 3), elapsed)
    t.update(k=k, earnings=[e + [c2(e[3] * k)] for e in earn], deductions=[d + [c2(d[1] * k)] for d in ded],
             gross=gross, total=total, net=gross - total, gross_ytd=c2(gross * k), total_ytd=c2(total * k),
             net_ytd=c2((gross - total) * k))
    return t


def render_stub(t, override=None, producer=None, created: date | None = None) -> bytes:
    """Draw the stub. `override` replaces printed strings by key without recomputing anything."""
    o = override or {}
    g = lambda key, val: o.get(key, val)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)
    W, H = letter
    reg, bold = t["font"]
    c.setFont(bold, 17)
    c.drawString(54, H - 62, t["employer"])
    c.setFont(reg, 10)
    c.drawString(54, H - 78, "Earnings Statement")
    y = H - 108
    for lab, val in (("Employee:", g("name", t["name"])), ("Employee ID:", str(t["emp_id"])),
                     ("Pay Period:", f"{t['period_start']:%m/%d/%Y} - {t['period_end']:%m/%d/%Y}"),
                     ("Pay Date:", g("pay_date", f"{t['pay_date']:%m/%d/%Y}")),
                     ("Pay Frequency:", FREQ_LABEL[t["freq"]])):
        c.setFont(bold, 9.5)
        c.drawString(54, y, lab)
        c.setFont(reg, 9.5)
        c.drawString(150, y, val)
        y -= 15
    y -= 14

    def table(x0, cols, title, rows, y):
        c.setFont(bold, 9.5)
        c.setFillGray(0.9)
        c.rect(x0 - 4, y - 4, cols[-1] - x0 + 8, 15, stroke=0, fill=1)
        c.setFillGray(0)
        c.drawString(x0, y, title[0])
        for cx, h in zip(cols, title[1:]):
            c.drawRightString(cx, y, h)
        y -= 17
        c.setFont(reg, 9.5)
        for r in rows:
            c.drawString(x0, y, r[0])
            for cx, v in zip(cols, r[1:]):
                if v is not None:
                    c.drawRightString(cx, y, v)
            y -= 14.5
        return y

    erows = [[e[0], fm(e[1]) if e[1] else None, fm(e[2]) if e[2] else None, g(f"earn{i}", fm(e[3])), fm(e[4])]
             for i, e in enumerate(t["earnings"])]
    drows = [[d[0], g(f"ded{i}", fm(d[1])), fm(d[2])] for i, d in enumerate(t["deductions"])]
    tot = [["Gross Pay", g("gross", fm(t["gross"])), g("gross_ytd", fm(t["gross_ytd"]))],
           ["Total Deductions", fm(t["total"]), fm(t["total_ytd"])],
           ["Net Pay", g("net", fm(t["net"])), fm(t["net_ytd"])]]
    if t["layout"] == "A":
        y = table(54, [300, 370, 460, 550], ["Earnings", "Rate", "Hours", "Current", "YTD"], erows, y) - 12
        y = table(54, [460, 550], ["Deductions", "Current", "YTD"], drows, y) - 12
        y = table(54, [460, 550], ["Summary", "Current", "YTD"], tot, y)
    else:
        y1 = table(40, [150, 195, 250, 305], ["Earnings", "Rate", "Hours", "Current", "YTD"], erows, y)
        y2 = table(330, [490, 560], ["Deductions", "Current", "YTD"], drows, y)
        y = table(40, [250, 305], ["Summary", "Current", "YTD"], tot, min(y1, y2) - 16)
    c.setFont(reg, 7.5)
    c.drawString(54, 60, f"Processed by {t['provider'][0]}. Retain this statement for your records.")
    c.save()
    return stamp(buf.getvalue(), producer or t["provider"], created or t["pay_date"] - timedelta(days=2))


def stamp(pdf: bytes, producer, created: date, modified: date | None = None, mod_hour=9) -> bytes:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    creator, prod = producer if isinstance(producer, tuple) else (producer, producer)
    doc.set_metadata({"creator": creator, "producer": prod, "creationDate": pdf_ts(created),
                      "modDate": pdf_ts(modified or created, mod_hour)})
    return doc.tobytes(garbage=3, deflate=True)


# ---------- tampering -------------------------------------------------------

def retype(pdf: bytes, old: str, new: str, rng, font=None, whiteout=False, jitter=True):
    """Replace one printed value. whiteout=True leaves the original text under a white box."""
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    page = doc[0]
    hits = page.search_for(old)
    if not hits:
        return pdf, False
    r = hits[0]
    span = next((s for b in page.get_text("dict")["blocks"] if b["type"] == 0 for ln in b["lines"]
                 for s in ln["spans"] if pymupdf.Rect(s["bbox"]).intersects(r)), None)
    size = span["size"] if span else 9.5
    orig = (span["font"] if span else "Helvetica").lower()
    native = "tiro" if "times" in orig else "cour" if "courier" in orig else "helv"
    font = font or native
    if whiteout:
        page.draw_rect(r + (-1, -1, 1, 1), color=None, fill=(1, 1, 1))
    else:
        page.add_redact_annot(r, fill=False)
        page.apply_redactions()
    width = pymupdf.get_text_length(new, fontname=font, fontsize=size)
    right_aligned = r.x0 > 200
    x = (r.x1 - width if right_aligned else r.x0) + (rng.uniform(-3, 3) if jitter else 0)
    page.insert_text((x, r.y1 - size * 0.23), new, fontname=font, fontsize=size)
    return doc.tobytes(garbage=0), True


def resave_incremental(pdf: bytes) -> bytes:
    """Append an empty-ish revision, the way an editor's 'Save' does."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf)
        path = f.name
    doc = pymupdf.open(path)
    doc[0].insert_text((700, 900), " ", fontsize=1)
    doc.saveIncr()
    doc.close()
    data = open(path, "rb").read()
    os.unlink(path)
    return data


def rasterize(pdf: bytes, dpi=150):
    pg = pymupdf.open(stream=pdf, filetype="pdf")[0]
    pix = pg.get_pixmap(dpi=dpi)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples), pg


def _ttf(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def image_edit(pdf: bytes, pairs, rng, dpi=150):
    """Scan the stub, save as JPEG, then paint a new number over the old one."""
    img, pg = rasterize(pdf, dpi)
    b = io.BytesIO()
    img.save(b, "JPEG", quality=rng.choice([70, 78, 85]))
    img = Image.open(io.BytesIO(b.getvalue())).convert("RGB")
    k = dpi / 72
    d = ImageDraw.Draw(img)
    for old, new in pairs:
        hits = pg.search_for(old)
        if not hits:
            continue
        r = hits[0]
        d.rectangle([r.x0 * k - 3, r.y0 * k - 1, r.x1 * k + 3, r.y1 * k + 1], fill="white")
        font = _ttf(int((r.y1 - r.y0) * k * 0.78))
        w = d.textlength(new, font=font)
        d.text((r.x1 * k - w, r.y0 * k + 1), new, fill="black", font=font)
    return img


def to_jpeg(img, quality=92, software=None) -> bytes:
    b = io.BytesIO()
    if software:
        ex = Image.Exif()
        ex[0x0131] = software
        img.save(b, "JPEG", quality=quality, exif=ex)
    else:
        img.save(b, "JPEG", quality=quality)
    return b.getvalue()


def image_pdf(img, producer="Scanner Pro", created=None) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=to_jpeg(img, 88))
    doc.set_metadata({"producer": producer, "creationDate": pdf_ts(created or date(2026, 9, 1))})
    return doc.tobytes()


def make_stub(rng: random.Random, fraud: bool, kind: str | None = None):
    """Return dict(data, ext, label, kind, applicant, today)."""
    t = stub_truth(rng)
    pdf = render_stub(t)
    today = t["pay_date"] + timedelta(days=rng.randint(3, 40))
    out = {"applicant": t["name"], "today": today, "label": int(fraud), "ext": "pdf", "truth": t}
    factor = D(str(round(rng.uniform(1.25, 2.4), 2)))
    big_gross, big_net = c2(t["gross"] * factor), c2(t["net"] * factor)
    if not fraud:
        kind = kind or rng.choices(["clean", "browser", "resaved", "office", "scan_jpg", "scan_pdf", "screenshot"],
                                   [50, 10, 8, 6, 9, 9, 8])[0]
        if kind == "browser":
            pdf = stamp(pdf, ("Chromium", "Skia/PDF m126"), today - timedelta(days=1))
        elif kind == "resaved":
            pdf = stamp(pdf, ("ADP Workforce Now", "macOS Version 14.5 Quartz PDFContext"), t["pay_date"] - timedelta(days=2),
                        today, 14)
        elif kind == "office":
            pdf = stamp(pdf, ("Microsoft Excel", "Microsoft Excel for Microsoft 365"), t["pay_date"] - timedelta(days=1))
        elif kind == "scan_jpg":
            pdf, out["ext"] = to_jpeg(rasterize(pdf, 170)[0], rng.choice([80, 90])), "jpg"
        elif kind == "scan_pdf":
            pdf = image_pdf(rasterize(pdf, 170)[0], created=today)
        elif kind == "screenshot":
            b = io.BytesIO()
            rasterize(pdf, 130)[0].save(b, "PNG")
            pdf, out["ext"] = b.getvalue(), "png"
    else:
        kind = kind or rng.choices(["retype", "whiteout", "rebuilt", "rebuilt_clean", "image_edit", "name_swap",
                                    "generator", "perfect"], [24, 16, 16, 8, 12, 8, 10, 6])[0]
        if kind in ("retype", "whiteout"):
            font = rng.choice(["helv", "tiro", "cour", None])
            wo = kind == "whiteout"
            pdf, _ = retype(pdf, fm(t["gross"]), fm(big_gross), rng, font, wo)
            pdf, _ = retype(pdf, fm(t["net"]), fm(big_net), rng, font, wo)
            if rng.random() < 0.5:      # the more careful forger also fixes the main earnings line
                pdf, _ = retype(pdf, fm(t["earnings"][0][3]), fm(c2(t["earnings"][0][3] * factor)), rng, font, wo)
            if rng.random() < 0.55:
                pdf = stamp(pdf, rng.choice(EDITORS), t["pay_date"] - timedelta(days=2), today - timedelta(days=1), 21)
            elif rng.random() < 0.6:
                pdf = stamp(pdf, t["provider"], t["pay_date"] - timedelta(days=2), today - timedelta(days=1), 21)
            if wo or rng.random() < 0.4:
                pdf = resave_incremental(pdf)
        elif kind in ("rebuilt", "rebuilt_clean"):
            # Rebuilt from a template with gross and net raised; taxes left at their real values.
            ov = {"gross": fm(big_gross), "net": fm(big_net)}
            if rng.random() < 0.6:
                ov["earn0"] = fm(c2(t["earnings"][0][3] * factor))
            if rng.random() < 0.4:
                ov["gross_ytd"] = fm(c2(t["gross_ytd"] * factor))
            prod = t["provider"] if kind == "rebuilt_clean" else rng.choice(
                [("Microsoft Word", "Microsoft Word for Microsoft 365"), "Canva", ("Writer", "LibreOffice 7.6")])
            pdf = render_stub(t, ov, prod, today - timedelta(days=1))
        elif kind == "image_edit":
            img = image_edit(pdf, [(fm(t["net"]), fm(big_net)), (fm(t["gross"]), fm(big_gross))], rng)
            sw = rng.choice([None, "Adobe Photoshop 25.0 (Windows)", "GIMP 2.10.36"])
            if rng.random() < 0.5:
                pdf, out["ext"] = to_jpeg(img, 93, sw), "jpg"
            else:
                pdf = image_pdf(img, rng.choice(["Adobe Photoshop 25.0", "Scanner Pro", "iLovePDF"]), today)
        elif kind == "name_swap":
            other = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            while other.split()[1] == t["name"].split()[1] or other.split()[0] == t["name"].split()[0]:
                other = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            out["applicant"] = other      # a real stub belonging to someone else
            if rng.random() < 0.5:        # ...or their name typed over it
                pdf, _ = retype(pdf, t["name"], other, rng, rng.choice(["helv", "tiro", None]))
                pdf = stamp(pdf, rng.choice(EDITORS + [t["provider"]]), t["pay_date"] - timedelta(days=2), today, 20)
        elif kind == "generator":
            # Fully consistent fake made with an online generator or office template.
            t2 = stub_truth(rng, scale=factor, base=t)
            prod = rng.choice(["Online Paystub Generator", ("Microsoft Word", "Microsoft Word for Microsoft 365"),
                               "Canva", ("Pages", "macOS Quartz PDFContext Pages")])
            pdf = render_stub(t2, None, prod, today - timedelta(days=rng.randint(0, 2)))
        elif kind == "perfect":
            # Internally consistent, provider-looking metadata: not detectable from the file alone.
            pdf = render_stub(stub_truth(rng, scale=factor, base=t))
    out.update(data=pdf, kind=kind)
    return out


# ---------- bank statements -------------------------------------------------

def bank_truth(rng, name=None, payroll=None, month=None, employer=None):
    month = month or date(2026, rng.randint(2, 9), 1)
    bal = c2(rng.uniform(600, 6000))
    t = {"name": name or f"{rng.choice(FIRST)} {rng.choice(LAST)}", "bank": rng.choice(BANKS), "start": month,
         "begin": bal, "txns": [], "font": rng.choice(FONTS), "acct": rng.randint(1000, 9999)}
    pays = payroll or [(month + timedelta(days=d), c2(rng.uniform(1100, 2900))) for d in (4, 18)]
    events = [(d, f"Direct Deposit {(employer or rng.choice(EMPLOYERS)).upper()[:22]} PAYROLL", a, 1) for d, a in pays]
    for _ in range(rng.randint(9, 16)):
        events.append((month + timedelta(days=rng.randint(0, 27)),
                       rng.choice(["Debit Card Purchase FRYS FOOD", "ACH Debit APS ELECTRIC", "Debit Card Purchase SHELL OIL",
                                   "Online Transfer to Savings", "Debit Card Purchase TARGET", "ACH Debit RENT PAYMENT",
                                   "ATM Withdrawal", "Debit Card Purchase AMAZON"]), c2(rng.uniform(8, 420)), -1))
    events.sort(key=lambda e: e[0])
    dep = wd = D(0)
    for d, desc, a, sign in events:
        if sign < 0 and bal - a < 20:
            continue
        bal += a * sign
        dep, wd = (dep + a, wd) if sign > 0 else (dep, wd + a)
        t["txns"].append([d, desc, a, bal])
    t.update(end=bal, deposits=dep, withdrawals=wd, last=t["txns"][-1][0])
    return t


def render_bank(t, override=None, producer=("Statement Composer", "OpenText Exstream 16.6")):
    o = override or {}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)
    W, H = letter
    reg, bold = t["font"]
    c.setFont(bold, 16)
    c.drawString(54, H - 60, t["bank"])
    c.setFont(reg, 9.5)
    end = t["start"] + timedelta(days=27)
    for i, s in enumerate((f"Account Holder: {t['name']}", f"Account Number: ******{t['acct']}",
                           f"Statement Period: {t['start']:%m/%d/%Y} - {end:%m/%d/%Y}")):
        c.drawString(54, H - 84 - 14 * i, s)
    y = H - 150
    c.setFont(bold, 10)
    c.drawString(54, y, "Account Summary")
    c.setFont(reg, 9.5)
    for lab, key, v in (("Beginning Balance", "begin", t["begin"]), ("Total Deposits", "deposits", t["deposits"]),
                        ("Total Withdrawals", "withdrawals", t["withdrawals"]), ("Ending Balance", "end", t["end"])):
        y -= 15
        c.drawString(54, y, lab)
        c.drawRightString(300, y, o.get(key, fm(v)))
    y -= 30
    c.setFont(bold, 9.5)
    c.drawString(54, y, "Date")
    c.drawString(128, y, "Description")
    c.drawRightString(470, y, "Amount")
    c.drawRightString(555, y, "Balance")
    c.setFont(reg, 9.5)
    for i, (d, desc, a, bal) in enumerate(t["txns"]):
        y -= 14.5
        c.drawString(54, y, f"{d:%m/%d/%Y}")
        c.drawString(128, y, desc)
        c.drawRightString(470, y, o.get(f"amt{i}", fm(a)))
        c.drawRightString(555, y, o.get(f"bal{i}", fm(bal)))
    c.save()
    return stamp(buf.getvalue(), producer, end + timedelta(days=1))


def make_bank(rng, fraud: bool, kind=None):
    t = bank_truth(rng)
    today = t["start"] + timedelta(days=rng.randint(30, 55))
    out = {"applicant": t["name"], "today": today, "label": int(fraud), "ext": "pdf", "truth": t}
    pdf = render_bank(t)
    if fraud:
        kind = kind or rng.choice(["retype", "rebuilt", "whiteout", "perfect"])
        i = next(k for k, x in enumerate(t["txns"]) if "PAYROLL" in x[1])
        new = fm(c2(t["txns"][i][2] * D(str(round(rng.uniform(1.5, 2.6), 2)))))
        if kind in ("retype", "whiteout"):
            pdf, _ = retype(pdf, fm(t["txns"][i][2]), new, rng, rng.choice(["helv", "tiro", "cour", None]), kind == "whiteout")
            if rng.random() < 0.6:
                pdf = stamp(pdf, rng.choice(EDITORS), today - timedelta(days=20), today, 22)
            if kind == "whiteout":
                pdf = resave_incremental(pdf)
        elif kind == "rebuilt":
            pdf = render_bank(t, {f"amt{i}": new}, rng.choice(["Canva", ("Microsoft Word", "Microsoft Word")]))
        else:
            pay = [(x[0], c2(x[2] * 2)) for x in t["txns"] if "PAYROLL" in x[1]]
            pdf = render_bank(bank_truth(rng, t["name"], pay, t["start"]))
    else:
        kind = kind or rng.choices(["clean", "browser", "scan_pdf"], [75, 15, 10])[0]
        if kind == "browser":
            pdf = stamp(pdf, ("Chromium", "Skia/PDF m126"), today)
        elif kind == "scan_pdf":
            pdf = image_pdf(rasterize(pdf, 170)[0], created=today)
    out.update(data=pdf, kind=kind)
    return out


def make(rng, fraud: bool):
    return make_bank(rng, fraud) if rng.random() < 0.2 else make_stub(rng, fraud)
