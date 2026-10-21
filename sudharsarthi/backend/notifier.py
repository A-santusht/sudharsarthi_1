"""
SUDHARSARTHI - ward officer notifier.

When a report is created the responsible ward officer gets an email with the photo,
a map link, the urgency score and the reasons behind it.

* If SMTP_HOST (and friends) are set in the environment, a real email is sent.
* If not, the message is written to ./outbox/<tracking-id>.txt so the demo still shows
  exactly what would have been sent.

Sending runs in a background thread so a slow mail server never delays the citizen.
"""
import json
import logging
import mimetypes
import os
import smtplib
import threading
from email.message import EmailMessage

from database import haversine_m

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTBOX = os.path.join(BASE_DIR, "outbox")
WARDS_PATH = os.path.join(BASE_DIR, "wards.json")
DEFAULT_ALERT_EMAIL = "awasthishailendra59@gmail.com"
log = logging.getLogger("sudharsarthi.notifier")


def _load_wards():
    try:
        with open(WARDS_PATH, encoding="utf-8") as f:
            return json.load(f)["wards"]
    except (OSError, ValueError, KeyError):
        return []


def nearest_ward(lat, lng):
    """Ward whose centre is closest to the point (None if no ward table)."""
    wards = _load_wards()
    if not wards or lat is None or lng is None:
        return None
    return min(wards, key=lambda w: haversine_m(lat, lng, w["lat"], w["lng"]))


def officer_for(ward_name):
    for w in _load_wards():
        if w["name"] == ward_name:
            return w["officer"], w["email"]
    return "Municipal Officer", os.environ.get("DEFAULT_OFFICER_EMAIL", "awasthishailendra59@gmail.com")


def build_message(report, reasons=None, image_path=None):
    name, to_addr = officer_for(report.get("ward"))
    urgency = report.get("priority", "Medium")
    sender = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER") or "sudharsarthi.alerts@gmail.com"
    msg = EmailMessage()
    target_email = os.environ.get("ALERT_RECIPIENT_EMAIL", DEFAULT_ALERT_EMAIL)
    msg["From"] = sender
    msg["To"] = target_email
    if to_addr and to_addr.lower() != target_email.lower():
        msg["Cc"] = to_addr
    msg["Subject"] = (f"[{urgency.upper()} urgency] {report['id']} - {report['category']} "
                      f"at {report.get('location', '')[:60]}")
    map_link = (f"https://www.openstreetmap.org/?mlat={report['lat']}&mlon={report['lng']}"
                f"#map=18/{report['lat']}/{report['lng']}")
    lines = [
        f"Dear {name},", "",
        f"A new civic issue was reported in your ward ({report.get('ward')}).", "",
        f"Tracking ID : {report['id']}",
        f"Category    : {report['category']}",
        f"Urgency     : {urgency} (score {report.get('riskScore')}/100)",
        f"Location    : {report.get('location')} ({report.get('landmark') or 'no landmark'})",
        f"Map         : {map_link}",
        f"Description : {report.get('description')}",
        f"Est. cost   : Rs. {report.get('estimatedCost'):,}",
    ]
    if reasons:
        lines += ["", "Why this urgency:"] + [f"  - {r}" for r in reasons]
    if report.get("duplicateOf"):
        lines += ["", f"Note: likely duplicate of {report['duplicateOf']} (same spot, same issue)."]
    lines += ["", "Please inspect and update the status on the SUDHARSARTHI repair console.",
              "", "- SUDHARSARTHI (automated message)"]
    msg.set_content("\n".join(lines))

    if image_path and os.path.exists(image_path):
        ctype = mimetypes.guess_type(image_path)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        with open(image_path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                               filename=os.path.basename(image_path))
    return msg


def _deliver(msg, report_id):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD", "")

    # Auto-detect Gmail if SMTP_USER is provided and SMTP_HOST was omitted
    if not host and user and "gmail.com" in user.lower():
        host = "smtp.gmail.com"

    if not host:
        os.makedirs(OUTBOX, exist_ok=True)
        path = os.path.join(OUTBOX, f"{report_id}.txt")
        body = msg.get_body(preferencelist=("plain",)).get_content()
        with open(path, "w", encoding="utf-8") as f:
            cc_hdr = f"\nCc: {msg['Cc']}" if msg.get("Cc") else ""
            f.write(f"To: {msg['To']}{cc_hdr}\nSubject: {msg['Subject']}\n\n{body}")
        log.info("SMTP not configured - saved email to %s", path)
        return "saved-to-outbox"

    port = int(os.environ.get("SMTP_PORT", "587"))
    use_ssl = (port == 465) or (os.environ.get("SMTP_SSL", "0") == "1")

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=25) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=25) as smtp:
            if os.environ.get("SMTP_STARTTLS", "1") == "1":
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)

    log.info("Real email successfully dispatched to %s for report %s", msg["To"], report_id)
    return "emailed"


def notify_ward_officer(report, reasons=None, image_path=None, wait=False):
    """Email the ward officer. Returns the thread (or the result string if wait=True)."""
    msg = build_message(report, reasons, image_path)
    result = {}

    def run():
        try:
            result["status"] = _deliver(msg, report["id"])
        except Exception as exc:  # never let email problems break report creation
            result["status"] = f"failed: {type(exc).__name__}"
            log.warning("Email for %s failed: %s", report["id"], exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    if wait:
        t.join(timeout=30)
        return result.get("status", "pending")
    return t
