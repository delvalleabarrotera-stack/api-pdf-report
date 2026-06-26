import re
import fitz
import requests

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="PDF → JSON Extractor (Free)", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────

def clean(val):
    """Return None for N/A / NA / dash values."""
    if val is None:
        return None
    v = str(val).strip()
    if v.upper() in ("N/A", "NA", "-", "", "NONE"):
        return None
    return v

def to_num(val):
    """Parse dollar / numeric strings to float."""
    if val is None:
        return None
    v = str(val).strip().replace("$", "").replace(",", "")
    try:
        return float(v)
    except ValueError:
        return None

def lines(text):
    return [l.strip() for l in text.splitlines() if l.strip()]


# ─────────────────────────────────────────
# Full-text extraction
# ─────────────────────────────────────────

def get_full_text(doc):
    parts = []
    for page in doc:
        t = page.get_text("text").strip()
        if t:
            parts.append(t)
    return "\n\n".join(parts)


# ─────────────────────────────────────────
# Section splitter
# ─────────────────────────────────────────

SECTION_HEADERS = [
    "REPORT SUMMARY",
    "PERSONAL INFORMATION",
    "CREDIT SCORE",
    "ADDRESS HISTORY",
    "EMPLOYMENT HISTORY",
    "TRADE ACCOUNTS",
    "INQUIRIES",
    "COLLECTIONS",
]

def split_sections(full_text):
    header_pattern = re.compile(
        r'(?m)^(REPORT SUMMARY|PERSONAL INFORMATION|CREDIT SCORE|'
        r'ADDRESS HISTORY|EMPLOYMENT HISTORY|TRADE ACCOUNTS|'
        r'(?<!\S)INQUIRIES(?!\s*:)|COLLECTIONS)\s*$'
    )
    matches = list(header_pattern.finditer(full_text))
    sections = {"HEADER": ""}
    if matches:
        sections["HEADER"] = full_text[:matches[0].start()].strip()
    for idx, match in enumerate(matches):
        header = match.group(1).strip()
        start  = match.end()
        end    = matches[idx + 1].start() if idx + 1 < len(matches) else len(full_text)
        sections[header] = full_text[start:end].strip()
    return sections


# ─────────────────────────────────────────
# Parsers per section
# ─────────────────────────────────────────

def parse_header(text):
    result = {}
    for line in lines(text):
        if "DATE PULLED:" in line:
            result["date_pulled"] = line.replace("DATE PULLED:", "").strip()
        elif line and not result.get("name"):
            result["name"] = line
    return result


def parse_report_summary(text):
    mapping = {
        "INQUIRIES":         "inquiries",
        "NEWEST OPEN":       "newest_open",
        "COLLECTIONS":       "collections",
        "OLDEST OPEN":       "oldest_open",
        "PUBLIC RECORDS":    "public_records",
        "MONTHLY PAYMENTS":  "monthly_payments",
        "30/60/90":          "delinquency_30_60_90",
        "BALANCE":           "balance",
        "ACCOUNTS":          "accounts_total",
        "CREDIT LIMIT":      "credit_limit",
        "OPEN ACCOUNTS":     "open_accounts",
        "AVAILABLE CREDIT":  "available_credit",
        "CLOSED ACCOUNTS":   "closed_accounts",
        "UTILIZATION RATIO": "utilization_ratio",
    }
    result = {}

    ls = lines(text)
    i = 0
    while i < len(ls):
        for key, field in mapping.items():
            line_up = ls[i].upper()
            if line_up.startswith(key):
                after_key = ls[i][len(key):].strip().lstrip(":").strip()
                if after_key and after_key not in ("-", ""):
                    result[field] = clean(after_key.split()[0])
                elif i + 1 < len(ls):
                    nxt = ls[i + 1].strip()
                    if nxt and not any(nxt.upper().startswith(k) for k in mapping):
                        result[field] = clean(nxt)
                        i += 1
                break
        i += 1

    inline_patterns = [
        (r"INQUIRIES[:\s]+(\d+)",          "inquiries"),
        (r"NEWEST OPEN[:\s]+([\d/]+)",      "newest_open"),
        (r"COLLECTIONS[:\s]+(\d+)",         "collections"),
        (r"OLDEST OPEN[:\s]+([\d/]+)",      "oldest_open"),
        (r"PUBLIC RECORDS[:\s]+(\d+)",      "public_records"),
        (r"30/60/90[:\s]+([\d/]+)",         "delinquency_30_60_90"),
        (r"OPEN ACCOUNTS[:\s]+(\d+)",       "open_accounts"),
        (r"CLOSED ACCOUNTS[:\s]+(\d+)",     "closed_accounts"),
        (r"\bACCOUNTS[:\s]+(\d+)",         "accounts_total"),
    ]
    text_up = text.upper()
    for pattern, field in inline_patterns:
        if not result.get(field):
            m = re.search(pattern, text_up)
            if m:
                val = clean(m.group(1))
                if val and val not in ("-",):
                    result[field] = val

    return result


