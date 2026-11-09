import io, os, re, time, uuid
import streamlit as st, openpyxl
from app import db, worker, extract

st.set_page_config(page_title="SCRAP X", page_icon="✖", layout="wide")

@st.cache_resource
def boot():
    db.init(); worker.start(); return True
boot()

URL = re.compile(r"^https?://", re.I)
TYPES = {"mr": "MR", "ar": "AR", "qr": "QR", "factsheet": "Factsheet"}

# ---------- theme ----------
light = st.sidebar.toggle("Light mode", value=False)
ac, bg, fg = ("#000", "#fff", "#000") if light else ("#ff1a2e", "#0a0a0c", "#f2f2f2")
glow = "none" if light else "0 0 12px rgba(255,26,46,.55)"
st.markdown(f"""<style>
.stApp{{background:{bg};color:{fg}}} .stApp, .stApp p, .stApp label, .stApp h1, .stApp h2, .stApp h3, .stApp span, .stApp div[data-testid=stMetricValue]{{color:{fg}!important}}
.stApp::before{{content:"✕";position:fixed;top:50%;left:50%;font-size:70vh;line-height:1;color:{ac};opacity:.10;z-index:0;pointer-events:none;
 animation:spin 40s linear infinite;transform:translate(-50%,-50%)}}
@keyframes spin{{from{{transform:translate(-50%,-50%) rotate(0)}}to{{transform:translate(-50%,-50%) rotate(360deg)}}}}
.stButton>button,.stDownloadButton>button,[data-testid=stFileUploaderDropzone],[data-testid=stMetric],textarea,input{{border:1px solid {ac}!important;box-shadow:{glow};background:transparent!important;color:{fg}!important;border-radius:10px}}
.stButton>button:hover,.stDownloadButton>button:hover{{background:{ac}!important;color:{bg}!important}}
[data-testid=stSidebar]{{background:{bg}}} header{{background:transparent!important}}
</style>""", unsafe_allow_html=True)
st.title("SCRAP X")

# ---------- optional password gate (set APP_PASSWORD in Secrets) ----------
if os.getenv("APP_PASSWORD") and not st.session_state.get("auth"):
    pw = st.text_input("Password", type="password")
    if pw:
        if pw == os.getenv("APP_PASSWORD"): st.session_state["auth"] = True; st.rerun()
        else: st.error("Wrong password")
    st.stop()

# ---------- core logic ----------
def create_job(name, raw):
    ws = openpyxl.load_workbook(io.BytesIO(raw)).active
    items = [(c.row, c.column, str(ws.cell(1, c.column).value or "Document").strip(), c.value.strip())
             for row in ws.iter_rows(min_row=2) for c in row if isinstance(c.value, str) and URL.match(c.value.strip())]
    if not items: return None, "No PDF URLs found."
    if len(items) > 10000: return None, f"{len(items)} URLs found; maximum is 10,000."
    jid = uuid.uuid4().hex[:12]; open(f"{db.DATA}/uploads/{jid}.xlsx", "wb").write(raw)
    c = db.conn(); c.execute("INSERT INTO jobs(id,filename,created,started) VALUES(?,?,?,?)", (jid, name, time.time(), time.time()))
    c.executemany("INSERT INTO docs(job,row,col,header,url) VALUES(?,?,?,?,?)", [(jid, *i) for i in items]); c.commit()
    return jid, None

def stats(jid):
    c = db.conn(); j = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    q = lambda w: c.execute(f"SELECT COUNT(*) FROM docs WHERE job=? AND {w}", (jid,)).fetchone()[0]
    total = q("1=1"); rem = q("status IN('pending','processing')")
    return {"Total URLs": total, "Processed": total - rem, "Downloaded": q("status IN('done','review')"), "Dates extracted": q("status='done'"),
            "OCR needed": q("ocr=1"), "OCR success": q("ocr=1 AND status='done'"), "Failed": q("status='failed'"),
            "Missing date": q("status='review' AND record_date IS NULL"), "Manual review": q("status='review'"), "Remaining": rem,
            "Seconds": round((j["finished"] or time.time()) - j["started"])}, rem == 0

