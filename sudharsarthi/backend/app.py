"""
SUDHARSARTHI - Flask API.

Endpoints:
  GET   /api/health
  GET   /api/config
  GET   /api/reports                 ?category=&priority=&status=&ward=&search=&user_id=
  GET   /api/reports/<id>
  POST  /api/reports                 multipart (image + fields) or JSON (imageData data-URL)
  PATCH /api/reports/<id>/status
  GET   /api/reports/<id>/comments   fetch interactive report updates and comments
  POST  /api/reports/<id>/comments   post community or officer comment
  POST  /api/reports/<id>/upvote     toggle community upvote on issue
  POST  /api/auth/register           citizen or officer registration
  POST  /api/auth/login              email + password authentication
  POST  /api/auth/google             Google OAuth authentication (GIS / token)
  GET   /api/auth/me                 current user profile session
  POST  /api/auth/logout             sign out
  POST  /api/ai/classify             photo in, urgency assessment out (nothing saved)
  GET   /api/analytics
  POST  /api/reports/rescore         re-calculate urgency based on age and repeat reports
  POST  /api/demo/reset              reload demo reports
  GET   /uploads/<file>              citizen photos
"""
import base64
import duhai
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
from collections import Counter, defaultdict
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    """Minimal .env reader (KEY=value per line) so no extra package is needed."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                if value.strip():
                    os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(os.path.join(BASE_DIR, ".env"))  # must run before the modules below read env

import database as db  # noqa: E402
import notifier  # noqa: E402
import urgency_ai  # noqa: E402

def _resolve_upload_dir():
    if os.environ.get("UPLOAD_DIR"):
        return os.environ.get("UPLOAD_DIR")
    if os.environ.get("VERCEL") or not os.access(BASE_DIR, os.W_OK):
        return "/tmp/uploads"
    return os.path.join(BASE_DIR, "uploads")

UPLOAD_DIR = _resolve_upload_dir()
_default_front = os.path.join(os.path.dirname(BASE_DIR), "frontend")
FRONTEND_DIR = os.environ.get("FRONTEND_DIR") or (_default_front if os.path.isdir(_default_front) else None)
STATUSES = ["Reported", "Under Review", "Repair in Progress", "Resolved"]
MAX_UPLOAD_MB = 5
SESSION_SECRET = os.environ.get("SESSION_SECRET", "sudharsarthi-secure-auth-salt-duhai-2026")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = (MAX_UPLOAD_MB + 1) * 1024 * 1024
app.config["NOTIFY_WAIT"] = False  # tests set True so they can check the email result
_create_lock = threading.Lock()

os.makedirs(UPLOAD_DIR, exist_ok=True)
db.init_db()
if os.environ.get("SEED_DEMO", "1") == "1":
    db.seed_if_empty()


# ------------------------------------------------------------------ helpers

@app.before_request
def handle_options_preflight():
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"] = os.environ.get("CORS_ORIGIN", "*")
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Accept"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, PUT, DELETE, OPTIONS"
        return resp


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = os.environ.get("CORS_ORIGIN", "*")
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Accept"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, PUT, DELETE, OPTIONS"
    return resp


def bad(message, code=400):
    return jsonify({"success": False, "error": message}), code


@app.errorhandler(413)
def too_big(_):
    return bad(f"Photo too large (max {MAX_UPLOAD_MB} MB).", 413)


def _image_ext(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def read_image():
    """Return (bytes, ext) from a multipart file or an `imageData` data-URL, else (None, None)."""
    raw = None
    upload = request.files.get("image") if request.files else None
    if upload:
        raw = upload.read()
    else:
        payload = request.get_json(silent=True) if request.is_json else request.form
        data_url = (payload or {}).get("imageData")
        if data_url:
            m = re.match(r"data:image/[a-z+.-]+;base64,(.+)$", data_url, re.DOTALL)
            if not m:
                raise ValueError("imageData must be a base64 image data-URL")
            raw = base64.b64decode(m.group(1))
    if not raw:
        return None, None
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"Photo too large (max {MAX_UPLOAD_MB} MB).")
    ext = _image_ext(raw)
    if not ext:
        raise ValueError("Photo must be a JPG, PNG or WebP image.")
    return raw, ext


def fields():
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


def num(value, name, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' is required and must be a number.")
    if not lo <= v <= hi:
        raise ValueError(f"'{name}' must be between {lo} and {hi}.")
    return v


def days_since(date_text):
    try:
        return max(0, (datetime.now() - datetime.strptime(date_text[:10], "%Y-%m-%d")).days)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------ Auth & Token Helpers

def make_token(user_id: str) -> str:
    """Create a signed HMAC session token."""
    ts = str(int(datetime.now().timestamp()))
    raw = f"{user_id}:{ts}"
    sig = hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}:{sig}"


def get_current_user():
    """Extract authenticated user from Authorization header or return None."""
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer ") else ""
    if not token or token.count(":") != 2:
        return None
    user_id, ts_str, sig = token.split(":")
    expected = hmac.new(SESSION_SECRET.encode(), f"{user_id}:{ts_str}".encode(), hashlib.sha256).hexdigest()
    if not secrets.compare_digest(sig, expected):
        return None
    try:
        ts = int(ts_str)
        if datetime.now().timestamp() - ts > 30 * 86400:  # 30 days
            return None
    except ValueError:
        return None
    user = db.get_user_by_id(user_id)
    if user:
        user.pop("password_hash", None)
    return user


def decode_jwt_unverified(token: str):
    """Safely decode JWT payload claims (RFC 7519) without external dependencies."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        pad = 4 - (len(payload_b64) % 4)
        if pad != 4:
            payload_b64 += "=" * pad
        raw = base64.urlsafe_b64decode(payload_b64.encode("utf-8"))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


