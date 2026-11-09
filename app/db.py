import sqlite3, os, threading
DATA = os.getenv("DATA_DIR", "data")
os.makedirs(DATA + "/uploads", exist_ok=True)
_l = threading.Lock()
def conn():
    c = sqlite3.connect(DATA + "/scrapx.db", timeout=30, check_same_thread=False); c.row_factory = sqlite3.Row; return c
def init():
    c = conn()
    c.executescript("""CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, filename TEXT, created REAL, started REAL, finished REAL);
    CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, job TEXT, row INT, col INT, header TEXT, url TEXT,
      status TEXT DEFAULT 'pending', record_date TEXT, doc_type TEXT, conf REAL, reason TEXT, ocr INT DEFAULT 0, attempts INT DEFAULT 0, page INT);
    CREATE INDEX IF NOT EXISTS i1 ON docs(job,status);
    CREATE TABLE IF NOT EXISTS pages(doc INTEGER, page INT, text TEXT);
    CREATE INDEX IF NOT EXISTS i2 ON pages(doc);""")
    c.execute("UPDATE docs SET status='pending' WHERE status='processing'")  # resume after crash
    c.commit(); c.close()