def parse_personal_info(text):
    ls = lines(text)
    result = {}
    for i, line in enumerate(ls):
        u = line.upper()
        if u.startswith("NAME:"):
            result["name"] = clean(line.split(":", 1)[-1].strip() or (ls[i+1] if i+1 < len(ls) else None))
        elif u.startswith("EMAIL:"):
            result["email"] = clean(line.split(":", 1)[-1].strip() or (ls[i+1] if i+1 < len(ls) else None))
        elif u.startswith("DOB:"):
            result["date_of_birth"] = clean(line.split(":", 1)[-1].strip() or (ls[i+1] if i+1 < len(ls) else None))
        elif u.startswith("SOCIAL:"):
            result["ssn"] = clean(line.split(":", 1)[-1].strip() or (ls[i+1] if i+1 < len(ls) else None))
        elif u.startswith("AKA:"):
            result["aka"] = clean(line.split(":", 1)[-1].strip() or (ls[i+1] if i+1 < len(ls) else None))
        elif u.startswith("PHONE:"):
            result["phone"] = clean(line.split(":", 1)[-1].strip() or (ls[i+1] if i+1 < len(ls) else None))
    if not result:
        combined = " ".join(ls)
        for pat, field in [
            (r"NAME:\s*([A-Z ]+?)(?=EMAIL:|DOB:|$)", "name"),
            (r"DOB:\s*([\d-]+)", "date_of_birth"),
            (r"SOCIAL:\s*([\d-]+)", "ssn"),
            (r"EMAIL:\s*(\S+)", "email"),
            (r"PHONE:\s*(\S+)", "phone"),
        ]:
            m = re.search(pat, combined)
            if m:
                result[field] = clean(m.group(1).strip())
    return result


def parse_credit_score(text):
    scores = []
    ls = lines(text)
    i = 0
    while i < len(ls):
        line = ls[i].upper()
        if "FICO" in line or "VANTAGE" in line:
            parts = ls[i].split()
            model = parts[0] + (" " + parts[1] if len(parts) > 1 and parts[1].isdigit() else "")
            score_val = None
            reasons = []
            j = i + 1
            while j < len(ls):
                if re.match(r"^\d{3}$", ls[j].strip()):
                    score_val = int(ls[j].strip())
                    j += 1
                    break
                m = re.search(r"\b(\d{3})\b", ls[i])
                if m:
                    score_val = int(m.group(1))
                break
            while j < len(ls):
                l = ls[j]
                if "FICO" in l.upper() or "VANTAGE" in l.upper() or "N/A" == l.upper():
                    break
                if l and not l.upper().startswith("SCORE") and not l.upper().startswith("REASON") and not l.upper().startswith("NO SCORE"):
                    reasons.append(l)
                j += 1
            scores.append({
                "model": model.strip(),
                "score": score_val,
                "reason_codes": reasons if reasons else None,
            })
            i = j
        else:
            i += 1
    return scores


def parse_address_history(text):
    addresses = []
    ls = lines(text)
    start = 0
    for i, l in enumerate(ls):
        if l.upper() in ("STATUS", "STREET", "CITY", "STATE/ZIP", "DATE REPORTED"):
            start = i + 1
        elif l.upper() in ("CURRENT", "PREVIOUS"):
            start = i
            break
    i = start
    while i < len(ls):
        status_raw = ls[i].upper().strip()
        if status_raw in ("CURRENT", "PREVIOUS"):
            street    = ls[i+1] if i+1 < len(ls) else None
            city      = ls[i+2] if i+2 < len(ls) else None
            state_zip = ls[i+3] if i+3 < len(ls) else None
            date      = ls[i+4] if i+4 < len(ls) else None
            if date and not re.match(r"\d{2}-\d{2}-\d{4}", date):
                date = None
            addresses.append({
                "status":        status_raw,
                "street":        clean(street),
                "city":          clean(city),
                "state_zip":     clean(state_zip),
                "date_reported": clean(date),
            })
            i += 5
        else:
            i += 1
    return addresses


def parse_employment_history(text):
    jobs = []
    ls = lines(text)
    start = 0
    for i, l in enumerate(ls):
        if l.upper() in ("STATUS", "NAME", "OCCUPATION", "DATE REPORTED"):
            start = i + 1
        elif l.upper() in ("CURRENT", "PREVIOUS"):
            start = i
            break
    i = start
    while i < len(ls):
        status_raw = ls[i].upper().strip()
        if status_raw in ("CURRENT", "PREVIOUS"):
            employer   = ls[i+1] if i+1 < len(ls) else None
            occupation = ls[i+2] if i+2 < len(ls) else None
            date       = ls[i+3] if i+3 < len(ls) else None
            if date and not re.match(r"\d{2}-\d{2}-\d{4}", date):
                date = None
            jobs.append({
                "status":        status_raw,
                "employer":      clean(employer),
                "occupation":    clean(occupation),
                "date_reported": clean(date),
            })
            i += 4
        else:
            i += 1
    return jobs