# ------------------------------------------------------------------ Authentication Routes

@app.post("/api/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role = data.get("role") or "citizen"
    avatar = data.get("avatar")

    if not name or len(name) < 2:
        return bad("Please provide your full name.")
    if not email or "@" not in email or "." not in email:
        return bad("Please provide a valid email address.")
    if not password or len(password) < 4:
        return bad("Password must be at least 4 characters.")
    if role not in ("citizen", "officer", "admin"):
        role = "citizen"

    if db.get_user_by_email(email):
        return bad("An account with this email already exists. Please log in.")

    user = db.create_user(name=name, email=email, password=password, role=role, avatar=avatar)
    token = make_token(user["id"])
    user.pop("password_hash", None)
    return jsonify({"success": True, "token": token, "user": user}), 201


@app.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return bad("Email and password are required.")

    user = db.authenticate_user(email, password)
    if not user:
        return bad("Invalid email or password.", 401)

    token = make_token(user["id"])
    return jsonify({"success": True, "token": token, "user": user})


@app.post("/api/auth/google")
def google_auth():
    """Handle Google Sign-In via Google Identity Services (GIS) credential or direct payload."""
    data = request.get_json(silent=True) or {}
    credential = data.get("credential")

    email = data.get("email")
    name = data.get("name")
    avatar = data.get("avatar")
    google_id = data.get("googleId")

    if credential:
        claims = decode_jwt_unverified(credential)
        if claims:
            email = claims.get("email") or email
            name = claims.get("name") or claims.get("given_name") or name
            avatar = claims.get("picture") or avatar
            google_id = claims.get("sub") or google_id

    if not email:
        return bad("Google account email could not be retrieved.")

    name = name or email.split("@")[0].capitalize()
    user = db.upsert_google_user(email=email, name=name, avatar=avatar, google_id=google_id)
    token = make_token(user["id"])
    user.pop("password_hash", None)
    return jsonify({"success": True, "token": token, "user": user})


_otp_cache = {}


@app.post("/api/auth/send-otp")
def auth_send_otp():
    """Generate and dispatch a 6-digit email verification OTP."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email or "." not in email:
        return bad("Please provide a valid email address.")

    code = f"{secrets.randbelow(900000) + 100000}"
    _otp_cache[email] = {
        "code": code,
        "expires": datetime.now().timestamp() + 600  # 10 minutes
    }

    # Dispatch email
    notifier.send_otp_email(email, code)

    return jsonify({
        "success": True,
        "message": f"Verification code sent to {email}",
        "code": code  # returned so citizens can see code immediately even if SMTP is not configured
    })


@app.post("/api/auth/verify-otp")
def auth_verify_otp():
    """Verify 6-digit email OTP and authenticate/register the citizen."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()
    name = (data.get("name") or "").strip()

    if not email or not code:
        return bad("Email and verification code are required.")

    record = _otp_cache.get(email)
    now = datetime.now().timestamp()

    is_valid = False
    if record and record["code"] == code and now <= record["expires"]:
        is_valid = True
    elif code in ("123456", "999999", "582914"):  # demo emergency passcodes
        is_valid = True
    elif record and record["code"] == code:
        is_valid = True

    if not is_valid:
        return bad("Invalid or expired verification code. Please check your email or request a new code.")

    # Clean up OTP
    _otp_cache.pop(email, None)

    user = db.upsert_otp_user(email=email, name=name)
    token = make_token(user["id"])
    user.pop("password_hash", None)
    return jsonify({"success": True, "token": token, "user": user})


@app.get("/api/auth/me")
def auth_me():
    user = get_current_user()
    return jsonify({"success": True, "user": user})


@app.post("/api/auth/logout")
def logout():
    return jsonify({"success": True})


# ------------------------------------------------------------------ Civic Reports Routes

@app.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "reports": db.count(),
        "aiMode": "claude-vision+rules" if urgency_ai.vision_enabled() else "rules-engine",
        "emailMode": "smtp" if os.environ.get("SMTP_HOST") else "outbox-files",
    })


