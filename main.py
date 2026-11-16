"""AgriPulse AI backend (FastAPI).

Endpoints used by index.html:
  GET  /api/v1/health
  POST /api/v1/diagnose-crop   (multipart: image, audio, text_query, lat, lon, district, save)
  GET  /api/v1/outbreaks
  POST /api/v1/ussd            (form: text)

Also serves the frontend (index.html, manifest.webmanifest, icon.svg, sw.js).
Without GEMINI_API_KEY it runs in demo mode (keyword matching on kb.json).
"""
import base64
import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

BASE = Path(__file__).parent
ROOT = BASE.parent
KB = json.loads((BASE / "kb.json").read_text(encoding="utf8"))
KB_BY_ID = {d["id"]: d for d in KB}

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DB_PATH = os.getenv("AGRIPULSE_DB", str(BASE / "agripulse.db"))

DAY = 86400
RADIUS_KM, MIN_FARMS, WINDOW = 5, 4, 14 * DAY

app = FastAPI(title="AgriPulse AI")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------- database
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS reports(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                farm_id TEXT, disease_id TEXT, crop TEXT,
                lat REAL, lon REAL, district TEXT, severity TEXT, ts INTEGER)"""
        )


init_db()

# Sample data so the map is not empty on a fresh install (flagged sample=1)
SEED = [
    ("s1", "rice_blast", "Rice", 24.85, 89.37, "Bogura", "high"),
    ("s2", "rice_blast", "Rice", 24.87, 89.40, "Bogura", "high"),
    ("s3", "rice_blast", "Rice", 24.83, 89.35, "Bogura", "high"),
    ("s4", "rice_blast", "Rice", 24.86, 89.34, "Bogura", "high"),
    ("s5", "tomato_late_blight", "Tomato", 25.75, 89.25, "Rangpur", "high"),
    ("s6", "tomato_late_blight", "Tomato", 25.78, 89.27, "Rangpur", "high"),
    ("s7", "potato_late_blight", "Potato", 25.63, 88.64, "Dinajpur", "high"),
    ("s8", "brinjal_shoot_borer", "Brinjal", 23.46, 91.18, "Cumilla", "medium"),
    ("s9", "rice_brown_spot", "Rice", 22.82, 89.55, "Khulna", "medium"),
    ("s10", "maize_rust", "Maize", 24.37, 88.60, "Rajshahi", "medium"),
    ("s11", "rice_bacterial_blight", "Rice", 24.90, 91.87, "Sylhet", "high"),
]


def seed_reports():
    now = int(time.time())
    return [
        dict(farm_id=r[0], disease_id=r[1], crop=r[2], lat=r[3], lon=r[4], district=r[5],
             severity=r[6], ts=now - i * 9 * 3600, sample=1)
        for i, r in enumerate(SEED)
    ]


def real_reports(since: int):
    with db() as con:
        rows = con.execute("SELECT * FROM reports WHERE ts >= ? ORDER BY ts DESC", (since,)).fetchall()
    return [dict(farm_id=r["farm_id"], disease_id=r["disease_id"], crop=r["crop"], lat=r["lat"],
                 lon=r["lon"], district=r["district"], severity=r["severity"], ts=r["ts"], sample=0)
            for r in rows]


def all_reports():
    return seed_reports() + real_reports(int(time.time()) - WINDOW)


# --------------------------------------------------------------- outbreak logic
def hav(a, b, c, d):
    r, p = 6371, math.pi / 180
    x = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def compute_alerts(reports):
    now = time.time()
    recent = [r for r in reports if now - r["ts"] <= WINDOW]
    alerts = []
    for r in recent:
        near = {o["farm_id"] for o in recent
                if o["disease_id"] == r["disease_id"] and hav(r["lat"], r["lon"], o["lat"], o["lon"]) <= RADIUS_KM}
        if len(near) < MIN_FARMS:
            continue
        if any(a["disease_id"] == r["disease_id"] and hav(a["lat"], a["lon"], r["lat"], r["lon"]) <= RADIUS_KM
               for a in alerts):
            continue
        alerts.append(dict(disease_id=r["disease_id"], lat=r["lat"], lon=r["lon"], farms=len(near),
                           ts=r["ts"], name=KB_BY_ID[r["disease_id"]]["name"]))
    return alerts


# --------------------------------------------------------------- diagnosis
def tokens(s: str):
    return set(re.findall(r"[\w\u0980-\u09FF]+", s.lower()))


def retrieve(q: str):
    q = q.lower()
    qt = tokens(q)
    out = []
    for d in KB:
        if d["id"] == "healthy":
            continue
        score = 0.0
        for kw in d["keywords"]:
            k = kw.lower()
            if k in q:
                score += 2 if (" " in k or len(k) > 4) else 1
            elif tokens(k) & qt:
                score += 0.5
        if score:
            out.append((score, d))
    return sorted(out, key=lambda x: -x[0])[:3]


def build(disease_id: str, conf: float):
    d = KB_BY_ID[disease_id]
    return dict(disease_id=disease_id, crop=d["crop"], name=d["name"], severity=d["severity"],
                confidence=conf, symptoms=d["symptoms"], organic=d["organic"], chemical=d["chemical"])


def demo_diagnose(text: str, image_bytes: Optional[bytes]):
    hits = retrieve(text)
    if hits:
        return build(hits[0][1]["id"], min(0.55 + hits[0][0] * 0.05, 0.8))
    if image_bytes:
        pool = [d for d in KB if d["id"] != "healthy"]
        return build(pool[len(image_bytes) % len(pool)]["id"], 0.5)
    return build("healthy", 0.3)


async def gemini_diagnose(text, image, image_mime, audio, audio_mime):
    """Returns (disease_id, confidence, transcript). Raises on failure."""
    catalog = "\n".join(f'- {d["id"]}: {d["crop"]["en"]}, {d["name"]["en"]}; {d["symptoms"]["en"]}' for d in KB)
    prompt = (
        "You are a crop-disease assistant for Bangladeshi farmers. Pick the single best match from this "
        "list (use 'healthy' if the plant looks fine or the input is unrelated):\n"
        f"{catalog}\n\nThe farmer's description (may be Bangla or English): {text or '(none)'}\n"
        "If audio is attached, transcribe it first and use it as the description.\n"
        'Reply with JSON only: {"disease_id": string, "confidence": number 0-1, "transcript": string or null}'
    )
    parts = [{"text": prompt}]
    if image:
        parts.append({"inline_data": {"mime_type": image_mime or "image/jpeg",
                                      "data": base64.b64encode(image).decode()}})
    if audio:
        parts.append({"inline_data": {"mime_type": audio_mime or "audio/webm",
                                      "data": base64.b64encode(audio).decode()}})
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(url, headers={"x-goog-api-key": GEMINI_KEY},
                         json={"contents": [{"parts": parts}],
                               "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}})
    r.raise_for_status()
    raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    j = json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip())
    did = j.get("disease_id")
    if did not in KB_BY_ID:
        did = "healthy"
    conf = max(0.0, min(1.0, float(j.get("confidence", 0.5))))
    return did, conf, j.get("transcript")


# --------------------------------------------------------------- routes
@app.get("/api/v1/health")
def health():
    return {"ok": True, "ai": "gemini" if GEMINI_KEY else "demo", "voice": bool(GEMINI_KEY)}


@app.post("/api/v1/diagnose-crop")
async def diagnose_crop(
    image: Optional[UploadFile] = File(None),
    audio: Optional[UploadFile] = File(None),
    text_query: str = Form(""),
    lat: Optional[float] = Form(None),
    lon: Optional[float] = Form(None),
    district: Optional[str] = Form(None),
    save: bool = Form(False),
):
    text = text_query.strip()
    img = await image.read() if image else None
    aud = await audio.read() if audio else None
    if not (text or img or aud):
        return JSONResponse({"detail": "Provide text, an image or audio."}, status_code=400)

    warnings, transcript, mode = [], None, "demo"
    diagnosis = None
    if GEMINI_KEY:
        try:
            did, conf, transcript = await gemini_diagnose(
                text, img, image.content_type if image else None, aud, audio.content_type if audio else None)
            diagnosis, mode = build(did, conf), "gemini"
        except Exception as ex:  # fall back so the farmer still gets an answer
            warnings.append(f"AI unavailable, used offline matching ({type(ex).__name__})")
    if diagnosis is None:
        diagnosis = demo_diagnose(text, img)

    outbreak = None
    if save and diagnosis["disease_id"] != "healthy" and lat is not None and lon is not None:
        now = int(time.time())
        farm_id = f"u{now}{int(lat * 1e4) % 1000}"
        with db() as con:
            con.execute(
                "INSERT INTO reports(farm_id,disease_id,crop,lat,lon,district,severity,ts) VALUES(?,?,?,?,?,?,?,?)",
                (farm_id, diagnosis["disease_id"], diagnosis["crop"]["en"], lat, lon, district or "",
                 diagnosis["severity"], now))
        near = {o["farm_id"] for o in all_reports()
                if o["disease_id"] == diagnosis["disease_id"] and hav(lat, lon, o["lat"], o["lon"]) <= RADIUS_KM}
        if len(near) >= MIN_FARMS:
            outbreak = {"farms": len(near)}

    return {"mode": mode, "transcript": transcript, "diagnosis": diagnosis, "outbreak": outbreak,
            "warnings": warnings}


@app.get("/api/v1/outbreaks")
def outbreaks():
    reports = all_reports()
    return {"reports": reports, "alerts": compute_alerts(reports)}


USSD_CROPS = [("ধান", "Rice"), ("টমেটো", "Tomato"), ("আলু", "Potato"), ("ভুট্টা", "Maize"),
              ("বেগুন", "Brinjal (eggplant)")]


@app.post("/api/v1/ussd", response_class=PlainTextResponse)
def ussd(text: str = Form("")):
    p = [x for x in text.split("*") if x]
    if not p:
        return "CON AgriPulse\n1. রোগ জানুন\n2. এলাকার সতর্কতা\n3. কৃষি অফিসার"
    if p[0] == "2":
        n = len(compute_alerts(all_reports()))
        return f"END গত ৭ দিনে {n}টি মহামারী সতর্কতা আছে। বিস্তারিত ওয়েবসাইটে দেখুন।"
    if p[0] == "3":
        return "END আপনার উপজেলা কৃষি অফিসে যোগাযোগ করুন। কৃষক কল সেন্টার: ১৬১২৩"
    if p[0] != "1":
        return "END ভুল অপশন।"
    if len(p) == 1:
        return "CON ফসল বাছাই করুন\n" + "\n".join(f"{i + 1}. {c[0]}" for i, c in enumerate(USSD_CROPS))
    if not p[1].isdigit() or not 1 <= int(p[1]) <= len(USSD_CROPS):
        return "END ভুল অপশন।"
    crop = USSD_CROPS[int(p[1]) - 1]
    ds = [d for d in KB if d["crop"]["en"] == crop[1]]
    if len(p) == 2:
        return "CON সমস্যা বাছাই করুন\n" + "\n".join(f"{i + 1}. {d['name']['bn']}" for i, d in enumerate(ds))
    if not p[2].isdigit() or not 1 <= int(p[2]) <= len(ds):
        return "END ভুল অপশন।"
    d = ds[int(p[2]) - 1]
    return (f"END {d['name']['bn']}\nজৈব: {d['organic']['bn'].split('।')[0]}।\n"
            "রাসায়নিকের আগে কৃষি অফিসারের পরামর্শ নিন।")


# --------------------------------------------------------------- frontend
@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")


@app.get("/{name}")
def static_file(name: str):
    if name in {"manifest.webmanifest", "icon.svg", "sw.js", "index.html"}:
        media = {"sw.js": "application/javascript", "icon.svg": "image/svg+xml",
                 "manifest.webmanifest": "application/manifest+json"}.get(name)
        return FileResponse(ROOT / name, media_type=media, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"detail": "Not found"}, status_code=404)
