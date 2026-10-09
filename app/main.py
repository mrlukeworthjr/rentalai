"""RentalAi API + web app. One service: FastAPI serves the JSON API and the
static single-page front end."""
from __future__ import annotations

import hashlib
import io
import logging
import os
import random
import secrets
import time
from datetime import date, datetime, timedelta

import joblib
import jwt
from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .db import Application, Audit, Document, Org, Session, Setting, User
from .engine import HEADLINE, analyze, checks, extract, income, model, pixel

log = logging.getLogger("rentalai")
SECRET = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
if not os.environ.get("SECRET_KEY"):
    logging.warning("SECRET_KEY is not set: sign-ins will be invalidated on every restart.")
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "15")) * 1024 * 1024
ALLOW_SIGNUP = os.environ.get("ALLOW_SIGNUP", "true").lower() == "true"
STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
RANK = {"pass": 0, "review": 1, "unreadable": 1, "fail": 2}

app = FastAPI(title="RentalAi", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.on_event("startup")
def startup():
    db.init()
    with Session() as s:                       # a retrained model, if one was saved, wins over the bundled one
        row = s.get(Setting, "model")
        if row:
            try:
                model.set_model(joblib.load(io.BytesIO(row.blob)))
            except Exception as e:
                log.warning("stored model could not be loaded: %s", e)
    model.load()


# ---------- auth -----------------------------------------------------------

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 240_000).hex()


def check_pw(pw, stored):
    salt = stored.split("$")[0]
    return secrets.compare_digest(hash_pw(pw, salt), stored)


def token_for(u):
    return jwt.encode({"sub": str(u.id), "exp": datetime.utcnow() + timedelta(days=7)}, SECRET, "HS256")


