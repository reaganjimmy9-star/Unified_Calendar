#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unified Campus Calendar (Google + Canvas ICS) — Enhanced Edition
Features:
 - ICS recurrence expansion (RRULE/RDATE/EXDATE)
 - Enhanced de-duplication with fuzzy matching
 - Comprehensive CLI flags and output formats
 - Better error handling and logging
 - Performance optimizations with caching
 - Conflict detection
 - Time zone handling improvements

Quick start
-----------
1) Python 3.10+
2) pip install:
   google-api-python-client google-auth-oauthlib google-auth-httplib2
   requests python-dateutil icalendar tzlocal
3) Put your Google OAuth Desktop client file at:
   ~/.ucc/client_secret.json (preferred) or ./client_secret.json
4) Export your Canvas ICS URL:
   export CANVAS_ICS="https://your.canvas/feeds/calendars/....ics"
5) Run:
   python3 unified_calendar.py --format agenda --days 14

Optional env vars
-----------------
HORIZON_DAYS                    (# of days to show; default 14)
GOOGLE_CLIENT_SECRET            (explicit path to client_secret.json)
GOOGLE_CAL_IDS                  (comma-separated calendar IDs to include)
CANVAS_DEFAULT_DURATION_HOURS   (fallback when ICS has no DTEND/DURATION; default 1)
UCC_CACHE_DIR                   (cache directory; default ~/.ucc/cache)
UCC_LOG_LEVEL                   (DEBUG, INFO, WARNING, ERROR; default INFO)
"""

from __future__ import annotations
import csv
import difflib
import hashlib
import json
import logging
import os
import pickle
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Dict

import requests
from dateutil import parser as dtparse
from dateutil.rrule import rrulestr
from dateutil.tz import gettz
from tzlocal import get_localzone

from icalendar import Calendar

# Version
__version__ = "2.0.0"

# --------------------------- Configuration ---------------------------

class Config:
    """Centralized configuration management"""
    
    def __init__(self):
        self.app_dir = Path.home() / ".ucc"
        self.app_dir.mkdir(parents=True, exist_ok=True)
        
        # Cache directory
        cache_dir = os.getenv('UCC_CACHE_DIR', str(self.app_dir / 'cache'))
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Logging
        log_level = os.getenv('UCC_LOG_LEVEL', 'INFO').upper()
        self.log_level = getattr(logging, log_level, logging.INFO)
        
        # API settings
        self.google_client_secret = os.getenv('GOOGLE_CLIENT_SECRET')
        self.google_cal_ids = self._parse_cal_ids(os.getenv('GOOGLE_CAL_IDS', ''))
        self.canvas_ics_url = os.getenv('CANVAS_ICS', '')
        
        # Display settings
        self.horizon_days = int(os.getenv('HORIZON_DAYS', '14'))
        self.canvas_default_hours = int(os.getenv('CANVAS_DEFAULT_DURATION_HOURS', '1'))
        
        # Cache settings
        self.cache_ttl = int(os.getenv('UCC_CACHE_TTL', '300'))  # 5 minutes default
        self.enable_cache = os.getenv('UCC_ENABLE_CACHE', 'true').lower() == 'true'
    
    @staticmethod
    def _parse_cal_ids(val: str) -> Optional[List[str]]:
        """Parse comma-separated calendar IDs"""
        if not val:
            return None
        return [s.strip() for s in val.split(',') if s.strip()]

# Global config instance
config = Config()

# --------------------------- Logging Setup ---------------------------

def setup_logging(level: int = None, quiet: bool = False, verbose: bool = False):
    """Configure logging with appropriate level and format"""
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    elif level is None:
        level = config.log_level
    
    # Rich format for DEBUG, simple for others
    if level == logging.DEBUG:
        fmt = '[%(asctime)s] [%(levelname)s] [%(name)s:%(lineno)d] %(message)s'
    else:
        fmt = '[%(levelname)s] %(message)s'
    
    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt='%Y-%m-%d %H:%M:%S'
    )

logger = logging.getLogger(__name__)

# --------------------------- Model ---------------------------

@dataclass
class Event:
    """Enhanced event model with additional metadata"""
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
    calendar_name: Optional[str] = None
    recurring: bool = False
    color: Optional[str] = None

    def key_for_dedupe(self) -> Tuple[datetime, datetime, str]:
        """Minute-rounded start/end + normalized title (basic dedupe key)"""
        start_key = self.start.replace(second=0, microsecond=0)
        end_key = self.end.replace(second=0, microsecond=0)
        title_norm = ' '.join(self.title.lower().split())
        return (start_key, end_key, title_norm)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        return {
            'uid': self.uid,
            'source': self.source,
            'source_id': self.source_id,
            'title': self.title,
            'start': self.start.isoformat(),
            'end': self.end.isoformat(),
            'tz': self.tz,
            'location': self.location,
            'description': self.description,
            'course': self.course,
            'calendar_name': self.calendar_name,
            'recurring': self.recurring,
            'color': self.color
        }
    
    def duration_minutes(self) -> int:
        """Calculate event duration in minutes"""
        return int((self.end - self.start).total_seconds() / 60)
    
    def overlaps_with(self, other: 'Event', threshold_minutes: int = 0) -> bool:
        """Check if this event overlaps with another event"""
        # Add threshold for fuzzy overlap detection
        threshold = timedelta(minutes=threshold_minutes)
        return (self.start < other.end + threshold and 
                self.end > other.start - threshold)


# --------------------------- Cache Management ---------------------------

class CacheManager:
    """Simple file-based cache with TTL support"""
    
    def __init__(self, cache_dir: Path, ttl: int = 300):
        self.cache_dir = cache_dir
        self.ttl = ttl
        self.enabled = config.enable_cache
    
    def _get_cache_key(self, source: str, params: dict) -> str:
        """Generate cache key from source and parameters"""
        params_str = json.dumps(params, sort_keys=True)
        hash_obj = hashlib.md5(f"{source}:{params_str}".encode())
        return hash_obj.hexdigest()
    
    def get(self, source: str, params: dict) -> Optional[List[Event]]:
        """Retrieve cached events if valid"""
        if not self.enabled:
            return None
        
        cache_key = self._get_cache_key(source, params)
        cache_file = self.cache_dir / f"{cache_key}.pkl"
        
        if not cache_file.exists():
            logger.debug(f"Cache miss for {source}")
            return None
        
        # Check if cache is still valid
        age = time.time() - cache_file.stat().st_mtime
        if age > self.ttl:
            logger.debug(f"Cache expired for {source} (age: {age:.1f}s)")
            cache_file.unlink()
            return None
        
        try:
            with open(cache_file, 'rb') as f:
                events = pickle.load(f)
            logger.debug(f"Cache hit for {source} ({len(events)} events)")
            return events
        except Exception as e:
            logger.warning(f"Failed to load cache for {source}: {e}")
            cache_file.unlink()
            return None
    
    def set(self, source: str, params: dict, events: List[Event]):
        """Store events in cache"""
        if not self.enabled:
            return
        
        cache_key = self._get_cache_key(source, params)
        cache_file = self.cache_dir / f"{cache_key}.pkl"
        
        try:
            with open(cache_file, 'wb') as f:
                pickle.dump(events, f)
            logger.debug(f"Cached {len(events)} events for {source}")
        except Exception as e:
            logger.warning(f"Failed to cache events for {source}: {e}")
    
    def clear(self):
        """Clear all cached files"""
        try:
            for cache_file in self.cache_dir.glob("*.pkl"):
                cache_file.unlink()
            logger.info("Cache cleared")
        except Exception as e:
            logger.warning(f"Failed to clear cache: {e}")


# Initialize cache manager
cache_manager = CacheManager(config.cache_dir, config.cache_ttl)


# --------------------------- Utils ---------------------------

def ensure_tz(dt: datetime) -> datetime:
    """Make sure a datetime is timezone-aware; assume local if naive"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=get_localzone())
    return dt

def norm_title(t: str) -> str:
    """Normalize title for comparison"""
    t = t.lower().strip()
    t = re.sub(r'\s+', ' ', t)
    t = re.sub(r'\s*-\s*section.*$', '', t)  # strip Canvas section suffixes
    t = t.replace('[canvas]', '').replace('(due)', '')
    return t.strip()

def similar_title(a: str, b: str, threshold: float = 0.9) -> bool:
    """Check if two titles are similar using fuzzy matching"""
    a1, b1 = norm_title(a), norm_title(b)
    if a1 == b1:
        return True
    return difflib.SequenceMatcher(None, a1, b1).ratio() >= threshold

def safe_int(val: str, default: int = 0) -> int:
    """Safely convert string to int with fallback"""
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


# --------------------------- Google Calendar ---------------------------

def _find_client_secret_path() -> str:
    """Locate Google OAuth client secret file"""
    # 1) explicit env var
    p = config.google_client_secret
    if p and Path(p).exists():
        logger.debug(f"Using Google client secret from env: {p}")
        return p
    
    # 2) ~/.ucc/client_secret.json
    home_p = config.app_dir / "client_secret.json"
    if home_p.exists():
        logger.debug(f"Using Google client secret from ~/.ucc/")
        return str(home_p)
    
    # 3) ./client_secret.json
    local_p = Path("client_secret.json")
    if local_p.exists():
        logger.debug(f"Using Google client secret from current directory")
        return str(local_p)
    
    raise FileNotFoundError(
        "client_secret.json not found. Place it at ~/.ucc/client_secret.json "
        "or ./client_secret.json, or set GOOGLE_CLIENT_SECRET to its path."
    )

def get_google_service():
    """Get authenticated Google Calendar service"""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
    token_path = config.app_dir / "token.json"

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            logger.debug("Loaded existing Google credentials")
        except Exception as e:
            logger.warning(f"Failed to load credentials: {e}")
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                logger.info("Refreshing Google credentials")
                creds.refresh(Request())
            except Exception as e:
                logger.warning(f"Failed to refresh credentials: {e}")
                creds = None
        
        if not creds:
            logger.info("Starting Google OAuth flow")
            flow = InstalledAppFlow.from_client_secrets_file(
                _find_client_secret_path(), 
                SCOPES
            )
            creds = flow.run_local_server(port=0)
        
        # Save credentials
        with open(token_path, "w") as f:
            f.write(creds.to_json())
        logger.debug("Saved Google credentials")

    return build('calendar', 'v3', credentials=creds, cache_discovery=False)

def fetch_google_all_calendars(
    service, 
    start: datetime, 
    end: datetime, 
    allow_ids: Optional[List[str]] = None
) -> List[Event]:
    """Fetch events from Google Calendar with caching"""
    
    # Check cache
    cache_params = {
        'start': start.isoformat(),
        'end': end.isoformat(),
        'allow_ids': allow_ids
    }
    cached = cache_manager.get('google', cache_params)
    if cached is not None:
        return cached
    
    all_events: List[Event] = []

    try:
        # Get calendar list
        cals_resp = service.calendarList().list().execute()
        calendars = cals_resp.get('items', [])
        cal_ids = [c['id'] for c in calendars]
        id_to_name = {c['id']: c.get('summary', c['id']) for c in calendars}
        id_to_color = {c['id']: c.get('backgroundColor', '#1a73e8') for c in calendars}

        # Filter by allowed IDs if specified
        if allow_ids:
            wanted = set(allow_ids)
            cal_ids = [cid for cid in cal_ids if cid in wanted]
            logger.info(f"Filtering to {len(cal_ids)} calendars")

        time_min = start.astimezone(timezone.utc).isoformat()
        time_max = end.astimezone(timezone.utc).isoformat()

        # Fetch from each calendar
        for cid in cal_ids:
            logger.debug(f"Fetching from calendar: {id_to_name.get(cid, cid)}")
            page_token = None
            cal_events = 0
            
            while True:
                try:
                    resp = service.events().list(
                        calendarId=cid,
                        timeMin=time_min,
                        timeMax=time_max,
                        singleEvents=True,
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
                        
                        # Check if recurring
                        recurring = bool(it.get('recurringEventId'))
                        
                        all_events.append(Event(
                            uid=f"google:{cid}:{it.get('id')}",
                            source='google',
                            source_id=f"{cid}:{it.get('id','')}",
                            title=f"{title} [{cal_name}]",
                            start=s_dt, 
                            end=e_dt, 
                            tz=s_dt.tzname() or 'UTC',
                            location=it.get('location'),
                            description=it.get('description'),
                            calendar_name=cal_name,
                            recurring=recurring,
                            color=id_to_color.get(cid)
                        ))
                        cal_events += 1

                    page_token = resp.get('nextPageToken')
                    if not page_token:
                        break
                
                except Exception as e:
                    logger.error(f"Error fetching from calendar {cid}: {e}")
                    break
            
            logger.debug(f"Fetched {cal_events} events from {cal_name}")

        logger.info(f"Google: fetched {len(all_events)} total events")
        
        # Cache the results
        cache_manager.set('google', cache_params, all_events)
        
        return all_events
    
    except Exception as e:
        logger.error(f"Google fetch failed: {e}", exc_info=True)
        return []


# --------------------------- Canvas (ICS-only) ---------------------------

def _expand_ics_component(
    component, 
    win_start: datetime, 
    win_end: datetime, 
    default_hours: int = 1
):
    """
    Yield (start, end) datetimes for each instance of a VEVENT within window.
    Handles: DTSTART (+TZID), DTEND or DURATION, RRULE/RDATE, EXDATE, VALUE=DATE
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
        try:
            rule = rrulestr(rrule_txt, dtstart=dtstart)
            for occ in rule.between(win_start, win_end, inc=True):
                if occ not in exdates:
                    yield (occ, end_from(occ))
        except Exception as e:
            logger.warning(f"Failed to parse RRULE: {e}")
    else:
        # single instance
        if (dtstart <= win_end) and (end_from(dtstart) >= win_start) and (dtstart not in exdates):
            yield (dtstart, end_from(dtstart))

    # RDATE additions
    for r in rdates:
        if (r <= win_end) and (end_from(r) >= win_start) and (r not in exdates):
            yield (r, end_from(r))

def fetch_canvas_ics(ics_url: str, start: datetime, end: datetime) -> List[Event]:
    """Fetch ICS, expand recurrences, return events list with caching"""
    
    # Check cache
    cache_params = {
        'url': ics_url,
        'start': start.isoformat(),
        'end': end.isoformat()
    }
    cached = cache_manager.get('canvas', cache_params)
    if cached is not None:
        return cached
    
    events: List[Event] = []
    
    try:
        logger.debug(f"Fetching Canvas ICS from {ics_url[:50]}...")
        resp = requests.get(ics_url, timeout=30)
        resp.raise_for_status()
        
        cal = Calendar.from_ical(resp.content)
        default_hours = config.canvas_default_hours

        for component in cal.walk():
            if component.name != 'VEVENT':
                continue

            # Basic fields
            summary = str(component.get('summary', '(Canvas)'))
            uid = str(component.get('uid', ''))
            location = str(component.get('location')) if component.get('location') else None
            description = str(component.get('description')) if component.get('description') else None
            
            # Check if recurring
            has_rrule = bool(component.get('rrule'))

            for s_dt, e_dt in _expand_ics_component(component, start, end, default_hours):
                events.append(Event(
                    uid=f"canvas-ics:{uid or 'anon'}:{s_dt.isoformat()}",
                    source='canvas-ics',
                    source_id=uid or '',
                    title=summary,
                    start=ensure_tz(s_dt), 
                    end=ensure_tz(e_dt), 
                    tz=s_dt.tzname() or 'UTC',
                    location=location, 
                    description=description,
                    recurring=has_rrule
                ))

        logger.info(f"Canvas: fetched {len(events)} events")
        
        # Cache the results
        cache_manager.set('canvas', cache_params, events)
        
        return events
    
    except requests.RequestException as e:
        logger.error(f"Canvas ICS fetch failed: {e}")
        return []
    except Exception as e:
        logger.error(f"Canvas ICS parse failed: {e}", exc_info=True)
        return []


# --------------------------- Merge & Conflicts ---------------------------

def merge_and_dedupe(events: List[Event]) -> List[Event]:
    """Merge events preferring Google on duplicates, using stronger dedupe"""
    if not events:
        return []
    
    logger.debug(f"Deduplicating {len(events)} events")
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
            
            # Check if same time window (minute precision)
            same_window = (
                abs(int(e.start.timestamp() / 60) - int(f.start.timestamp() / 60)) == 0 and
                abs(int(e.end.timestamp() / 60) - int(f.end.timestamp() / 60)) == 0
            )
            
            if same_window and similar_title(e.title, f.title):
                group.append(f)
                used[j] = True
            elif f.start > e.end + timedelta(minutes=1):
                break
        
        # Prefer Google events
        group.sort(key=lambda x: 0 if x.source == 'google' else 1)
        out.append(group[0])
        
        if len(group) > 1:
            logger.debug(f"Deduped {len(group)} similar events: {group[0].title[:50]}")
    
    logger.info(f"After deduplication: {len(out)} events")
    return out

def find_conflicts(events: List[Event], threshold_minutes: int = 10) -> List[Tuple[Event, Event]]:
    """Find overlapping events with configurable threshold"""
    conflicts = []
    events = sorted(events, key=lambda e: e.start)
    th = timedelta(minutes=threshold_minutes)
    
    for i in range(len(events) - 1):
        a = events[i]
        for j in range(i + 1, len(events)):
            b = events[j]
            
            # If b starts after a ends (plus threshold), no more conflicts for a
            if b.start >= a.end + th:
                break
            
            # Check for overlap
            if a.overlaps_with(b, threshold_minutes):
                conflicts.append((a, b))
                logger.debug(f"Conflict found: '{a.title[:30]}' vs '{b.title[:30]}'")
    
    logger.info(f"Found {len(conflicts)} conflicts")
    return conflicts


# --------------------------- Output Formatting ---------------------------

def fmt_event(e: Event, show_source: bool = True) -> str:
    """Format event as human-readable string"""
    local = get_localzone()
    st = e.start.astimezone(local).strftime('%a %b %d %I:%M %p')
    en = e.end.astimezone(local).strftime('%I:%M %p')
    
    source_tag = ''
    if show_source:
        src = 'G' if e.source == 'google' else 'C'
        source_tag = f"({src}) "
    
    course = f" [{e.course}]" if e.course else ''
    recurring = " 🔄" if e.recurring else ''
    
    return f"{source_tag}{st}–{en}  {e.title}{course}{recurring}"

def to_json(events: List[Event]) -> str:
    """Convert events to JSON format"""
    return json.dumps([e.to_dict() for e in events], indent=2)

def to_csv(events: List[Event]) -> str:
    """Convert events to CSV format"""
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["source", "title", "start", "end", "tz", "location", "description", "recurring"])
    for e in events:
        w.writerow([
            e.source, 
            e.title, 
            e.start.isoformat(), 
            e.end.isoformat(), 
            e.tz, 
            e.location or "", 
            e.description or "",
            e.recurring
        ])
    return buf.getvalue()

def print_agenda(events: List[Event], show_conflicts: bool = True, conflict_threshold: int = 10):
    """Print events in agenda format grouped by day"""
    events = sorted(events, key=lambda e: (e.start, e.end, e.title))
    current_day = None
    
    for e in events:
        day = e.start.astimezone(get_localzone()).strftime('%A, %B %d, %Y')
        if day != current_day:
            current_day = day
            print(f"\n{'='*60}")
            print(f"{current_day}")
            print('='*60)
        print(f"  {fmt_event(e)}")
        if e.location:
            print(f"    📍 {e.location}")

    # Print conflicts if requested
    if show_conflicts:
        conflicts = find_conflicts(events, threshold_minutes=conflict_threshold)
        if conflicts:
            print(f"\n{'='*60}")
            print(f"⚠️  CONFLICTS (overlap > {conflict_threshold} min)")
            print('='*60)
            for a, b in conflicts:
                print(f"\n  {fmt_event(a, show_source=True)}")
                print(f"    ⚠️  overlaps with")
                print(f"  {fmt_event(b, show_source=True)}")
        else:
            print(f"\n✅ No conflicts detected (threshold {conflict_threshold} min)")


# --------------------------- CLI ---------------------------

def parse_args(argv: Optional[List[str]] = None):
    """Parse command line arguments"""
    import argparse
    
    p = argparse.ArgumentParser(
        description="Unified Campus Calendar (Google + Canvas ICS) — Enhanced Edition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  %(prog)s --days 7 --format agenda
  %(prog)s --after 2025-11-01 --before 2025-11-30 --format json
  %(prog)s --only google --format csv > events.csv
  %(prog)s --clear-cache
  
Version: {__version__}
        """
    )
    
    # Time range
    p.add_argument('--days', type=int, default=config.horizon_days,
                   help=f'Days to show (default: {config.horizon_days})')
    p.add_argument('--after', help='ISO start datetime (overrides --days)')
    p.add_argument('--before', help='ISO end datetime (optional)')
    
    # Output format
    p.add_argument('--format', choices=['agenda', 'json', 'csv'], default='agenda',
                   help='Output format (default: agenda)')
    
    # Filtering
    p.add_argument('--only', choices=['google', 'canvas'],
                   help='Filter to a single source')
    p.add_argument('--cal', help='Comma-separated Google Calendar IDs to include')
    
    # Conflict detection
    p.add_argument('--conflict-min', type=int, default=10,
                   help='Conflict threshold in minutes (default: 10)')
    p.add_argument('--no-conflicts', action='store_true',
                   help='Hide conflict detection')
    
    # Cache management
    p.add_argument('--clear-cache', action='store_true',
                   help='Clear cache and exit')
    p.add_argument('--no-cache', action='store_true',
                   help='Disable caching for this run')
    
    # Logging
    p.add_argument('--verbose', '-v', action='store_true',
                   help='Enable verbose logging')
    p.add_argument('--quiet', '-q', action='store_true',
                   help='Suppress most output')
    
    # Version
    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    
    return p.parse_args(argv)


# --------------------------- Main ---------------------------

def main(argv: Optional[List[str]] = None):
    """Main entry point"""
    args = parse_args(argv)
    
    # Setup logging
    setup_logging(quiet=args.quiet, verbose=args.verbose)
    
    logger.info(f"Unified Calendar v{__version__}")
    
    # Handle cache clearing
    if args.clear_cache:
        cache_manager.clear()
        print("Cache cleared successfully")
        return 0
    
    # Disable cache if requested
    if args.no_cache:
        config.enable_cache = False
        logger.info("Cache disabled for this run")
    
    # Time window
    local_tz = get_localzone()
    if args.after:
        try:
            start = ensure_tz(dtparse.parse(args.after))
        except Exception as e:
            logger.error(f"Invalid --after date: {e}")
            return 1
    else:
        start = datetime.now(local_tz)
    
    if args.before:
        try:
            end = ensure_tz(dtparse.parse(args.before))
        except Exception as e:
            logger.error(f"Invalid --before date: {e}")
            return 1
    else:
        end = start + timedelta(days=args.days)
    
    logger.info(f"Fetching events from {start.date()} to {end.date()}")
    
    # Fetch Google events
    g_events: List[Event] = []
    if args.only in (None, 'google'):
        try:
            allow_ids = None
            if args.cal:
                allow_ids = [s.strip() for s in args.cal.split(',')]
            elif config.google_cal_ids:
                allow_ids = config.google_cal_ids
            
            gsvc = get_google_service()
            g_events = fetch_google_all_calendars(gsvc, start, end, allow_ids=allow_ids)
        except FileNotFoundError as e:
            logger.error(str(e))
        except Exception as e:
            logger.error(f"Google fetch failed: {e}")
            if args.verbose:
                logger.exception("Full traceback:")
    
    # Fetch Canvas events
    c_events: List[Event] = []
    if args.only in (None, 'canvas'):
        canvas_ics = config.canvas_ics_url
        if canvas_ics:
            c_events = fetch_canvas_ics(canvas_ics, start, end)
        else:
            logger.warning("Canvas ICS not configured. Set CANVAS_ICS environment variable.")
    
    # Merge and filter
    all_events = g_events + c_events
    all_events = [e for e in all_events if (e.end > start and e.start < end)]
    merged = merge_and_dedupe(all_events)
    
    if not merged:
        logger.warning("No events found in the specified time range")
        if args.format == 'agenda':
            print("\nNo events found.")
        return 0
    
    # Output
    if args.format == 'json':
        print(to_json(merged))
    elif args.format == 'csv':
        print(to_csv(merged), end='')
    else:  # agenda
        print_agenda(merged, show_conflicts=not args.no_conflicts, 
                    conflict_threshold=args.conflict_min)
    
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        logger.exception("Full traceback:")
        sys.exit(1)
