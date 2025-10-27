from __future__ import annotations
import os, json, base64
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, redirect, url_for, make_response
from tzlocal import get_localzone

# import your existing helpers
from unified_calendar import (
    fetch_google_all_calendars,
    fetch_canvas_ics,
    merge_and_dedupe,
)

# Flask & static
app = Flask(__name__, static_folder="static", static_url_path="/static")

# ----- simple signed-cookie helpers (demo) -----
COOKIE_SECRET = os.getenv("COOKIE_SECRET", "dev-secret-change-me")
app.secret_key = COOKIE_SECRET

def _get_cookie_json(req, name, default=None):
    try:
        raw = req.cookies.get(name)
        if not raw: return default
        data = base64.b64decode(raw.encode("utf-8")).decode("utf-8")
        return json.loads(data)
    except Exception:
        return default

def _set_cookie_json(resp, name, obj, max_age_days=180):
    raw = json.dumps(obj).encode("utf-8")
    b64 = base64.b64encode(raw).decode("utf-8")
    resp.set_cookie(name, b64, max_age=60*60*24*max_age_days, httponly=True, samesite="Lax")

# ----- health & index -----
@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/")
def index():
    return app.send_static_file("index.html")

# ----- setup page: store Canvas ICS per user -----
@app.get("/setup")
def setup_form():
    return """
    <html><body style="font-family:system-ui;max-width:640px;margin:40px auto">
      <h2>Unified Calendar – Setup</h2>
      <ol>
        <li>Paste your Canvas <b>Calendar Feed (.ics)</b> URL.</li>
        <li><a href="/auth/google/start">Sign in with Google</a> (read-only).</li>
      </ol>
      <form method="POST" action="/setup">
        <label>Canvas ICS URL</label><br/>
        <input name="ics" style="width:100%;padding:8px" placeholder="https://.../calendar.ics"/><br/><br/>
        <button type="submit" style="padding:8px 14px">Save</button>
      </form>
      <p style="margin-top:12px">Then return to <a href="/">the calendar</a>.</p>
    </body></html>
    """

@app.post("/setup")
def setup_save():
    ics = (request.form.get("ics") or "").strip()
    if not ics.startswith("http"):
        return "Invalid ICS URL", 400
    resp = make_response(redirect(url_for("index")))
    _set_cookie_json(resp, "ucc_user", {"canvas_ics": ics})
    return resp

# ----- Google Web OAuth flow -----
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "")  # https://YOUR-APP.onrender.com/auth/google/callback

def _flow():
    return Flow(
        client_config={
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [OAUTH_REDIRECT_URI],
            }
        },
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI
    )

@app.get("/auth/google/start")
def auth_start():
    flow = _flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )
    resp = make_response(redirect(auth_url))
    _set_cookie_json(resp, "ucc_state", {"state": state}, max_age_days=1)
    return resp

@app.get("/auth/google/callback")
def auth_callback():
    flow = _flow()
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or []),
    }
    resp = make_response(redirect(url_for("index")))
    _set_cookie_json(resp, "ucc_creds", data)
    return resp

def _google_service_from_cookie(req):
    data = _get_cookie_json(req, "ucc_creds")
    if not data: return None
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes") or SCOPES,
    )
    return build('calendar', 'v3', credentials=creds, cache_discovery=False)

# ----- API used by FullCalendar -----
@app.get("/api/events")
def api_events():
    user = _get_cookie_json(request, "ucc_user") or {}
    ics = user.get("canvas_ics")
    service = _google_service_from_cookie(request)

    # If not configured, instruct the UI
    if not ics or not service:
        return jsonify({"error": "not_configured", "next": "/setup"}), 400

    tz = get_localzone()
    days = int(os.getenv("HORIZON_DAYS", "14"))
    now = datetime.now(tz)
    after = now.replace(hour=0, minute=0, second=0, microsecond=0)
    before = after + timedelta(days=days)

    events = []
    # Google
    try:
        google_cal_ids = [s.strip() for s in os.getenv("GOOGLE_CAL_IDS", "").split(",") if s.strip()] or None
        events += fetch_google_all_calendars(service, after, before, allow_ids=google_cal_ids)
    except Exception as e:
        print("[server] Google fetch failed:", e)

    # Canvas
    try:
        events += fetch_canvas_ics(ics, after, before)
    except Exception as e:
        print("[server] Canvas ICS fetch failed:", e)

    merged = merge_and_dedupe(events)
    merged.sort(key=lambda e: (e.start, e.end, e.title))

    SOURCE_COLOR = {"google": "#1a73e8", "canvas-ics": "#d93025"}
    out = [{
        "title": e.title,
        "start": e.start.isoformat(),
        "end": e.end.isoformat(),
        "color": SOURCE_COLOR.get(e.source),
        "extendedProps": {"source": e.source, "location": e.location, "description": e.description},
    } for e in merged]
    return jsonify(out)

if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "5000")))
