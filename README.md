# AgriPulse AI

```
agripulse/
├── index.html              # frontend (your file)
├── manifest.webmanifest    # PWA manifest
├── icon.svg                # app icon
├── sw.js                   # service worker (offline shell)
└── backend/
    ├── main.py             # FastAPI server
    ├── kb.json             # disease knowledge base (same as in index.html)
    ├── requirements.txt
    └── .env.example
```

## Run

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 . The badge in the app switches from demo to API mode on its own.

## Gemini (optional)

```bash
export GEMINI_API_KEY=your_key
export GEMINI_MODEL=gemini-2.5-flash   # change if Google renames models
```

Without a key, the server uses offline keyword matching (same as the HTML demo).
If Gemini fails, it falls back to keyword matching and returns a warning.

Note: browsers record voice as webm, which Gemini may not accept. If voice fails,
convert audio to wav/ogg/mp3 in the frontend or with ffmpeg on the server.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | /api/v1/health | `{ok, ai, voice}` |
| POST | /api/v1/diagnose-crop | image / audio / text_query, lat, lon, district, save |
| GET | /api/v1/outbreaks | reports + alerts (4+ farms, 5 km, 14 days) |
| POST | /api/v1/ussd | USSD menu, form field `text` |

Reports are stored in SQLite (`agripulse.db`). Map sample data is flagged `sample: 1`.

## Deploy

Any host that runs Python works (Render, Railway, a VPS). Service workers need HTTPS
(localhost is fine). If the frontend is hosted separately, paste the API URL in the
app's settings box; CORS is already open.
