"""
End-to-end tests. Run:  python test_api.py
Uses a throw-away database/uploads/outbox, needs no API key and no network.
"""
import io
import json
import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ["SUDHARSARTHI_DB"] = os.path.join(_tmp, "test.db")
os.environ["UPLOAD_DIR"] = os.path.join(_tmp, "uploads")
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("SMTP_HOST", None)

import app as app_module          # noqa: E402
import notifier                   # noqa: E402
import urgency_ai                 # noqa: E402
from PIL import Image             # noqa: E402

notifier.OUTBOX = os.path.join(_tmp, "outbox")
app_module.app.config["NOTIFY_WAIT"] = True
app_module.app.config["TESTING"] = True


def fake_photo(color=(90, 90, 90)):
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(buf, "JPEG")
    buf.seek(0)
    return buf


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code, self._payload = status, payload

    def json(self):
        return {"content": [{"type": "text", "text": self._payload}]}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.c = app_module.app.test_client()

    # --- storage / health
    def test_01_health_and_seed(self):
        r = self.c.get("/api/health").get_json()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["reports"], 12)
        self.assertEqual(r["aiMode"], "rules-engine")

    def test_02_list_and_filter(self):
        high = self.c.get("/api/reports?priority=High").get_json()
        self.assertTrue(high and all(x["priority"] == "High" for x in high))
        one = self.c.get("/api/reports/ns-2026-1042").get_json()
        self.assertEqual(one["category"], "Pothole")
        self.assertEqual(self.c.get("/api/reports/NS-0000").status_code, 404)

    # --- creating reports + AI urgency
    def test_03_create_urgent_report_with_photo(self):
        r = self.c.post("/api/reports", data={
            "category": "Pothole", "lat": "28.7285", "lng": "77.4740",
            "location": "Master Plan Road near City Hospital, Raj Nagar",
            "description": "Deep pothole, about 14 cm deep, two-wheelers fell here last night, "
                           "no street light.",
            "image": (fake_photo(), "pothole.jpg")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 201, r.get_json())
        d = r.get_json()
        self.assertRegex(d["id"], r"^NS-\d{4}-1043$")
        self.assertEqual(d["priority"], "High")
        self.assertGreaterEqual(d["riskScore"], 60)
        self.assertEqual(d["ward"], "Zone 2 (Duhai RRTS Station)")  # auto-assigned from GPS
        self.assertEqual(d["aiClassification"]["source"], "rules-engine")
        self.assertFalse(d["aiClassification"]["isSimulated"])
        self.assertTrue(any("hospital" in x for x in d["urgencyReasons"]))
        self.assertEqual(d["notification"], "saved-to-outbox")
        self.assertTrue(os.path.exists(os.path.join(notifier.OUTBOX, f"{d['id']}.txt")))
        photo = self.c.get(d["imageUrl"].replace("http://localhost", ""))
        self.assertEqual(photo.status_code, 200)
        photo.close()
        b = d["scoreBreakdown"]
        self.assertEqual(d["riskScore"], sum(b.values()))            # explainable: parts add up

    def test_04_duplicate_detection(self):
        r = self.c.post("/api/reports", json={
            "category": "Pothole", "lat": 28.72853, "lng": 77.47403,   # ~4 m from #1043
            "description": "Pothole on main road"})
        d = r.get_json()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(d["duplicateOf"], "NS-2026-1043".replace("2026", d["id"].split("-")[1]))
        self.assertEqual(d["repeatCount"], 1)
        self.assertGreaterEqual(d["scoreBreakdown"]["repeatReports"], 7)

    def test_05_low_urgency_quiet_lane(self):
        d = self.c.post("/api/reports", json={
            "category": "Road Crack", "lat": 28.7400, "lng": 77.4850,
            "location": "Inside colony lane", "description": "Small hairline crack"}).get_json()
        self.assertEqual(d["priority"], "Low")

    def test_06_validation(self):
        self.assertEqual(self.c.post("/api/reports", json={"category": "Dragon"}).status_code, 400)
        no_gps = self.c.post("/api/reports", json={"category": "Pothole"})
        self.assertEqual(no_gps.status_code, 400)                    # no random coordinates
        self.assertIn("lat", no_gps.get_json()["error"])
        junk = self.c.post("/api/reports", data={
            "category": "Pothole", "lat": "28.7285", "lng": "77.4740",
            "image": (io.BytesIO(b"not an image"), "x.jpg")}, content_type="multipart/form-data")
        self.assertEqual(junk.status_code, 400)

    def test_06b_outside_duhai_rejected(self):
        r = self.c.post("/api/reports", json={"category": "Pothole", "lat": 28.6300, "lng": 77.4300})
        self.assertEqual(r.status_code, 400)
        self.assertIn("outside Duhai", r.get_json()["error"])
        cfg = self.c.get("/api/config").get_json()
        self.assertEqual(cfg["area"], "Duhai, Ghaziabad")

    # --- status updates
    def test_07_status_patch(self):
        r = self.c.patch("/api/reports/NS-2026-1042/status", json={
            "status": "Resolved", "verifiedByInspector": True, "contractor": "Apex Roadways Ltd"})
        d = r.get_json()
        self.assertEqual(d["status"], "Resolved")
        self.assertTrue(d["verifiedByInspector"])
        self.assertEqual(self.c.patch("/api/reports/NS-2026-1042/status",
                                      json={"status": "Nonsense"}).status_code, 400)
        self.assertEqual(self.c.patch("/api/reports/NS-9/status", json={"status": "Resolved"})
                         .status_code, 404)

    # --- classify endpoint
    def test_08_classify_endpoint(self):
        r = self.c.post("/api/ai/classify", data={
            "category": "Waterlogging", "description": "Road blocked, water 30 cm deep near school",
            "image": (fake_photo(), "w.jpg")}, content_type="multipart/form-data")
        d = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(d["severity"], 4)
        self.assertEqual(self.c.post("/api/ai/classify", data={}).status_code, 400)

    # --- vision stage with a mocked model reply (no network)
    def test_09_vision_stage_parsing(self):
        reply = json.dumps({"is_civic_issue": True, "category": "Pothole", "severity": 5,
                            "confidence": 93, "estimated_area_sqm": 0.9,
                            "traffic_exposure": "high", "hazards": ["exposed rebar"],
                            "reason": "Deep crater in the fast lane."})
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)
            return FakeResponse("```json\n" + reply + "\n```")

        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        try:
            res = urgency_ai.assess(fake_photo().read(), "Other", "pothole", "Main road", _post=fake_post)
        finally:
            os.environ.pop("ANTHROPIC_API_KEY")
        self.assertEqual(res["aiClassification"]["source"], "claude-vision+rules")
        self.assertEqual(res["category"], "Pothole")                 # AI corrected the category
        self.assertEqual(res["priority"], "High")
        self.assertEqual(calls[0]["messages"][0]["content"][0]["type"], "image")

    def test_10_vision_says_not_civic_issue_goes_to_review(self):
        reply = json.dumps({"is_civic_issue": False, "category": "Other", "severity": 4,
                            "confidence": 20, "traffic_exposure": "low", "hazards": [],
                            "reason": "The photo shows a cat."})
        res = urgency_ai.assess(fake_photo().read(), "Pothole", "pothole",
                                _post=lambda *a, **k: FakeResponse(reply))
        self.assertTrue(res["flagged"])
        self.assertLessEqual(res["severity"], 2)

    def test_11_vision_failure_falls_back_to_rules(self):
        res = urgency_ai.assess(fake_photo().read(), "Pothole", "deep pothole 12 cm",
                                _post=lambda *a, **k: FakeResponse("", status=500))
        self.assertEqual(res["aiClassification"]["source"], "rules-engine")
        self.assertIn("not analysed", res["aiClassification"]["note"])

    # --- analytics, budget, rescore
    def test_12_analytics(self):
        a = self.c.get("/api/analytics").get_json()
        self.assertEqual(a["totalReports"], len(self.c.get("/api/reports").get_json()))
        self.assertIn("Pothole", a["byCategory"])
        self.assertTrue(a["monthlyTrend"])

    def test_13_budget_knapsack(self):
        for strategy in ("priority", "coverage", "balanced"):
            r = self.c.post("/api/budget/optimize", json={"budget": 250000, "strategy": strategy})
            d = r.get_json()
            self.assertEqual(r.status_code, 200)
            self.assertLessEqual(d["allocatedCost"], 250000)
            self.assertEqual(d["allocatedCost"] + d["remaining"], 250000)
        cov = self.c.post("/api/budget/optimize", json={"budget": 250000, "strategy": "coverage"}).get_json()
        pri = self.c.post("/api/budget/optimize", json={"budget": 250000, "strategy": "priority"}).get_json()
        self.assertGreaterEqual(len(cov["selected"]), len(pri["selected"]))
        self.assertEqual(self.c.post("/api/budget/optimize", json={"budget": -5}).status_code, 400)

    def test_14_knapsack_is_optimal(self):
        items = [("a", 6000, 10), ("b", 5000, 7), ("c", 5000, 7)]   # greedy-by-value takes only "a"
        self.assertEqual(app_module.knapsack(items, 10000), {"b", "c"})

    def test_15_rescore_ages_reports(self):
        before = self.c.get("/api/reports/NS-2026-1043".replace("2026", self._year())).get_json()
        # pretend it has been open for 6 days
        import database as db
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        with db.connect() as conn:
            conn.execute("UPDATE reports SET date = ? WHERE id = ?", (old, before["id"]))
        out = self.c.post("/api/reports/rescore").get_json()
        after = self.c.get(f"/api/reports/{before['id']}").get_json()
        self.assertTrue(out["success"])
        self.assertEqual(after["scoreBreakdown"]["agingFactor"], 9)
        self.assertGreater(after["riskScore"], before["riskScore"] - 1)
        demo = self.c.get("/api/reports/NS-2026-1041").get_json()
        self.assertEqual(demo["riskScore"], 55)                      # demo scores untouched

    def test_16_demo_reset(self):
        r = self.c.post("/api/demo/reset").get_json()
        self.assertEqual(r["reports"], 12)
        self.assertEqual(self.c.get("/api/health").get_json()["reports"], 12)

    @staticmethod
    def _year():
        from datetime import datetime
        return str(datetime.now().year)


if __name__ == "__main__":
    unittest.main(verbosity=2)
