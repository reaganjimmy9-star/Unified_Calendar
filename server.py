from __future__ import annotations
from flask import Flask, jsonify
from datetime import datetime, timedelta
import os
from tzlocal import get_localzone

# Import from your existing unified_calendar.py
from unified_calendar import (
    get_google_service,
    fetch_google_all_calendars,
    fetch_canvas_ics,
    merge_and_dedupe,
)

SOURCE_COLOR = {
    "google": "#1a73e8",
    "canvas-ics": "#d93025",
}

app = Flask(__name__, static_folder="static", static_url_path="/static")

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/")
def index():
    # most reliable way to serve /static/index.html
    return app.send_static_file("index.html")

@app.get("/api/events")
def api_events():
    tz = get_localzone()
    days = int(os.getenv("HORIZON_DAYS", "14"))
    now = datetime.now(tz)
    after = now.replace(hour=0, minute=0, second=0, microsecond=0)
    before = after + timedelta(days=days)

    events = []

    # Google
    try:
        google_cal_ids = [s.strip() for s in os.getenv("GOOGLE_CAL_IDS", "").split(",") if s.strip()] or None
        svc = get_google_service()
        events += fetch_google_all_calendars(svc, after, before, allow_ids=google_cal_ids)
    except Exception as e:
        print("[server] Google fetch failed:", e)

    # Canvas (ICS)
    try:
        ics = os.getenv("CANVAS_ICS")
        if ics:
            events += fetch_canvas_ics(ics, after, before)
        else:
            print("[server] CANVAS_ICS not set; serving Google only")
    except Exception as e:
        print("[server] Canvas ICS fetch failed:", e)

    merged = merge_and_dedupe(events)
    merged.sort(key=lambda e: (e.start, e.end, e.title))

    out = []
    for e in merged:
        out.append({
            "title": e.title,
            "start": e.start.isoformat(),
            "end": e.end.isoformat(),
            "color": SOURCE_COLOR.get(e.source),
            "extendedProps": {
                "source": e.source,
                "location": e.location,
                "description": e.description,
            }
        })
    return jsonify(out)

if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    print(f"Serving on http://{host}:{port}")
    app.run(host=host, port=port)