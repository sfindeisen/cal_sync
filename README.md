# cal_sync

Syncs Cal.com date overrides from FD/SD all-day events in a Google Calendar.

- `FD` -> available 17:00-23:59 that day
- `SD` -> available 00:00-13:00 that day
- Always intersected with your normal Cal.com weekly hours (fetched live,
  never hardcoded), so it only restricts availability, never expands it.
- Both FD and SD on one date: both rules applied (warning printed).
- Non-`FD`/`SD`-exact titles still count, with a warning (e.g. `FD - note`).
- Existing overrides for dates this tool doesn't manage are left alone.
  Nothing is ever deleted.

## Files

- `cal_sync.py` - the tool. Run this regularly (e.g. via cron).
- `config.example.json` - copy and edit.
- `requirements.txt` - dependencies.
- `test_cal_sync.py` - tests.

## Setup

```bash
pip install -r requirements.txt --break-system-packages
```

### 1. Google Calendar

Place your OAuth client file at `~/.config/cal_sync/credentials.json`
(Google Cloud Console -> Desktop app -> download JSON).

You don't need a `token.json` yet. The first time you run `cal_sync.py`
interactively (i.e. not from cron) with no token present, it opens a
browser for you to grant read-only Calendar access, then saves
`~/.config/cal_sync/token.json` automatically. Every run after that just
uses the saved token, refreshing it silently when it expires. If you ever
run it from cron before that first interactive run, it will exit with a
clear message asking you to run it interactively once first instead of
hanging. `cal_sync.py` only ever reads from this calendar, it never writes
to it.

### 2. Cal.com API key

```bash
export CALCOM_API_KEY="cal_live_..."
```

(Or put `calcom_api_key` in config.json instead.)

### 3. Config

```bash
cp config.example.json ~/.config/cal_sync/config.json
```

Edit `google_calendar_id` (any calendar your account can read, regardless
of owner) and `calcom_schedule_id` (leave `null` for your default schedule).

## Usage

```bash
python3 cal_sync.py --dry-run     # preview changes
python3 cal_sync.py               # apply changes
python3 cal_sync.py --days 30 --config /path/to/config.json
```

## Tests

```bash
python3 -m unittest test_cal_sync -v
```

## Notes

- Multiple time ranges on one date are sent as multiple override entries
  sharing the same `date`. Not yet confirmed against a live account; if
  Cal.com rejects it, `--dry-run` plus the error message (now includes the
  full API response body) will show exactly what to adjust.
- A date with zero remaining availability is sent as
  `{"date": ..., "startTime": "00:00", "endTime": "00:00", "isUnavailable": true}`.
  Confirmed against a live 400 response: Cal.com requires `startTime`/
  `endTime` as strings on every override entry, even unavailable ones.