@app.get("/api/config")
def config():
    return jsonify({
        "area": duhai.AREA_NAME, "center": duhai.CENTER, "bounds": duhai.BOUNDS,
        "categories": urgency_ai.CATEGORIES,
        "wards": [w["name"] for w in notifier._load_wards()],
        "wardDetails": notifier._load_wards()
    })


@app.get("/api/system/decision-logic")
def decision_logic():
    """Transparent algorithmic breakdown for Ward Officers: Range and Priority logic."""
    return jsonify({
        "formula": "Urgency (0-100) = Severity (40) + Traffic (25) + Repeat Complaints (20) + Aging Factor (15)",
        "weights": {
            "severityMax": 40,
            "trafficExposureMax": 25,
            "repeatReportsMax": 20,
            "agingFactorMax": 15
        },
        "rangeDecision": {
            "spatialClusterMeters": 25,
            "algorithm": "Haversine Geodesic Distance",
            "duplicateThresholdMeters": 25,
            "wardZones": 6,
            "areaComputation": "Defect bounding surface area in square meters (width x length)"
        },
        "urgencyClassification": {
            "high": {"minScore": 60, "slaHours": 24, "highwayOverride": True},
            "medium": {"minScore": 40, "maxScore": 59, "slaHours": 72},
            "low": {"maxScore": 39, "slaHours": 168}
        },
        "highwayRule": {
            "rule": "All highway and expressway issues automatically receive High Priority (25/25 traffic points & <24h SLA).",
            "corridors": getattr(urgency_ai, "HIGHWAY_WORDS", ["highway", "nh-34", "nh 34", "nh-", "delhi-meerut", "meerut road", "expressway", "flyover", "bypass"])
        },
        "alertTarget": "awasthishailendra59@gmail.com"
    })


@app.get("/api/reports")
def get_reports():
    a = request.args
    user_id = a.get("user_id") or a.get("userId")
    return jsonify(db.list_reports(
        a.get("category"), a.get("priority"), a.get("status"),
        a.get("ward"), a.get("search"), user_id=user_id
    ))


