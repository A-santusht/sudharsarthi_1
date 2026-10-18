# SUDHARSARTHI Backend

Four small programs that give the SUDHARSARTHI front end a real backend:

| File | Job |
|---|---|
| `database.py` | **Storage.** SQLite database (`sudharsarthi.db`), auto-created and seeded with the 12 demo Ghaziabad reports. |
| `urgency_ai.py` | **AI urgency checker.** Claude vision looks at the photo; an open 40/25/20/15 formula turns it into a 0-100 score. Falls back to a text rules engine if there is no API key or network. |
| `notifier.py` | **Ward officer email.** Photo, map link, urgency and reasons. Real SMTP if configured, otherwise saved as text files in `outbox/`. |
| `app.py` | **Flask API** connecting everything, with the endpoints the front end expects. |

`test_api.py` has 16 tests (`python test_api.py`), no API key or network needed.

## Run it

```bash
pip install -r requirements.txt
python app.py                      # http://localhost:5000
```

Optional: serve your front end from the same port (no CORS, one command for the demo):

```bash
FRONTEND_DIR=../SUDHARSARTHI python app.py     # Windows PowerShell: $env:FRONTEND_DIR="..\SUDHARSARTHI"; python app.py
```

Settings live in `.env` (copy `.env.example`). Nothing is required; with no settings you get
rules-engine scoring and emails saved to `outbox/`.

- `ANTHROPIC_API_KEY` turns on the photo check (check `GET /api/health` -> `aiMode`).
- `SMTP_*` turns on real emails (a Gmail app password works).
- **Replace the placeholder emails in `wards.json`** (they are `@example.org`) before any live use.

## How a report is scored

1. **Photo check (needs API key).** The model says whether the photo shows a real civic issue, its
   category, severity 1-5, rough area, hazards and traffic exposure. If it is not a civic issue
   the report goes to *Under Review* instead of being trusted.
2. **Open formula (always).**
   `urgency = severity (max 40) + traffic exposure (max 25) + repeat reports (max 20) + ageing (max 15)`
   - repeat = other open reports of the same category within 25 m (x7 each)
   - ageing = 1.5 points per day open
   - High >= 60, Medium >= 40, else Low; severity 5 is always High
3. The parts are returned (`scoreBreakdown`, `urgencyReasons`), so every score can be explained to a judge.

New reports can score at most 65 because they have no repeats or age yet; `POST /api/reports/rescore`
re-ages open reports (run it daily, or from a button) so they climb over time.

## Endpoints

| Method | Route | Notes |
|---|---|---|
| GET | `/api/health` | status, report count, AI mode, email mode |
| GET | `/api/reports` | filters `?category=&priority=&status=&ward=&search=` |
| GET | `/api/reports/<id>` | |
| POST | `/api/reports` | multipart (`image` + fields) **or** JSON with `imageData` (data-URL). `category`, `lat`, `lng` required. Ward is auto-assigned from GPS. Returns the saved report plus `urgencyReasons`, `slaHours`, `notification` |
| PATCH | `/api/reports/<id>/status` | `status`, `contractor`, `inspectorName`, `verifiedByInspector`, `estimatedCost`, `afterImageUrl`, `priority` |
| POST | `/api/ai/classify` | photo/description in, urgency assessment out, nothing saved |
| GET | `/api/analytics` | totals, by category/status, 6-month trend, ward stats |
| POST | `/api/budget/optimize` | `{"budget": 500000, "strategy": "priority"|"coverage"|"balanced"}` - true 0/1 knapsack |
| POST | `/api/reports/rescore` | re-age open reports |
| POST | `/api/demo/reset` | reload demo data before a presentation |

Quick try:

```bash
curl -X POST localhost:5000/api/reports -F category=Pothole -F lat=28.6951 -F lng=77.4402 \
     -F "location=Meerut Road near Govt School" -F "description=Crater 12 cm deep, scooter fell" \
     -F image=@pothole.jpg
```

## Connecting the front end

In `js/api.js` set `USE_BACKEND_API = true` **and uncomment the `fetch` blocks** inside each method
(they are commented out in the repo, so flipping the flag alone does nothing). Point `API_BASE_URL`
at this server. For `createReport`, send the photo as `imageData` (the data-URL the report page
already creates for its preview) plus `lat`/`lng`, and use the returned object as the new report.

## Known limits (be upfront about these)

- The live Claude call has not been run against the real API in testing (no key/network here); the
  request building, reply parsing, error fallback and "not a civic issue" path are tested with mocked replies.
- No login: anyone who can reach the server can PATCH statuses or reset the demo. Fine for a hackathon
  demo; add an officer login (or an API token on PATCH/reset) before real use.
- SQLite + local `uploads/` suit one server. A real deployment would move to Postgres and object storage.
- Duplicate detection compares against the category the citizen chose, not the category the AI corrects it to.
- Cost estimates are rough per-category figures, not tender rates.
