#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unified Calendar Web Server — Enhanced Edition

Features:
 - Google OAuth flow with secure cookie storage
 - Per-user Canvas ICS configuration
 - Enhanced event API with filtering and caching
 - Conflict detection endpoint
 - Rate limiting and security headers
 - Comprehensive logging
 - Health checks and diagnostics

Environment Variables:
 - GOOGLE_CLIENT_ID: OAuth client ID
 - GOOGLE_CLIENT_SECRET: OAuth client secret
 - OAUTH_REDIRECT_URI: OAuth callback URL
 - COOKIE_SECRET: Secret for cookie signing (required in production)
 - HOST: Server host (default: 0.0.0.0)
 - PORT: Server port (default: 5000)
 - DEBUG: Enable debug mode (default: false)
 - RATE_LIMIT_ENABLED: Enable rate limiting (default: true)
"""

from __future__ import annotations
import base64
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, redirect, url_for, make_response

# Import from unified_calendar module
try:
    from unified_calendar import (
        fetch_google_all_calendars,
        fetch_canvas_ics,
        merge_and_dedupe,
        find_conflicts,
        Event,
        __version__
    )
except ImportError:
    print("ERROR: Could not import unified_calendar module", file=sys.stderr)
    print("Make sure unified_calendar.py is in the same directory", file=sys.stderr)
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

class ServerConfig:
    """Server configuration from environment"""
    
    def __init__(self):
        self.host = os.getenv("HOST", "0.0.0.0")
        self.port = int(os.getenv("PORT", "5000"))
        self.debug = os.getenv("DEBUG", "false").lower() == "true"
        self.cookie_secret = os.getenv("COOKIE_SECRET", "dev-secret-change-me")
        
        if self.cookie_secret == "dev-secret-change-me" and not self.debug:
            print("WARNING: Using default COOKIE_SECRET in production!", file=sys.stderr)
        
        self.google_client_id = os.getenv("GOOGLE_CLIENT_ID", "")
        self.google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
        self.oauth_redirect_uri = os.getenv("OAUTH_REDIRECT_URI", "")
        self.rate_limit_enabled = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
        self.cache_ttl = int(os.getenv("CACHE_TTL", "300"))

config = ServerConfig()

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG if config.debug else logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)
logger.info(f"Unified Calendar Server v{__version__}")

if config.debug:
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

# ──────────────────────────────────────────────────────────────────────────────
# Flask App
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = config.cookie_secret

# ──────────────────────────────────────────────────────────────────────────────
# Rate Limiting
# ──────────────────────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, requests_per_minute: int = 60):
        self.requests_per_minute = requests_per_minute
        self.requests: Dict[str, list] = defaultdict(list)
    
    def is_allowed(self, key: str) -> bool:
        if not config.rate_limit_enabled:
            return True
        
        now = time.time()
        minute_ago = now - 60
        self.requests[key] = [t for t in self.requests[key] if t > minute_ago]
        
        if len(self.requests[key]) >= self.requests_per_minute:
            return False
        
        self.requests[key].append(now)
        return True
    
    def cleanup(self):
        now = time.time()
        minute_ago = now - 60
        for key in list(self.requests.keys()):
            self.requests[key] = [t for t in self.requests[key] if t > minute_ago]
            if not self.requests[key]:
                del self.requests[key]

rate_limiter = RateLimiter()

def rate_limit(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        key = request.remote_addr or 'unknown'
        if not rate_limiter.is_allowed(key):
            logger.warning(f"Rate limit exceeded for {key}")
            return jsonify({"error": "rate_limit_exceeded"}), 429
        return func(*args, **kwargs)
    return wrapper

# ──────────────────────────────────────────────────────────────────────────────
# Security Headers
# ──────────────────────────────────────────────────────────────────────────────

@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    if not config.debug and request.is_secure:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

# ──────────────────────────────────────────────────────────────────────────────
# Cookie Utilities
# ──────────────────────────────────────────────────────────────────────────────

def _get_cookie_json(req, name: str, default=None):
    try:
        raw = req.cookies.get(name)
        if not raw:
            return default
        data = base64.b64decode(raw.encode("utf-8")).decode("utf-8")
        return json.loads(data)
    except Exception as e:
        logger.warning(f"Failed to decode cookie {name}: {e}")
        return default

def _set_cookie_json(resp, name: str, obj, max_age_days: int = 180):
    try:
        raw = json.dumps(obj).encode("utf-8")
        b64 = base64.b64encode(raw).decode("utf-8")
        resp.set_cookie(name, b64, max_age=60*60*24*max_age_days, httponly=True, 
                       samesite="Lax", secure=not config.debug)
    except Exception as e:
        logger.error(f"Failed to set cookie {name}: {e}")

def _mask(s: str | None, keep: int = 6) -> str:
    if not s:
        return "∅"
    s = str(s)
    return (s[:keep] + "…" + s[-keep:]) if len(s) > 16 else s

# ──────────────────────────────────────────────────────────────────────────────
# Health & Diagnostics
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz():
    return jsonify({"status": "healthy", "version": __version__}), 200

@app.get("/ping")
def ping():
    return "pong", 200

@app.get("/diag")
@rate_limit
def diag():
    return jsonify({
        "version": __version__,
        "has_client_id": bool(config.google_client_id),
        "client_id_preview": _mask(config.google_client_id),
        "has_client_secret": bool(config.google_client_secret),
        "oauth_redirect_uri": config.oauth_redirect_uri or "∅",
        "rate_limit_enabled": config.rate_limit_enabled,
        "debug_mode": config.debug
    })

@app.get("/routes")
def routes():
    return jsonify(sorted([str(r.rule) for r in app.url_map.iter_rules()]))

# ──────────────────────────────────────────────────────────────────────────────
# Google Service Helper
# ──────────────────────────────────────────────────────────────────────────────

def _google_service_from_cookie(req):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    
    data = _get_cookie_json(req, "ucc_creds")
    if not data:
        return None
    
    try:
        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes") or ['https://www.googleapis.com/auth/calendar.readonly']
        )
        return build('calendar', 'v3', credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.error(f"Failed to build Google service: {e}")
        return None

def _status_for_request(req) -> dict:
    user = _get_cookie_json(req, "ucc_user") or {}
    canvas_ics = (user.get("canvas_ics") or "").strip()
    service = _google_service_from_cookie(req)
    return {
        "google_connected": bool(service),
        "canvas_ics_present": bool(canvas_ics),
        "ready": bool(service) and bool(canvas_ics),
    }

# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/", strict_slashes=False)
def index():
    st = _status_for_request(request)
    if not st["ready"]:
        return redirect(url_for("setup_form"))
    return app.send_static_file("index.html")

@app.get("/me/status")
@rate_limit
def me_status():
    return jsonify(_status_for_request(request))

@app.get("/setup", strict_slashes=False)
def setup_form():
    return """
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Setup – Unified Calendar</title>
      <style>
        body{font-family:system-ui,sans-serif;max-width:800px;margin:40px auto;padding:0 20px;line-height:1.6}
        h2{margin-bottom:8px;color:#1a73e8}.subtitle{color:#666;margin-top:0}
        .cards{display:flex;gap:20px;flex-wrap:wrap;margin:30px 0}
        .card{flex:1;min-width:300px;border:1px solid #e0e0e0;border-radius:12px;padding:20px;background:#fafafa}
        .card h3{margin-top:0;color:#333}
        .status{margin:10px 0;padding:10px;border-radius:8px;font-weight:500}
        .status.checking{background:#fff3cd;color:#856404}
        .status.connected{background:#d4edda;color:#155724}
        .status.disconnected{background:#f8d7da;color:#721c24}
        button,.button{padding:10px 20px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
        .primary{background:#1a73e8;color:#fff}.secondary{background:#e0e0e0;color:#333}
        button:hover,.button:hover{opacity:.9}
        input[type="text"]{width:100%;padding:10px;border:1px solid #ccc;border-radius:8px;font-size:14px;margin:10px 0}
        .help-text{font-size:13px;color:#666;margin-top:5px}
      </style>
    </head>
    <body>
      <h2>Unified Calendar – Setup</h2>
      <p class="subtitle">Connect Google Calendar and Canvas to get started.</p>
      <div class="cards">
        <div class="card">
          <h3>📅 Google Calendar</h3>
          <div id="gStatus" class="status checking">Checking...</div>
          <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:15px">
            <a href="/auth/google/start?next=/" class="button primary">Sign in</a>
            <form method="POST" action="/auth/signout" style="display:inline">
              <button type="submit" class="secondary">Sign out</button>
            </form>
          </div>
        </div>
        <div class="card">
          <h3>📚 Canvas LMS</h3>
          <div id="cStatus" class="status checking">Checking...</div>
          <form method="POST" action="/setup">
            <input name="ics" type="text" placeholder="https://canvas.edu/.../calendar.ics"/>
            <p class="help-text">Canvas → Calendar → Calendar Feed</p>
            <div style="display:flex;gap:10px;flex-wrap:wrap">
              <button type="submit" class="primary">Save</button>
              <button type="button" onclick="alert('Go to Canvas → Calendar → Look for Calendar Feed in right sidebar → Copy URL')" class="secondary">Help</button>
            </div>
          </form>
          <form method="POST" action="/setup/clear" style="margin-top:10px">
            <button type="submit" class="secondary">Clear</button>
          </form>
        </div>
      </div>
      <script>
        async function updateStatus(){
          try{
            const s=await fetch('/me/status').then(r=>r.json());
            const g=document.getElementById('gStatus'),c=document.getElementById('cStatus');
            g.textContent=s.google_connected?'✅ Connected':'⚠️ Not connected';
            g.className='status '+(s.google_connected?'connected':'disconnected');
            c.textContent=s.canvas_ics_present?'✅ Configured':'⚠️ Not configured';
            c.className='status '+(s.canvas_ics_present?'connected':'disconnected');
            if(s.ready)setTimeout(()=>{window.location.href='/'},500);
          }catch(e){console.error(e)}
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
        return "Invalid URL", 400
    resp = make_response(redirect(url_for("index")))
    _set_cookie_json(resp, "ucc_user", {"canvas_ics": ics})
    return resp

@app.post("/setup/clear")
def setup_clear():
    resp = make_response(redirect(url_for("setup_form")))
    resp.delete_cookie("ucc_user")
    return resp

# ──────────────────────────────────────────────────────────────────────────────
# OAuth
# ──────────────────────────────────────────────────────────────────────────────

from google_auth_oauthlib.flow import Flow

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

def _flow():
    return Flow.from_client_config({
        "web": {
            "client_id": config.google_client_id,
            "client_secret": config.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }, scopes=SCOPES, redirect_uri=config.oauth_redirect_uri)

@app.get("/auth/google/start")
def auth_start():
    try:
        flow = _flow()
        auth_url, state = flow.authorization_url(access_type="offline", 
                                                  include_granted_scopes="true", 
                                                  prompt="consent")
        next_url = request.args.get("next") or "/"
        resp = make_response(redirect(auth_url))
        _set_cookie_json(resp, "ucc_state", {"state": state, "next": next_url}, max_age_days=1)
        return resp
    except Exception as e:
        logger.error(f"OAuth start failed: {e}", exc_info=True)
        return f"Auth failed: {e}", 500

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
        
        state_cookie = _get_cookie_json(request, "ucc_state") or {}
        next_url = state_cookie.get("next") or "/"
        resp = make_response(redirect(next_url))
        _set_cookie_json(resp, "ucc_creds", data)
        return resp
    except Exception as e:
        logger.error(f"OAuth callback failed: {e}", exc_info=True)
        return f"Callback failed: {e}", 500

@app.route("/auth/signout", methods=["GET", "POST"])
def auth_signout():
    resp = make_response(redirect(url_for("setup_form")))
    resp.delete_cookie("ucc_creds")
    resp.delete_cookie("ucc_user")
    return resp

@app.get("/logout")
def logout():
    return auth_signout()

# ──────────────────────────────────────────────────────────────────────────────
# Time Utilities
# ──────────────────────────────────────────────────────────────────────────────

LOCAL_TZ = ZoneInfo("America/New_York")

def _iso_with_local_offset(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ).replace(microsecond=0).isoformat()

def _as_local_wall(dt: datetime, tz=LOCAL_TZ) -> datetime:
    if dt is None:
        return None
    return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, tzinfo=tz)

def _is_midnight(dt: datetime) -> bool:
    return dt.hour == 0 and dt.minute == 0 and dt.second == 0

def _is_whole_days(start: datetime, end: datetime) -> bool:
    return abs((end - start).total_seconds()) % 86400 < 0.5

def _is_all_day_like(start: datetime, end: datetime) -> bool:
    return _is_whole_days(start, end) and _is_midnight(start) and _is_midnight(end)

# ──────────────────────────────────────────────────────────────────────────────
# Events API
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/events")
@rate_limit
def api_events():
    try:
        user = _get_cookie_json(request, "ucc_user") or {}
        canvas_ics = (user.get("canvas_ics") or "").strip()
        service = _google_service_from_cookie(request)
        
        source = (request.args.get("source") or "both").lower()
        days_q = request.args.get("days")
        
        try:
            horizon_days = int(days_q) if days_q else 14
            horizon_days = max(1, min(90, horizon_days))
        except:
            horizon_days = 14
        
        tz = LOCAL_TZ
        now = datetime.now(tz)
        after_param = request.args.get("after")
        before_param = request.args.get("before")
        
        if after_param or before_param:
            try:
                after = datetime.fromisoformat(after_param) if after_param else now.replace(hour=0, minute=0, second=0, microsecond=0)
                if after.tzinfo is None:
                    after = after.replace(tzinfo=LOCAL_TZ)
            except:
                return jsonify({"error": "bad_after"}), 400
            
            try:
                before = datetime.fromisoformat(before_param) if before_param else after + timedelta(days=horizon_days)
                if before.tzinfo is None:
                    before = before.replace(tzinfo=LOCAL_TZ)
            except:
                return jsonify({"error": "bad_before"}), 400
        else:
            after = now.replace(hour=0, minute=0, second=0, microsecond=0)
            before = after + timedelta(days=horizon_days)
        
        want_google = source in ("google", "both")
        want_canvas = source in ("canvas", "both")
        have_google = bool(service)
        have_canvas = bool(canvas_ics and canvas_ics.startswith("http"))
        
        if (want_google and not have_google) and (want_canvas and not have_canvas):
            return jsonify({"error": "not_configured", "next": "/setup"}), 400
        
        events = []
        
        if want_google and have_google:
            try:
                google_cal_ids = [s.strip() for s in os.getenv("GOOGLE_CAL_IDS", "").split(",") if s.strip()] or None
                events += fetch_google_all_calendars(service, after, before, allow_ids=google_cal_ids)
            except Exception as e:
                logger.error(f"Google fetch failed: {e}")
        
        if want_canvas and have_canvas:
            try:
                events += fetch_canvas_ics(canvas_ics, after, before)
            except Exception as e:
                logger.error(f"Canvas fetch failed: {e}")
        
        if not events:
            resp = jsonify([])
            resp.headers["Cache-Control"] = "private, max-age=300"
            return resp
        
        merged = merge_and_dedupe(events)
        merged.sort(key=lambda e: (e.start, e.end, e.title))
        
        detect_conflicts_param = request.args.get("detect_conflicts", "false").lower() == "true"
        conflict_map = {}
        
        if detect_conflicts_param:
            conflicts = find_conflicts(merged, threshold_minutes=10)
            for a, b in conflicts:
                conflict_map[a.uid] = b.title
                conflict_map[b.uid] = a.title
        
        SOURCE_COLOR = {"google": "#1a73e8", "canvas-ics": "#d93025"}
        out = []
        
        for e in merged:
            start, end = e.start, e.end
            is_all_day = _is_all_day_like(start, end)
            
            base = {
                "id": e.uid,
                "title": e.title,
                "color": SOURCE_COLOR.get(e.source),
                "extendedProps": {
                    "source": e.source,
                    "location": e.location,
                    "description": e.description,
                },
            }
            
            if e.uid in conflict_map:
                base["classNames"] = ["has-conflict"]
                base["extendedProps"]["conflict_with"] = conflict_map[e.uid]
            
            if is_all_day:
                base["start"] = start.date().isoformat()
                base["end"] = end.date().isoformat()
                base["allDay"] = True
            else:
                is_canvas = (e.source == "canvas-ics")
                if is_canvas:
                    base["start"] = _iso_with_local_offset(_as_local_wall(start))
                    base["end"] = _iso_with_local_offset(_as_local_wall(end))
                else:
                    base["start"] = _iso_with_local_offset(start)
                    base["end"] = _iso_with_local_offset(end)
            
            out.append(base)
        
        resp = jsonify(out)
        resp.headers["Cache-Control"] = f"private, max-age={config.cache_ttl}"
        return resp
    
    except Exception as e:
        logger.error(f"Events API error: {e}", exc_info=True)
        return jsonify({"error": "server_error"}), 500

# ──────────────────────────────────────────────────────────────────────────────
# Conflicts API
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/conflicts")
@rate_limit
def api_conflicts():
    try:
        user = _get_cookie_json(request, "ucc_user") or {}
        canvas_ics = (user.get("canvas_ics") or "").strip()
        service = _google_service_from_cookie(request)
        
        if not service or not canvas_ics:
            return jsonify({"conflicts": [], "count": 0}), 200
        
        days = int(request.args.get("days", "14"))
        threshold = int(request.args.get("threshold", "10"))
        
        tz = LOCAL_TZ
        now = datetime.now(tz)
        after = now.replace(hour=0, minute=0, second=0, microsecond=0)
        before = after + timedelta(days=days)
        
        events = []
        try:
            events += fetch_google_all_calendars(service, after, before)
        except:
            pass
        
        try:
            events += fetch_canvas_ics(canvas_ics, after, before)
        except:
            pass
        
        if not events:
            return jsonify({"conflicts": [], "count": 0}), 200
        
        merged = merge_and_dedupe(events)
        conflicts = find_conflicts(merged, threshold_minutes=threshold)
        
        conflict_list = []
        for a, b in conflicts:
            conflict_list.append({
                "event_a": {"title": a.title, "start": a.start.isoformat(), "end": a.end.isoformat()},
                "event_b": {"title": b.title, "start": b.start.isoformat(), "end": b.end.isoformat()},
                "overlap_minutes": int((min(a.end, b.end) - max(a.start, b.start)).total_seconds() / 60)
            })
        
        return jsonify({"conflicts": conflict_list, "count": len(conflict_list)}), 200
    
    except Exception as e:
        logger.error(f"Conflicts API error: {e}", exc_info=True)
        return jsonify({"error": "server_error"}), 500

# ──────────────────────────────────────────────────────────────────────────────
# Error Handlers
# ──────────────────────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "not_found"}), 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f"Internal error: {e}", exc_info=True)
    return jsonify({"error": "server_error"}), 500

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info(f"Starting on {config.host}:{config.port}")
    
    import threading
    def cleanup():
        while True:
            time.sleep(300)
            rate_limiter.cleanup()
    
    threading.Thread(target=cleanup, daemon=True).start()
    
    app.run(host=config.host, port=config.port, debug=config.debug)
