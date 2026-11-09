import time, threading, traceback
from concurrent.futures import ThreadPoolExecutor
import os
from .db import conn
from . import extract
N = int(os.getenv("CONCURRENCY", 16)); MAXTRY = 5
def claim(c, k):
    rows = c.execute("SELECT id FROM docs WHERE status='pending' LIMIT ?", (k,)).fetchall()
    for r in rows: c.execute("UPDATE docs SET status='processing' WHERE id=?", (r["id"],))
    c.commit(); return [r["id"] for r in rows]
def run(did):
    c = conn(); d = c.execute("SELECT * FROM docs WHERE id=?", (did,)).fetchone()
    try:
        res = extract.analyse(extract.fetch(d["url"]), d["header"])
        c.execute("DELETE FROM pages WHERE doc=?", (did,))
        c.executemany("INSERT INTO pages VALUES(?,?,?)", [(did, i + 1, t[:6000]) for i, t in enumerate(res["pages"])])
        ok = res["record_date"] and (res.get("confidence") or 0) >= 0.75
        status = "done" if ok else "review"
        reason = None if ok else (res.get("evidence") or "Low confidence")
        c.execute("UPDATE docs SET status=?,record_date=?,doc_type=?,conf=?,reason=?,ocr=?,page=?,attempts=attempts+1 WHERE id=?",
                  (status, res["record_date"] if ok else None, res.get("doc_type"), res.get("confidence"), reason, int(res["ocr"]), res.get("page"), did))
    except extract.Transient as e:  # connectivity loss: requeue, never lose it
        a = d["attempts"] + 1
        c.execute("UPDATE docs SET status=?,reason=?,attempts=? WHERE id=?", ("pending" if a < MAXTRY else "failed", f"Transient: {e}", a, did))
        time.sleep(min(60, 2 ** a))
    except Exception as e:
        c.execute("UPDATE docs SET status='failed',reason=?,attempts=attempts+1 WHERE id=?", (f"{type(e).__name__}: {e}"[:300], did))
    c.commit(); c.close()
def loop():
    c = conn(); pool = ThreadPoolExecutor(N); sem = threading.Semaphore(N)
    def wrap(i):
        try: run(i)
        finally: sem.release()
    while True:
        try:
            sem.acquire(); ids = claim(c, 1)
            if not ids:
                sem.release()
                c.execute("UPDATE jobs SET finished=? WHERE finished IS NULL AND NOT EXISTS (SELECT 1 FROM docs WHERE job=jobs.id AND status IN('pending','processing'))", (time.time(),)); c.commit()
                time.sleep(2); continue
            pool.submit(wrap, ids[0])
        except Exception: traceback.print_exc(); time.sleep(5)
def start(): threading.Thread(target=loop, daemon=True).start()