def current(authorization: str = Header(None)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sign in required")
    try:
        uid = int(jwt.decode(authorization[7:], SECRET, ["HS256"])["sub"])
    except Exception:
        raise HTTPException(401, "Session expired")
    with Session() as s:
        u = s.get(User, uid)
        if not u:
            raise HTTPException(401, "Unknown user")
        u.org
        return u


def audit(s, u, action, target="", detail=""):
    s.add(Audit(org_id=u.org_id if u else None, user_id=u.id if u else None, action=action, target=str(target),
                detail=detail))


class Register(BaseModel):
    company: str
    name: str
    email: str
    password: str


class Login(BaseModel):
    email: str
    password: str


@app.post("/api/auth/register")
def register(b: Register):
    if not ALLOW_SIGNUP:
        raise HTTPException(403, "Sign-up is closed")
    if len(b.password) < 8 or "@" not in b.email:
        raise HTTPException(400, "Use a valid email and a password of at least 8 characters")
    with Session() as s:
        if s.query(User).filter_by(email=b.email.lower().strip()).first():
            raise HTTPException(400, "That email already has an account")
        org = Org(name=b.company.strip() or "My company")
        s.add(org)
        s.flush()
        u = User(org_id=org.id, email=b.email.lower().strip(), name=b.name.strip(), pw=hash_pw(b.password))
        s.add(u)
        s.commit()
        return {"token": token_for(u)}


@app.post("/api/auth/login")
def login(b: Login):
    with Session() as s:
        u = s.query(User).filter_by(email=b.email.lower().strip()).first()
        if not u or not check_pw(b.password, u.pw):
            time.sleep(0.4)
            raise HTTPException(401, "Wrong email or password")
        return {"token": token_for(u)}


@app.get("/api/me")
def me(u: User = Depends(current)):
    return {"name": u.name, "email": u.email, "company": u.org.name, "income_multiple": u.org.income_multiple}


# ---------- processing -----------------------------------------------------

def process(doc_id: int):
    with Session() as s:
        d = s.get(Document, doc_id)
        if not d:
            return
        d.status = "processing"
        s.commit()
        try:
            rep = analyze(d.data, d.filename, d.application.applicant_name)
            d.report, d.doc_type, d.verdict, d.risk, d.status = rep, rep["doc_type"], rep["verdict"], rep["risk"], "done"
        except Exception as e:            # never leave a document stuck
            log.exception("analysis failed")
            d.status, d.verdict = "error", "unreadable"
            d.report = {"filename": d.filename, "verdict": "unreadable", "headline": "Analysis failed",
                        "findings": [], "failed_because": [f"Internal error: {type(e).__name__}"],
                        "needs_to_pass": ["Run the analysis again; if it fails again the file is malformed."],
                        "applicant_requests": [], "pages": [], "passed": [], "fields": {}, "doc_type": "unknown",
                        "sha256": d.sha256, "risk": 0}
        s.commit()
        refresh(s, d.application_id)


def effective(d):
    """Verdict after any analyst decision."""
    if d.review:
        return "pass" if d.review["decision"] == "genuine" else "fail"
    return d.verdict


def refresh(s, app_id: int):
    a = s.get(Application, app_id)
    done = [d for d in a.documents if d.status in ("done", "error") and d.report]
    reports = [dict(d.report, verdict=effective(d), filename=d.filename) for d in done]
    cross = checks.cross_checks([r for r in reports if r.get("fields") is not None and r.get("sha256")])
    org = s.get(Org, a.org_id)
    from decimal import Decimal
    inc = income.calculate(reports, a.monthly_rent, Decimal(str(org.income_multiple or 3)))
    worst = max([RANK[r["verdict"]] for r in reports] + [RANK[model.verdict(cross, None)]] if reports else [0])
    pending = any(d.status in ("queued", "processing") for d in a.documents)
    needs, asks = [], []
    for r in reports:
        if r["verdict"] != "pass":
            for n in r.get("needs_to_pass", []):
                if n not in needs:
                    needs.append(n)
            for q in r.get("applicant_requests", []):
                if q not in asks:
                    asks.append(q)
    for f in cross:
        if f["severity"] in ("high", "medium"):
            needs.append(f["fix"]) if f["fix"] not in needs else None
            if f.get("applicant_request") and f["applicant_request"] not in asks:
                asks.append(f["applicant_request"])
    a.verdict = None if not reports else ["pass", "review", "fail"][worst]
    a.summary = {"cross": cross, "income": inc, "needs_to_pass": needs, "applicant_requests": asks,
                 "pending": pending}
    s.commit()


def store(s, a, files, source, bg):
    ids = []
    for f in files[:10]:
        data = f.file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, f"{f.filename} is larger than {MAX_BYTES // 1048576} MB")
        if not data:
            continue
        if extract.sniff(data) is None:
            raise HTTPException(400, f"{f.filename}: upload a PDF, JPEG or PNG")
        d = Document(application_id=a.id, filename=os.path.basename(f.filename or "document")[:200],
                     sha256=hashlib.sha256(data).hexdigest(), size=len(data), data=data, source=source)
        s.add(d)
        s.flush()
        ids.append(d.id)
    s.commit()
    for i in ids:
        bg.add_task(process, i)
    return ids


# ---------- applications ---------------------------------------------------

class NewApp(BaseModel):
    applicant_name: str
    applicant_email: str | None = None
    property: str | None = None
    monthly_rent: float | None = None


def doc_row(d):
    r = d.report or {}
    return {"id": d.id, "filename": d.filename, "status": d.status, "verdict": effective(d), "engine_verdict": d.verdict,
            "risk": d.risk, "doc_type": d.doc_type, "source": d.source, "reviewed": bool(d.review),
            "headline": HEADLINE.get(effective(d) or "", r.get("headline")),
            "failed_because": r.get("failed_because", []), "created_at": d.created_at.isoformat()}


def own_app(s, u, app_id) -> Application:
    a = s.get(Application, app_id)
    if not a or a.org_id != u.org_id:
        raise HTTPException(404, "Application not found")
    return a