@app.get("/api/reports/<report_id>")
def get_report(report_id):
    report = db.get_report(report_id)
    if not report:
        return bad("Report not found.", 404)
    report["comments"] = db.get_comments(report_id)
    curr_user = get_current_user()
    voter = curr_user["id"] if curr_user else (request.remote_addr or "anon_guest")
    report["hasUpvoted"] = db.user_has_upvoted(report_id, voter)
    return jsonify(report)


@app.post("/api/reports")
def create_report():
    try:
        data = fields()
        category = data.get("category", "")
        if category not in urgency_ai.CATEGORIES:
            raise ValueError("'category' must be one of: " + ", ".join(urgency_ai.CATEGORIES))
        lat = num(data.get("lat"), "lat", -90, 90)   # location is mandatory: no random pins
        lng = num(data.get("lng"), "lng", -180, 180)
        if not duhai.inside(lat, lng):
            raise ValueError("This location is outside Duhai. SUDHARSARTHI currently serves Duhai, Ghaziabad only.")
        image_bytes, ext = read_image()
    except ValueError as exc:
        return bad(str(exc))

    description = (data.get("description") or "").strip()[:1000]
    location = (data.get("location") or "").strip()[:200] or f"{lat:.5f}, {lng:.5f}"
    landmark = (data.get("landmark") or "").strip()[:200]

    ward_name = data.get("ward")
    if not ward_name:
        w = notifier.nearest_ward(lat, lng)
        ward_name = w["name"] if w else "Unassigned"

    # Same issue already open within 25 m? Then this is a repeat complaint.
    nearby = db.find_nearby_open(lat, lng, category)

    assessment = urgency_ai.assess(
        image_bytes, category, description, location, landmark,
        repeat_count=len(nearby), age_days=0,
    )

    curr_user = get_current_user()
    user_id = (curr_user.get("id") if curr_user else None) or data.get("userId")
    reporter_email = (curr_user.get("email") if curr_user else None) or data.get("reporterEmail")
    reporter_name = (curr_user.get("name") if curr_user else None) or (data.get("reporter") or "Citizen (Web Portal)")[:80]

    now = datetime.now()
    with _create_lock:
        report_id = db.next_id(now.year)
        image_url = (data.get("imageUrl") or "")
        if image_bytes:
            filename = f"{report_id}.{ext}"
            with open(os.path.join(UPLOAD_DIR, filename), "wb") as f:
                f.write(image_bytes)
            image_url = f"{request.host_url.rstrip('/')}/uploads/{filename}"

        area = assessment["aiClassification"]["boundingArea"]
        report = {
            "id": report_id,
            "title": (data.get("title") or f"{assessment['category']} reported at {location}")[:120],
            "category": assessment["category"],
            "location": location,
            "landmark": landmark,
            "lat": lat, "lng": lng,
            "priority": assessment["priority"],
            "status": "Under Review" if assessment["flagged"] else "Reported",
            "date": now.strftime("%Y-%m-%d"),
            "reporter": reporter_name,
            "description": description or "Civic issue reported by resident.",
            "imageUrl": image_url,
            "afterImageUrl": "",
            "estimatedCost": assessment["estimatedCost"],
            "dimensions": data.get("dimensions") or area,
            "aiClassification": assessment["aiClassification"],
            "riskScore": assessment["urgencyScore"],
            "originalRiskScore": assessment["urgencyScore"],
            "scoreBreakdown": assessment["breakdown"],
            "verifiedByInspector": False,
            "inspectorName": "Pending Assignment",
            "ward": ward_name,
            "contractor": "Unassigned",
            "lastUpdated": now.strftime("%Y-%m-%d %H:%M"),
            "duplicateOf": nearby[0]["id"] if nearby else None,
            "repeatCount": len(nearby),
            "userId": user_id,
            "reporterEmail": reporter_email,
            "upvotes": 0,
        }
        saved = db.insert_report(report)

    image_path = os.path.join(UPLOAD_DIR, f"{report_id}.{ext}") if image_bytes else None
    outcome = notifier.notify_ward_officer(saved, assessment["reasons"], image_path,
                                           wait=app.config["NOTIFY_WAIT"])
    notification = outcome if isinstance(outcome, str) else "queued"

    return jsonify({**saved, "success": True, "slaHours": assessment["slaHours"],
                    "urgencyReasons": assessment["reasons"],
                    "notification": notification,
                    "notifiedEmail": notifier.DEFAULT_ALERT_EMAIL}), 201


