"""
SUDHARSARTHI - storage layer (SQLite).

Tables:
- reports: Civic complaints and reports lodged by citizens and monitored by officers
- users: Registered citizen & officer accounts (password & Google Auth)
- comments: Interactive timeline and updates on reports
- upvotes: Community upvoting on issues

Run `python database.py` to create the database and load the demo reports.
"""
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEED_PATH = os.path.join(BASE_DIR, "seed_reports.json")

def _resolve_db_path():
    if os.environ.get("SUDHARSARTHI_DB"):
        return os.environ.get("SUDHARSARTHI_DB")
    # On serverless platforms like Vercel or AWS Lambda, the app directory is read-only.
    # We must write to /tmp to allow SQLite reads and writes.
    if os.environ.get("VERCEL") or not os.access(BASE_DIR, os.W_OK):
        import shutil
        tmp_db = os.path.join("/tmp", "sudharsarthi.db")
        orig_db = os.path.join(BASE_DIR, "sudharsarthi.db")
        if os.path.exists(orig_db) and not os.path.exists(tmp_db):
            try:
                shutil.copyfile(orig_db, tmp_db)
            except Exception:
                pass
        return tmp_db
    return os.path.join(BASE_DIR, "sudharsarthi.db")

DB_PATH = _resolve_db_path()

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
    repeat_count INTEGER DEFAULT 0,
    user_id TEXT,
    reporter_email TEXT,
    upvotes INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
CREATE INDEX IF NOT EXISTS idx_reports_ward ON reports(ward);
CREATE INDEX IF NOT EXISTS idx_reports_category ON reports(category);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT,
    role TEXT DEFAULT 'citizen',
    avatar TEXT,
    google_id TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL,
    user_id TEXT,
    user_name TEXT NOT NULL,
    user_role TEXT DEFAULT 'citizen',
    avatar TEXT,
    comment TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_report ON comments(report_id);

CREATE TABLE IF NOT EXISTS upvotes (
    report_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (report_id, user_id)
);
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
    "userId": "user_id",
    "reporterEmail": "reporter_email",
    "upvotes": "upvotes",
}
JSON_FIELDS = {"aiClassification", "scoreBreakdown"}
UPDATABLE = {
    "status", "priority", "contractor", "inspectorName", "verifiedByInspector",
    "estimatedCost", "afterImageUrl", "lastUpdated", "ward", "riskScore",
    "scoreBreakdown", "repeatCount", "upvotes",
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


def _migrate_db(conn):
    """Ensure newly added columns exist in older database files."""
    try:
        cur = conn.execute("PRAGMA table_info(reports)")
        cols = {row["name"] for row in cur.fetchall()}
        if "user_id" not in cols:
            conn.execute("ALTER TABLE reports ADD COLUMN user_id TEXT")
        if "reporter_email" not in cols:
            conn.execute("ALTER TABLE reports ADD COLUMN reporter_email TEXT")
        if "upvotes" not in cols:
            conn.execute("ALTER TABLE reports ADD COLUMN upvotes INTEGER DEFAULT 0")
    except Exception:
        pass


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_db(conn)


def _row_to_report(row):
    out = {}
    row_keys = row.keys()
    for field, col in FIELD_TO_COLUMN.items():
        if col in row_keys:
            value = row[col]
            if field in JSON_FIELDS:
                value = json.loads(value) if value else {}
            elif field == "verifiedByInspector":
                value = bool(value)
            elif field == "upvotes":
                value = int(value or 0)
            out[field] = value
        else:
            out[field] = 0 if field == "upvotes" else None
    return out


def _seq_of(report_id):
    m = re.search(r"(\d+)$", report_id or "")
    return int(m.group(1)) if m else 0


def next_id(year=None):
    """Next sequential tracking ID, e.g. NS-2026-1043."""
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


def list_reports(category=None, priority=None, status=None, ward=None, search=None, user_id=None):
    sql, args = "SELECT * FROM reports WHERE 1=1", []
    for col, val in (("category", category), ("priority", priority),
                     ("status", status), ("ward", ward)):
        if val and val != "All":
            sql += f" AND {col} = ?"
            args.append(val)
    if user_id:
        sql += " AND (user_id = ? OR LOWER(reporter_email) = LOWER(?))"
        args += [user_id, user_id]
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
        r.setdefault("upvotes", 0)
        insert_report(r)
    return len(reports)


def reset_demo():
    """Wipe the table and reload the demo reports (for presentations)."""
    init_db()
    with _write_lock, connect() as conn:
        conn.execute("DELETE FROM reports")
    return seed_if_empty()


# ------------------------------------------------------------------ User & Auth Helpers

def hash_password(password: str) -> str:
    """Generate salted PBKDF2 hash using pure Python hashlib."""
    salt = secrets.token_hex(16)
    k = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()
    return f"{salt}:{k}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify password against stored salt:hash."""
    if not stored_hash or ":" not in stored_hash:
        return False
    salt, k = stored_hash.split(":", 1)
    check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()
    return secrets.compare_digest(check, k)


def create_user(name: str, email: str, password: str = None, role: str = "citizen", avatar: str = None, google_id: str = None):
    """Create a new citizen or officer user in database."""
    email_clean = email.strip().lower()
    user_id = f"usr_{secrets.token_hex(8)}"
    now = datetime.now().isoformat()
    pwd_hash = hash_password(password) if password else None
    with _write_lock, connect() as conn:
        conn.execute(
            "INSERT INTO users (id, name, email, password_hash, role, avatar, google_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, name.strip(), email_clean, pwd_hash, role, avatar, google_id, now)
        )
    return get_user_by_id(user_id)


def get_user_by_email(email: str):
    if not email:
        return None
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email.strip(),)).fetchone()
    if not row:
        return None
    d = dict(row)
    return d


