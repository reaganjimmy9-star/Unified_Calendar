from __future__ import annotations
import os, json, base64
from datetime import datetime, timedelta, time
from flask import Flask, jsonify, request, redirect, url_for, make_response, session
from tzlocal import get_localzone
from zoneinfo import ZoneInfo

# import your existing helpers
from unified_calendar import (
    fetch_google_all_calendars,
    fetch_canvas_ics,
    merge_and_dedupe,
)

# ──────────────────────────────────────────────────────────────────────────────
# Flask & static
app = Flask(__name__, static_folder="static", static_url_path="/static")

@app.get("/logout")
def logout():
    session.clear()
    resp = make_response(redirect(url_for("setup_form")))  # or url_for("index") if you prefer
    resp.delete_cookie("ucc_creds")
    resp.delete_cookie("ucc_user")
    return resp


# ──────────────────────────────────────────────────────────────────────────────
# Canary routes (safe to keep while iterating)
@app.get("/ping")
def ping():
    return "pong", 200

@app.get("/routes")
def routes():
    return jsonify(sorted([str(r.rule) for r in app.url_map.iter_rules()]))

def _mask(s: str | None, keep: int = 6) -> str:
    if not s:
        return "∅"
    s = str(s)
    return (s[:keep] + "…" + s[-keep:]) if len(s) > 16 else s

@app.get("/diag")
def diag():
    return jsonify({
        "has_client_id": bool(os.getenv("GOOGLE_CLIENT_ID")),
        "client_id_preview": _mask(os.getenv("GOOGLE_CLIENT_ID")),
        "has_client_secret": bool(os.getenv("GOOGLE_CLIENT_SECRET")),
        "client_secret_preview": _mask(os.getenv("GOOGLE_CLIENT_SECRET")),
        "oauth_redirect_uri": os.getenv("OAUTH_REDIRECT_URI") or "∅",
        "cookie_secret_set": bool(os.getenv("COOKIE_SECRET")),
    })

# ──────────────────────────────────────────────────────────────────────────────
# Signed-cookie helpers (demo)
COOKIE_SECRET = os.getenv("COOKIE_SECRET", "dev-secret-change-me")
app.secret_key = COOKIE_SECRET

def _get_cookie_json(req, name, default=None):
    try:
        raw = req.cookies.get(name)
        if not raw:
            return default
        data = base64.b64decode(raw.encode("utf-8")).decode("utf-8")
        return json.loads(data)
    except Exception:
        return default

def _set_cookie_json(resp, name, obj, max_age_days=180):
    raw = json.dumps(obj).encode("utf-8")
    b64 = base64.b64encode(raw).decode("utf-8")
    resp.set_cookie(name, b64, max_age=60*60*24*max_age_days, httponly=True, samesite="Lax", secure=True)

def _status_for_request(req):
    user = _get_cookie_json(req, "ucc_user") or {}
    canvas_ics = (user.get("canvas_ics") or "").strip()
    service = _google_service_from_cookie(req)
    return {
        "google_connected": bool(service),
        "canvas_ics_present": bool(canvas_ics),
        "ready": bool(service) and bool(canvas_ics),
    }

# ──────────────────────────────────────────────────────────────────────────────
# Health & index
@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/", strict_slashes=False)
def index():
    st = _status_for_request(request)
    if not st["ready"]:
        return redirect(url_for("setup_form"))
    return app.send_static_file("index.html")