def own_doc(s, u, doc_id) -> Document:
    d = s.get(Document, doc_id)
    if not d or d.application.org_id != u.org_id:
        raise HTTPException(404, "Document not found")
    return d


@app.get("/api/applications")
def list_apps(u: User = Depends(current)):
    with Session() as s:
        rows = s.query(Application).filter_by(org_id=u.org_id).order_by(Application.id.desc()).limit(300).all()
        return [{"id": a.id, "applicant_name": a.applicant_name, "property": a.property, "verdict": a.verdict,
                 "monthly_rent": a.monthly_rent, "n_docs": len(a.documents),
                 "pending": any(d.status in ("queued", "processing") for d in a.documents),
                 "monthly_income": (a.summary or {}).get("income", {}).get("monthly_gross"),
                 "created_at": a.created_at.isoformat()} for a in rows]


@app.post("/api/applications")
def create_app(b: NewApp, u: User = Depends(current)):
    if not b.applicant_name.strip():
        raise HTTPException(400, "Applicant name is required")
    with Session() as s:
        a = Application(org_id=u.org_id, applicant_name=b.applicant_name.strip(), applicant_email=b.applicant_email,
                        property=b.property, monthly_rent=b.monthly_rent, summary={})
        s.add(a)
        s.flush()
        audit(s, u, "application.create", a.id)
        s.commit()
        return {"id": a.id}


@app.get("/api/applications/{app_id}")
def get_app(app_id: int, u: User = Depends(current)):
    with Session() as s:
        a = own_app(s, u, app_id)
        sm = a.summary or {}
        return {"id": a.id, "applicant_name": a.applicant_name, "applicant_email": a.applicant_email,
                "property": a.property, "monthly_rent": a.monthly_rent, "verdict": a.verdict, "token": a.token,
                "training_consent": bool(a.training_consent), "consent_by": a.consent_by,
                "consent_at": a.consent_at.isoformat() if a.consent_at else None,
                "pending": any(d.status in ("queued", "processing") for d in a.documents),
                "documents": [doc_row(d) for d in a.documents], "cross": sm.get("cross", []),
                "income": sm.get("income"), "needs_to_pass": sm.get("needs_to_pass", []),
                "applicant_requests": sm.get("applicant_requests", []), "created_at": a.created_at.isoformat()}


class ConsentIn(BaseModel):
    training: bool


def set_consent(s, a, value: bool, who: str):
    a.training_consent, a.consent_at, a.consent_by = value, datetime.utcnow(), who[:40]
    s.add(Audit(org_id=a.org_id, action="consent.grant" if value else "consent.withdraw", target=str(a.id), detail=who))


@app.post("/api/applications/{app_id}/consent")
def staff_consent(app_id: int, b: ConsentIn, u: User = Depends(current)):
    """Staff record consent the applicant gave outside the upload page (for example on a signed form)."""
    with Session() as s:
        a = own_app(s, u, app_id)
        set_consent(s, a, b.training, "staff:" + u.email)
        s.commit()
        return {"training_consent": a.training_consent}


@app.delete("/api/applications/{app_id}")
def delete_app(app_id: int, u: User = Depends(current)):
    with Session() as s:
        a = own_app(s, u, app_id)
        audit(s, u, "application.delete", a.id, a.applicant_name)
        s.delete(a)
        s.commit()
        return {"ok": True}


@app.post("/api/applications/{app_id}/documents")
def upload(app_id: int, bg: BackgroundTasks, files: list[UploadFile] = File(...), u: User = Depends(current)):
    with Session() as s:
        a = own_app(s, u, app_id)
        ids = store(s, a, files, "staff", bg)
        audit(s, u, "document.upload", a.id, f"{len(ids)} file(s)")
        s.commit()
        return {"ids": ids}


