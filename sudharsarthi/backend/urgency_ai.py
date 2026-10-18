"""
SUDHARSARTHI - AI urgency checker.

Two stages, so the result is both smart and explainable:

1. LOOK AT THE PHOTO (optional, needs ANTHROPIC_API_KEY)
   A Claude vision model checks that the photo really shows a civic problem and reports
   category, severity (1-5), approximate area, visible hazards and traffic exposure.
   If there is no API key / no network / no photo, a keyword rules engine reads the
   citizen's text instead. The result says which one was used (`source`).

2. SCORE IT (always the same formula, never a black box)
   Urgency (0-100) = severity (max 40) + traffic exposure (max 25)
                     + repeat reports (max 20) + ageing (max 15)
   These are the same weights shown on the Insights page (XAI simulator).
   Priority: >=60 High, >=40 Medium, else Low. A severity-5 hazard (immediate danger to
   life) is always High. A brand-new report can reach at most 65 points, so reports
   keep climbing as they age or attract repeat complaints (see POST /api/reports/rescore).

Only the standard library plus `requests` are required; Pillow is used (if installed)
to shrink big photos before they are sent to the model.
"""
import base64
import io
import json
import os
import re

try:
    import requests
except ImportError:  # the rules engine still works without it
    requests = None

try:
    from PIL import Image
except ImportError:
    Image = None

CATEGORIES = ["Pothole", "Road Crack", "Surface Damage", "Waterlogging",
              "Fallen Tree", "Garbage", "Other"]

BASE_SEVERITY = {"Pothole": 3.0, "Road Crack": 2.0, "Surface Damage": 2.0,
                 "Waterlogging": 3.0, "Fallen Tree": 4.0, "Garbage": 2.0, "Other": 1.5}

BASE_COST = {"Pothole": 35000, "Road Crack": 42000, "Surface Damage": 38000,
             "Waterlogging": 95000, "Fallen Tree": 18000, "Garbage": 8000, "Other": 25000}
COST_MULTIPLIER = {1: 0.6, 2: 0.8, 3: 1.0, 4: 1.2, 5: 1.45}

GRADE_LABEL = {5: "Critical", 4: "Critical", 3: "Severe", 2: "Moderate", 1: "Minor"}

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

INJURY_WORDS = ["accident", "injur", "fell", "fall ", "falling", "crash", "skid",
                "lost balance", "losing balance", "hurt", "collision"]
BLOCK_WORDS = ["block", "no passage", "impass", "road closed", "cut off"]
ELECTRIC_WORDS = ["live wire", "electric", "short circuit", "sparking", "current"]
DARK_WORDS = ["night", "dark", "unlit", "no light", "no street light", "streetlight"]

BUSY_ROAD_WORDS = ["highway", "nh-", "nh ", "expressway", "master plan road", "main road",
                   "flyover", "circle", "chowk", "junction", "bypass", "arterial",
                   "meerut road", "market", "industrial", "ring road"]
SENSITIVE_WORDS = ["school", "hospital", "college", "bus stand", "bus stop", "metro",
                   "railway", "station", "clinic", "temple", "mosque", "gurudwara", "church"]
QUIET_WORDS = ["lane", "gali", "dead end", "internal road", "inside colony", "park road"]


def vision_enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and requests is not None


# ---------------------------------------------------------------- stage 1: vision

def _prepare_image(image_bytes):
    """Return (base64_text, media_type); shrink to <=1024px JPEG when Pillow is present."""
    if Image is not None:
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img = img.convert("RGB")
            img.thumbnail((1024, 1024))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
        except Exception:
            pass
    media = "image/png" if image_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return base64.b64encode(image_bytes).decode(), media