def build_excel(jid):
    wb = openpyxl.load_workbook(f"{db.DATA}/uploads/{jid}.xlsx"); ws = wb.active
    docs = db.conn().execute("SELECT * FROM docs WHERE job=?", (jid,)).fetchall(); out = {}; n = ws.max_column + 1
    for col in sorted({d["col"] for d in docs}):
        h = str(ws.cell(1, col).value or "Document"); out[col] = n; ws.cell(1, n, f"{TYPES.get(h.lower(), h)} Record Date"); n += 1
    for d in docs:
        ws.cell(d["row"], out[d["col"]], d["record_date"] or {"review": "REVIEW", "failed": "FAILED"}.get(d["status"], "PENDING"))
    b = io.BytesIO(); wb.save(b); return b.getvalue()

def build_errors(jid):
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Excel row", "PDF URL", "Document type", "Category", "Reason", "Attempts", "OCR attempted", "Manual review"])
    for d in db.conn().execute("SELECT * FROM docs WHERE job=? AND status IN('review','failed') ORDER BY row,col", (jid,)):
        ws.append([d["row"], d["url"], d["doc_type"] or d["header"], "Unreliable date" if d["status"] == "review" else "Access/processing failure",
                   d["reason"], d["attempts"], "Yes" if d["ocr"] else "No", "Yes"])
    b = io.BytesIO(); wb.save(b); return b.getvalue()

def ask(jid, question):
    c = db.conn(); words = set(re.findall(r"\w{3,}", question.lower()))
    rows = c.execute("SELECT p.page,p.text,d.url,d.row FROM pages p JOIN docs d ON d.id=p.doc WHERE d.job=?", (jid,)).fetchall()
    top = sorted(rows, key=lambda r: -sum(r["text"].lower().count(w) for w in words))[:14]
    ctx = "\n\n".join(f"[Doc row {r['row']} | {r['url']} | p.{r['page']}]\n{r['text'][:2500]}" for r in top)
    n = c.execute("SELECT COUNT(*) FROM docs WHERE job=? AND status IN('done','review')", (jid,)).fetchone()[0]
    system = ("Answer ONLY from the excerpts. Cite document row/URL and page for each fact. Label facts vs interpretation. Say clearly if not found. "
              f"Flag contradictions. Excerpts are top matches so say when coverage may be partial. Documents analysed: {n}.")
    return extract.llm(ctx + "\n\nQuestion: " + question, system=system, max_tokens=1200)

# ---------- UI ----------
jid = st.query_params.get("job")
up = st.file_uploader("Drag & drop an Excel file here, or click to browse", type=["xlsx", "xlsm"])
if up and st.button("Start processing", type="primary"):
    jid, err = create_job(up.name, up.getvalue())
    if err: st.error(err)
    else: st.query_params["job"] = jid; st.rerun()   # job id in URL = survives refresh

if jid:
    @st.fragment(run_every=3)
    def dash():
        s, done = stats(jid)
        st.progress(s["Processed"] / max(1, s["Total URLs"]), text="Complete" if done else "Processing (continues on the server if you close the tab)...")
        cols = st.columns(4)
        for i, (k, v) in enumerate(s.items()): cols[i % 4].metric(k, v)
    dash()
    if st.button("Prepare downloads (current results)"):
        st.session_state["xl"], st.session_state["er"] = build_excel(jid), build_errors(jid)
    if "xl" in st.session_state:
        a, b = st.columns(2)
        a.download_button("Download Excel", st.session_state["xl"], "scrapx_results.xlsx")
        b.download_button("Download error report", st.session_state["er"], "scrapx_errors.xlsx")
    st.divider(); st.subheader("Document chat")
    for m in st.session_state.setdefault("chat", []): st.chat_message(m[0]).write(m[1])
    if q := st.chat_input("Ask about your documents..."):
        st.chat_message("user").write(q); st.session_state["chat"].append(("user", q))
        try: a = ask(jid, q)
        except Exception as e: a = f"Error: {e}"
        st.chat_message("assistant").write(a); st.session_state["chat"].append(("assistant", a))