def parse_trade_accounts(text):
    accounts = []
    blocks = re.split(r"(?=\bFURNISHER\b)", text)
    ACCOUNT_FIELDS = [
        "reported_date", "closure_date", "acc_status", "acc_rating",
        "acc_number", "pymt_history_start", "pymt_pattern", "remarks",
        "sales_indicator", "acct_type", "portfolio_type", "pymt_frequency",
        "responsibility", "date_of_opening", "terms", "last_pymt_date",
        "org_loan_amt", "high_balance", "balance", "high_credit",
        "credit_limit", "monthly_pymt", "actual_pymt", "past_due",
        "last_activity_date", "recent_pymt", "deferred_date",
        "bln_due_amt", "bln_due_date", "months_reviewed",
        "delinquency_30_60_90", "max_delinquency_date",
    ]
    for block in blocks:
        ls = [l.strip() for l in block.splitlines() if l.strip()]
        if not ls or "FURNISHER" not in ls[0].upper():
            continue
        company = ls[0].strip()
        data_lines = [l for l in ls[1:] if not re.match(r"^[\d\s\w]{2,}$", l) or
                      any(k in l.upper() for k in [
                          "CLOSED", "OPEN", "ACCOUNT", "CREDIT", "REVOLVING",
                          "INSTALLMENT", "INDIVIDUAL", "JOINT", "MONTHLY",
                          "FURNISHER", "REQUIRED", "LENDER", "PURCHASED",
                          "SIGNER", "CHARGE", "AUTO", "UNSECURED",
                      ])]
        acc = {"company": company}
        field_idx = 0
        for line in data_lines:
            if field_idx >= len(ACCOUNT_FIELDS):
                break
            field = ACCOUNT_FIELDS[field_idx]
            if re.match(r"^[0-9BCEUR ]{5,}$", line):
                continue
            val = clean(line)
            if field in ("org_loan_amt", "high_balance", "balance", "high_credit",
                         "credit_limit", "monthly_pymt", "actual_pymt", "past_due",
                         "recent_pymt", "bln_due_amt"):
                acc[field] = to_num(val) if val else None
            elif field == "months_reviewed":
                try:
                    acc[field] = int(val) if val else None
                except (ValueError, TypeError):
                    acc[field] = None
            else:
                acc[field] = val
            field_idx += 1
        if acc:
            accounts.append(acc)
    return accounts


def parse_inquiries(text):
    inquiries = []
    ls = lines(text)
    start = 0
    for i, l in enumerate(ls):
        if "COMPANY" in l.upper() and "DATE" in l.upper():
            start = i + 1
            break
    for line in ls[start:]:
        parts = line.split()
        if len(parts) >= 2 and re.match(r"\d{2}-\d{2}-\d{4}", parts[-2] if len(parts) >= 3 else parts[-1]):
            date_idx = None
            for j, p in enumerate(parts):
                if re.match(r"\d{2}-\d{2}-\d{4}", p):
                    date_idx = j
                    break
            if date_idx is None:
                continue
            company  = " ".join(parts[:date_idx])
            date     = parts[date_idx]
            industry = " ".join(parts[date_idx+1:]) if date_idx + 1 < len(parts) else None
            inquiries.append({
                "company":  clean(company),
                "date":     clean(date),
                "industry": clean(industry),
            })
    return inquiries


def parse_collections(text):
    collections = []
    ls = lines(text)
    start = 0
    for i, l in enumerate(ls):
        if "SUBSCRIBER" in l.upper() or "ORGN. CRED" in l.upper():
            start = i + 1
    blocks = re.split(r"(?=\bFURNISHER\b)", "\n".join(ls[start:]))
    for block in blocks:
        bls = [l.strip() for l in block.splitlines() if l.strip()]
        if not bls:
            continue
        col = {}
        for line in bls:
            u = line.upper()
            if u in ("FURNISHER",):
                continue
            elif u in ("INDIVIDUAL", "JOINT"):
                col["responsibility"] = line
            elif u in ("RETAIL", "FINANCIAL", "MEDICAL", "UTILITY"):
                col["creditor_type"] = line
            elif u in ("INSTALLMENT", "REVOLVING", "OPEN"):
                col["portfolio_type"] = line
            elif "UNKNOWN" in u or "COLLECTION" in u:
                col["account_type"] = line
            elif re.match(r"\d{2}-\d{2}-\d{4}", line):
                if "report_date" not in col:
                    col["report_date"] = line
                elif "assigned_date" not in col:
                    col["assigned_date"] = line
                elif "last_pymt_date" not in col:
                    col["last_pymt_date"] = line
            elif line.startswith("$"):
                if "balance" not in col:
                    col["balance"] = to_num(line)
                else:
                    col["original_amount"] = to_num(line)
            elif line == "N/A":
                pass
        if col:
            collections.append(col)
    return collections


