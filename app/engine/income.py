"""Income calculation. Deterministic decimal arithmetic; inputs are kept so a
reviewer can reproduce the figure."""
from datetime import date
from decimal import Decimal as D

FACTOR = {"weekly": D(52) / 12, "biweekly": D(26) / 12, "semimonthly": D(2), "monthly": D(1)}
POLICY = "income-v1"


def q(v):
    return str(v.quantize(D("0.01")))


def calculate(docs, monthly_rent=None, multiple=D(3)):
    """`docs` are stored reports. Failed documents never count toward income."""
    used, excluded = [], []
    for d in docs:
        f = d.get("fields", {})
        if d["doc_type"] != "paystub" or not f.get("gross"):
            continue
        (excluded if d["verdict"] in ("fail", "unreadable") else used).append(d)
    out = {"policy": POLICY, "inputs": [], "excluded": [e["filename"] for e in excluded],
           "monthly_gross": None, "annual_gross": None, "ytd_monthly": None, "notes": []}
    per = []
    for d in used:
        f = d["fields"]
        freq = f.get("frequency")
        if freq not in FACTOR:
            out["notes"].append(f"{d['filename']}: pay frequency unknown, not annualised.")
            continue
        monthly = D(f["gross"]) * FACTOR[freq]
        per.append(monthly)
        out["inputs"].append({"file": d["filename"], "gross": f["gross"], "frequency": freq,
                              "pay_date": f.get("pay_date"), "monthly": q(monthly),
                              "status": d["verdict"]})
    if per:
        mg = sum(per) / len(per)
        out["monthly_gross"], out["annual_gross"] = q(mg), q(mg * 12)
        latest = max((d for d in used if d["fields"].get("gross_ytd") and d["fields"].get("pay_date")),
                     key=lambda d: d["fields"]["pay_date"], default=None)
        if latest:
            pd = date.fromisoformat(latest["fields"]["pay_date"])
            months = D(pd.timetuple().tm_yday) / D(365) * 12
            if months >= 1:
                ym = D(latest["fields"]["gross_ytd"]) / months
                out["ytd_monthly"] = q(ym)
                if ym < mg * D("0.85"):
                    out["notes"].append("Year-to-date pay averages below the recent paychecks: income may have "
                                        "risen recently, started mid-year, or vary by period.")
        if any(i["status"] == "review" for i in out["inputs"]):
            out["notes"].append("Includes documents still awaiting review; treat as provisional.")
        if monthly_rent:
            rent = D(str(monthly_rent))
            out["rent"] = q(rent)
            out["required_monthly"] = q(rent * multiple)
            out["income_to_rent"] = str((mg / rent).quantize(D("0.01"))) if rent else None
            out["meets_requirement"] = mg >= rent * multiple
    if excluded:
        out["notes"].append("Failed documents are excluded from income.")
    return out