VISION_PROMPT = """You are a road-safety inspector for an Indian municipal corporation.
Look at the photo a citizen submitted. Citizen's chosen category: "{category}".
Citizen's description: "{description}"

Reply with ONE JSON object and nothing else:
{{
  "is_civic_issue": true/false,        // does the photo really show a road/civic defect?
  "category": one of {categories},
  "severity": integer 1-5,             // 1 cosmetic ... 5 immediate danger to life
  "confidence": integer 0-100,         // how sure you are about category + severity
  "estimated_area_sqm": number or null,
  "traffic_exposure": "low" | "medium" | "high",   // judged from what is visible
  "hazards": [short strings, max 4],
  "reason": "one plain-English sentence a ward officer can read"
}}
Be conservative: if the photo is blurry, indoors, or not a civic issue, set
is_civic_issue to false and a low confidence. Do not invent details you cannot see."""


def _parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def vision_assess(image_bytes, category_hint, description, _post=None):
    """Ask Claude to look at the photo. Returns (result_dict | None, error_text | None).

    `_post` lets tests inject a fake HTTP function.
    """
    if not vision_enabled() and _post is None:
        return None, "vision disabled (no ANTHROPIC_API_KEY)"
    b64, media_type = _prepare_image(image_bytes)
    prompt = VISION_PROMPT.format(category=category_hint or "unknown",
                                  description=(description or "")[:500],
                                  categories=json.dumps(CATEGORIES))
    body = {
        "model": MODEL,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text", "text": prompt},
        ]}],
    }
    headers = {"x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
               "anthropic-version": "2023-06-01", "content-type": "application/json"}
    post = _post or requests.post
    try:
        resp = post(ANTHROPIC_URL, headers=headers, json=body, timeout=30)
        if resp.status_code != 200:
            return None, f"vision API returned HTTP {resp.status_code}"
        text = "".join(b.get("text", "") for b in resp.json().get("content", []))
        data = _parse_json(text)
    except Exception as exc:  # network down, bad JSON, timeout ...
        return None, f"vision call failed: {type(exc).__name__}"

    if data.get("category") not in CATEGORIES:
        data["category"] = category_hint if category_hint in CATEGORIES else "Other"
    try:
        data["severity"] = max(1, min(5, int(round(float(data.get("severity", 3))))))
        data["confidence"] = max(0, min(100, int(round(float(data.get("confidence", 50))))))
    except (TypeError, ValueError):
        return None, "vision reply had invalid numbers"
    data["is_civic_issue"] = bool(data.get("is_civic_issue", True))
    return data, None


# ---------------------------------------------------------------- stage 1b: rules

def rules_severity(category, description):
    """Keyword/number reading of the citizen's text. Returns (severity 1-5, reasons, signals)."""
    text = (description or "").lower()
    sev = BASE_SEVERITY.get(category, 2.0)
    reasons, signals = [], 0

    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:cm|centimet)", text)
    if m:
        depth = float(m.group(1))
        signals += 1
        if depth >= 10:
            sev += 1.0
            reasons.append(f"Reported depth {depth:g} cm is deep enough to unseat two-wheelers")
        elif depth >= 5:
            sev += 0.5
            reasons.append(f"Reported depth {depth:g} cm")
    if any(w in text for w in INJURY_WORDS):
        sev += 1.0; signals += 1
        reasons.append("Description mentions falls/accidents at this spot")
    if any(w in text for w in BLOCK_WORDS):
        sev += 0.7; signals += 1
        reasons.append("Road appears blocked or partly impassable")
    if any(w in text for w in ELECTRIC_WORDS):
        sev += 1.5; signals += 1
        reasons.append("Possible electrical hazard mentioned")
    if any(w in text for w in DARK_WORDS):
        sev += 0.4; signals += 1
        reasons.append("Poor lighting makes the hazard harder to see at night")
    return max(1, min(5, int(round(sev)))), reasons, signals


def traffic_points(location, landmark, description, vision_exposure=None):
    """0-25 points for how many people the problem exposes to risk."""
    text = f"{location or ''} {landmark or ''} {description or ''}".lower()
    pts, reasons = 8.0, []
    if any(w in text for w in BUSY_ROAD_WORDS):
        pts += 8
        reasons.append("Located on or near a busy road, junction or market")
    hit = next((w for w in SENSITIVE_WORDS if w in text), None)
    if hit:
        pts += 6
        reasons.append(f"Close to a sensitive place ({hit}) with vulnerable road users")
    if any(w in text for w in QUIET_WORDS) and not hit:
        pts -= 4
        reasons.append("Low-traffic internal lane")
    if vision_exposure == "high":
        pts += 4
        reasons.append("Photo shows heavy traffic exposure")
    elif vision_exposure == "low":
        pts -= 3
    return int(max(3, min(25, round(pts)))), reasons