# ─────────────────────────────────────────
# Main parser
# ─────────────────────────────────────────

def parse_credit_report(full_text):
    sections = split_sections(full_text)
    header   = parse_header(sections.get("HEADER", ""))
    return {
        "report_info": {
            "subject_name": header.get("name"),
            "date_pulled":  header.get("date_pulled"),
            "report_type":  "SOFT PULL CREDIT REPORT",
        },
        "report_summary":       parse_report_summary(sections.get("REPORT SUMMARY", "")),
        "personal_information": parse_personal_info(sections.get("PERSONAL INFORMATION", "")),
        "credit_scores":        parse_credit_score(sections.get("CREDIT SCORE", "")),
        "address_history":      parse_address_history(sections.get("ADDRESS HISTORY", "")),
        "employment_history":   parse_employment_history(sections.get("EMPLOYMENT HISTORY", "")),
        "trade_accounts":       parse_trade_accounts(sections.get("TRADE ACCOUNTS", "")),
        "inquiries":            parse_inquiries(sections.get("INQUIRIES", "")),
        "collections":          parse_collections(sections.get("COLLECTIONS", "")),
    }


# ─────────────────────────────────────────
# Shared helpers for endpoints
# ─────────────────────────────────────────

def _build_result(filename: str, doc, structured: dict) -> dict:
    meta = doc.metadata or {}
    return {
        "filename": filename,
        "pdf_metadata": {
            "title":    meta.get("title") or None,
            "creator":  meta.get("creator") or None,
            "producer": meta.get("producer") or None,
            "created":  meta.get("creationDate") or None,
        },
        **structured,
    }

def _open_pdf(content: bytes):
    try:
        return fitz.open(stream=content, filetype="pdf")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not open PDF: {str(e)}")


# ─────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────

@app.post("/extract")
async def extract_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Max 20 MB.")
    doc = _open_pdf(content)
    full_text  = get_full_text(doc)
    structured = parse_credit_report(full_text)
    result     = _build_result(file.filename, doc, structured)
    doc.close()
    return JSONResponse(content=result)


class UrlRequest(BaseModel):
    url: str
    session_cookie: str | None = None

@app.post("/extract_url")
async def extract_pdf_url(body: UrlRequest):
    headers = {"User-Agent": "Mozilla/5.0"}
    if body.session_cookie:
        headers["Cookie"] = body.session_cookie

    try:
        r = requests.get(body.url, headers=headers, timeout=30)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch URL: {str(e)}")

    if r.status_code in (401, 403):
        raise HTTPException(status_code=403, detail="URL requires authentication")
    if r.status_code != 200:
        raise HTTPException(status_code=400, detail=f"URL returned {r.status_code}")
    if "application/pdf" not in r.headers.get("content-type", ""):
        raise HTTPException(status_code=422, detail="URL did not return a PDF")

    doc = _open_pdf(r.content)
    full_text  = get_full_text(doc)
    structured = parse_credit_report(full_text)
    filename   = body.url.split("/")[-1].split("?")[0] or "report.pdf"
    result     = _build_result(filename, doc, structured)
    doc.close()
    return JSONResponse(content=result)


@app.post("/extract/raw")
async def extract_pdf_raw(file: UploadFile = File(...)):
    """Returns raw text + blocks without parsing."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Max 20 MB.")
    doc = _open_pdf(content)
    pages = []
    for i, page in enumerate(doc):
        text       = page.get_text("text").strip()
        blocks_raw = page.get_text("blocks")
        blocks = []
        for b in blocks_raw:
            x0, y0, x1, y1, c, bn, bt = b
            if c.strip():
                blocks.append({
                    "block": bn,
                    "x0": round(x0, 1), "y0": round(y0, 1),
                    "x1": round(x1, 1), "y1": round(y1, 1),
                    "text": c.strip(),
                })
        pages.append({"page": i + 1, "text": text, "blocks": blocks})
    doc.close()
    return JSONResponse(content={"filename": file.filename, "pages": pages})


@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/")
async def root():
    return {
        "message": "PDF → JSON API v2",
        "endpoints": {
            "POST /extract":     "Parse credit report PDF into structured JSON",
            "POST /extract_url": "Fetch PDF from URL and parse (supports session_cookie)",
            "POST /extract/raw": "Extract raw text and block positions",
            "GET  /health":      "Health check",
        }
    }
