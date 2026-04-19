# SMS MySchool Homework Scraper

Scrapes `sms.eursc.eu` for your child's homework, upcoming tests, course diary, and schedule, and writes structured JSON/CSV for easy viewing or automation (Home Assistant, cron, etc).

## What gets scraped

| File | Source page | Contents |
|------|-------------|----------|
| `output/homework.json/.csv` | Course Diary (Assignments filter) | Upcoming homework + tests, with due date and course |
| `output/course_diary.json/.csv` | Course Diary (default view) | What was covered in each class |
| `output/tests.json/.csv` | Graded Exercises | Grades already recorded (often empty early in year) |
| `output/schedule.json/.csv` | Schedule | Calendar events (classes + all-day assignment markers) |
| `output/summary.json` | — | Run summary + **new items since last run** |
| `output/run_log.txt` | — | Append-only log of each run |

## Installation

```bash
cd sms-scraper
python3 -m venv venv
source venv/bin/activate
pip install playwright pandas openpyxl python-dotenv requests schedule
python -m playwright install chromium
```

## Configure credentials

Edit `.env`:

```
SMS_USERNAME=parent_email@example.com
SMS_PASSWORD=yourpassword
HA_WEBHOOK_URL=          # optional, leave blank to disable
```

> `.env` is gitignored. Never commit it.

## Run once

```bash
source venv/bin/activate
python scraper.py
```

Outputs land in `output/`.

## Run on a schedule (09:00 + 17:00 daily)

**Option A — macOS launchd (recommended, survives reboots):**

```bash
./install_schedule.sh
```

This installs `~/Library/LaunchAgents/com.sms.scraper.plist` which runs the scraper every day at 09:00 and 17:00.

To uninstall: `launchctl unload ~/Library/LaunchAgents/com.sms.scraper.plist`

**Option B — foreground scheduler (requires a terminal to stay open):**

```bash
python run_daily.py
```

Runs immediately, then every day at 09:00 and 17:00 local time.

## Web dashboard

```bash
source venv/bin/activate
python app.py
# open http://127.0.0.1:5055
```

Features:
- Upcoming / next-7-days / past views with subject filter and "hide done" toggle
- Per-assignment done checkbox and free-form notes (persisted in `output/_ui_state.json`)
- Test entries highlighted, today's items highlighted yellow, 1-3 days out highlighted blue
- One-click **Run scrape now** button (runs in background; page auto-reflects when done)
- Tabs for Course Diary, Schedule, and Graded Exercises
- JSON status endpoint at `/api/status`

## First-run inspection (already done)

`inspect_sms.py` and `inspect_pages.py` logged in, mapped the nav, and dumped HTML/screenshots used to author the selectors. You can re-run them any time the site layout changes.

## Home Assistant integration

Set `HA_WEBHOOK_URL` in `.env` to an HA webhook, e.g. `http://homeassistant.local:8123/api/webhook/sms_homework`. Each run POSTs:

```json
{
  "homework_count": 246,
  "tests_count": 0,
  "new_items": [ { "date": "11/05/2026", "subject": "S5L2-FRA", "description": "Lecture 3 - Test" } ],
  "next_due": "2026-04-24"
}
```

Wire it up in HA as a `webhook` trigger and fire a notification / calendar event.

## Notes

- Session is cached in `session.json` — subsequent runs skip the login form if still valid.
- Assignments filter toggles the Course Diary view to show only upcoming/assignment-tagged entries (this is what shows future tests).
- The scraper dedupes on `subject + date + hash(description)` and tracks a `_prev_keys.json` to identify new items per run.
- Site language is auto-handled (EN/FR/DE/IT/ES content flows through unchanged — selectors are language-agnostic).
- 1–2s random delays are inserted between navigations.
# sms-dashboard
