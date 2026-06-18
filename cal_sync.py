#!/usr/bin/env python3
"""Sync Cal.com date overrides from FD/SD all-day markers in Google Calendar.

FD -> available 17:00-23:59 that day. SD -> available 00:00-13:00 that day.
The result is always intersected with your normal Cal.com weekly hours, so
overrides only ever restrict availability, never expand it. Existing
overrides for dates this tool doesn't manage are left untouched; nothing is
ever deleted.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

DEFAULT_CONFIG = Path.home() / ".config/cal_sync/config.json"
DEFAULT_CREDENTIALS = Path.home() / ".config/cal_sync/credentials.json"
DEFAULT_TOKEN = Path.home() / ".config/cal_sync/token.json"

RULES = {"FD": (17 * 60, 23 * 60 + 59), "SD": (0, 13 * 60)}
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------- time interval helpers ----------

def parse_hhmm(s):
    h, m = s.split(":")[:2]
    return int(h) * 60 + int(m)


def format_hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def merge(intervals):
    if not intervals:
        return []
    out = [sorted(intervals)[0]]
    for start, end in sorted(intervals)[1:]:
        last_start, last_end = out[-1]
        if start <= last_end:
            out[-1] = (last_start, max(last_end, end))
        else:
            out.append((start, end))
    return out


def intersect2(a, b):
    out = []
    for s1, e1 in a:
        for s2, e2 in b:
            s, e = max(s1, s2), min(e1, e2)
            if s < e:
                out.append((s, e))
    return merge(out)


def intersect_all(interval_lists):
    if not interval_lists:
        return []
    result = merge(interval_lists[0])
    for lst in interval_lists[1:]:
        if not result:
            return []
        result = intersect2(result, lst)
    return result


# ---------- marker classification ----------

def classify(title):
    title = title or ""
    for marker in ("FD", "SD"):
        if title.startswith(marker):
            return marker, title == marker
    return None, None


# ---------- Google Calendar (read-only, plain REST for the calls; the
# official google-auth library handles tokens, since hand-rolling OAuth
# refresh/bootstrap is exactly the kind of fiddly code that library exists
# to avoid) ----------

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def load_google_credentials(token_path, credentials_path):
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

    if not creds or not creds.valid:
        if not credentials_path.exists():
            sys.exit(
                f"No Google credentials found ({credentials_path} is missing). "
                "Download an OAuth Desktop app client JSON from Google Cloud "
                "Console and save it there."
            )
        if not sys.stdin.isatty():
            sys.exit(
                f"No valid Google token at {token_path}. Run this command once "
                "interactively (not from cron) to authorize Google Calendar access."
            )
        print("No valid Google token found; opening a browser to authorize access...")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    return creds


def fetch_all_day_events(calendar_id, token_path, credentials_path, days):
    creds = load_google_credentials(token_path, credentials_path)
    now = datetime.now(timezone.utc)
    url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
    params = {
        "timeMin": now.isoformat(),
        "timeMax": (now + timedelta(days=days)).isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    headers = {"Authorization": f"Bearer {creds.token}"}
    events = []
    while True:
        resp = requests.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        events.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        params["pageToken"] = page_token
    return [e for e in events if "date" in e.get("start", {})]


def expand_dates(event):
    start = date.fromisoformat(event["start"]["date"])
    end_str = event.get("end", {}).get("date")
    if not end_str:
        return [start]
    end = date.fromisoformat(end_str)  # exclusive, per Google's all-day event convention
    out, d = [], start
    while d < end:
        out.append(d)
        d += timedelta(days=1)
    return out or [start]


# ---------- Cal.com API v2 ----------

class CalCom:
    def __init__(self, api_key, base_url, api_version):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {api_key}", "cal-api-version": api_version}
        )

    def _request(self, method, path, **kwargs):
        r = self.session.request(method, f"{self.base_url}{path}", **kwargs)
        if not r.ok:
            sys.exit(f"Cal.com API error {r.status_code} on {method} {path}:\n{r.text}")
        return r.json()["data"]

    def list_schedules(self):
        return self._request("GET", "/schedules")

    def get_schedule(self, schedule_id):
        return self._request("GET", f"/schedules/{schedule_id}")

    def set_overrides(self, schedule_id, overrides):
        return self._request("PATCH", f"/schedules/{schedule_id}", json={"overrides": overrides})

    def resolve_schedule(self, schedule_id):
        if schedule_id is not None:
            return self.get_schedule(schedule_id)
        schedules = self.list_schedules()
        if not schedules:
            sys.exit("No Cal.com schedules found for this account.")
        return next((s for s in schedules if s.get("isDefault")), schedules[0])


def normalize_day(value):
    if isinstance(value, str):
        return DAY_NAMES.index(value)
    return (value - 1) % 7  # Cal.com numeric days are JS-style: 0=Sunday


def normal_availability(schedule):
    by_day = defaultdict(list)
    for block in schedule.get("availability", []):
        start, end = parse_hhmm(block["startTime"]), parse_hhmm(block["endTime"])
        for raw_day in block.get("days", []):
            by_day[normalize_day(raw_day)].append((start, end))
    return {day: merge(v) for day, v in by_day.items()}


# ---------- sync engine ----------

def collect_markers(events):
    markers = defaultdict(set)
    for event in events:
        marker, is_standard = classify((event.get("summary") or "").strip())
        if marker is None:
            continue
        if not is_standard:
            event_date = event.get("start", {}).get("date")
            print(f"WARNING: non-standard {marker} marker on {event_date}: {event.get('summary')!r}", file=sys.stderr)
        for d in expand_dates(event):
            markers[d.isoformat()].add(marker)
    return markers


def desired_intervals(markers, normal):
    result = {}
    for date_str, marker_types in markers.items():
        if "FD" in marker_types and "SD" in marker_types:
            print(f"WARNING: both FD and SD on {date_str}; intersecting both rules", file=sys.stderr)
        weekday = date.fromisoformat(date_str).weekday()
        rule_lists = [[RULES[t]] for t in marker_types]
        result[date_str] = intersect_all(rule_lists + [normal.get(weekday, [])])
    return result


def to_override_entries(date_str, intervals):
    if not intervals:
        # Cal.com requires startTime/endTime as strings on every override
        # entry, even when isUnavailable is set; 00:00-00:00 is a harmless
        # placeholder since isUnavailable is what actually takes effect.
        return [{"date": date_str, "startTime": "00:00", "endTime": "00:00", "isUnavailable": True}]
    return [{"date": date_str, "startTime": format_hhmm(s), "endTime": format_hhmm(e)} for s, e in intervals]


def entry_key(entries):
    def key_for(e):
        # Cal.com's GET response doesn't reliably echo the isUnavailable
        # flag back; a zero-length time range means the same thing.
        if e.get("isUnavailable") or e.get("startTime") == e.get("endTime"):
            return ("u",)
        return ("r", e["startTime"], e["endTime"])

    return sorted(key_for(e) for e in entries)


def diff_and_merge(existing, desired):
    existing_by_date = defaultdict(list)
    for entry in existing:
        existing_by_date[entry.get("date")].append(entry)

    created, updated, unchanged, new_by_date = [], [], [], {}
    for date_str, intervals in desired.items():
        entries = to_override_entries(date_str, intervals)
        new_by_date[date_str] = entries
        old = existing_by_date.get(date_str, [])
        if not old:
            created.append(date_str)
        elif entry_key(old) != entry_key(entries):
            updated.append(date_str)
        else:
            unchanged.append(date_str)

    managed = set(desired)
    final = [e for e in existing if e.get("date") not in managed]
    for date_str in sorted(managed):
        final.extend(new_by_date[date_str])

    return final, sorted(created), sorted(updated), sorted(unchanged)


def describe(intervals):
    if not intervals:
        return "fully unavailable"
    return ", ".join(f"{format_hhmm(s)}-{format_hhmm(e)}" for s, e in intervals)


def run(config, dry_run):
    print(f"Reading '{config['google_calendar_id']}' for the next {config['days']} days...")
    events = fetch_all_day_events(
        config["google_calendar_id"], config["token_path"], config["credentials_path"], config["days"]
    )
    markers = collect_markers(events)
    print(f"{len(markers)} date(s) with FD/SD markers.")

    cal = CalCom(config["api_key"], config["api_base"], config["api_version"])
    schedule = cal.resolve_schedule(config["schedule_id"])
    print(f"Cal.com schedule: {schedule.get('name')} (id={schedule['id']})")

    desired = desired_intervals(markers, normal_availability(schedule))
    final, created, updated, unchanged = diff_and_merge(schedule.get("overrides", []), desired)

    def label(d):
        return "+".join(sorted(markers[d]))

    created_set, updated_set = set(created), set(updated)
    for d in sorted(desired):
        if d in created_set:
            print(f"  TRIM {d} to {describe(desired[d])} due to {label(d)}")
        elif d in updated_set:
            print(f"  TRIM {d} to {describe(desired[d])} due to {label(d)} (replacing existing override)")
        else:
            print(f"  OK   {d} already {describe(desired[d])} due to {label(d)}")

    if not created and not updated:
        print("No changes needed.")
        return
    if dry_run:
        print(f"Dry run: {len(created) + len(updated)} change(s) would be written. Nothing sent.")
        return
    cal.set_overrides(schedule["id"], final)
    print(f"Wrote {len(created) + len(updated)} change(s) to Cal.com.")


# ---------- config & CLI ----------

def load_config(path, days_override):
    path = Path(path).expanduser()
    if not path.exists():
        sys.exit(f"Config file not found: {path}. Copy config.example.json there first.")
    data = json.loads(path.read_text())
    if "google_calendar_id" not in data:
        sys.exit("Config is missing required key 'google_calendar_id'.")
    api_key = os.environ.get("CALCOM_API_KEY") or data.get("calcom_api_key")
    if not api_key:
        sys.exit("Set CALCOM_API_KEY, or add 'calcom_api_key' to the config file.")
    return {
        "google_calendar_id": data["google_calendar_id"],
        "schedule_id": data.get("calcom_schedule_id"),
        "days": days_override or data.get("sync_window_days", 90),
        "api_base": data.get("calcom_api_base", "https://api.cal.com/v2"),
        "api_version": data.get("calcom_api_version", "2024-06-11"),
        "api_key": api_key,
        "token_path": Path(data.get("token_path", DEFAULT_TOKEN)).expanduser(),
        "credentials_path": Path(data.get("credentials_path", DEFAULT_CREDENTIALS)).expanduser(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--days", type=int, default=None, help="Override sync_window_days from config")
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes without writing them")
    args = parser.parse_args()
    config = load_config(args.config, args.days)
    run(config, args.dry_run)


if __name__ == "__main__":
    main()