@app.get("/api/documents/{doc_id}")
def get_doc(doc_id: int, u: User = Depends(current)):
    with Session() as s:
        d = own_doc(s, u, doc_id)
        audit(s, u, "document.view", d.id)
        s.commit()
        rep = dict(d.report or {})
        rep.pop("features", None)
        return {**doc_row(d), "application_id": d.application_id, "applicant_name": d.application.applicant_name,
                "review": d.review, "report": rep}


@app.get("/api/documents/{doc_id}/pages/{n}")
def page_image(doc_id: int, n: int, u: User = Depends(current)):
    with Session() as s:
        d = own_doc(s, u, doc_id)
        try:
            return Response(extract.render_page(d.data, n), media_type="image/png",
                            headers={"Cache-Control": "private, max-age=3600"})
        except Exception:
            raise HTTPException(404, "Page not available")


@app.get("/api/documents/{doc_id}/original")
def original(doc_id: int, u: User = Depends(current)):
    with Session() as s:
        d = own_doc(s, u, doc_id)
        audit(s, u, "document.download", d.id)
        s.commit()
        return Response(d.data, media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{d.filename}"'})


@app.post("/api/documents/{doc_id}/reanalyze")
def reanalyze(doc_id: int, bg: BackgroundTasks, u: User = Depends(current)):
    with Session() as s:
        d = own_doc(s, u, doc_id)
        d.status = "queued"
        s.commit()
    bg.add_task(process, doc_id)
    return {"ok": True}


class ReviewIn(BaseModel):
    decision: str            # genuine | fraudulent | clear
    notes: str | None = None


@app.post("/api/documents/{doc_id}/review")
def review(doc_id: int, b: ReviewIn, u: User = Depends(current)):
    if b.decision not in ("genuine", "fraudulent", "clear"):
        raise HTTPException(400, "decision must be genuine, fraudulent or clear")
    with Session() as s:
        d = own_doc(s, u, doc_id)
        d.review = None if b.decision == "clear" else {
            "decision": b.decision, "notes": (b.notes or "")[:2000], "by": u.name or u.email,
            "at": datetime.utcnow().isoformat()}
        audit(s, u, "document.review", d.id, b.decision)
        s.commit()
        refresh(s, d.application_id)
        return {"ok": True}


# ---------- model ----------------------------------------------------------

def trainable(d) -> bool:
    """The single gate for training use: an analyst label, stored features, and the applicant's consent."""
    return bool(d.review and d.report and d.report.get("features") and d.application.training_consent)


@app.get("/api/model")
def model_info(u: User = Depends(current)):
    m = model.load() or {}
    with Session() as s:
        labelled = [d for d in s.query(Document).filter(Document.review.isnot(None)).all() if d.review]
        usable = len([d for d in labelled if trainable(d)])
    return {"available": bool(m), "version": m.get("version"), "trained_on": m.get("trained_on"),
            "metrics": m.get("metrics"), "labelled_documents": len(labelled), "consented_documents": usable,
            "pixel": (pixel.load() or {}).get("metrics"),
            "features": model.FEATURES}


@app.post("/api/model/retrain")
def retrain(u: User = Depends(current)):
    from ml.train import retrain as do
    base = model.load()
    if not base:
        raise HTTPException(400, "No base model is installed")
    with Session() as s:
        rows = [d for d in s.query(Document).filter(Document.review.isnot(None)).all() if trainable(d)]
        X = [d.report["features"] for d in rows]
        y = [1 if d.review["decision"] == "fraudulent" else 0 for d in rows]
        new = do(base, X, y)
        buf = io.BytesIO()
        joblib.dump(new, buf, compress=3)
        row = s.get(Setting, "model") or Setting(key="model")
        row.blob = buf.getvalue()
        s.add(row)
        audit(s, u, "model.retrain", new["version"], f"{len(y)} labelled")
        s.commit()
    model.set_model(new)
    return {"version": new["version"], "trained_on": new["trained_on"]}


# ---------- sample data ----------------------------------------------------

@app.post("/api/demo")
def demo(bg: BackgroundTasks, u: User = Depends(current)):
    """Create two sample applications from synthetic documents so the product can be explored."""
    from ml import synth
    today = date.today()
    rng = random.Random(today.toordinal())
    made = []
    with Session() as s:
        for tampered in (False, True):
            t1 = synth.stub_truth(rng, today - timedelta(days=24))
            t1.update(freq="biweekly", ot=False)
            t1 = synth.stub_truth(rng, today - timedelta(days=24), base=dict(t1, k=None))
            t2 = synth.stub_truth(rng, today - timedelta(days=10), base=dict(t1, k=t1["k"] + 1))
            for _ in range(40):
                bt = synth.bank_truth(rng, t1["name"], [(t1["pay_date"], t1["net"]), (t2["pay_date"], t2["net"])],
                                      today - timedelta(days=30), t1["employer"])
                if bt["last"] >= t2["pay_date"] + timedelta(days=4):
                    break
            files = {"paystub-1.pdf": synth.render_stub(t1), "bank-statement.pdf": synth.render_bank(bt)}
            p2 = synth.render_stub(t2)
            if tampered:
                f = synth.D("1.8")
                for old in (t2["gross"], t2["net"], t2["earnings"][0][3]):
                    p2, _ = synth.retype(p2, synth.fm(old), synth.fm(synth.c2(old * f)), rng, "helv", True)
                p2 = synth.resave_incremental(synth.stamp(p2, "Sejda 3.2", t2["pay_date"] - timedelta(days=2), today, 21))
            files["paystub-2.pdf"] = p2
            rent = round(float(t1["gross"]) * 26 / 12 / (3.4 if not tampered else 2.4), -1)
            a = Application(org_id=u.org_id, applicant_name=t1["name"], property="Sample: 2B at Mesa Verde Lofts",
                            monthly_rent=rent, summary={})
            s.add(a)
            s.flush()
            for name, data in files.items():
                d = Document(application_id=a.id, filename=name, sha256=hashlib.sha256(data).hexdigest(),
                             size=len(data), data=data, source="sample")
                s.add(d)
                s.flush()
                bg.add_task(process, d.id)
            made.append(a.id)
        s.commit()
    return {"ids": made}


# ---------- applicant (public, token-scoped) -------------------------------

def by_token(s, token) -> Application:
    a = s.query(Application).filter_by(token=token).first()
    if not a:
        raise HTTPException(404, "This link is not valid")
    return a


@app.get("/api/public/{token}")
def public_status(token: str):
    """Applicants see what to send next, never the forensic detail of why."""
    with Session() as s:
        a = by_token(s, token)
        org = s.get(Org, a.org_id)
        state = lambda d: ("Checking" if d.status in ("queued", "processing") else
                           "Received" if effective(d) == "pass" else "More information needed")
        return {"applicant_name": a.applicant_name, "company": org.name, "property": a.property,
                "training_consent": bool(a.training_consent),
                "documents": [{"filename": d.filename, "state": state(d)} for d in a.documents],
                "requests": (a.summary or {}).get("applicant_requests", [])}


@app.post("/api/public/{token}/consent")
def public_consent(token: str, b: ConsentIn):
    """The applicant opts in to, or withdraws from, their documents being used to improve detection."""
    with Session() as s:
        a = by_token(s, token)
        set_consent(s, a, b.training, "applicant")
        s.commit()
        return {"training_consent": a.training_consent}


@app.post("/api/public/{token}/documents")
def public_upload(token: str, bg: BackgroundTasks, files: list[UploadFile] = File(...)):
    with Session() as s:
        a = by_token(s, token)
        if len(a.documents) >= 30:
            raise HTTPException(400, "Upload limit reached for this application")
        return {"received": len(store(s, a, files, "applicant", bg))}


# ---------- front end ------------------------------------------------------

@app.get("/healthz")
def health():
    return {"ok": True, "model": bool(model.load())}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str, request: Request):
    if path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})
