"""analyze(bytes) -> report. The single entry point for one document."""
from __future__ import annotations

import hashlib
from datetime import date

from . import checks, extract, forensics, model, parse
from .forensics import ASK_CLEAR, finding

ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
ENGINE_VERSION = "1.0.0"
HEADLINE = {"pass": "No problems found", "review": "Needs a closer look", "fail": "Failed verification",
            "unreadable": "Could not be read"}


def _s(v):
    if v is None:
        return None
    x = v.value
    return x.isoformat() if isinstance(x, date) else str(x)


def serialize(f):
    line = lambda l: {"label": l.label, "current": _s(l.current), "ytd": _s(l.ytd), "rate": _s(l.rate),
                      "hours": _s(l.hours)}
    cur = lambda l: _s(l.current) if l else None
    return {
        "employer": _s(f.employer), "employee": _s(f.employee), "period_start": _s(f.period_start),
        "period_end": _s(f.period_end), "pay_date": _s(f.pay_date), "frequency": f.frequency,
        "gross": cur(f.gross), "net": cur(f.net), "total_deductions": cur(f.total_deductions),
        "gross_ytd": _s(f.gross.ytd) if f.gross else None,
        "earnings": [line(l) for l in f.earnings], "deductions": [line(l) for l in f.deductions],
        "begin_balance": _s(f.begin_balance), "end_balance": _s(f.end_balance),
        "total_deposits": _s(f.total_deposits), "total_withdrawals": _s(f.total_withdrawals),
        "txns": [{"date": t.date.isoformat(), "desc": t.desc, "amount": _s(t.amount), "balance": _s(t.balance),
                  "kind": getattr(t, "kind", None)} for t in f.txns],
    }


def passed_checks(doc, f, codes, applicant):
    c = []
    add = lambda cond, code, label: c.append(label) if cond and code not in codes else None
    pdf = doc.kind == "pdf" and not doc.ocr_used
    producer = (doc.meta.get("producer") or doc.meta.get("creator")) if doc.meta else None
    add(pdf and producer, "F_EDITOR", f"Produced by “{producer}”, not a known editing tool")
    add(pdf and doc.meta.get("creationDate") and doc.meta.get("modDate"), "F_MODIFIED", "Not modified after creation")
    add(pdf, "F_INCREMENTAL", "Single saved revision")
    add(pdf, "F_STACKED", "No covered or retyped text")
    add(pdf, "F_ANNOT", "No overlays or annotations")
    add(pdf and len(f.money_spans) >= 5 and "F_OFFICE" not in codes, "F_FONT", "All amounts share one typeface")
    if f.doc_type == "paystub":
        g, n = f.gross and f.gross.current, f.net and f.net.current
        add(g and n and (f.total_deductions or f.deductions), "C_NET_MATH", "Gross − deductions = net pay")
        add(f.total_deductions and len(f.deductions) >= 2, "C_DED_SUM", "Deduction lines sum to the total")
        add(g and f.earnings, "C_EARN_SUM", "Earnings lines sum to gross pay")
        add(any(e.rate and e.hours for e in f.earnings), "C_RATE_HOURS", "Rate × hours = amount paid")
        add(g and any("social" in d.label or "oasdi" in d.label for d in f.deductions), "C_FICA_SS",
            "Social Security equals 6.2% of wages")
        add(g and any("medicare" in d.label for d in f.deductions), "C_FICA_MED", "Medicare equals 1.45% of wages")
        add(f.gross and f.gross.ytd, "C_YTD_LT", "Year-to-date totals are consistent")
        add(f.pay_date and "C_STALE" not in codes, "C_FUTURE", "Pay date is recent and not in the future")
        add(applicant and f.employee, "C_NAME", "Name matches the applicant")
    if f.doc_type == "bank_statement":
        add(f.txns and f.begin_balance, "B_RUNNING", f"Running balance verified across {len(f.txns)} transactions")
        add(f.begin_balance and f.end_balance and f.total_deposits and f.total_withdrawals, "B_SUMMARY",
            "Summary totals balance")
        add(applicant, "C_NAME", "Applicant's name appears on the statement")
    return c


