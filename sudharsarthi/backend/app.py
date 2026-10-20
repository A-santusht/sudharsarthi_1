"""
SUDHARSARTHI - Flask API.

Same endpoints the front end (js/api.js) was designed for:

  GET   /api/health
  GET   /api/reports                 ?category=&priority=&status=&ward=&search=
  GET   /api/reports/<id>
  POST  /api/reports                 multipart (image + fields) or JSON (imageData data-URL)
  PATCH /api/reports/<id>/status
  POST  /api/ai/classify             photo in, urgency assessment out (nothing saved)
  GET   /api/analytics
  POST  /api/budget/optimize         true 0/1 knapsack
  POST  /api/demo/reset              reload the demo reports (for presentations)
  GET   /uploads/<file>              citizen photos

Run:  python app.py        (see README.md)
"""
import base64
import duhai
import math
import os
import re
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

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
_default_front = os.path.join(os.path.dirname(BASE_DIR), "frontend")
FRONTEND_DIR = os.environ.get("FRONTEND_DIR") or (_default_front if os.path.isdir(_default_front) else None)
STATUSES = ["Reported", "Under Review", "Repair in Progress", "Resolved"]
MAX_UPLOAD_MB = 5

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


# ------------------------------------------------------------------ routes

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
    return jsonify({"area": duhai.AREA_NAME, "center": duhai.CENTER, "bounds": duhai.BOUNDS,
                    "categories": urgency_ai.CATEGORIES,
                    "wards": [w["name"] for w in notifier._load_wards()],
                    "wardDetails": notifier._load_wards()})


@app.get("/api/reports")
def get_reports():
    a = request.args
    return jsonify(db.list_reports(a.get("category"), a.get("priority"), a.get("status"),
                                   a.get("ward"), a.get("search")))


@app.get("/api/reports/<report_id>")
def get_report(report_id):
    report = db.get_report(report_id)
    return jsonify(report) if report else bad("Report not found.", 404)


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

    # Same issue already open within 25 m?  Then this is a repeat complaint.
    nearby = db.find_nearby_open(lat, lng, category)

    assessment = urgency_ai.assess(
        image_bytes, category, description, location, landmark,
        repeat_count=len(nearby), age_days=0,
    )

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
            "reporter": (data.get("reporter") or "Citizen (Web Portal)")[:80],
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
    return jsonify({**updated, "success": True}) if updated else bad("Report not found.", 404)


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


def knapsack(items, budget):
    """0/1 knapsack. items = [(id, cost, value)]. Returns the set of chosen ids."""
    unit = max(500, math.ceil(budget / 4000))           # keep the DP table small
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
        "priority": lambda r: r["riskScore"] ** 2,       # most dangerous first
        "coverage": lambda r: 1000 + r["riskScore"],     # most repairs; risk breaks ties
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
    """Re-age open reports: recount repeat complaints and days open, raise priority if earned.

    Call it from a daily cron job or a button. Only reports scored by this backend are
    touched (demo reports keep their hand-set scores). Priority is only ever raised here.
    """
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


if FRONTEND_DIR:
    @app.get("/", defaults={"path": "index.html"})
    @app.get("/<path:path>")
    def frontend(path):
        return send_from_directory(FRONTEND_DIR, path)


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("FLASK_DEBUG") == "1")
