"""Content checks: does the document agree with itself, with payroll tax law,
with the calendar, and with the other documents in the application."""
from __future__ import annotations

import math
import re
from datetime import date, timedelta
from decimal import Decimal as D

from .forensics import ASK_CLEAR, ASK_CONNECT, ASK_ORIGINAL, finding
from .parse import PRETAX

TOL = D("0.02")
SS_RATE, MED_RATE = D("0.062"), D("0.0145")
SS_WAGE_BASE = {2023: 160200, 2024: 168600, 2025: 176100, 2026: 184500}
PER_YEAR = {"weekly": 52, "biweekly": 26, "semimonthly": 24, "monthly": 12}
ASK_RECENT = "Upload your most recent pay stubs (dated within the last 60 days)."
ASK_NAME = "Upload documents issued in your own legal name, matching your application."
ASK_FULL = "Upload the complete document, including every page and the totals section."


def m(v):
    return f"${v:,.2f}"


def refs(*vals):
    return [v.ref() for v in vals if v is not None]


def name_tokens(s):
    return {t for t in re.sub(r"[^a-z ]", " ", (s or "").lower()).split() if len(t) > 1}


def names_match(a, b):
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return None
    return len(ta & tb) >= min(2, len(ta), len(tb))


def paystub_checks(f, doc, applicant, today: date):
    out = []
    soft = doc.ocr_used      # OCR can misread a digit, so arithmetic misses are not conclusive
    note = (" Because this copy was read with OCR, a misread digit could also cause this; compare against "
            "the highlighted figures.") if soft else ""
    sev = lambda s: {"critical": "medium", "high": "medium"}.get(s, s) if soft else s
    cat = dict(category="content")

    gross = f.gross.current if f.gross else None
    net = f.net.current if f.net else None
    tot = f.total_deductions.current if f.total_deductions else None
    listed = [d.current for d in f.deductions if d.current]
    listed_sum = sum((v.value for v in listed), D(0))

    missing = [n for n, v in (("gross pay", gross), ("net pay", net), ("pay date", f.pay_date),
                              ("employee name", f.employee), ("employer name", f.employer)) if v is None]
    if missing:
        out.append(finding(
            "C_MISSING", "medium", "Required information could not be found",
            f"Not found on the document: {', '.join(missing)}.",
            "Without these the stub cannot be tied to a person, an employer and a pay period, so income "
            "cannot be calculated from it with confidence.",
            "A complete pay stub showing employer, employee, pay period, pay date, gross pay, itemised "
            "deductions and net pay.", ASK_FULL if not soft else ASK_CLEAR, missing=missing, **cat))

    # Gross - deductions = net
    if gross and net:
        ded = tot.value if tot else (listed_sum if listed else None)
        if ded is not None:
            want = gross.value - ded
            if abs(want - net.value) > TOL:
                basis = "the stated total deductions" if tot else "the deductions listed"
                out.append(finding(
                    "C_NET_MATH", sev("critical" if tot else "high"), "Gross pay minus deductions does not equal net pay",
                    f"Gross {m(gross.value)} − {basis} {m(ded)} = {m(want)}, but net pay is printed as "
                    f"{m(net.value)} — off by {m(abs(want - net.value))}." + note,
                    "Payroll software computes net pay from gross and deductions, so the three always balance to "
                    "the cent. When they do not, at least one figure was changed without the others.",
                    f"A stub that balances. With these deductions net pay would be {m(want)}; with this net pay, "
                    f"gross would be {m(net.value + ded)}. Whichever figures are true, a genuine export shows "
                    "them agreeing.", ASK_ORIGINAL if not soft else ASK_CLEAR, refs(gross, tot, net),
                    rel=float(abs(want - net.value) / max(gross.value, D(1))), **cat))
    if tot and listed and abs(listed_sum - tot.value) > TOL and len(listed) >= 2:
        out.append(finding(
            "C_DED_SUM", sev("high"), "Itemised deductions do not add up to the stated total",
            f"The {len(listed)} deduction lines add up to {m(listed_sum)}; total deductions is printed as "
            f"{m(tot.value)}." + note,
            "The total is a sum of the lines above it. A mismatch means a line or the total was altered, or a "
            "line is missing from the copy.",
            f"A stub whose deduction lines sum to its total ({m(listed_sum)} for the lines shown).",
            ASK_ORIGINAL if not soft else ASK_CLEAR, refs(tot, *listed), **cat))
    earn = [e.current for e in f.earnings if e.current]
    if gross and len(earn) >= 1:
        es = sum((v.value for v in earn), D(0))
        if abs(es - gross.value) > TOL:
            out.append(finding(
                "C_EARN_SUM", sev("high"), "Earnings lines do not add up to gross pay",
                f"Earnings lines total {m(es)}; gross pay is printed as {m(gross.value)}." + note,
                "Gross pay is the sum of the earnings lines. If gross was raised without touching the lines "
                "beneath it, the two stop agreeing.",
                f"A stub where the earnings lines sum to gross pay ({m(es)} for the lines shown).",
                ASK_ORIGINAL if not soft else ASK_CLEAR, refs(gross, *earn), **cat))
    for e in f.earnings:
        if e.rate and e.hours and e.current:
            want = (e.rate.value * e.hours.value).quantize(D("0.01"))
            if abs(want - e.current.value) > D("0.05"):
                out.append(finding(
                    "C_RATE_HOURS", sev("high"), "Rate × hours does not equal the amount paid",
                    f"“{e.label}”: {e.rate.value} × {e.hours.value} = {m(want)}, but the line shows "
                    f"{m(e.current.value)}." + note,
                    "Hourly pay is a multiplication the payroll system performs. A line that fails it was typed, "
                    "not calculated.",
                    f"A stub where this line reads {m(want)}, or where the rate and hours support the amount shown.",
                    ASK_ORIGINAL if not soft else ASK_CLEAR, refs(e.rate, e.hours, e.current), **cat))

    # Payroll tax arithmetic: the strongest single test of an inflated gross.
    year = f.pay_date.value.year if f.pay_date else today.year
    base_cap = SS_WAGE_BASE.get(year, max(SS_WAGE_BASE.values()))
    ytd_gross = f.gross.ytd.value if f.gross and f.gross.ytd else None
    pretax = sum((d.current.value for d in f.deductions if d.current and PRETAX.search(d.label)), D(0))
    if gross:
        for code, pat, rate, label, cap in (
                ("C_FICA_SS", r"social sec|oasdi|\bss\b|fica.?ss", SS_RATE, "Social Security", base_cap),
                ("C_FICA_MED", r"medicare|fica.?med", MED_RATE, "Medicare", 200000)):
            line = next((d for d in f.deductions if d.current and re.search(pat, d.label)), None)
            if not line or (ytd_gross and ytd_gross > cap):
                continue
            lo = rate * (gross.value - pretax) - D("0.03")
            hi = rate * gross.value + D("0.03")
            got = line.current.value
            if not (lo <= got <= hi):
                implied = (got / rate).quantize(D("0.01"))
                out.append(finding(
                    code, sev("high"), f"{label} tax does not match the gross pay shown",
                    f"{label} is withheld at {rate * 100:.2f}% of taxable wages. For gross pay of {m(gross.value)} "
                    f"it should be about {m((rate * gross.value).quantize(D('0.01')))}; the stub shows {m(got)}, "
                    f"which is what would be withheld on wages of {m(implied)}." + note,
                    f"The {label} rate is fixed by federal law and applied by every payroll system. When gross "
                    "pay is raised on a document, this line usually keeps the amount calculated on the real wages "
                    "— so it reveals them.",
                    f"A stub where {label} equals {rate * 100:.2f}% of taxable wages. Either gross pay is really "
                    f"about {m(implied)}, or the employer must explain the difference (for example large pre-tax "
                    "benefits) through direct verification.", ASK_CONNECT, refs(gross, line.current),
                    implied_gross=float(implied), rel=float(abs(implied - gross.value) / gross.value), **cat))

    # Year-to-date logic
    for l in [x for x in (f.gross, f.net) if x] + f.deductions + f.earnings:
        if l.current and l.ytd and l.ytd.value + TOL < l.current.value:
            out.append(finding(
                "C_YTD_LT", sev("critical"), "Year-to-date amount is smaller than this period's amount",
                f"“{l.label}”: this period {m(l.current.value)}, year to date {m(l.ytd.value)}." + note,
                "Year-to-date includes the current period, so it can never be the smaller number. This happens "
                "when the current figure is raised and the running total is left alone.",
                "A stub whose year-to-date figures are at least as large as the current-period figures.",
                ASK_ORIGINAL if not soft else ASK_CLEAR, refs(l.current, l.ytd), **cat))
            break
    if gross and ytd_gross and f.pay_date and f.frequency in PER_YEAR and gross.value > 0:
        elapsed = math.ceil(f.pay_date.value.timetuple().tm_yday / 365 * PER_YEAR[f.frequency]) + 1
        implied = float(ytd_gross / gross.value)
        if implied > (elapsed + 2) * 1.3:
            out.append(finding(
                "C_YTD_PACE", "medium", "Year-to-date pay is too high for the date",
                f"Year-to-date gross {m(ytd_gross)} equals about {implied:.0f} paychecks of {m(gross.value)}, "
                f"but only about {elapsed} {f.frequency} pay periods have occurred by {f.pay_date.value:%b %d}.",
                "Year-to-date grows by one paycheck per period. A total far above what the calendar allows "
                "suggests either figure was altered, or large bonuses that should appear as their own lines.",
                "Earlier stubs from this year showing how the year-to-date total built up, or employer verification.",
                "Upload your two most recent consecutive pay stubs.", refs(gross, f.gross.ytd), **cat))

    # Dates
    ps, pe, pd = (x.value if x else None for x in (f.period_start, f.period_end, f.pay_date))
    if ps and pe and pe < ps:
        out.append(finding("C_DATES", "high", "Pay period ends before it starts",
                           f"Period start {ps:%b %d, %Y}, period end {pe:%b %d, %Y}.",
                           "A payroll system cannot produce a negative-length period; the dates were typed by hand.",
                           "A stub with a valid pay period.", ASK_ORIGINAL, refs(f.period_start, f.period_end), **cat))
    if pd and pd > today + timedelta(days=3):
        out.append(finding("C_FUTURE", "high", "Pay date is in the future",
                           f"The pay date is {pd:%b %d, %Y}; today is {today:%b %d, %Y}.",
                           "A stub is issued when wages are paid. One dated ahead of today documents income that "
                           "has not been paid.",
                           "A stub for a pay date that has already passed.", ASK_RECENT, refs(f.pay_date), **cat))
    elif pd and (today - pd).days > 90:
        out.append(finding("C_STALE", "low", "Pay stub is more than 90 days old",
                           f"Pay date {pd:%b %d, %Y} is {(today - pd).days} days ago.",
                           "An old stub shows past income, not current income.",
                           "Stubs from the most recent 60 days.", ASK_RECENT, refs(f.pay_date), **cat))
    if ps and pd and pd < ps:
        out.append(finding("C_DATES", "medium", "Pay date falls before the pay period begins",
                           f"Pay date {pd:%b %d, %Y}; period starts {ps:%b %d, %Y}.",
                           "Wages are paid during or after the period they cover.",
                           "A stub whose pay date is on or after the period it covers.", ASK_ORIGINAL,
                           refs(f.pay_date, f.period_start), **cat))
    if ps and pe and f.frequency_stated and f.frequency in PER_YEAR:
        n = (pe - ps).days + 1
        ok = {"weekly": (6, 8), "biweekly": (13, 15), "semimonthly": (13, 16), "monthly": (28, 31)}[f.frequency]
        if not ok[0] <= n <= ok[1]:
            out.append(finding("C_PERIOD", "medium", "Pay period length does not match the pay frequency",
                               f"The stub says pay is {f.frequency}, but the period shown is {n} days long.",
                               "Period length follows from pay frequency. A mismatch points to edited dates.",
                               "A stub whose period length fits its stated pay frequency.", ASK_ORIGINAL,
                               refs(f.period_start, f.period_end), **cat))

    from .forensics import pdf_date
    created = pdf_date((doc.meta or {}).get("creationDate"))
    if created and pd and (pd - created.date()).days > 10:
        out.append(finding("C_PREDATED", "medium", "File was created well before the pay date it reports",
                           f"The PDF was created {created:%b %d, %Y}, {(pd - created.date()).days} days before the "
                           f"pay date of {pd:%b %d, %Y}.",
                           "Payroll runs a few days ahead at most. A file made weeks before its pay date is "
                           "usually a template whose dates were changed.",
                           "A stub generated by the payroll run for that pay date.", ASK_ORIGINAL,
                           refs(f.pay_date), **cat))

    money_vals = [v.value for v in (gross, net, *listed) if v]
    if len(money_vals) >= 4 and all(v == v.to_integral_value() for v in money_vals):
        out.append(finding("C_ROUND", "low", "Every amount is a whole-dollar figure",
                           "Gross, net and all deductions end in .00.",
                           "Percentage-based taxes almost never land on whole dollars. All-round figures suggest "
                           "numbers that were chosen rather than calculated.",
                           "Corroboration from a second source such as bank deposits.", ASK_CONNECT, **cat))

    if applicant and f.employee:
        if names_match(applicant, f.employee.value) is False:
            out.append(finding("C_NAME", "high", "Name on the document does not match the applicant",
                               f"The stub is issued to “{f.employee.value}”; the application is for “{applicant}”.",
                               "Income only counts if it belongs to the person applying.",
                               "Documents issued to the applicant, or proof of a legal name change.",
                               ASK_NAME, refs(f.employee), **cat))
    return out


