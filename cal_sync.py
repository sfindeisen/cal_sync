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


# ---------- Google Calendar (read-only, plain REST) ----------

def refresh_google_token(info, token_path, credentials_path):
    refresh_token = info.get("refresh_token")
    if not refresh_token:
        sys.exit("Google token expired and has no refresh_token; re-authorize (see README).")
    client_id, client_secret = info.get("client_id"), info.get("client_secret")
    if (not client_id or not client_secret) and credentials_path.exists():
        installed = json.loads(credentials_path.read_text()).get("installed", {})
        client_id = client_id or installed.get("client_id")
        client_secret = client_secret or installed.get("client_secret")
    resp = requests.post(
        info.get("token_uri", "https://oauth2.googleapis.com/token"),
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    resp.raise_for_status()
    info["token"] = resp.json()["access_token"]
    token_path.write_text(json.dumps(info))
    return info["token"]


def google_get(url, params, token_path, credentials_path):
    if not token_path.exists():
        sys.exit(f"Google token not found: {token_path} (see README).")
    info = json.loads(token_path.read_text())
    token = info.get("token") or info.get("access_token")
    resp = requests.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code == 401:
        token = refresh_google_token(info, token_path, credentials_path)
        resp = requests.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json()


def fetch_all_day_events(calendar_id, token_path, credentials_path, days):
    now = datetime.now(timezone.utc)
    url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
    params = {
        "timeMin": now.isoformat(),
        "timeMax": (now + timedelta(days=days)).isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    events = []
    while True:
        data = google_get(url, params, token_path, credentials_path)
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

    def list_schedules(self):
        r = self.session.get(f"{self.base_url}/schedules")
        r.raise_for_status()
        return r.json()["data"]

    def get_schedule(self, schedule_id):
        r = self.session.get(f"{self.base_url}/schedules/{schedule_id}")
        r.raise_for_status()
        return r.json()["data"]

    def set_overrides(self, schedule_id, overrides):
        r = self.session.patch(f"{self.base_url}/schedules/{schedule_id}", json={"overrides": overrides})
        r.raise_for_status()
        return r.json()["data"]

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
            print(f"WARNING: non-standard {marker} marker: {event.get('summary')!r}", file=sys.stderr)
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
        return [{"date": date_str, "isUnavailable": True}]
    return [{"date": date_str, "startTime": format_hhmm(s), "endTime": format_hhmm(e)} for s, e in intervals]


def entry_key(entries):
    return sorted(("u",) if e.get("isUnavailable") else ("r", e["startTime"], e["endTime"]) for e in entries)


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

    for d in created:
        print(f"  CREATE {d}: {describe(desired[d])}")
    for d in updated:
        print(f"  UPDATE {d}: {describe(desired[d])}")
    for d in unchanged:
        print(f"  OK     {d}: unchanged")

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