# ──────────────────────────────────────────────────────────────────────────────
# Setup page (store Canvas ICS per user)
@app.get("/setup", strict_slashes=False)
def setup_form():
    return """
    <html>
    <body style="font-family:system-ui;max-width:720px;margin:40px auto;line-height:1.45">
      <h2 style="margin-bottom:6px">Unified Calendar – Setup</h2>
      <p style="margin-top:0;color:#666">Connect Google and paste your Canvas Calendar Feed (ICS) link.</p>

      <div style="display:flex;gap:16px;flex-wrap:wrap">
        <!-- Google card -->
        <div style="flex:1 1 320px;border:1px solid #eee;border-radius:10px;padding:14px">
          <h3 style="margin-top:0">Google</h3>
          <p id="gStatus" style="margin:4px 0;color:#666">Checking…</p>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <a href="/auth/google/start?next=/">
              <button style="padding:10px 14px;border:0;border-radius:10px;background:#1a73e8;color:#fff">Sign in with Google</button>
            </a>
            <form method="POST" action="/auth/signout">
              <button type="submit" style="padding:10px 14px;border:0;border-radius:10px;background:#eee;color:#111">Sign out</button>
            </form>
          </div>
        </div>

        <!-- Canvas card -->
        <div style="flex:1 1 320px;border:1px solid #eee;border-radius:10px;padding:14px">
          <h3 style="margin-top:0">Canvas ICS</h3>
          <p id="cStatus" style="margin:4px 0;color:#666">Checking…</p>
          <form method="POST" action="/setup" style="margin-bottom:8px">
            <input name="ics" style="width:100%;padding:10px;border:1px solid #ccc;border-radius:8px" placeholder="https://.../calendar.ics" />
            <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
              <button type="submit" style="padding:10px 14px;border:0;border-radius:10px;background:#1a73e8;color:#fff">Save</button>
              <button type="button" onclick="alert('Canvas → Calendar → right sidebar “Calendar Feed” → Enable/Copy the URL ending in calendar.ics')" style="padding:10px 14px;border:0;border-radius:10px;background:#eee;color:#111">Where do I find this?</button>
            </div>
          </form>
          <form method="POST" action="/setup/clear">
            <button type="submit" style="padding:10px 14px;border:0;border-radius:10px;background:#eee;color:#111">Clear saved ICS</button>
          </form>
        </div>
      </div>

      <script>
        async function updateStatus() {
          try {
            const s = await fetch('/me/status').then(r => r.json());
            const g = document.getElementById('gStatus');
            const c = document.getElementById('cStatus');

            g.textContent = s.google_connected ? "Google connected ✓" : "Not connected";
            g.style.color = s.google_connected ? "#0a7f3f" : "#b03a2e";

            c.textContent = s.canvas_ics_present ? "Canvas ICS saved ✓" : "Not saved";
            c.style.color = s.canvas_ics_present ? "#0a7f3f" : "#b03a2e";

            // Auto-redirect to calendar if both are ready
            if (s.ready) {
              // small delay so the user can see both checks turn green
              setTimeout(() => { window.location.href = "/"; }, 400);
            }
          } catch (e) {
            document.getElementById('gStatus').textContent = "Status error";
            document.getElementById('cStatus').textContent = "Status error";
          }
        }
        updateStatus();
      </script>
    </body>
    </html>
    """





@app.post("/setup")
def setup_save():
    ics = (request.form.get("ics") or "").strip()
    if not ics.startswith("http"):
        return "Invalid ICS URL", 400
    resp = make_response(redirect(url_for("index")))
    _set_cookie_json(resp, "ucc_user", {"canvas_ics": ics})
    return resp

# ──────────────────────────────────────────────────────────────────────────────
# Google Web OAuth flow
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

def _flow():
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
    )
    flow.redirect_uri = os.getenv("OAUTH_REDIRECT_URI", "")
    return flow

@app.get("/me/status")
def me_status():
    user = _get_cookie_json(request, "ucc_user") or {}
    canvas_ics = (user.get("canvas_ics") or "").strip()
    service = _google_service_from_cookie(request)
    return jsonify({
        "google_connected": bool(service),
        "canvas_ics_present": bool(canvas_ics),
        "ready": bool(service) and bool(canvas_ics),
    })

@app.get("/auth/google/start")
def auth_start():
    try:
        flow = _flow()
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        # carry next (defaults to "/")
        next_url = request.args.get("next") or "/"
        resp = make_response(redirect(auth_url))
        _set_cookie_json(resp, "ucc_state", {"state": state, "next": next_url}, max_age_days=1)
        return resp
    except Exception as e:
        import traceback; traceback.print_exc()
        return f"Auth start failed: {type(e).__name__}: {e}", 500