def bank_checks(f, doc, applicant, today: date):
    out, cat = [], dict(category="content")
    soft = doc.ocr_used
    sev = lambda s: "medium" if soft and s in ("critical", "high") else s
    note = " This copy was read with OCR, so a misread digit could also cause this." if soft else ""
    if not f.txns and f.begin_balance is None:
        out.append(finding("C_MISSING", "medium", "No balances or transactions could be read",
                           "The statement's summary and transaction table were not found.",
                           "Without them the statement cannot be reconciled.",
                           "The full statement PDF, all pages.", ASK_FULL, **cat))
        return out
    prev = f.begin_balance.value if f.begin_balance else None
    dep = wd = D(0)
    broke = False
    for t in f.txns:
        a, b = t.amount.value, t.balance.value
        kind = None
        if prev is not None:
            if abs(prev + a - b) <= TOL:
                kind = "deposit"
            elif abs(prev - a - b) <= TOL:
                kind = "withdrawal"
            elif not broke:
                broke = True
                out.append(finding(
                    "B_RUNNING", sev("critical"), "Running balance does not follow from the transaction",
                    f"On {t.date:%b %d} (“{t.desc[:40]}”) the prior balance was {m(prev)} and the amount is "
                    f"{m(a)}. The balance should be {m(prev + a)} for a deposit or {m(prev - a)} for a withdrawal, "
                    f"but {m(b)} is printed." + note,
                    "Each balance on a statement is the previous balance plus or minus that line's amount. A line "
                    "that breaks the chain had its amount or balance changed.",
                    "The statement downloaded from the bank, where every balance follows from the line before it.",
                    ASK_ORIGINAL if not soft else ASK_CLEAR, refs(t.amount, t.balance), **cat))
        if kind is None:
            kind = "deposit" if re.search(r"deposit|payroll|credit|direct dep|refund|transfer from", t.desc, re.I) else "withdrawal"
        t.kind = kind
        dep, wd = (dep + a, wd) if kind == "deposit" else (dep, wd + a)
        prev = b
    if f.txns and f.end_balance and prev is not None and abs(prev - f.end_balance.value) > TOL and not broke:
        out.append(finding(
            "B_ENDING", sev("high"), "Last transaction balance differs from the ending balance",
            f"The final transaction leaves {m(prev)}; the summary shows an ending balance of {m(f.end_balance.value)}."
            + note, "The summary is derived from the transactions. A difference means lines were removed, added "
            "or changed.", "The complete statement with every transaction.", ASK_FULL, refs(f.end_balance), **cat))
    if f.begin_balance and f.end_balance and f.total_deposits and f.total_withdrawals:
        want = f.begin_balance.value + f.total_deposits.value - f.total_withdrawals.value
        if abs(want - f.end_balance.value) > TOL:
            out.append(finding(
                "B_SUMMARY", sev("critical"), "Statement summary does not balance",
                f"Beginning {m(f.begin_balance.value)} + deposits {m(f.total_deposits.value)} − withdrawals "
                f"{m(f.total_withdrawals.value)} = {m(want)}, but the ending balance is {m(f.end_balance.value)}." + note,
                "This is the basic accounting identity of every bank statement.",
                f"A statement whose summary balances (ending balance {m(want)} for the totals shown).",
                ASK_ORIGINAL if not soft else ASK_CLEAR,
                refs(f.begin_balance, f.total_deposits, f.total_withdrawals, f.end_balance), **cat))
    if f.total_deposits and f.txns and not broke and abs(dep - f.total_deposits.value) > TOL:
        out.append(finding(
            "B_DEPOSITS", sev("high"), "Listed deposits do not add up to the deposit total",
            f"Deposits in the transaction list add up to {m(dep)}; the summary shows {m(f.total_deposits.value)}." + note,
            "If a deposit is inflated or inserted, the summary total no longer matches the lines.",
            "The complete, unaltered statement from the bank.", ASK_ORIGINAL if not soft else ASK_FULL,
            refs(f.total_deposits), **cat))
    if applicant and names_match(applicant, doc.text[:1500]) is False and not name_tokens(applicant) <= name_tokens(doc.text):
        out.append(finding("C_NAME", "high", "Applicant's name does not appear on the statement",
                           f"“{applicant}” was not found on the statement.",
                           "Deposits only count as the applicant's income if the account is theirs.",
                           "A statement for an account held in the applicant's name.", ASK_NAME, **cat))
    return out


