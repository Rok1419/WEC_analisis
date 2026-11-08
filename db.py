import sqlite3
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path("wec_analytics.db")

def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with connect() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            filename TEXT NOT NULL,
            series TEXT,
            season TEXT,
            event TEXT,
            report_type TEXT,
            status TEXT,
            summary TEXT,
            text_preview TEXT,
            source_url TEXT
        )
        """)

def save_import(p):
    with connect() as con:
        con.execute("""
        INSERT INTO imports (created_at, filename, series, season, event, report_type, status, summary, text_preview, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            p.get("filename"), p.get("series"), p.get("season"), p.get("event"),
            p.get("report_type"), "Needs review", p.get("summary"), p.get("text", "")[:50000],
            p.get("source_url")
        ))

def list_imports():
    with connect() as con:
        rows = con.execute("SELECT * FROM imports ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

def delete_import(import_id):
    with connect() as con:
        # Find the event to delete the orphaned driver metrics
        row = con.execute("SELECT event, season FROM imports WHERE id = ?", (import_id,)).fetchone()
        if row:
            con.execute("DELETE FROM driver_metrics WHERE event = ? AND season = ?", (row["event"], row["season"]))
        
        # Delete the PDF import record
        con.execute("DELETE FROM imports WHERE id = ?", (import_id,))