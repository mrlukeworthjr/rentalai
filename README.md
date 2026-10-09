# RentalAi

Income-document verification for rental applications. Upload a pay stub or bank statement and RentalAi
returns a verdict with, for every problem it finds: **what** is wrong, **why** it matters, and **what it
would take to pass** — with the exact spot highlighted on the page.

One service: FastAPI (API + analysis engine) serving a no-build web app. PostgreSQL on Railway, SQLite locally.

## Deploy to Railway

1. Put this folder in a GitHub repository.
2. In Railway: **New Project → Deploy from GitHub repo** and pick it. Railway builds the `Dockerfile`.
3. Add a database: **New → Database → PostgreSQL**.
4. On the RentalAi service, open **Variables** and set:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `SECRET_KEY` = a long random string (`python -c "import secrets;print(secrets.token_hex(32))"`)
5. **Settings → Networking → Generate Domain**, open it, and create your account.
6. Set `ALLOW_SIGNUP=false` once your account exists, otherwise anyone with the URL can register.

Without `DATABASE_URL` the app falls back to a SQLite file inside the container, which Railway erases on
every deploy. Uploaded files are stored in the database, so Postgres is the only thing to back up.

## Run locally

```bash
sudo apt-get install tesseract-ocr        # macOS: brew install tesseract
pip install -r requirements.txt
SECRET_KEY=dev uvicorn app.main:app --reload
# http://localhost:8000  → create an account → "Add sample applications"
python -m pytest -q                        # pip install pytest
```

`samples/` holds genuine and tampered example documents to upload by hand (applicant name: Jordan Rivera).

## What it checks

| Layer | Checks |
|---|---|
| File structure (PDF) | Saved by an editor (Photoshop, Canva, iLovePDF, Sejda…); made in Word/Excel; modified after creation; multiple saved revisions; annotations or overlays; a value covered and retyped (the original text is still in the file); isolated white boxes under figures; figures in a different typeface or size; figures out of line with their column; typed text on top of a scan; stripped metadata |
| Images and scans | Editing-software signature; error-level analysis for locally recompressed regions; flagged as "can be read but not fully verified" |
| Pay stub arithmetic | Gross − deductions = net; deduction lines sum to total; earnings lines sum to gross; rate × hours = amount |
| Payroll tax law | Social Security = 6.2% and Medicare = 1.45% of taxable wages (allowing pre-tax benefits and the annual wage base). An inflated gross almost always leaves these lines at their true values, so the report states the wages they imply |
| Calendar | Year-to-date below current; year-to-date impossible for the date; period length vs pay frequency; future or stale pay date; file created long before its pay date |
| Bank statements | Every running balance follows from the previous line; summary identity; listed deposits vs deposit total |
| Identity | Name on the document vs the applicant |
| Across documents | Year-to-date progression between consecutive stubs; two versions of one pay date; net pay present as a bank deposit; duplicate uploads |

Income is calculated in decimal arithmetic from documents that did not fail (`app/engine/income.py`), with
the inputs stored so the figure can be reproduced.

## How the result is decided

1. Deterministic checks produce findings, each with a severity.
2. A gradient-boosted model (`app/engine/model.py`) scores the pattern of findings into one probability.
3. Verdict: **Failed** on any conclusive finding or two serious ones; **Needs review** on one serious
   finding, two that need explanation, or a high model probability; otherwise **Passed**.
4. An analyst can override any result. The override is logged and becomes a training label.

Read from OCR, arithmetic misses are downgraded to "needs explanation" because one misread digit can cause them.

## Making the model better — read this

The bundled model is trained on **synthetic** documents produced by `ml/synth.py`: generated stubs and
statements, tampered the way people really do it (retyping in a PDF editor, white-out and overtype,
rebuilding from a template, painting over a photo, using someone else's stub). On that synthetic set it
passes 94% of genuine documents, fails none of them, and flags 91% of tampered ones.

Those figures describe the simulator, **not real applicants' documents**. Nobody can honestly claim this
beats a commercial product until it has been measured on real, labelled files. The path there is built in:

- Every analyst decision ("Confirm genuine" / "Confirm fraudulent") is stored with the document's features.
- **Consent gate:** a document is used for training only if its applicant has consented — a tick box on the
  applicant upload page, or recorded by staff on the application. Consent can be withdrawn; the next retrain
  drops those documents. The check lives in one function, `trainable()` in `app/main.py`.
- **Detection model → Retrain with analyst decisions** refits on synthetic + real labels, weighting each
  real one 8×, and stores the new model in the database. As real labels accumulate they dominate.
- `python -m ml.train` rebuilds the base model; extend `ml/synth.py` with layouts from the providers you see.

Known blind spot, shown plainly on the model page: a fake that is internally consistent and carries
provider-style metadata cannot be caught from the file alone. Closing that gap needs a second source —
the bank-deposit cross-check here, and next a payroll/bank data connection (Argyle, Pinwheel, Plaid).

## What applicants see

The applicant link (`/apply/<token>`) accepts uploads without an account and shows only what to send next
("upload the original PDF from your payroll portal"). The forensic detail — which figure, which font, what
the number should have been — is shown to your staff only, so the report cannot be used as a guide to
making a better forgery.

## Not built yet

- Payroll and bank account connections (needs a contract with a provider)
- Object storage (S3/R2) for originals; a job queue and separate workers for volume
- Roles within a company, SSO, password reset email, rate limiting
- PDF report export, webhooks, dispute workflow for applicants
- A security review. This handles pay and bank data: get one before real applicants use it.

## Compliance

Screening tenants on income documents is regulated (in the US: the Fair Credit Reporting Act, fair-housing
law, state and city rules). Results here are evidence for a person to weigh, not an automated denial.
Talk to a lawyer about adverse-action notices, dispute handling and data retention before going live.

## Layout

```
app/main.py            API, auth, processing, applicant endpoints
app/db.py              tables (orgs, users, applications, documents, audit_events, settings)
app/engine/extract.py  PDF/image → positioned text and structural facts (PyMuPDF, Tesseract)
app/engine/parse.py    document type and fields
app/engine/forensics.py  file-level checks
app/engine/checks.py   content and cross-document checks
app/engine/model.py    features, model scoring, verdict
app/engine/income.py   income calculation
ml/synth.py, ml/train.py   synthetic data and training
static/                web app
```
