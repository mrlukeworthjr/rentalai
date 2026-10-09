"""Classify the document and pull out the fields the checks reason about.
Every value keeps the page and box it was read from so findings can point at it."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

MONEY = re.compile(r"(?<![\w/.])\(?-?\$?\s?((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})\)?(?![\d/%])")
DATE = re.compile(
    r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b|\b(\d{4})-(\d{2})-(\d{2})\b|"
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", re.I)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


@dataclass
class Val:
    value: object
    page: int
    bbox: tuple
    font: str | None = None
    size: float | None = None

    def ref(self):
        return {"page": self.page, "bbox": [round(x, 1) for x in self.bbox]}


@dataclass
class Line:                      # one earnings or deduction line
    label: str
    current: Val | None = None
    ytd: Val | None = None
    rate: Val | None = None
    hours: Val | None = None


@dataclass
class Txn:
    date: date
    desc: str
    amount: Val
    balance: Val


@dataclass
class Fields:
    doc_type: str = "unknown"
    employer: Val | None = None
    employee: Val | None = None
    period_start: Val | None = None
    period_end: Val | None = None
    pay_date: Val | None = None
    frequency: str | None = None
    frequency_stated: bool = False
    gross: Line | None = None
    net: Line | None = None
    total_deductions: Line | None = None
    earnings: list = field(default_factory=list)
    deductions: list = field(default_factory=list)
    # bank statement
    begin_balance: Val | None = None
    end_balance: Val | None = None
    total_deposits: Val | None = None
    total_withdrawals: Val | None = None
    txns: list = field(default_factory=list)
    money_spans: list = field(default_factory=list)


def to_date(m) -> date | None:
    try:
        if m.group(1):
            mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            y += 2000 if y < 100 else 0
        elif m.group(4):
            y, mo, d = int(m.group(4)), int(m.group(5)), int(m.group(6))
        else:
            mo, d, y = MONTHS.index(m.group(7)[:3].lower()) + 1, int(m.group(8)), int(m.group(9))
        return date(y, mo, d)
    except ValueError:
        return None


def money(text):
    return [Decimal(m.group(1).replace(",", "")) for m in MONEY.finditer(text)]


def classify(text: str) -> str:
    t = text.lower()
    stub = sum(k in t for k in ("net pay", "gross", "ytd", "year to date", "pay period", "pay date",
                                "deductions", "earnings", "medicare", "social security", "withholding"))
    bank = sum(k in t for k in ("beginning balance", "ending balance", "opening balance", "closing balance",
                                "deposits", "withdrawals", "statement period", "account number",
                                "account summary", "daily balance"))
    if stub >= 3 and stub >= bank:
        return "paystub"
    if bank >= 3:
        return "bank_statement"
    return "unknown"


def segments(row):
    """Split a row where a second label starts after numbers (side-by-side columns)."""
    segs, cur, seen_num = [], [], False
    for s in row.spans:
        is_num = bool(MONEY.search(s.text)) and not re.search(r"[A-Za-z]{2}", s.text)
        if not is_num and seen_num and re.search(r"[A-Za-z]{2}", s.text):
            segs.append(cur)
            cur, seen_num = [], False
        cur.append(s)
        seen_num = seen_num or is_num
    if cur:
        segs.append(cur)
    return segs


def seg_values(seg):
    label, vals, started = [], [], False
    for s in seg:
        ms = list(MONEY.finditer(s.text))
        if ms:
            started = True
            if s.text[:ms[0].start()].strip(" $:-") and not vals:
                label.append(s.text[:ms[0].start()])
            for m in ms:
                vals.append(Val(Decimal(m.group(1).replace(",", "")), s.page, s.bbox, s.font, s.size))
        elif not started:
            label.append(s.text)
    return " ".join(label).lower().strip(" :"), vals


DED = re.compile(r"fed|state (income )?tax|\bsit\b|\bfit\b|social sec|oasdi|fica|medicare|401|403|roth|"
                 r"health|medical|dental|vision|insurance|garnish|hsa|fsa|\bsdi\b|local tax|city tax|"
                 r"union|child support|withholding|retirement|life ins|disability")
EARN = re.compile(r"^(regular|salary|hourly|overtime|ot\b|bonus|commission|holiday|pto|vacation|sick|tips|"
                  r"shift|double ?time|retro|base pay)")
PRETAX = re.compile(r"health|medical|dental|vision|hsa|fsa|section 125|cafeteria")


def _name_after(row, pattern):
    """Value that follows a label such as 'Employee:' on the same row."""
    spans = row.spans
    for i, s in enumerate(spans):
        m = re.match(pattern, s.text, re.I)
        if not m:
            continue
        if re.match(r"(id|no|number|#)\b", s.text[m.end():].strip(), re.I):
            continue
        parts, box = [], None
        rest = s.text[m.end():].strip(" :-")
        if rest:
            parts.append(rest)
            box = s.bbox
        prev = s
        for n in spans[i + 1:]:
            gap = n.bbox[0] - prev.bbox[2]
            h = max(prev.bbox[3] - prev.bbox[1], 4)
            if (parts and gap > 2.2 * h) or re.search(r":$|\d{2}/\d{2}", n.text):
                break
            if not parts and re.match(r"(id|no\.?|number|#|name)\b:?$", n.text, re.I):
                if not re.match(r"name", n.text, re.I):
                    return None
                prev = n
                continue
            parts.append(n.text.strip(" :"))
            box = n.bbox if box is None else (min(box[0], n.bbox[0]), min(box[1], n.bbox[1]),
                                              max(box[2], n.bbox[2]), max(box[3], n.bbox[3]))
            prev = n
            if len(parts) >= 4:
                break
        name = " ".join(p for p in parts if p).strip()
        if len(re.findall(r"[A-Za-z]", name)) >= 3:
            return Val(name, row.page, box)
    return None


def parse_dates(f: Fields, rows):
    for row in rows:
        low, last = row.text.lower(), 0
        found = []
        for m in DATE.finditer(row.text):
            d = to_date(m)
            if d:
                found.append((low[last:m.start()], d))
            last = m.end()
        if not found:
            continue
        v = lambda d: Val(d, row.page, row.bbox)
        for i, (lab, d) in enumerate(found):
            if re.search(r"pay ?date|check date|advice date|paid on|payment date", lab):
                f.pay_date = f.pay_date or v(d)
            elif re.search(r"(period )?(begin|start|from)", lab):
                f.period_start = f.period_start or v(d)
            elif re.search(r"(period )?(end|thru|through)", lab) and "period" in low:
                f.period_end = f.period_end or v(d)
            elif "period" in lab and i + 1 < len(found) and re.fullmatch(r"\s*(-|–|to|thru|through)\s*", found[i + 1][0]):
                f.period_start = f.period_start or v(d)
                f.period_end = f.period_end or v(found[i + 1][1])


def parse_paystub(f: Fields, doc):
    rows = [r for p in doc.pages for r in p.rows]
    parse_dates(f, rows)
    text = doc.text.lower()
    ytd_first = False
    for row in rows:
        low = row.text.lower()
        if "current" in low and re.search(r"ytd|year to date", low):
            ytd_first = re.search(r"ytd|year to date", low).start() < low.index("current")
            break

    def cur_ytd(vals):
        if len(vals) >= 2:
            return (vals[1], vals[0]) if ytd_first else (vals[0], vals[1])
        return (vals[0], None) if vals else (None, None)

    for row in rows:
        if f.employee is None and re.search(r"employee|pay to|paid to|\bname\b", row.text, re.I):
            f.employee = _name_after(row, r"(employee name|employee|pay to the order of|pay to|paid to|name)\s*:?")
        if f.employer is None and re.search(r"employer|company", row.text, re.I):
            f.employer = _name_after(row, r"(employer name|employer|company name|company)\s*:?")
        for seg in segments(row):
            label, vals = seg_values(seg)
            if not vals or not label:
                continue
            f.money_spans.extend(vals)
            if re.search(r"\bnet (pay|amount|check|wages)|take.?home", label):
                if f.net is None:
                    c, y = cur_ytd(vals)
                    f.net = Line(label, c, y)
            elif re.search(r"total (deductions|taxes|withh)|deductions total", label):
                if f.total_deductions is None:
                    c, y = cur_ytd(vals)
                    f.total_deductions = Line(label, c, y)
            elif re.search(r"\bgross|total earnings|total pay\b", label):
                if f.gross is None:
                    c, y = cur_ytd(vals[-2:] if len(vals) > 2 else vals)
                    f.gross = Line(label, c, y)
            elif EARN.search(label):
                ln = Line(label)
                if len(vals) >= 3 and abs(vals[0].value * vals[1].value - vals[2].value) <= Decimal("0.05"):
                    ln.rate, ln.hours, ln.current = vals[0], vals[1], vals[2]
                    ln.ytd = vals[3] if len(vals) > 3 else None
                elif len(vals) >= 4 and min(vals[0].value, vals[1].value) <= 400 and max(vals[0].value, vals[1].value) <= 2000:
                    ln.rate, ln.hours, ln.current, ln.ytd = vals[0], vals[1], vals[2], vals[3]
                elif len(vals) >= 3:
                    ln.current, ln.ytd = cur_ytd(vals[-2:])
                else:
                    ln.current, ln.ytd = cur_ytd(vals)
                f.earnings.append(ln)
            elif DED.search(label):
                c, y = cur_ytd(vals)
                f.deductions.append(Line(label, c, y))

    if f.employer is None and doc.pages:
        p = doc.pages[0]
        top = [s for s in p.spans if s.bbox[1] < p.height * 0.3 and re.search(r"[A-Za-z]{3}", s.text)
               and not re.search(r"pay ?stub|statement|earnings|employee|period|date|page", s.text, re.I)]
        if top:
            big = max(s.size or 0 for s in top)
            line_spans = [s for s in top if (s.size or 0) >= big * 0.92]
            first = min(line_spans, key=lambda s: (round(s.bbox[1] / 4), s.bbox[0]))
            same = sorted([s for s in line_spans if abs(s.bbox[1] - first.bbox[1]) < 4], key=lambda s: s.bbox[0])
            f.employer = Val(" ".join(s.text for s in same), 1,
                             (same[0].bbox[0], same[0].bbox[1], same[-1].bbox[2], same[-1].bbox[3]))

    for key, pat in (("weekly", r"\bbi-?weekly|every (two|2) weeks"), ("semimonthly", r"semi-?monthly|twice (a|per) month"),
                     ("weekly1", r"\bweekly"), ("monthly", r"\bmonthly")):
        if re.search(pat, text):
            f.frequency = {"weekly": "biweekly", "weekly1": "weekly"}.get(key, key)
            f.frequency_stated = True
            break
    if not f.frequency and f.period_start and f.period_end:
        n = (f.period_end.value - f.period_start.value).days + 1
        f.frequency = ("weekly" if 6 <= n <= 8 else "biweekly" if n == 14 else
                       "semimonthly" if 13 <= n <= 16 else "monthly" if 28 <= n <= 31 else None)


def parse_bank(f: Fields, doc):
    rows = [r for p in doc.pages for r in p.rows]
    year = None
    for row in rows:
        m = DATE.search(row.text)
        if m and to_date(m):
            year = to_date(m).year
            break
    year = year or datetime.utcnow().year
    for row in rows:
        low = row.text.lower()
        _, vals = seg_values(row.spans)
        f.money_spans.extend(vals)
        if not vals:
            continue
        tx = re.match(r"\s*(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\s*(.*)", row.text)
        if tx and len(vals) >= 2 and not re.search(r"balance (forward|brought)", low):
            try:
                y = int(tx.group(3)) if tx.group(3) else year
                d = date(y + 2000 if y < 100 else y, int(tx.group(1)), int(tx.group(2)))
            except ValueError:
                continue
            desc = MONEY.sub("", tx.group(4)).strip(" -$")
            f.txns.append(Txn(d, desc, vals[-2], vals[-1]))
        elif re.search(r"(beginning|opening|previous|starting) balance", low):
            f.begin_balance = f.begin_balance or vals[-1]
        elif re.search(r"(ending|closing|new) balance", low):
            f.end_balance = f.end_balance or vals[-1]
        elif re.search(r"deposits|credits|additions", low) and not f.total_deposits:
            f.total_deposits = vals[-1]
        elif re.search(r"withdrawals|debits|subtractions|checks paid", low) and not f.total_withdrawals:
            f.total_withdrawals = vals[-1]
    parse_dates(f, rows)
    for row in rows[:14]:
        if f.employee is None and re.search(r"account (holder|owner|name)|prepared for|customer", row.text, re.I):
            f.employee = _name_after(row, r"(account holder|account owner|account name|prepared for|customer name|customer)\s*:?")


def parse(doc) -> Fields:
    f = Fields(doc_type=classify(doc.text))
    if f.doc_type == "paystub":
        parse_paystub(f, doc)
    elif f.doc_type == "bank_statement":
        parse_bank(f, doc)
    return f
