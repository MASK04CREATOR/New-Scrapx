import io, os, re, json, datetime as dt
import requests, fitz, time, threading
MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
_rl = threading.Lock(); _last = [0.0]
RPM = int(os.getenv("RPM", 10))
KEY = re.compile(r"(as at|as of|as on|period ended|year ended|quarter ended|month ended|nav|valuation|record date|reporting)", re.I)

class Transient(Exception): pass

def llm(prompt, system=None, max_tokens=800):
    """Free Gemini API call, rate limited, 429/5xx -> Transient (job is retried later)."""
    with _rl:
        wait = 60.0 / RPM - (time.time() - _last[0])
        if wait > 0: time.sleep(wait)
        _last[0] = time.time()
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0}}
    if system: body["systemInstruction"] = {"parts": [{"text": system}]}
    try:
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
                          headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, json=body, timeout=90)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise Transient(str(e))
    if r.status_code in (429, 500, 502, 503, 504): raise Transient(f"Gemini HTTP {r.status_code} (rate limit/outage)")
    if r.status_code != 200: raise ValueError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
    try: return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError): raise ValueError("Gemini returned no text")

def fetch(url):
    try:
        r = requests.get(url, timeout=40, headers={"User-Agent": "ScrapX/1.0"})
    except (requests.ConnectionError, requests.Timeout) as e:
        raise Transient(str(e))
    if r.status_code in (429, 500, 502, 503, 504): raise Transient(f"HTTP {r.status_code}")
    if r.status_code != 200: raise ValueError(f"HTTP {r.status_code}")
    if len(r.content) > 30_000_000: raise ValueError("File too large")
    return r.content

def parse_pdf_date(s):
    m = re.search(r"(\d{4})(\d{2})(\d{2})", s or "")
    try: return dt.date(*map(int, m.groups())).isoformat() if m else None
    except ValueError: return None

def read_pages(data):
    doc = fitz.open(stream=data, filetype="pdf")
    meta = {"created": parse_pdf_date(doc.metadata.get("creationDate")), "modified": parse_pdf_date(doc.metadata.get("modDate"))}
    pages = [p.get_text() for p in doc]
    ocr = False
    if sum(len(t.strip()) for t in pages) < 200 * max(1, min(len(pages), 3)):
        ocr = True
        try:
            import pytesseract
            from PIL import Image
            for i, p in enumerate(doc):
                if i >= 60: break
                pix = p.get_pixmap(dpi=200)
                pages[i] = pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png"))))
        except Exception:
            pass  # OCR engine not installed here
    return meta, pages, ocr


MONTHS = {m: i + 1 for i, m in enumerate("january february march april may june july august september october november december".split())}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())}); MONTHS["sept"] = 9
_MN = "|".join(sorted(MONTHS, key=len, reverse=True))
_D = rf"(?:(\d{{1,2}})(?:st|nd|rd|th)?[\s\-]+({_MN})\.?,?[\s\-]+(\d{{4}})|({_MN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})|(\d{{4}})-(\d{{2}})-(\d{{2}}))"
RULE = re.compile(r"(as at|as of|as on|period ended|year ended|quarter ended|month ended|valuation date|nav date|reporting date)\W{0,6}" + _D, re.I)

def rule_date(pages):
    """Strict first pass: accept only if every keyword-anchored date on the first pages agrees (>=2 hits). Otherwise None -> AI."""
    found = []
    for t in pages[:4]:
        for m in RULE.finditer(t[:8000]):
            g = m.groups()[1:]
            try:
                if g[0]: d = dt.date(int(g[2]), MONTHS[g[1].lower()], int(g[0]))
                elif g[3]: d = dt.date(int(g[5]), MONTHS[g[3].lower()], int(g[4]))
                else: d = dt.date(int(g[6]), int(g[7]), int(g[8]))
                found.append(d.isoformat())
            except (ValueError, KeyError): pass
    return found[0] if len(found) >= 2 and len(set(found)) == 1 else None

def pick_context(pages, limit=14000):
    idx = list(dict.fromkeys([0, 1, 2, len(pages) - 1] + [i for i, t in enumerate(pages) if KEY.search(t[:1500])][:8]))
    out = ""
    for i in idx:
        if 0 <= i < len(pages): out += f"\n[PAGE {i+1}]\n{pages[i][:2500]}"
    return out[:limit]

PROMPT = """You analyse fund/financial PDFs. Identify the document type (Monthly Report, Annual Report, Quarterly Report, Factsheet, Fund Report, Portfolio Report, Financial Statements, Valuation Report, Other) and the RECORD DATE: the date the data is as-at (month-end for monthly, financial year-end for annual, quarter-end for quarterly, factsheet/portfolio as-at date for factsheets). Never return publication, print or PDF creation dates unless the document itself says it is the as-at date. Never guess; if unsure return null.
Expected type hint: {hint}. PDF metadata (hints only, NOT record dates): {meta}.
Reply with JSON only: {{"doc_type":str,"record_date":"YYYY-MM-DD"|null,"confidence":0-1,"evidence":str,"page":int|null}}
DOCUMENT:{ctx}"""

def analyse(data, hint):
    meta, pages, ocr = read_pages(data)
    if not "".join(pages).strip(): return dict(doc_type=None, record_date=None, confidence=0, evidence="No readable text (scanned PDF; OCR unavailable or empty)", page=None, ocr=ocr, pages=pages, meta=meta)
    rd = rule_date(pages)
    if rd:
        return validate(dict(doc_type=hint, record_date=rd, confidence=0.9, evidence="Rule match: all as-at dates on first pages agree", page=1, ocr=ocr, pages=pages, meta=meta))
    txt = llm(PROMPT.format(hint=hint, meta=meta, ctx=pick_context(pages)), max_tokens=400)
    res = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
    res.update(ocr=ocr, pages=pages, meta=meta)
    return validate(res)

def validate(res):
    d = res.get("record_date")
    if d:
        try:
            x = dt.date.fromisoformat(d)
            if x > dt.date.today() or x.year < 1990: res["record_date"], res["evidence"] = None, "Date out of plausible range"
        except ValueError: res["record_date"] = None
    return res