def analyze(data: bytes, filename: str = "document", applicant: str | None = None,
            today: date | None = None, with_internals: bool = False):
    today = today or date.today()
    rep = {"filename": filename, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
           "engine_version": ENGINE_VERSION, "analyzed_on": today.isoformat(), "doc_type": "unknown",
           "pages": [], "fields": {}, "findings": [], "passed": []}
    doc = fields = None
    try:
        doc = extract.load(data)
    except extract.Unreadable as e:
        rep["findings"] = [finding(
            "C_UNREADABLE", "high", "The file could not be opened", str(e),
            "Nothing in it can be checked.", "A PDF, JPEG or PNG that opens normally and is not password-protected.",
            "Upload the document as an unlocked PDF, or a clear photo.")]
        rep.update(verdict="unreadable", risk=0, headline=HEADLINE["unreadable"], features=None,
                   model={"available": False})
        return _summarise(rep)

    rep["pages"] = [{"number": p.number, "width": p.width, "height": p.height, "ocr": p.ocr} for p in doc.pages]
    fields = parse.parse(doc)
    rep["doc_type"] = fields.doc_type
    words = sum(len(p.spans) for p in doc.pages)
    fs = []
    if words < 15:
        fs.append(finding("C_UNREADABLE", "high", "Almost no text could be read",
                          f"Only {words} words were recognised on the page.",
                          "The copy is too blurry, dark, small or cropped to check.",
                          "A sharp, complete copy — ideally the original PDF.", ASK_CLEAR))
        rep["findings"] = fs
        rep.update(verdict="unreadable", risk=0, headline=HEADLINE["unreadable"], features=None,
                   model={"available": False})
        return _summarise(rep)
    fs += forensics.file_checks(doc, fields)
    if fields.doc_type == "paystub":
        fs += checks.paystub_checks(fields, doc, applicant, today)
    elif fields.doc_type == "bank_statement":
        fs += checks.bank_checks(fields, doc, applicant, today)
    else:
        # "high" so an unrecognised document can never come back as Passed
        fs.append(finding("C_TYPE", "high", "Not recognised as a pay stub or bank statement",
                          "The text does not contain the labels found on pay stubs or bank statements.",
                          "Only those two document types can be reconciled and used for income.",
                          "A pay stub or a bank statement.", "Upload a pay stub or a bank statement.",
                          category="content"))
    fs.sort(key=lambda f: ORDER[f["severity"]])
    feats = model.featurize(fs, doc, fields)
    risk, minfo = model.score(feats, fs)
    v = model.verdict(fs, minfo.get("probability"))
    rep.update(findings=fs, fields=serialize(fields), features=feats, model=minfo, verdict=v,
               risk=round(risk * 100), headline=HEADLINE[v],
               passed=passed_checks(doc, fields, {f["code"] for f in fs}, applicant))
    rep = _summarise(rep)
    if with_internals:
        rep["_doc"], rep["_fields"] = doc, fields
    return rep


def _summarise(rep):
    real = [f for f in rep["findings"] if f["severity"] in ("critical", "high", "medium")]
    rep["failed_because"] = [f["title"] for f in real]
    seen, needs = set(), []
    major = [f for f in real if f["severity"] in ("critical", "high")]
    for f in (major or real)[:5]:      # the summary names the decisive items; every finding carries its own fix
        if f["fix"] not in seen:
            seen.add(f["fix"])
            needs.append(f["fix"])
    rep["needs_to_pass"] = needs
    asks = []
    for f in rep["findings"]:
        a = f.get("applicant_request")
        if a and a not in asks and f["severity"] != "info":
            asks.append(a)
    rep["applicant_requests"] = asks if rep.get("verdict") != "pass" else []
    return rep