# ---------------------------------------------------------------- stage 2: scoring

def priority_from_score(score, severity=0):
    if severity >= 5:
        return "High"
    return "High" if score >= 60 else "Medium" if score >= 40 else "Low"


SLA_HOURS = {"High": 24, "Medium": 72, "Low": 168}


def assess(image_bytes=None, category=None, description="", location="", landmark="",
           repeat_count=0, age_days=0, _post=None):
    """Full urgency assessment. Always returns a dict; never raises for AI problems."""
    category = category if category in CATEGORIES else "Other"
    reasons, notes = [], []
    source, vision, flagged = "rules-engine", None, False

    if image_bytes:
        vision, err = vision_assess(image_bytes, category, description, _post=_post)
        if err:
            notes.append(f"Photo not analysed by AI ({err}); scored from the text description.")
    else:
        notes.append("No photo supplied; scored from the text description.")

    rule_sev, rule_reasons, signals = rules_severity(category, description)

    if vision:
        source = "claude-vision+rules"
        category = vision["category"]
        severity = vision["severity"]
        confidence = vision["confidence"]
        exposure = vision.get("traffic_exposure")
        area = vision.get("estimated_area_sqm")
        if vision.get("reason"):
            reasons.append(vision["reason"])
        reasons += [f"Visible hazard: {h}" for h in vision.get("hazards", [])[:4]]
        # Safety net: text evidence of danger can raise, never lower, the photo's severity by 1.
        if rule_sev > severity:
            severity = min(5, severity + 1)
            reasons.append("Severity nudged up because the description reports extra danger")
        if not vision["is_civic_issue"]:
            flagged = True
            severity = min(severity, 2)
            reasons.append("AI could not see a civic issue in the photo - sent for manual review")
    else:
        severity, confidence, exposure, area = rule_sev, min(80, 50 + 10 * signals), None, None
        reasons += rule_reasons

    sev_pts = round(8 * severity)  # 1..5 -> 8..40
    traffic_pts, traffic_reasons = traffic_points(location, landmark, description, exposure)
    reasons += traffic_reasons
    repeat_pts = min(20, int(repeat_count) * 7)
    if repeat_pts:
        reasons.append(f"{repeat_count} other open report(s) within 25 m - repeated complaints")
    aging_pts = min(15, int(age_days * 1.5))
    if aging_pts:
        reasons.append(f"Open for {age_days} day(s) without resolution")

    score = int(min(100, sev_pts + traffic_pts + repeat_pts + aging_pts))
    priority = priority_from_score(score, severity)
    cost = int(round(BASE_COST.get(category, 25000) * COST_MULTIPLIER[severity] / 500.0) * 500)
    area_text = f"{area:g} sq.m" if isinstance(area, (int, float)) else "Not measured"

    return {
        "category": category,
        "severity": severity,
        "urgencyScore": score,
        "priority": priority,
        "slaHours": SLA_HOURS[priority],
        "estimatedCost": cost,
        "flagged": flagged,
        "reasons": reasons,
        "breakdown": {"severity": sev_pts, "trafficVolume": traffic_pts,
                      "repeatReports": repeat_pts, "agingFactor": aging_pts},
        "aiClassification": {
            "predictedType": category,
            "confidence": confidence,
            "severityGrade": f"{GRADE_LABEL[severity]} (Grade {severity})",
            "boundingArea": area_text,
            "source": source,
            "isSimulated": False,
            "flagged": flagged,
            "note": " ".join(notes) if notes else
                    ("Photo analysed by a vision model; score computed by the open formula."
                     if vision else ""),
        },
    }