def cross_checks(docs):
    """Application-level checks across analysed documents. `docs` are stored reports."""
    out, cat = [], dict(category="cross-document")
    stubs = sorted([d for d in docs if d["doc_type"] == "paystub" and d["fields"].get("pay_date")],
                   key=lambda d: d["fields"]["pay_date"])
    banks = [d for d in docs if d["doc_type"] == "bank_statement"]
    seen = {}
    for d in docs:
        if d["sha256"] in seen:
            out.append(finding("X_DUP", "low", "The same file was uploaded twice",
                               f"“{d['filename']}” is identical to “{seen[d['sha256']]}”.",
                               "A duplicate adds no evidence and can make income look better supported than it is.",
                               "A different pay period for each stub.", "Upload stubs from different pay periods.", **cat))
        seen[d["sha256"]] = d["filename"]
    ok = [d for d in stubs if d.get("verdict") != "fail"]     # failed stubs already carry their own reasons
    for a, b in zip(ok, ok[1:]):
        fa, fb = a["fields"], b["fields"]
        if fa.get("employer") and fb.get("employer") and not names_match(fa["employer"], fb["employer"]):
            continue
        if fa["pay_date"] == fb["pay_date"] and fa.get("net") != fb.get("net"):
            out.append(finding("X_SAME_PERIOD", "high", "Two stubs for the same pay date show different pay",
                               f"“{a['filename']}” shows net {fa.get('net')} and “{b['filename']}” shows net "
                               f"{fb.get('net')} for {fa['pay_date']}.",
                               "One employer issues one stub per pay date. Two versions means at least one is not genuine.",
                               "The single stub issued for that pay date.", ASK_ORIGINAL, **cat))
            continue
        ya, yb, gb = fa.get("gross_ytd"), fb.get("gross_ytd"), fb.get("gross")
        if ya and yb and gb and fa["pay_date"][:4] == fb["pay_date"][:4]:
            gap_days = (date.fromisoformat(fb["pay_date"]) - date.fromisoformat(fa["pay_date"])).days
            per = {"weekly": 7, "biweekly": 14, "semimonthly": 15, "monthly": 30}.get(fb.get("frequency"), 0)
            consecutive = per and gap_days <= per + 3
            diff = D(yb) - D(ya)
            if consecutive and abs(diff - D(gb)) > TOL:
                out.append(finding(
                    "X_YTD", "high", "Year-to-date totals do not progress correctly between stubs",
                    f"Year-to-date gross went from {m(D(ya))} ({fa['pay_date']}) to {m(D(yb))} ({fb['pay_date']}), "
                    f"an increase of {m(diff)} — but the later stub reports gross pay of {m(D(gb))}.",
                    "On consecutive stubs, year-to-date rises by exactly that period's gross. A different increase "
                    "means one of the stubs was altered.",
                    f"Consecutive stubs where the year-to-date increase equals the period's gross pay ({m(diff)} "
                    "according to the running totals).", ASK_ORIGINAL, **cat))
            elif not consecutive and diff < D(gb) - TOL:
                out.append(finding(
                    "X_YTD", "high", "Year-to-date total rose by less than a single paycheck",
                    f"Between {fa['pay_date']} and {fb['pay_date']} year-to-date gross rose {m(diff)}, less than "
                    f"the {m(D(gb))} gross on the later stub alone.",
                    "Year-to-date must grow by at least the pay of each period.", "Consistent consecutive stubs.",
                    ASK_ORIGINAL, **cat))
    # Net pay should land in the bank account.
    banks = [b for b in banks if b.get("verdict") != "fail"]
    for st in stubs:
        fs = st["fields"]
        if not fs.get("net"):
            continue
        pd = date.fromisoformat(fs["pay_date"])
        covering, hit, near_all = [], None, []
        for b in banks:
            tx = [t for t in b["fields"].get("txns", []) if t["kind"] == "deposit"]
            dates = [date.fromisoformat(t["date"]) for t in b["fields"].get("txns", [])]
            if not tx or not (min(dates) <= pd <= max(dates) - timedelta(days=4)):
                continue
            covering.append(b)
            near = [t for t in tx if abs((date.fromisoformat(t["date"]) - pd).days) <= 5]
            near_all += near
            if any(abs(D(t["amount"]) - D(fs["net"])) <= D("1.00") for t in near):
                hit = b
        if hit:
            out.append(finding(
                "X_DEPOSIT_OK", "info", "Net pay was found as a bank deposit",
                f"The {m(D(fs['net']))} net pay on “{st['filename']}” matches a deposit on “{hit['filename']}”.",
                "A matching deposit in an independent document corroborates the stub.", "Nothing needed.", **cat))
        elif covering:
            seen_amts = ", ".join(m(D(t["amount"])) for t in near_all[:3]) or "none"
            out.append(finding(
                "X_DEPOSIT", "medium", "Net pay does not appear as a deposit in the bank statement",
                f"“{st['filename']}” shows net pay of {m(D(fs['net']))} paid {fs['pay_date']}. The statement "
                f"“{covering[0]['filename']}” covers that date but has no matching deposit (deposits within 5 days: {seen_amts}).",
                "Wages paid by direct deposit show up in the account. A missing or smaller deposit means the "
                "stub overstates pay, or pay goes to a different account.",
                "The statement for the account that receives this pay, or direct payroll verification.",
                "Upload the statement for the bank account your pay is deposited into.", **cat))
    return out