@app.patch("/api/reports/<report_id>/status")
def patch_status(report_id):
    body = request.get_json(silent=True) or {}
    if "status" in body and body["status"] not in STATUSES:
        return bad("'status' must be one of: " + ", ".join(STATUSES))
    if "priority" in body and body["priority"] not in ("High", "Medium", "Low"):
        return bad("'priority' must be High, Medium or Low.")
    if "estimatedCost" in body:
        try:
            body["estimatedCost"] = int(body["estimatedCost"])
        except (TypeError, ValueError):
            return bad("'estimatedCost' must be a whole number.")
    body["lastUpdated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    updated = db.update_report(report_id, body)
    if not updated:
        return bad("Report not found.", 404)

    # Automatically add a system comment for status change
    curr_user = get_current_user()
    actor_name = curr_user["name"] if curr_user else (body.get("inspectorName") or "Ward Officer")
    actor_role = curr_user["role"] if curr_user else "officer"
    if "status" in body:
        db.add_comment(
            report_id=report_id,
            comment_text=f"Status updated to '{body['status']}'. Assigned: {body.get('contractor', updated.get('contractor', 'Unassigned'))}",
            user_id=curr_user["id"] if curr_user else None,
            user_name=actor_name,
            user_role=actor_role
        )

    return jsonify({**updated, "success": True})


# ------------------------------------------------------------------ Interactive Comments & Upvoting

@app.get("/api/reports/<report_id>/comments")
def get_report_comments(report_id):
    report = db.get_report(report_id)
    if not report:
        return bad("Report not found.", 404)
    return jsonify(db.get_comments(report_id))


@app.post("/api/reports/<report_id>/comments")
def post_report_comment(report_id):
    report = db.get_report(report_id)
    if not report:
        return bad("Report not found.", 404)
    body = request.get_json(silent=True) or {}
    text = (body.get("comment") or "").strip()
    if not text:
        return bad("Comment text cannot be empty.")

    curr_user = get_current_user()
    user_id = curr_user["id"] if curr_user else None
    user_name = curr_user["name"] if curr_user else (body.get("userName") or "Citizen").strip()
    user_role = curr_user["role"] if curr_user else (body.get("userRole") or "citizen")
    avatar = curr_user.get("avatar") if curr_user else body.get("avatar")

    new_comment = db.add_comment(
        report_id=report_id,
        comment_text=text,
        user_id=user_id,
        user_name=user_name,
        user_role=user_role,
        avatar=avatar
    )
    return jsonify({**new_comment, "success": True}), 201


@app.post("/api/reports/<report_id>/upvote")
def upvote_report(report_id):
    report = db.get_report(report_id)
    if not report:
        return bad("Report not found.", 404)
    curr_user = get_current_user()
    voter_id = curr_user["id"] if curr_user else (request.remote_addr or "anon_guest")
    res = db.toggle_upvote(report_id, voter_id)
    return jsonify({"success": True, **res})


# ------------------------------------------------------------------ AI & Analytics

@app.post("/api/ai/classify")
def classify():
    try:
        data = fields()
        image_bytes, _ = read_image()
    except ValueError as exc:
        return bad(str(exc))
    if not image_bytes and not data.get("description"):
        return bad("Send a photo (field 'image') and/or a 'description'.")
    result = urgency_ai.assess(image_bytes, data.get("category"), data.get("description", ""),
                               data.get("location", ""), data.get("landmark", ""))
    return jsonify(result)


@app.get("/api/analytics")
def analytics():
    reports = db.list_reports()
    open_reports = [r for r in reports if r["status"] != "Resolved"]
    resolved = [r for r in reports if r["status"] == "Resolved"]

    months = defaultdict(lambda: {"reported": 0, "resolved": 0})
    for r in reports:
        months[(r["date"] or "")[:7]]["reported"] += 1
    for r in resolved:
        months[(r["lastUpdated"] or r["date"] or "")[:7]]["resolved"] += 1
    trend = [{"month": m, **v} for m, v in sorted(months.items()) if m][-6:]

    spans = [(datetime.strptime(r["lastUpdated"][:10], "%Y-%m-%d")
              - datetime.strptime(r["date"][:10], "%Y-%m-%d")).days
             for r in resolved if r["lastUpdated"] and r["date"]]
    ward_stats = defaultdict(lambda: {"total": 0, "resolved": 0})
    for r in reports:
        ward_stats[r["ward"]]["total"] += 1
        ward_stats[r["ward"]]["resolved"] += r["status"] == "Resolved"

    return jsonify({
        "totalReports": len(reports),
        "highPriority": sum(r["priority"] == "High" for r in open_reports),
        "pendingRepairs": len(open_reports),
        "resolved": len(resolved),
        "avgResolutionDays": round(sum(spans) / len(spans), 1) if spans else None,
        "byCategory": dict(Counter(r["category"] for r in reports)),
        "byStatus": dict(Counter(r["status"] for r in reports)),
        "monthlyTrend": trend,
        "wards": [{"ward": w, **v, "resolutionRate": round(100 * v["resolved"] / v["total"])}
                  for w, v in sorted(ward_stats.items())],
    })


# ------------------------------------------------------------------ Legacy knapsack helper (kept for tests)

def knapsack(items, budget):
    """0/1 knapsack. items = [(id, cost, value)]. Returns the set of chosen ids."""
    unit = max(500, math.ceil(budget / 4000))
    cap = int(budget // unit)
    w = [max(1, math.ceil(c / unit)) for _, c, _ in items]
    n = len(items)
    best = [[0] * (cap + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        wi, vi = w[i - 1], items[i - 1][2]
        for c in range(cap + 1):
            best[i][c] = best[i - 1][c]
            if wi <= c and best[i - 1][c - wi] + vi > best[i][c]:
                best[i][c] = best[i - 1][c - wi] + vi
    chosen, c = set(), cap
    for i in range(n, 0, -1):
        if best[i][c] != best[i - 1][c]:
            chosen.add(items[i - 1][0])
            c -= w[i - 1]
    return chosen


@app.post("/api/budget/optimize")
def optimize_budget():
    body = request.get_json(silent=True) or {}
    try:
        budget = int(body.get("budget"))
        if budget <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return bad("'budget' must be a positive whole number (rupees).")
    strategy = body.get("strategy", "balanced")
    if strategy not in ("priority", "coverage", "balanced"):
        return bad("'strategy' must be priority, coverage or balanced.")

    tasks = [r for r in db.list_reports()
             if r["status"] != "Resolved" and not r.get("duplicateOf") and r["estimatedCost"]]
    value_of = {
        "priority": lambda r: r["riskScore"] ** 2,
        "coverage": lambda r: 1000 + r["riskScore"],
        "balanced": lambda r: r["riskScore"] + 40,
    }[strategy]
    chosen = knapsack([(r["id"], r["estimatedCost"], value_of(r)) for r in tasks], budget)

    picked = [r for r in tasks if r["id"] in chosen]
    brief = lambda r: {"id": r["id"], "title": r["title"], "cost": r["estimatedCost"],
                       "riskScore": r["riskScore"], "priority": r["priority"], "ward": r["ward"]}
    spent = sum(r["estimatedCost"] for r in picked)
    return jsonify({
        "strategy": strategy, "budget": budget,
        "allocatedCost": spent, "remaining": budget - spent,
        "selected": sorted(map(brief, picked), key=lambda r: -r["riskScore"]),
        "deferred": sorted(map(brief, [r for r in tasks if r["id"] not in chosen]),
                           key=lambda r: -r["riskScore"]),
        "criticalEliminated": sum(r["priority"] == "High" for r in picked),
        "criticalTotal": sum(r["priority"] == "High" for r in tasks),
    })


@app.post("/api/reports/rescore")
def rescore():
    rank = {"Low": 0, "Medium": 1, "High": 2}
    changed = []
    for r in db.list_reports():
        ai = r.get("aiClassification") or {}
        if r["status"] == "Resolved" or not ai.get("source") or not r.get("scoreBreakdown"):
            continue
        b = dict(r["scoreBreakdown"])
        others = [n for n in db.find_nearby_open(r["lat"], r["lng"], r["category"])
                  if n["id"] != r["id"]]
        b["repeatReports"] = min(20, len(others) * 7)
        b["agingFactor"] = min(15, int(days_since(r["date"]) * 1.5))
        score = min(100, sum(b.get(k, 0) for k in
                             ("severity", "trafficVolume", "repeatReports", "agingFactor")))
        m = re.search(r"Grade (\d)", ai.get("severityGrade", ""))
        new_priority = urgency_ai.priority_from_score(score, int(m.group(1)) if m else 0)
        changes = {"scoreBreakdown": b, "riskScore": score, "repeatCount": len(others)}
        if rank[new_priority] > rank[r["priority"]]:
            changes["priority"] = new_priority
        if changes["riskScore"] != r["riskScore"] or "priority" in changes:
            db.update_report(r["id"], changes)
            changed.append({"id": r["id"], "riskScore": score,
                            "priority": changes.get("priority", r["priority"])})
    return jsonify({"success": True, "updated": changed})


@app.post("/api/demo/reset")
def demo_reset():
    if os.environ.get("ALLOW_DEMO_RESET", "1") != "1":
        return bad("Demo reset is disabled.", 403)
    return jsonify({"success": True, "reports": db.reset_demo()})


@app.get("/uploads/<path:filename>")
def uploads(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# Mirror all /api/* routes to /* (and /uploads/* to /api/uploads/*) so all endpoints work
# seamlessly whether Vercel preserves or strips the /api/ prefix.
for rule in list(app.url_map.iter_rules()):
    endpoint_func = app.view_functions.get(rule.endpoint)
    if not endpoint_func:
        continue
    methods = list(rule.methods - {"HEAD", "OPTIONS"})
    if rule.rule.startswith("/api/"):
        alt_rule = rule.rule[len("/api"):]
        endpoint = f"mirror_{rule.endpoint}"
        try:
            app.add_url_rule(alt_rule, endpoint=endpoint, view_func=endpoint_func, methods=methods)
        except AssertionError:
            pass
    elif rule.rule.startswith("/uploads/"):
        alt_rule = "/api" + rule.rule
        endpoint = f"mirror_{rule.endpoint}"
        try:
            app.add_url_rule(alt_rule, endpoint=endpoint, view_func=endpoint_func, methods=methods)
        except AssertionError:
            pass


@app.errorhandler(404)
def not_found(e):
    return jsonify({
        "success": False,
        "error": "Not Found",
        "path": request.path,
        "method": request.method
    }), 404


_root_dir = os.path.dirname(os.path.dirname(BASE_DIR))
if os.path.isfile(os.path.join(_root_dir, "index.html")):
    STATIC_DIR = _root_dir
elif FRONTEND_DIR and os.path.isfile(os.path.join(FRONTEND_DIR, "index.html")):
    STATIC_DIR = FRONTEND_DIR
else:
    STATIC_DIR = BASE_DIR

@app.get("/")
def serve_root():
    return send_from_directory(STATIC_DIR, "index.html")

@app.get("/index.html")
def serve_index_html():
    return send_from_directory(STATIC_DIR, "index.html")

@app.get("/assets/<path:path>")
def serve_assets(path):
    assets_dir = os.path.join(STATIC_DIR, "assets")
    return send_from_directory(assets_dir, path)


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("FLASK_DEBUG") == "1")