def get_user_by_id(user_id: str):
    if not user_id:
        return None
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return None
    return dict(row)


def authenticate_user(email: str, password: str):
    user = get_user_by_email(email)
    if not user or not user.get("password_hash"):
        return None
    if verify_password(password, user["password_hash"]):
        clean = dict(user)
        clean.pop("password_hash", None)
        return clean
    return None


def upsert_otp_user(email: str, name: str = None):
    """Sign in or register a user authenticating via verified Email OTP."""
    email_clean = email.strip().lower()
    existing = get_user_by_email(email_clean)
    if existing:
        return existing
    default_name = name or email_clean.split("@")[0].replace(".", " ").capitalize()
    return create_user(name=default_name, email=email_clean, role="citizen")


def upsert_google_user(email: str, name: str, avatar: str = None, google_id: str = None):
    """Sign in or register a user authenticating via Google."""
    email_clean = email.strip().lower()
    existing = get_user_by_email(email_clean)
    now = datetime.now().isoformat()
    if existing:
        with _write_lock, connect() as conn:
            conn.execute(
                "UPDATE users SET name = COALESCE(?, name), avatar = COALESCE(?, avatar), "
                "google_id = COALESCE(?, google_id) WHERE id = ?",
                (name, avatar, google_id, existing["id"])
            )
        return get_user_by_id(existing["id"])
    else:
        return create_user(name=name, email=email_clean, role="citizen", avatar=avatar, google_id=google_id)


# ------------------------------------------------------------------ Interactive Comments & Upvotes

def add_comment(report_id: str, comment_text: str, user_id: str = None, user_name: str = "Citizen", user_role: str = "citizen", avatar: str = None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with _write_lock, connect() as conn:
        cur = conn.execute(
            "INSERT INTO comments (report_id, user_id, user_name, user_role, avatar, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (report_id, user_id, user_name, user_role, avatar, comment_text.strip(), now)
        )
        comm_id = cur.lastrowid
    return {
        "id": comm_id,
        "reportId": report_id,
        "userId": user_id,
        "userName": user_name,
        "userRole": user_role,
        "avatar": avatar,
        "comment": comment_text.strip(),
        "createdAt": now
    }


def get_comments(report_id: str):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM comments WHERE LOWER(report_id) = LOWER(?) ORDER BY id ASC",
            (report_id,)
        ).fetchall()
    return [
        {
            "id": r["id"],
            "reportId": r["report_id"],
            "userId": r["user_id"],
            "userName": r["user_name"],
            "userRole": r["user_role"],
            "avatar": r["avatar"],
            "comment": r["comment"],
            "createdAt": r["created_at"]
        } for r in rows
    ]


def toggle_upvote(report_id: str, user_id: str):
    """Toggle community upvote: increment or decrement on repeat toggle."""
    now = datetime.now().isoformat()
    with _write_lock, connect() as conn:
        existing = conn.execute(
            "SELECT 1 FROM upvotes WHERE LOWER(report_id) = LOWER(?) AND user_id = ?",
            (report_id, user_id)
        ).fetchone()

        if existing:
            conn.execute(
                "DELETE FROM upvotes WHERE LOWER(report_id) = LOWER(?) AND user_id = ?",
                (report_id, user_id)
            )
            has_upvoted = False
        else:
            conn.execute(
                "INSERT INTO upvotes (report_id, user_id, created_at) VALUES (?, ?, ?)",
                (report_id, user_id, now)
            )
            has_upvoted = True

        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM upvotes WHERE LOWER(report_id) = LOWER(?)",
            (report_id,)
        ).fetchone()["c"]

        conn.execute(
            "UPDATE reports SET upvotes = ? WHERE LOWER(id) = LOWER(?)",
            (cnt, report_id)
        )

    return {"upvotes": cnt, "hasUpvoted": has_upvoted}


def user_has_upvoted(report_id: str, user_id: str) -> bool:
    if not user_id:
        return False
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM upvotes WHERE LOWER(report_id) = LOWER(?) AND user_id = ?",
            (report_id, user_id)
        ).fetchone()
    return bool(row)


if __name__ == "__main__":
    n = seed_if_empty()
    print(f"Database ready at {DB_PATH} - {count()} reports ({n} newly seeded).")
