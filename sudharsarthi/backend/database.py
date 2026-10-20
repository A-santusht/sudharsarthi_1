"""
SUDHARSARTHI - storage layer (SQLite).

One table, `reports`. The API hands the front end camelCase objects exactly like the
ones in js/api.js / data/demo-data.js, so the existing pages keep working unchanged.

Run `python database.py` to create the database and load the demo reports.
"""
import json
import math
import os
import re
import sqlite3
import threading
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SUDHARSARTHI_DB", os.path.join(BASE_DIR, "sudharsarthi.db"))
SEED_PATH = os.path.join(BASE_DIR, "seed_reports.json")

_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    title TEXT, category TEXT, location TEXT, landmark TEXT,
    lat REAL, lng REAL,
    priority TEXT, status TEXT, date TEXT,
    reporter TEXT, description TEXT,
    image_url TEXT, after_image_url TEXT,
    estimated_cost INTEGER, dimensions TEXT,
    ai_classification TEXT,
    risk_score INTEGER, original_risk_score INTEGER, score_breakdown TEXT,
    verified_by_inspector INTEGER DEFAULT 0,
    inspector_name TEXT, ward TEXT, contractor TEXT,
    last_updated TEXT,
    duplicate_of TEXT,
    repeat_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
CREATE INDEX IF NOT EXISTS idx_reports_ward ON reports(ward);
CREATE INDEX IF NOT EXISTS idx_reports_category ON reports(category);
"""

# camelCase key (API/front end)  ->  column name
FIELD_TO_COLUMN = {
    "id": "id", "title": "title", "category": "category", "location": "location",
    "landmark": "landmark", "lat": "lat", "lng": "lng", "priority": "priority",
    "status": "status", "date": "date", "reporter": "reporter",
    "description": "description", "imageUrl": "image_url",
    "afterImageUrl": "after_image_url", "estimatedCost": "estimated_cost",
    "dimensions": "dimensions", "aiClassification": "ai_classification",
    "riskScore": "risk_score", "originalRiskScore": "original_risk_score",
    "scoreBreakdown": "score_breakdown", "verifiedByInspector": "verified_by_inspector",
    "inspectorName": "inspector_name", "ward": "ward", "contractor": "contractor",
    "lastUpdated": "last_updated", "duplicateOf": "duplicate_of",
    "repeatCount": "repeat_count",
}
JSON_FIELDS = {"aiClassification", "scoreBreakdown"}
UPDATABLE = {
    "status", "priority", "contractor", "inspectorName", "verifiedByInspector",
    "estimatedCost", "afterImageUrl", "lastUpdated", "ward", "riskScore",
    "scoreBreakdown", "repeatCount",
}


@contextmanager
def connect():
    """Open a connection, commit on success / roll back on error, and always close it."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)


def _row_to_report(row):
    out = {}
    for field, col in FIELD_TO_COLUMN.items():
        value = row[col]
        if field in JSON_FIELDS:
            value = json.loads(value) if value else {}
        elif field == "verifiedByInspector":
            value = bool(value)
        out[field] = value
    return out


def _seq_of(report_id):
    m = re.search(r"(\d+)$", report_id or "")
    return int(m.group(1)) if m else 0


def next_id(year=None):
    """Next sequential tracking ID, e.g. NS-2026-1043."""
    from datetime import datetime
    year = year or datetime.now().year
    with connect() as conn:
        row = conn.execute("SELECT MAX(seq) AS m FROM reports").fetchone()
    return f"NS-{year}-{(row['m'] or 1000) + 1}"


def insert_report(report):
    cols, vals = [], []
    for field, col in FIELD_TO_COLUMN.items():
        if field not in report:
            continue
        value = report[field]
        if field in JSON_FIELDS:
            value = json.dumps(value)
        elif field == "verifiedByInspector":
            value = 1 if value else 0
        cols.append(col)
        vals.append(value)
    cols.append("seq")
    vals.append(_seq_of(report["id"]))
    sql = f"INSERT INTO reports ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    with _write_lock, connect() as conn:
        conn.execute(sql, vals)
    return get_report(report["id"])


def get_report(report_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM reports WHERE LOWER(id) = LOWER(?)", (report_id,)
        ).fetchone()
    return _row_to_report(row) if row else None


def list_reports(category=None, priority=None, status=None, ward=None, search=None):
    sql, args = "SELECT * FROM reports WHERE 1=1", []
    for col, val in (("category", category), ("priority", priority),
                     ("status", status), ("ward", ward)):
        if val and val != "All":
            sql += f" AND {col} = ?"
            args.append(val)
    if search:
        like = f"%{search.lower()}%"
        sql += (" AND (LOWER(id) LIKE ? OR LOWER(title) LIKE ? OR LOWER(location) LIKE ?"
                " OR LOWER(category) LIKE ? OR LOWER(COALESCE(ward,'')) LIKE ?)")
        args += [like] * 5
    sql += " ORDER BY seq DESC"
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_report(r) for r in rows]


def update_report(report_id, changes):
    """Apply a whitelisted set of changes. Returns the updated report or None."""
    sets, args = [], []
    for field, value in changes.items():
        if field not in UPDATABLE:
            continue
        if field == "verifiedByInspector":
            value = 1 if value else 0
        elif field in JSON_FIELDS:
            value = json.dumps(value)
        sets.append(f"{FIELD_TO_COLUMN[field]} = ?")
        args.append(value)
    if not sets:
        return get_report(report_id)
    args.append(report_id)
    with _write_lock, connect() as conn:
        cur = conn.execute(f"UPDATE reports SET {', '.join(sets)} WHERE LOWER(id) = LOWER(?)", args)
        if cur.rowcount == 0:
            return None
    return get_report(report_id)


def haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_nearby_open(lat, lng, category, radius_m=25):
    """Unresolved reports of the same category within radius_m of a point (duplicates)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reports WHERE category = ? AND status != 'Resolved'", (category,)
        ).fetchall()
    hits = []
    for row in rows:
        if row["lat"] is None or row["lng"] is None:
            continue
        d = haversine_m(lat, lng, row["lat"], row["lng"])
        if d <= radius_m:
            hits.append((d, _row_to_report(row)))
    hits.sort(key=lambda t: t[0])
    return [r for _, r in hits]


def count():
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM reports").fetchone()["c"]


def seed_if_empty(path=SEED_PATH):
    """Load the demo Ghaziabad reports the first time the database is created."""
    init_db()
    if count() > 0 or not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        reports = json.load(f)
    for r in reports:
        r.setdefault("repeatCount", 0)
        insert_report(r)
    return len(reports)


def reset_demo():
    """Wipe the table and reload the demo reports (for presentations)."""
    init_db()
    with _write_lock, connect() as conn:
        conn.execute("DELETE FROM reports")
    return seed_if_empty()


if __name__ == "__main__":
    n = seed_if_empty()
    print(f"Database ready at {DB_PATH} - {count()} reports ({n} newly seeded).")