@app.get("/auth/google/callback")
def auth_callback():
    try:
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
        # figure out where to return
        state_cookie = _get_cookie_json(request, "ucc_state") or {}
        next_url = state_cookie.get("next") or "/"
        resp = make_response(redirect(next_url))
        _set_cookie_json(resp, "ucc_creds", data)
        return resp
    except Exception as e:
        import traceback; traceback.print_exc()
        return f"Auth callback failed: {type(e).__name__}: {e}", 500


def _google_service_from_cookie(req):
    data = _get_cookie_json(req, "ucc_creds")
    if not data:
        return None
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes") or SCOPES,
    )
    return build('calendar', 'v3', credentials=creds, cache_discovery=False)

# ──────────────────────────────────────────────────────────────────────────────
# Auth / sign-out helpers
@app.post("/auth/signout")
@app.get("/auth/signout")  # allow GET for convenience
def auth_signout():
    resp = make_response(redirect(url_for("setup_form")))
    # Clear both cookies (Google creds and Canvas ICS/user data)
    resp.delete_cookie("ucc_creds")
    resp.delete_cookie("ucc_user")
    return resp

@app.post("/setup/clear")
def setup_clear():
    # Clear only the Canvas ICS cookie (keep Google sign-in)
    resp = make_response(redirect(url_for("setup_form")))
    resp.delete_cookie("ucc_user")
    return resp

# ──────────────────────────────────────────────────────────────────────────────
# Helpers for ISO serialization with local offset
LOCAL_TZ = ZoneInfo("America/New_York")

