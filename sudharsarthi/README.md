# SUDHARSARTHI

Civic issue reporting for **Duhai, Ghaziabad**. Citizens report a problem with a photo and a map location;
AI scores the urgency and the issue appears on the Duhai map as a coloured pin:
red = High (60+), amber = Medium (40-59), green = Low, grey = Resolved. Zone officers are notified by email.
Reports outside the Duhai boundary (defined in `backend/duhai.py`) are rejected.

```
sudharsarthi/
├── backend/    Flask API + SQLite (see backend/README.md)
├── frontend/   index.html (Duhai map + report form) + assets/ (logo, favicon)
├── Procfile    start command for hosting
└── render.yaml one-click Render config
```

## Run locally
```bash
cd backend
pip install -r requirements.txt
python app.py        # open http://localhost:5000  (serves ../frontend too)
```

## Put it live (Render, free tier)
1. Push this whole folder to GitHub (repo root = this folder).
2. Go to render.com -> New -> Web Service -> connect the repo.
3. Build command: `pip install -r backend/requirements.txt`
   Start command: `gunicorn --chdir backend app:app --bind 0.0.0.0:$PORT`
   (or choose "Blueprint" and Render reads render.yaml).
4. Add environment variables: `ANTHROPIC_API_KEY` (optional, enables photo AI),
   `ALLOW_DEMO_RESET=0`, and `SMTP_*` + `MAIL_FROM` for real officer emails.
5. Deploy. You get a link like https://sudharsarthi.onrender.com (frontend and API on one URL).
6. Open the Render link: the map page and the API share one address, so nothing else to configure.

Notes: the free tier sleeps when idle and its disk is wiped on redeploy, so reports and
uploaded photos reset. For real use, add a Render Postgres database/persistent disk
and an officer login. Replace the `@example.org` emails in `backend/wards.json`.

Before going live: rename the placeholder zones in `backend/wards.json` to the real Duhai wards and replace the example.org emails.
Demo reports in `backend/seed_reports.json` are sample data; remove them before launch.
