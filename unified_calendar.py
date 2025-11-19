#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unified Campus Calendar (Google + Canvas ICS) — display-only
Now with:
 - ICS recurrence expansion (RRULE/RDATE/EXDATE)
 - Stronger de-duplication (normalized + fuzzy title when times match)
 - CLI flags: --days/--after/--before/--format/--conflict-min/--only/--cal
 - JSON/CSV output modes in addition to agenda
Keep Canvas REST OUT (ICS-only), read-only Google.

Quick start
-----------
1) Python 3.10+
2) pip install:
   google-api-python-client google-auth-oauthlib google-auth-httplib2
   requests python-dateutil icalendar tzlocal
3) Put your Google OAuth Desktop client file at either:
   - ~/.ucc/client_secret.json   (preferred), or
   - ./client_secret.json        (same folder as this script)
4) Export your Canvas ICS URL (from Canvas → Calendar → Calendar Feed):
   export CANVAS_ICS="https://your.canvas/feeds/calendars/....ics"
5) Run:
   python3 unified_calendar.py --format agenda --days 14

Optional env vars
-----------------
HORIZON_DAYS                    (# of days to show; default 14)
GOOGLE_CLIENT_SECRET            (explicit path to client_secret.json)
GOOGLE_CAL_IDS                  (comma-separated calendar IDs to include; otherwise all)
CANVAS_DEFAULT_DURATION_HOURS   (fallback when ICS has start but no DTEND/DURATION; default 1)
"""

from __future__ import annotations
import csv
import difflib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import requests
from dateutil import parser as dtparse
from dateutil.rrule import rrulestr
from dateutil.tz import gettz
from tzlocal import get_localzone

from icalendar import Calendar

# --------------------------- Model ---------------------------

@dataclass
class Event:
    uid: str
    source: str              # 'google' | 'canvas-ics'
    source_id: str
    title: str
    start: datetime
    end: datetime
    tz: str
    location: Optional[str] = None
    description: Optional[str] = None
    course: Optional[str] = None

    def key_for_dedupe(self) -> Tuple[datetime, datetime, str]:
        """Minute-rounded start/end + normalized title (basic dedupe key)."""
        start_key = self.start.replace(second=0, microsecond=0)
        end_key = self.end.replace(second=0, microsecond=0)
        title_norm = ' '.join(self.title.lower().split())
        return (start_key, end_key, title_norm)


# --------------------------- Utils ---------------------------

def ensure_tz(dt: datetime) -> datetime:
    """Make sure a datetime is timezone-aware; assume local if naive."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=get_localzone())
    return dt

def norm_title(t: str) -> str:
    t = t.lower().strip()
    t = re.sub(r'\s+', ' ', t)
    t = re.sub(r'\s*-\s*section.*$', '', t)  # strip Canvas section suffixes
    t = t.replace('[canvas]', '').replace('(due)', '')
    return t.strip()

def similar_title(a: str, b: str) -> bool:
    a1, b1 = norm_title(a), norm_title(b)
    if a1 == b1:
        return True
    return difflib.SequenceMatcher(None, a1, b1).ratio() >= 0.9


# --------------------------- Google ---------------------------

def _find_client_secret_path() -> str:
    # 1) explicit env var
    p = os.getenv('GOOGLE_CLIENT_SECRET')
    if p and Path(p).exists():
        return p
    # 2) ~/.ucc/client_secret.json
    home_p = Path.home() / ".ucc" / "client_secret.json"
    if home_p.exists():
        return str(home_p)
    # 3) ./client_secret.json
    local_p = Path("client_secret.json")
    if local_p.exists():
        return str(local_p)
    raise FileNotFoundError(
        "client_secret.json not found. Place it at ~/.ucc/client_secret.json "
        "or ./client_secret.json, or set GOOGLE_CLIENT_SECRET to its path."
    )

def get_google_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
    app_dir = Path.home() / ".ucc"
    app_dir.mkdir(parents=True, exist_ok=True)
    token_path = app_dir / "token.json"

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None  # fall through to fresh flow
        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(_find_client_secret_path(), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build('calendar', 'v3', credentials=creds, cache_discovery=False)

def fetch_google_all_calendars(service, start: datetime, end: datetime, allow_ids: Optional[List[str]] = None) -> List[Event]:
    """Fetch events from selected calendars (or all). Recurrences expanded server-side."""
    all_events: List[Event] = []

    cals_resp = service.calendarList().list().execute()
    calendars = cals_resp.get('items', [])
    cal_ids = [c['id'] for c in calendars]
    id_to_name = {c['id']: c.get('summary', c['id']) for c in calendars}

    if allow_ids:
        wanted = set(allow_ids)
        cal_ids = [cid for cid in cal_ids if cid in wanted]

    time_min = start.astimezone(timezone.utc).isoformat()
    time_max = end.astimezone(timezone.utc).isoformat()

    for cid in cal_ids:
        page_token = None
        while True:
            resp = service.events().list(
                calendarId=cid,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,        # expand recurrences
                orderBy='startTime',
                pageToken=page_token
            ).execute()

            for it in resp.get('items', []):
                s_raw, e_raw = it.get('start', {}), it.get('end', {})
                s = s_raw.get('dateTime') or s_raw.get('date')
                e = e_raw.get('dateTime') or e_raw.get('date')
                if not s or not e:
                    continue
                s_dt, e_dt = ensure_tz(dtparse.parse(s)), ensure_tz(dtparse.parse(e))
                title = it.get('summary', '(no title)')
                cal_name = id_to_name.get(cid, cid)
                title_disp = f"{title} [{cal_name}]"
                all_events.append(Event(
                    uid=f"google:{cid}:{it.get('id')}",
                    source='google',
                    source_id=f"{cid}:{it.get('id','')}",
                    title=title_disp,
                    start=s_dt, end=e_dt, tz=s_dt.tzname() or 'UTC',
                    location=it.get('location'),
                    description=it.get('description')
                ))

            page_token = resp.get('nextPageToken')
            if not page_token:
                break

    return all_events


# --------------------------- Canvas (ICS-only) ---------------------------

def _expand_ics_component(component, win_start: datetime, win_end: datetime, default_hours: int = 1):
    """
    Yield (start, end) datetimes for each instance of a VEVENT within [win_start, win_end].
    Handles: DTSTART (+TZID), DTEND or DURATION, RRULE/RDATE, EXDATE, VALUE=DATE all-day.
    """
    def _ensure_dt(v):
        if isinstance(v, datetime):
            if v.tzinfo:
                return v
            # use component TZID if present; else local
            tzid = None
            try:
                tzid = str(component.get('dtstart').params.get('TZID'))
            except Exception:
                pass
            return v.replace(tzinfo=gettz(tzid) if tzid else get_localzone())
        # VALUE=DATE
        return datetime.combine(v, datetime.min.time(), tzinfo=get_localzone())

    dtstart_prop = component.get('dtstart')
    if not dtstart_prop:
        return
    dtstart = _ensure_dt(dtstart_prop.dt)

    # Duration logic
    if component.get('duration'):
        duration = component.get('duration').dt
    elif component.get('dtend'):
        base_end = _ensure_dt(component.get('dtend').dt)
        duration = (base_end - dtstart)
    else:
        duration = timedelta(hours=default_hours)

    def end_from(start_dt):
        # If DTEND was VALUE=DATE, treat as all-day exclusive end
        if component.get('dtend') and component.get('dtend').params.get('VALUE') == 'DATE':
            return start_dt + timedelta(days=1)
        return start_dt + duration

    # EXDATEs
    exdates = set()
    for ex in component.get('exdate', []):
        for x in ex.dts:
            exdates.add(_ensure_dt(x.dt))

    # RDATEs
    rdates = []
    for rd in component.get('rdate', []):
        for r in rd.dts:
            rdates.append(_ensure_dt(r.dt))

    # RRULE
    if component.get('rrule'):
        rrule_txt = "RRULE:" + component.get('rrule').to_ical().decode().strip()
        rule = rrulestr(rrule_txt, dtstart=dtstart)
        for occ in rule.between(win_start, win_end, inc=True):
            if occ not in exdates:
                yield (occ, end_from(occ))
    else:
        # single instance
        if (dtstart <= win_end) and (end_from(dtstart) >= win_start) and (dtstart not in exdates):
            yield (dtstart, end_from(dtstart))

    # RDATE additions
    for r in rdates:
        if (r <= win_end) and (end_from(r) >= win_start) and (r not in exdates):
            yield (r, end_from(r))

def fetch_canvas_ics(ics_url: str, start: datetime, end: datetime) -> List[Event]:
    """Fetch ICS, expand recurrences, return events list."""
    resp = requests.get(ics_url, timeout=30)
    resp.raise_for_status()
    cal = Calendar.from_ical(resp.content)
    events: List[Event] = []
    default_hours = int(os.getenv('CANVAS_DEFAULT_DURATION_HOURS', '1'))

    for component in cal.walk():
        if component.name != 'VEVENT':
            continue

        # Basic fields
        summary = str(component.get('summary', '(Canvas)'))
        uid = str(component.get('uid', ''))
        location = str(component.get('location')) if component.get('location') else None
        description = str(component.get('description')) if component.get('description') else None

        for s_dt, e_dt in _expand_ics_component(component, start, end, default_hours):
            events.append(Event(
                uid=f"canvas-ics:{uid or 'anon'}:{s_dt.isoformat()}",
                source='canvas-ics',
                source_id=uid or '',
                title=summary,
                start=ensure_tz(s_dt), end=ensure_tz(e_dt), tz=s_dt.tzname() or 'UTC',
                location=location, description=description
            ))

    return events


# --------------------------- Merge & conflicts ---------------------------

def merge_and_dedupe(events: List[Event]) -> List[Event]:
    """Merge events preferring Google on duplicates, using stronger dedupe."""
    if not events:
        return []
    events = sorted(events, key=lambda e: (e.start, e.end))
    out: List[Event] = []
    used = [False] * len(events)

    for i, e in enumerate(events):
        if used[i]:
            continue
        group = [e]
        used[i] = True
        for j in range(i + 1, len(events)):
            if used[j]:
                continue
            f = events[j]
            same_window = (
                abs(int(e.start.timestamp() / 60) - int(f.start.timestamp() / 60)) == 0 and
                abs(int(e.end.timestamp() / 60) - int(f.end.timestamp() / 60)) == 0
            )
            if same_window and similar_title(e.title, f.title):
                group.append(f)
                used[j] = True
            elif f.start > e.end + timedelta(minutes=1):
                # future items beyond window; safe to break inner loop
                break
        group.sort(key=lambda x: 0 if x.source == 'google' else 1)
        out.append(group[0])
    return out

def find_conflicts(events: List[Event], threshold_minutes: int = 10) -> List[Tuple[Event, Event]]:
    out = []
    events = sorted(events, key=lambda e: e.start)
    th = timedelta(minutes=threshold_minutes)
    for i in range(len(events) - 1):
        a, b = events[i], events[i + 1]
        if a.end - b.start > th and a.start <= b.start:
            out.append((a, b))
    return out


# --------------------------- Output formatting ---------------------------

def fmt_event(e: Event) -> str:
    local = get_localzone()
    st = e.start.astimezone(local).strftime('%a %b %d %I:%M %p')
    en = e.end.astimezone(local).strftime('%I:%M %p')
    src = 'G' if e.source == 'google' else 'C'
    course = f" [{e.course}]" if e.course else ''
    return f"({src}) {st}–{en}  {e.title}{course}"

def to_json(events: List[Event]) -> str:
    def ev(e: Event):
        return {
            "uid": e.uid,
            "source": e.source,
            "title": e.title,
            "start": e.start.isoformat(),
            "end": e.end.isoformat(),
            "tz": e.tz,
            "location": e.location,
            "description": e.description,
            "course": e.course,
        }
    return json.dumps([ev(e) for e in events], indent=2)

def to_csv(events: List[Event]) -> str:
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["source", "title", "start", "end", "tz", "location"])
    for e in events:
        w.writerow([e.source, e.title, e.start.isoformat(), e.end.isoformat(), e.tz, e.location or ""])
    return buf.getvalue()


# --------------------------- CLI ---------------------------

def parse_args(argv: Optional[List[str]] = None):
    import argparse
    p = argparse.ArgumentParser(description="Unified (Google + Canvas ICS) agenda, display-only.")
    p.add_argument('--days', type=int, default=int(os.getenv('HORIZON_DAYS', '14')), help='Days to show (default from HORIZON_DAYS or 14)')
    p.add_argument('--after', help='ISO start datetime (overrides --days start)')
    p.add_argument('--before', help='ISO end datetime (optional; else after+days)')
    p.add_argument('--format', choices=['agenda', 'json', 'csv'], default='agenda')
    p.add_argument('--conflict-min', type=int, default=10, help='Conflict threshold minutes (default 10)')
    p.add_argument('--only', choices=['google', 'canvas'], help='Filter to a single source')
    p.add_argument('--cal', help='Comma-separated Google Calendar IDs to include')
    p.add_argument('--verbose', action='store_true')
    p.add_argument('--quiet', action='store_true')
    return p.parse_args(argv)


# --------------------------- Main ---------------------------

def main(argv: Optional[List[str]] = None):
    args = parse_args(argv)

    # logging
    level = logging.WARNING if args.quiet else (logging.DEBUG if args.verbose else logging.INFO)
    logging.basicConfig(level=level, format='[%(levelname)s] %(message)s')

    # time window
    local_tz = get_localzone()
    if args.after:
        start = ensure_tz(dtparse.parse(args.after))
    else:
        start = datetime.now(local_tz)
    if args.before:
        end = ensure_tz(dtparse.parse(args.before))
    else:
        end = start + timedelta(days=args.days)

    # Google
    g_events: List[Event] = []
    try:
        if args.only in (None, 'google'):
            try:
                allow_ids = [s.strip() for s in args.cal.split(',')] if args.cal else \
                            [s.strip() for s in os.getenv('GOOGLE_CAL_IDS','').split(',') if s.strip()]
            except Exception:
                allow_ids = None
            gsvc = get_google_service()
            g_events = fetch_google_all_calendars(gsvc, start, end, allow_ids=allow_ids)
            logging.info("Google events: %d", len(g_events))
    except Exception as e:
        logging.error("Google fetch failed: %s", e)

    # Canvas ICS
    c_events: List[Event] = []
    try:
        if args.only in (None, 'canvas'):
            canvas_ics = os.getenv('CANVAS_ICS')
            if canvas_ics:
                c_events = fetch_canvas_ics(canvas_ics, start, end)
                logging.info("Canvas (ICS) events: %d", len(c_events))
            else:
                logging.info("Canvas ICS not configured. Set CANVAS_ICS to your feed URL.")
    except Exception as e:
        logging.error("Canvas ICS fetch failed: %s", e)

    # Merge
    all_events = g_events + c_events
    all_events = [e for e in all_events if (e.end > start and e.start < end)]
    merged = merge_and_dedupe(all_events)

    # Output
    if args.format == 'json':
        print(to_json(merged))
    elif args.format == 'csv':
        print(to_csv(merged), end='')
    else:
        # Agenda by day
        merged.sort(key=lambda e: (e.start, e.end, e.title))
        current_day = None
        for e in merged:
            day = e.start.astimezone(get_localzone()).strftime('%A, %B %d, %Y')
            if day != current_day:
                current_day = day
                print(f"\n=== {current_day} ===")
            print("-", fmt_event(e))

        # Conflicts
        conflicts = find_conflicts(merged, threshold_minutes=args.conflict_min)
        if conflicts:
            print(f"\n=== Conflicts (overlap > {args.conflict_min} min) ===")
            for a, b in conflicts:
                print("-", fmt_event(a))
                print("  overlaps with")
                print("  ", fmt_event(b))
        else:
            print(f"\nNo conflicts detected (threshold {args.conflict_min} min).")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")


"""
---

## 🔌 Quick path (no packaging): drop-in `server.py` + `static/index.html`

If you want the **fastest** way to get the visual calendar running **with your current `unified_calendar.py`**, do this minimal setup:

**File tree**
```
project/
  unified_calendar.py        # your existing (updated) script
  server.py                  # <-- add this
  static/
    index.html               # <-- add this
```

**server.py**
```python
from __future__ import annotations
from flask import Flask, jsonify, send_from_directory
from datetime import datetime, timedelta
import os
from tzlocal import get_localzone

# Import from your existing script
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

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

@app.get("/api/events")
def api_events():
    tz = get_localzone()
    days = int(os.getenv("HORIZON_DAYS", "14"))
    now = datetime.now(tz)
    after = now.replace(hour=0, minute=0, second=0, microsecond=0)
    before = after + timedelta(days=days)

    # Google
    events = []
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

    # Convert to FullCalendar format
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


```

**static/index.html**
```html
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Unified Calendar</title>
    <link href="https://cdn.jsdelivr.net/npm/fullcalendar@6.1.15/index.global.min.css" rel="stylesheet">
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; }
      header { padding: 12px 16px; border-bottom: 1px solid #eee; display:flex; gap:12px; align-items:center; }
      #calendar { max-width: 1100px; margin: 24px auto; padding: 0 16px; }
      .legend { margin-left: auto; display:flex; gap:12px; align-items:center; }
      .dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
    </style>
  </head>
  <body>
    <header>
      <h3 style="margin:0">Unified Calendar (read-only)</h3>
      <div class="legend">
        <span><span class="dot" style="background:#1a73e8"></span> Google</span>
        <span><span class="dot" style="background:#d93025"></span> Canvas</span>
      </div>
    </header>
    <div id="calendar"></div>

    <script src="https://cdn.jsdelivr.net/npm/fullcalendar@6.1.15/index.global.min.js"></script>
    <script>
      document.addEventListener('DOMContentLoaded', function() {
        const el = document.getElementById('calendar');
        const calendar = new FullCalendar.Calendar(el, {
          initialView: 'dayGridMonth',
          headerToolbar: { left: 'prev,next today', center: 'title', right: 'dayGridMonth,timeGridWeek,timeGridDay,listWeek' },
          nowIndicator: true,
          navLinks: true,
          eventOverlap: true,
          editable: false,
          selectable: false,
          events: async function(fetchInfo, successCb, failureCb) {
            try {
              const res = await fetch('/api/events');
              if (!res.ok) throw new Error('HTTP '+res.status);
              successCb(await res.json());
            } catch (err) { failureCb(err); }
          },
          eventClick: function(info) {
            const p = info.event.extendedProps || {};
            const lines = [info.event.title];
            if (p.source) lines.push('Source: '+p.source);
            if (p.location) lines.push('Location: '+p.location);
            if (p.description) lines.push('
'+p.description);
            alert(lines.join('
'));
          }
        });
        calendar.render();
      });
    </script>
  </body>
</html>
```

**Run it**
```bash
# from the project folder containing unified_calendar.py and server.py
pip install flask tzlocal  # (plus your existing deps already installed)
export CANVAS_ICS="https://…/calendar.ics"
# optional: export GOOGLE_CAL_IDS="primary,other@group.calendar.google.com"
python3 server.py
# open http://127.0.0.1:5000
```

This quick path uses your existing module directly; no packaging step required. You can switch to the fuller packaged CLI later if you want distributable installs.

---

If you want, I can also produce a PyInstaller spec so you can ship a single executable that launches the same UI.
"""