def _iso_with_local_offset(dt: datetime) -> str:
    """Render with correct -04:00/-05:00 for that date (DST-aware)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)     # treat naive as local wall time
    return dt.astimezone(LOCAL_TZ).replace(microsecond=0).isoformat()

def _looks_all_day_like(start: datetime, end: datetime) -> bool:
    """Keep your existing logic but make name explicit."""
    return _is_all_day_like(start, end)   # uses your function above

def _as_local_wall(dt: datetime, tz=LOCAL_TZ) -> datetime:
    """
    Reinterpret the *wall clock* of dt as local time, ignoring any existing tzinfo.
    Example: '2025-10-29 00:30:00 Z' becomes '2025-10-29 00:30:00 -04:00' (no 5h shift).
    """
    if dt is None:
        return None
    return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, tzinfo=tz)



# ──────────────────────────────────────────────────────────────────────────────
# Helpers for all-day detection/serialization
def _is_midnight(dt: datetime) -> bool:
    return dt.timetz().hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0

def _is_whole_days(start: datetime, end: datetime) -> bool:
    secs = (end - start).total_seconds()
    # allow tiny rounding noise (+/- 0.5s)
    return abs(secs) % 86400 < 0.5

def _is_all_day_like(start: datetime, end: datetime) -> bool:
    # Many all-day events arrive as [midnight, midnight] with end exclusive.
    return _is_whole_days(start, end) and _is_midnight(start) and _is_midnight(end)

# ──────────────────────────────────────────────────────────────────────────────
# API used by FullCalendar (relaxed: works with Google-only or Canvas-only)
@app.get("/api/events")
def api_events():
    # Read per-user settings from cookie
    user = _get_cookie_json(request, "ucc_user") or {}
    canvas_ics = (user.get("canvas_ics") or "").strip()

    # Build Google service if present
    service = _google_service_from_cookie(request)

    # Optional query params
    source = (request.args.get("source") or "both").lower()  # google | canvas | both
    days_q = request.args.get("days")
    try:
        horizon_days = int(days_q) if days_q else int(os.getenv("HORIZON_DAYS", "14"))
        horizon_days = max(1, min(90, horizon_days))  # clamp a little
    except Exception:
        horizon_days = int(os.getenv("HORIZON_DAYS", "14"))

    # Time window (allow explicit after/before to override days)
    tz = get_localzone()
    now = datetime.now(tz)
    after_param = request.args.get("after")
    before_param = request.args.get("before")
    if after_param or before_param:
        try:
            after = datetime.fromisoformat(after_param) if after_param else now.replace(hour=0, minute=0, second=0, microsecond=0)
            if after.tzinfo is None:
                after = after.replace(tzinfo=tz)  # ✅ zoneinfo-stamp, not tz.localize
        except Exception:
            return jsonify({"error": "bad_after", "hint": "Use ISO 8601 e.g. 2025-10-26T00:00:00-04:00"}), 400
        try:
            before = datetime.fromisoformat(before_param) if before_param else after + timedelta(days=horizon_days)
            if before.tzinfo is None:
                before = before.replace(tzinfo=tz)  # ✅
        except Exception:
            return jsonify({"error": "bad_before", "hint": "Use ISO 8601 e.g. 2025-11-02T00:00:00-05:00"}), 400
    else:
        after = now.replace(hour=0, minute=0, second=0, microsecond=0)
        before = after + timedelta(days=horizon_days)

    # Determine which sources we are allowed/asked to use
    want_google = source in ("google", "both")
    want_canvas = source in ("canvas", "both")

    have_google = bool(service)
    have_canvas = bool(canvas_ics and canvas_ics.startswith("http"))

    if (want_google and not have_google) and (want_canvas and not have_canvas):
        # User asked for both (or a missing one) and neither is configured
        return jsonify({
            "error": "not_configured",
            "next": "/setup",
            "details": {
                "google_connected": have_google,
                "canvas_ics_present": have_canvas
            }
        }), 400

    events = []

    # Google
    if want_google and have_google:
        try:
            google_cal_ids = [s.strip() for s in os.getenv("GOOGLE_CAL_IDS", "").split(",") if s.strip()] or None
            events += fetch_google_all_calendars(service, after, before, allow_ids=google_cal_ids)
        except Exception as e:
            print("[server] Google fetch failed:", e)

    # Canvas
    if want_canvas and have_canvas:
        try:
            events += fetch_canvas_ics(canvas_ics, after, before)
        except Exception as e:
            print("[server] Canvas ICS fetch failed:", e)

    # If neither source is configured, tell the client to go to setup
    if not have_google and not have_canvas:
        return jsonify({
            "error": "not_configured",
            "next": "/setup",
            "details": {
                "requested": source,
                "google_connected": have_google,
                "canvas_ics_present": have_canvas
            }
        }), 400

    # If at least one source is configured, but we fetched zero events, return 200 with []
    if not events:
        resp = jsonify([])
        resp.headers["Cache-Control"] = "no-store"
        return resp


    merged = merge_and_dedupe(events)
    merged.sort(key=lambda e: (e.start, e.end, e.title))

    SOURCE_COLOR = {"google": "#1a73e8", "canvas-ics": "#d93025"}
    out = []

    for e in merged:
        start = e.start
        end = e.end
        is_all_day = _looks_all_day_like(start, end)

        base = {
            "title": e.title,
            "color": SOURCE_COLOR.get(e.source),
            "extendedProps": {
                "source": e.source,
                "location": e.location,
                "description": e.description,
            },
        }

        if is_all_day:
            base["start"] = start.date().isoformat()
            base["end"]   = end.date().isoformat()
            base["allDay"] = True
        else:
            is_canvas = (e.source == "canvas-ics")
            if is_canvas:
                # 🔧 Coerce Canvas times to *local wall time* (drop UTC/Z interpretation)
                start_local = _as_local_wall(start)
                end_local   = _as_local_wall(end)
                base["start"] = _iso_with_local_offset(start_local)
                base["end"]   = _iso_with_local_offset(end_local)
            else:
                # Google already good: keep as an instant and render with explicit local offset
                base["start"] = _iso_with_local_offset(start)
                base["end"]   = _iso_with_local_offset(end)

        out.append(base)

    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "5000")))
