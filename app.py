"""Web UI for managing SMS scraper data."""
import html as _html
import json
import subprocess
import sys
import threading
from datetime import datetime, date
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request, url_for

try:
    from dotenv import load_dotenv as _ld
    _ld()
except Exception:
    pass

import db as _db

ROOT = Path(__file__).parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)
STATE = OUT / "_ui_state.json"

import os as _os
CLOUD_MODE = bool(_os.environ.get("PORT"))  # Railway/Heroku set PORT
INGEST_TOKEN = _os.environ.get("INGEST_TOKEN", "")

app = Flask(__name__)
_scrape_lock = threading.Lock()
_last_scrape = {"status": "idle", "started": None, "finished": None, "output": ""}

# ---------- data ----------

def load_json(name, default):
    p = OUT / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def load_state():
    """Legacy JSON state (kept as fallback if Supabase is unreachable)."""
    if not STATE.exists():
        return {"done": {}, "notes": {}}
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"done": {}, "notes": {}}


def save_state(s):
    STATE.write_text(json.dumps(s, indent=2, ensure_ascii=False))


USE_SUPABASE = _db.get_client() is not None


def parse_date(s):
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except Exception:
        return None


def enrich(items, state, today):
    out = []
    for it in items:
        d = parse_date(it.get("date", ""))
        days = (d - today).days if d else None
        out.append({
            **it,
            "iso_date": d.isoformat() if d else "",
            "weekday": d.strftime("%a") if d else "",
            "pretty_date": d.strftime("%a %d %b") if d else it.get("date", ""),
            "days_until": days,
            "is_upcoming": d is not None and d >= today,
            "is_today": d == today,
            "is_overdue": d is not None and d < today and not state["done"].get(it.get("key", ""), False),
            "done": state["done"].get(it.get("key", ""), False),
            "note": state["notes"].get(it.get("key", ""), ""),
        })
    return out


def _neg_iso(iso: str) -> str:
    """Return a string that sorts descending when compared ascending."""
    # invert each digit so larger dates sort first under ascending sort
    trans = str.maketrans("0123456789", "9876543210")
    return iso.translate(trans)


def _load_from_json(today):
    state = load_state()
    assignments = enrich(load_json("homework.json", []), state, today)
    diary = enrich(load_json("course_diary.json", []), state, today)
    tests = load_json("tests.json", [])
    schedule = load_json("schedule.json", [])
    summary = load_json("summary.json", {})
    # continue with the same sort logic below (returns tuple via caller path)
    def _sort_key(x):
        iso = x["iso_date"]
        if not iso:
            return (2, "")
        if x["is_upcoming"] or x["is_overdue"]:
            return (0, iso)
        return (1, _neg_iso(iso))
    assignments.sort(key=_sort_key)
    diary.sort(key=lambda x: (x["iso_date"] == "", x["iso_date"]), reverse=True)
    diary.sort(key=lambda x: x["iso_date"] == "")
    return assignments, diary, tests, schedule, summary


def _db_row_to_entry(row: dict) -> dict:
    """Map a row from entries_with_state view to the dict shape the UI expects."""
    raw_date = row.get("entry_date_text") or ""
    if not raw_date and row.get("entry_date"):
        try:
            raw_date = datetime.strptime(row["entry_date"], "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            raw_date = row["entry_date"]
    return {
        "key": row.get("entry_key"),
        "subject": row.get("subject") or "",
        "date": raw_date,
        "type": row.get("entry_type") or "",
        "description": row.get("description") or "",
        "attachments": row.get("attachments") or [],
        "_done_from_db": row.get("done", False),
        "_note_from_db": row.get("note", ""),
    }


def _db_row_to_test(row: dict) -> dict:
    d = row.get("test_date")
    raw = ""
    if d:
        try:
            raw = datetime.strptime(d, "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            raw = d
    return {
        "date": raw,
        "subject": row.get("subject", ""),
        "test_type": row.get("test_type", ""),
        "description": row.get("description", ""),
        "weight": row.get("weight", ""),
        "grade": row.get("grade", ""),
    }


def _db_row_to_sched(row: dict) -> dict:
    return {
        "start": row.get("start_time", ""),
        "title": row.get("title", ""),
        "text": row.get("details", ""),
    }


def _enrich_db(items, today):
    """Like enrich() but pulls done/note from the row itself."""
    out = []
    for it in items:
        d = parse_date(it.get("date", ""))
        days = (d - today).days if d else None
        done = bool(it.get("_done_from_db"))
        out.append({
            **it,
            "iso_date": d.isoformat() if d else "",
            "weekday": d.strftime("%a") if d else "",
            "pretty_date": d.strftime("%a %d %b") if d else it.get("date", ""),
            "days_until": days,
            "is_upcoming": d is not None and d >= today,
            "is_today": d == today,
            "is_overdue": d is not None and d < today and not done,
            "done": done,
            "note": it.get("_note_from_db") or "",
        })
    return out


def load_all():
    today = date.today()
    if USE_SUPABASE:
        try:
            rows = _db.fetch_entries()
            assignments = _enrich_db([_db_row_to_entry(r) for r in rows if r.get("kind") == "assignment"], today)
            diary = _enrich_db([_db_row_to_entry(r) for r in rows if r.get("kind") == "course_diary"], today)
            tests = [_db_row_to_test(r) for r in _db.fetch_tests()]
            schedule = [_db_row_to_sched(r) for r in _db.fetch_schedule()]
            summary = {}
        except Exception as e:
            print(f"[!] Supabase read failed, falling back to JSON: {e}")
            return _load_from_json(today)
    # Upcoming first (soonest due date at top), then past items most-recent first, undated last.
    def _sort_key(x):
        iso = x["iso_date"]
        if not iso:
            return (2, "")  # undated last
        if x["is_upcoming"] or x["is_overdue"]:
            return (0, iso)  # ascending: closest due date first
        return (1, _neg_iso(iso))  # past items: most recent first
    assignments.sort(key=_sort_key)
    diary.sort(key=lambda x: (x["iso_date"] == "", x["iso_date"]), reverse=True)
    diary.sort(key=lambda x: x["iso_date"] == "")
    return assignments, diary, tests, schedule, summary


# ---------- helpers ----------

def _courses_cached():
    """Cached { course_code: {teachers, description} } for UI enrichment."""
    global _COURSES_CACHE
    try:
        if _COURSES_CACHE is None:
            _COURSES_CACHE = _db.fetch_courses() if USE_SUPABASE else {}
    except Exception:
        _COURSES_CACHE = {}
    return _COURSES_CACHE


_COURSES_CACHE = None


def teacher_label(subject_code: str) -> str:
    c = _courses_cached().get(subject_code)
    if not c:
        return ""
    tchs = c.get("teachers") or []
    if not tchs:
        return ""
    first = tchs[0]
    name = first.get("name", "")
    # Keep last name only for compact display
    parts = name.replace(",", "").split()
    short = parts[-1] if parts else name
    if len(tchs) > 1:
        short = f"{short} +{len(tchs)-1}"
    return short


def unread_message_count() -> int:
    if not USE_SUPABASE:
        return 0
    try:
        return _db.get_client().table("messages").select("id", count="exact").eq("unread", True).limit(1).execute().count or 0
    except Exception:
        return 0


def _count_summary(assignments):
    in_week = lambda a: a["days_until"] is not None and 0 <= a["days_until"] <= 7 and not a["done"]
    return {
        "upcoming": sum(1 for a in assignments if a["is_upcoming"] and not a["done"]),
        "today": sum(1 for a in assignments if a["is_today"] and not a["done"]),
        "week": sum(1 for a in assignments if in_week(a)),
        "tests_week": sum(1 for a in assignments if in_week(a) and is_test_entry(a)),
        "total": len(assignments),
        "done": sum(1 for a in assignments if a["done"]),
        "overdue": sum(1 for a in assignments if a["is_overdue"]),
    }


def _last_run():
    # Prefer Supabase scrape_runs (works on cloud)
    if USE_SUPABASE:
        try:
            r = _db.fetch_last_run()
            if r and r.get("started_at"):
                # friendly format: "YYYY-MM-DD HH:MM"
                ts = r["started_at"].replace("T", " ")[:16]
                return ts
        except Exception:
            pass
    # Fallback: local run_log.txt
    log = OUT / "run_log.txt"
    if not log.exists():
        return None
    try:
        last = log.read_text().strip().splitlines()[-1]
        return last.split("] ")[0].lstrip("[")
    except Exception:
        return None


# Stable colour per subject code
def subject_hue(subject: str) -> int:
    h = 0
    for ch in subject:
        h = (h * 31 + ord(ch)) & 0xFFFF
    return h % 360


import re as _re
# Match word-start (not mid-word) for test-indicating stems, allowing any suffix.
_TEST_RE = _re.compile(
    r"(?:^|[^\w])("
    r"test|exam|examen|quiz|interrog|evalua|\u00e9valua|"
    r"control|contr\u00f4le|pr\u00fcfung|klausur|klassenarbeit|"
    r"verifica|prueba|esame"
    r")",
    _re.IGNORECASE | _re.UNICODE,
)

def is_test_entry(a) -> bool:
    text = f"{a.get('description','')} {a.get('subject','')}"
    return bool(_TEST_RE.search(text))


def esc(s):
    return _html.escape(s or "", quote=True)


# ---------- templates ----------

BASE_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500&display=swap');

:root{
  --bg:#fafaf7;            /* warm paper */
  --surface:#ffffff;
  --surface-2:#f4f3ee;
  --text:#1a1a1c;
  --text-muted:#6b7280;
  --text-soft:#9ca3af;
  --border:#e8e6df;
  --border-strong:#d4d2ca;
  --accent:#4338ca;         /* deep indigo */
  --accent-2:#7c3aed;       /* violet companion */
  --accent-hover:#3730a3;
  --accent-soft:#eef2ff;
  --danger:#b91c1c;
  --danger-soft:#fef2f2;
  --warn:#b45309;
  --warn-soft:#fffbeb;
  --ok:#047857;
  --ok-soft:#ecfdf5;
  --info:#0369a1;
  --info-soft:#f0f9ff;
  --shadow-sm:0 1px 2px rgba(20,22,28,.04), 0 1px 3px rgba(20,22,28,.05);
  --shadow-md:0 6px 20px -4px rgba(20,22,28,.09), 0 2px 6px rgba(20,22,28,.04);
  --shadow-lg:0 18px 40px -12px rgba(20,22,28,.14);
  --radius:12px;
  --radius-sm:8px;
  --sidebar-w:240px;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0d0f14;
    --surface:#151821;
    --surface-2:#1d212d;
    --text:#e8eaed;
    --text-muted:#9ca3af;
    --text-soft:#6b7280;
    --border:#232734;
    --border-strong:#363b4a;
    --accent:#a5b4fc;
    --accent-2:#c4b5fd;
    --accent-hover:#c7d2fe;
    --accent-soft:#1e1b4b;
    --danger:#fca5a5;
    --danger-soft:#3a1717;
    --warn:#fcd34d;
    --warn-soft:#3a2a0e;
    --ok:#6ee7b7;
    --ok-soft:#0c3a28;
    --info:#7dd3fc;
    --info-soft:#0d2838;
    --shadow-sm:0 1px 2px rgba(0,0,0,.4);
    --shadow-md:0 8px 24px rgba(0,0,0,.4);
    --shadow-lg:0 20px 48px rgba(0,0,0,.6);
  }
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  background:var(--bg); color:var(--text); line-height:1.55;
  -webkit-font-smoothing:antialiased; font-feature-settings:"cv11","ss01";
  letter-spacing:-0.005em;
}
a{color:var(--accent); text-decoration:none}
a:hover{color:var(--accent-hover)}
h1,h2,h3,h4{margin:0; letter-spacing:-0.02em; font-weight:600; color:var(--text)}

/* layout — sidebar + main */
.app{display:grid; grid-template-columns:var(--sidebar-w) 1fr; min-height:100vh}
.sidebar{
  position:sticky; top:0; height:100vh; background:var(--surface);
  border-right:1px solid var(--border); display:flex; flex-direction:column;
  padding:1.25rem 0.85rem 1rem;
}
.main{padding:1.25rem 1.75rem 3.5rem; max-width:1100px; width:100%; margin:0 auto}

/* brand */
.brand{display:flex; align-items:center; gap:.65rem; padding:0 .35rem 1rem; border-bottom:1px solid var(--border); margin-bottom:.85rem}
.brand .logo{
  width:34px; height:34px; border-radius:10px; display:grid; place-items:center; font-size:1.15rem;
  background:linear-gradient(135deg, var(--accent), var(--accent-2)); color:#fff;
  box-shadow:0 2px 8px rgba(67,56,202,.3);
}
.brand .brand-name{font-weight:700; font-size:.95rem; letter-spacing:-0.01em}
.brand .brand-sub{color:var(--text-muted); font-size:.72rem; font-weight:500}

/* nav links */
nav.side{display:flex; flex-direction:column; gap:1px; flex:1}
nav.side a{
  display:flex; align-items:center; gap:.7rem; padding:.55rem .65rem; font-size:.875rem;
  font-weight:500; color:var(--text-muted); border-radius:var(--radius-sm); transition:all .12s;
}
nav.side a:hover{background:var(--surface-2); color:var(--text)}
nav.side a.active{background:var(--accent-soft); color:var(--accent)}
nav.side a.active .ico{color:var(--accent)}
nav.side a .ico{width:16px; height:16px; color:var(--text-soft); transition:color .12s; flex-shrink:0}
nav.side a:hover .ico{color:var(--text)}
nav.side a .count{
  margin-left:auto; padding:.05rem .45rem; font-size:.7rem; font-weight:600;
  background:var(--surface-2); border-radius:999px; color:var(--text-muted); min-width:22px; text-align:center;
}
nav.side a.active .count{background:var(--accent); color:#fff}

/* sidebar footer */
.side-footer{border-top:1px solid var(--border); padding-top:.7rem; margin-top:.4rem}
.status-line{display:flex; align-items:center; gap:.45rem; font-size:.75rem; color:var(--text-muted); padding:.3rem .6rem; margin-bottom:.5rem}
.status-dot{width:7px; height:7px; border-radius:50%; background:var(--ok); flex-shrink:0}
.status-dot.running{background:var(--warn); animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1} 50%{opacity:.35}}

/* page header */
.page-head{display:flex; align-items:flex-end; justify-content:space-between; margin-bottom:1.5rem; gap:1rem; flex-wrap:wrap}
.page-head h1{font-size:1.65rem; line-height:1.15}
.page-head .sub{color:var(--text-muted); font-size:.9rem; margin-top:.2rem}

/* buttons */
.btn{
  display:inline-flex; align-items:center; gap:.5rem; padding:.5rem .95rem;
  background:var(--accent); color:#fff; border:0; border-radius:var(--radius-sm);
  font-family:inherit; font-size:.875rem; font-weight:500; cursor:pointer;
  transition:all .15s; box-shadow:var(--shadow-sm);
}
.btn:hover{background:var(--accent-hover); transform:translateY(-1px); box-shadow:var(--shadow-md)}
.btn:active{transform:translateY(0)}
.btn:disabled{opacity:.5; cursor:not-allowed; transform:none}
.btn.ghost{background:transparent; color:var(--text); border:1px solid var(--border-strong); box-shadow:none}
.btn.ghost:hover{background:var(--surface-2); transform:none; box-shadow:none}
.btn .ico{width:15px; height:15px}
.btn.sm{padding:.35rem .65rem; font-size:.8rem}

/* stats grid */
.stats{display:grid; grid-template-columns:repeat(auto-fit, minmax(128px, 1fr)); gap:.65rem; margin-bottom:1.5rem}
.stat{
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  padding:.8rem 1rem; box-shadow:var(--shadow-sm); position:relative; overflow:hidden;
  transition:all .15s;
}
.stat:hover{border-color:var(--border-strong); transform:translateY(-1px)}
.stat .v{font-size:1.75rem; font-weight:700; letter-spacing:-.035em; line-height:1; font-variant-numeric:tabular-nums}
.stat .l{font-size:.72rem; color:var(--text-muted); margin-top:.3rem; text-transform:uppercase; letter-spacing:.07em; font-weight:600}
.stat.danger::before,.stat.warn::before,.stat.info::before,.stat.ok::before{
  content:""; position:absolute; left:0; top:0; bottom:0; width:3px; border-radius:3px 0 0 3px;
}
.stat.danger::before{background:var(--danger)}
.stat.warn::before{background:var(--warn)}
.stat.info::before{background:var(--info)}
.stat.ok::before{background:var(--ok)}
.stat.danger .v{color:var(--danger)}
.stat.warn .v{color:var(--warn)}
.stat.ok .v{color:var(--ok)}
.stat.info .v{color:var(--info)}

/* filters */
.filters{
  display:flex; gap:.6rem; flex-wrap:wrap; align-items:center; margin-bottom:1.25rem;
  background:transparent; padding:0;
}
.seg{display:inline-flex; background:var(--surface); padding:3px; border-radius:9px; gap:2px; border:1px solid var(--border); box-shadow:var(--shadow-sm)}
.seg a{
  padding:.4rem .85rem; font-size:.825rem; color:var(--text-muted); border-radius:6px; font-weight:500;
  transition:all .12s;
}
.seg a:hover{color:var(--text)}
.seg a.on{background:var(--accent); color:#fff; box-shadow:0 1px 3px rgba(67,56,202,.3)}
.filters select, .filters input[type=text]{
  padding:.45rem .7rem; font-size:.85rem; background:var(--surface); color:var(--text);
  border:1px solid var(--border); border-radius:var(--radius-sm); outline:none;
  font-family:inherit; box-shadow:var(--shadow-sm); transition:border-color .12s;
}
.filters select:focus, .filters input:focus{border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft)}
.filters .toggle{display:inline-flex; align-items:center; gap:.4rem; font-size:.85rem; color:var(--text-muted); cursor:pointer; padding:.4rem .7rem; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm); box-shadow:var(--shadow-sm)}
.filters .toggle input{accent-color:var(--accent)}

/* card list */
.card-list{display:flex; flex-direction:column; gap:.55rem}
.card{
  position:relative; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  padding:.95rem 1.1rem .95rem 1.25rem; display:grid; grid-template-columns:auto 96px 1fr; gap:.9rem;
  align-items:start; box-shadow:var(--shadow-sm); transition:all .15s;
}
.card::before{content:""; position:absolute; left:0; top:10px; bottom:10px; width:3px; border-radius:3px; background:transparent; transition:background .15s}
.card:hover{border-color:var(--border-strong); box-shadow:var(--shadow-md); transform:translateY(-1px)}
.card.done{opacity:.5}
.card.done .desc, .card.done .subj{text-decoration:line-through; text-decoration-thickness:1.5px}
.card.today::before{background:var(--warn)}
.card.overdue::before{background:var(--danger)}
.card.overdue{background:linear-gradient(90deg, var(--danger-soft) 0%, var(--surface) 40%)}
.card.soon::before{background:var(--info)}

.check{
  width:22px; height:22px; border:2px solid var(--border-strong); border-radius:7px;
  background:var(--surface); cursor:pointer; display:grid; place-items:center; padding:0; margin-top:2px;
  transition:all .15s;
}
.check:hover{border-color:var(--accent); transform:scale(1.05)}
.check.checked{background:var(--accent); border-color:var(--accent); color:#fff}
.check.checked::after{content:""; width:11px; height:6px; border-left:2px solid currentColor; border-bottom:2px solid currentColor; transform:rotate(-45deg) translate(1px,-1px)}

.when{display:flex; flex-direction:column; gap:.1rem; padding-top:2px}
.when .date{font-size:.72rem; color:var(--text-muted); font-weight:600; text-transform:uppercase; letter-spacing:.05em}
.when .days{font-size:1rem; font-weight:600; letter-spacing:-.015em; font-variant-numeric:tabular-nums}
.when.today .days{color:var(--warn)}
.when.overdue .days{color:var(--danger)}
.when.soon .days{color:var(--info)}
.when.later .days{color:var(--text)}

.body{display:flex; flex-direction:column; gap:.45rem; min-width:0}
.meta{display:flex; gap:.5rem; align-items:center; flex-wrap:wrap}
.subj{
  display:inline-flex; align-items:center; gap:.3rem; font-size:.7rem; font-weight:700;
  padding:.2rem .55rem; border-radius:5px; letter-spacing:.03em;
  background:hsl(var(--h) 70% 94%); color:hsl(var(--h) 55% 28%);
  font-family:'JetBrains Mono',ui-monospace,monospace;
}
.teacher{font-size:.78rem; color:var(--text-muted); font-weight:500}

/* messages */
.msg-card{
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  padding:.85rem 1rem; box-shadow:var(--shadow-sm); transition:border-color .15s, box-shadow .15s;
}
.msg-card:hover{border-color:var(--border-strong); box-shadow:var(--shadow-md)}
.msg-card.unread{border-left:3px solid var(--accent); padding-left:calc(1rem - 3px); background:linear-gradient(90deg, var(--accent-soft), var(--surface) 40%)}
.msg-meta{display:flex; justify-content:space-between; align-items:center; font-size:.8rem; color:var(--text-muted); margin-bottom:.25rem}
.msg-sender{font-weight:600; color:var(--text)}
.msg-subject{margin:.1rem 0 .3rem; font-size:.95rem; font-weight:600; color:var(--text); line-height:1.35}
.msg-card.unread .msg-subject{color:var(--accent)}
.msg-excerpt{font-size:.85rem; color:var(--text-muted); line-height:1.45; max-height:3.3em; overflow:hidden}

/* grades / term reports */
.year-heading{margin:1rem 0 .5rem; font-size:.9rem; color:var(--text-muted); font-weight:600; letter-spacing:.03em; text-transform:uppercase}
.report-grid{display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:.5rem; margin-bottom:1rem}
.report-card{
  display:flex; align-items:center; gap:.75rem; padding:.75rem 1rem; background:var(--surface);
  border:1px solid var(--border); border-radius:var(--radius); text-decoration:none; color:var(--text);
  box-shadow:var(--shadow-sm); transition:all .15s;
}
.report-card:hover{border-color:var(--accent); background:var(--accent-soft); transform:translateY(-1px)}
.report-icon{font-size:1.6rem; line-height:1}
.report-label{font-size:.9rem; font-weight:600; color:var(--text)}
.report-sub{font-size:.75rem; color:var(--text-muted); margin-top:2px}
.muted-small{color:var(--text-muted); font-size:.82rem; max-width:320px}
@media (prefers-color-scheme: dark){
  .subj{background:hsl(var(--h) 40% 20%); color:hsl(var(--h) 80% 78%)}
}
.pill{
  display:inline-flex; align-items:center; gap:.25rem; padding:.15rem .5rem; border-radius:5px;
  font-size:.68rem; font-weight:700; letter-spacing:.05em; text-transform:uppercase;
}
.pill.test{background:var(--danger-soft); color:var(--danger)}
.pill.assign{background:var(--accent-soft); color:var(--accent)}
.pill.diary{background:var(--info-soft); color:var(--info)}

.desc{font-size:.935rem; color:var(--text); line-height:1.55; white-space:pre-wrap; word-wrap:break-word}
.desc a{color:var(--accent); text-decoration:underline; text-underline-offset:2px}
.atts{display:flex; flex-wrap:wrap; gap:.4rem; margin-top:.15rem}
.atts .att{
  display:inline-flex; align-items:center; gap:.35rem; font-size:.78rem; padding:.3rem .6rem;
  background:var(--surface-2); color:var(--text); border:1px solid var(--border);
  border-radius:7px; text-decoration:none; transition:all .12s; font-weight:500;
}
.atts .att:hover{background:var(--accent-soft); border-color:var(--accent); color:var(--accent); transform:translateY(-1px)}
.note-input{
  margin-top:.35rem; font-size:.82rem; padding:.4rem .6rem; background:transparent; color:var(--text);
  border:1px dashed var(--border-strong); border-radius:6px; width:100%; font-family:inherit;
  transition:all .12s;
}
.note-input:hover{background:var(--surface-2)}
.note-input:focus{outline:none; border-color:var(--accent); border-style:solid; background:var(--surface); box-shadow:0 0 0 3px var(--accent-soft)}
.note-input::placeholder{color:var(--text-soft)}

.actions{display:flex; align-items:start}

/* tables (diary, schedule, tests) */
.data-table{
  width:100%; border-collapse:collapse; background:var(--surface);
  border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; box-shadow:var(--shadow-sm);
}
.data-table th, .data-table td{
  padding:.7rem .95rem; text-align:left; border-bottom:1px solid var(--border); font-size:.9rem; vertical-align:top;
}
.data-table th{
  background:var(--surface-2); font-size:.7rem; font-weight:700; text-transform:uppercase;
  color:var(--text-muted); letter-spacing:.08em;
}
.data-table tr:last-child td{border-bottom:0}
.data-table tr:hover td{background:var(--surface-2)}

/* empty state */
.empty{
  text-align:center; padding:4rem 1rem; background:var(--surface); border:1px dashed var(--border-strong);
  border-radius:var(--radius); color:var(--text-muted);
}
.empty .icon{font-size:2.75rem; margin-bottom:.7rem; opacity:.7}
.empty h3{margin:.25rem 0; color:var(--text); font-weight:600; font-size:1.05rem}
.empty p{margin:.3rem 0 0; font-size:.9rem; max-width:360px; margin-left:auto; margin-right:auto}

/* toast */
.toast{
  position:fixed; bottom:1.25rem; right:1.25rem; padding:.75rem 1.15rem; border-radius:var(--radius);
  background:var(--surface); color:var(--text); border:1px solid var(--border); font-size:.88rem; font-weight:500;
  box-shadow:var(--shadow-lg); animation:slideUp .3s ease, fadeOut .4s 3.5s forwards; z-index:100;
  display:flex; align-items:center; gap:.5rem; max-width:420px;
}
.toast::before{content:""; width:8px; height:8px; border-radius:50%; background:var(--ok)}
.toast.err{color:var(--danger)}
.toast.err::before{background:var(--danger)}
@keyframes slideUp{from{transform:translateY(12px); opacity:0} to{transform:none; opacity:1}}
@keyframes fadeOut{to{opacity:0; transform:translateY(-6px)}}

/* responsive */
@media (max-width:840px){
  .app{grid-template-columns:1fr}
  .sidebar{
    position:fixed; bottom:0; left:0; right:0; top:auto; height:auto; z-index:20;
    flex-direction:row; padding:.4rem .5rem; border-top:1px solid var(--border); border-right:0;
    box-shadow:var(--shadow-lg);
  }
  .brand, .side-footer, .status-line{display:none}
  nav.side{flex-direction:row; justify-content:space-around; overflow-x:auto; gap:.1rem}
  nav.side a{flex-direction:column; gap:.15rem; padding:.4rem .5rem; font-size:.68rem; text-align:center; flex:1; min-width:0}
  nav.side a .count{margin-left:0; font-size:.65rem; padding:0 .3rem}
  .main{padding:1rem 1rem 5rem}
  .page-head h1{font-size:1.35rem}
  .stats{grid-template-columns:repeat(auto-fit, minmax(108px, 1fr))}
  .card{grid-template-columns:auto 1fr; gap:.65rem}
  .card .when{grid-column:2; order:2}
  .card .body{grid-column:1/-1; order:3}
}

form.inline{display:inline; margin:0}
"""

SHELL = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} · SMS Dashboard</title>
<style>{{ css | safe }}</style>
</head><body>

<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <div class="logo">📚</div>
      <div>
        <div class="brand-name">SMS</div>
        <div class="brand-sub">Philippe · B4 S5</div>
      </div>
    </div>

    <nav class="side">
      <a href="{{ url_for('home') }}" class="{% if view=='home' %}active{% endif %}" title="Homework">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
        <span>Homework</span>
        {% if counts.upcoming %}<span class="count">{{ counts.upcoming }}</span>{% endif %}
      </a>
      <a href="{{ url_for('messages_view') }}" class="{% if view=='messages' %}active{% endif %}" title="Messages">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
        <span>Messages</span>
        {% if unread %}<span class="count">{{ unread }}</span>{% endif %}
      </a>
      <a href="{{ url_for('diary_view') }}" class="{% if view=='diary' %}active{% endif %}" title="Course Diary">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
        <span>Course Diary</span>
      </a>
      <a href="{{ url_for('schedule_view') }}" class="{% if view=='schedule' %}active{% endif %}" title="Schedule">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
        <span>Schedule</span>
      </a>
      <a href="{{ url_for('grades_view') }}" class="{% if view=='grades' %}active{% endif %}" title="Grades">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2z"/></svg>
        <span>Grades</span>
      </a>
      <a href="{{ url_for('files_view') }}" class="{% if view=='files' %}active{% endif %}" title="Files">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        <span>Files</span>
      </a>
      <a href="{{ url_for('tests_view') }}" class="{% if view=='tests' %}active{% endif %}" title="Exercises">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
        <span>Exercises</span>
      </a>
    </nav>

    <div class="side-footer">
      <div class="status-line">
        <span class="status-dot {% if scraping %}running{% endif %}"></span>
        <span>{% if scraping %}Scraping…{% elif last_run %}Updated {{ last_run }}{% else %}No data yet{% endif %}</span>
      </div>
      <form class="inline" action="{{ url_for('scrape_now') }}" method="post" style="display:block; padding:0 .25rem">
        <button class="btn ghost sm" {% if scraping %}disabled{% endif %} style="width:100%; justify-content:center">
          {% if scraping %}
            <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            Running
          {% else %}
            <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
            Refresh
          {% endif %}
        </button>
      </form>
    </div>
  </aside>

  <main class="main">
    {% if flash %}<div class="toast {% if flash_cls=='err' %}err{% endif %}">{{ flash }}</div>{% endif %}

    <div class="page-head">
      <div>
        <h1>{{ page_title or title }}</h1>
        {% if page_sub %}<div class="sub">{{ page_sub }}</div>{% endif %}
      </div>
    </div>

    {% if show_stats %}
    <div class="stats">
      <div class="stat {% if counts.overdue %}danger{% endif %}"><div class="v">{{ counts.overdue }}</div><div class="l">Overdue</div></div>
      <div class="stat {% if counts.today %}warn{% endif %}"><div class="v">{{ counts.today }}</div><div class="l">Due today</div></div>
      <div class="stat info"><div class="v">{{ counts.week }}</div><div class="l">This week</div></div>
      <div class="stat {% if counts.tests_week %}danger{% endif %}"><div class="v">{{ counts.tests_week }}</div><div class="l">Tests</div></div>
      <div class="stat"><div class="v">{{ counts.upcoming }}</div><div class="l">Upcoming</div></div>
    </div>
    {% endif %}

    {{ body | safe }}
  </main>
</div>

<script>
// poll status; reload when a run completes
(function(){
  let tries = 0;
  let wasRunning = {{ 'true' if scraping else 'false' }};
  const poll = () => {
    fetch('/api/status').then(r=>r.json()).then(s=>{
      if (s.scraping) {
        wasRunning = true;
        if (++tries < 180) setTimeout(poll, 5000);
      } else if (wasRunning) {
        location.reload();
      } else if (++tries < 12) {
        // not running yet but user just clicked — check a few more times
        setTimeout(poll, 3000);
      }
    }).catch(()=>setTimeout(poll, 6000));
  };
  setTimeout(poll, 3000);
})();
// enhance note inputs: save on blur via fetch, no redirect
document.querySelectorAll('.note-input').forEach(i => {
  i.addEventListener('blur', () => {
    const key = i.dataset.key;
    const fd = new FormData();
    fd.append('key', key); fd.append('note', i.value);
    fetch('/set-note', {method:'POST', body:fd});
  });
});
// checkbox toggle without full redirect
document.querySelectorAll('.check').forEach(b => {
  b.addEventListener('click', async e => {
    e.preventDefault();
    const key = b.dataset.key;
    const fd = new FormData(); fd.append('key', key);
    const r = await fetch('/toggle-done', {method:'POST', body:fd});
    if(r.ok){
      b.classList.toggle('checked');
      b.closest('.card').classList.toggle('done');
    }
  });
});
</script>
</body></html>
"""


PAGE_TITLES = {
    "home":     ("Homework",         "What's due and what's done"),
    "messages": ("Messages",         "Announcements from teachers and school"),
    "diary":    ("Course Diary",     "Teachers' in-class notes, by subject"),
    "schedule": ("Schedule",         "Classes and all-day events this week"),
    "grades":   ("Grades",           "Term reports going back through the years"),
    "files":    ("Files",            "All attachments across diary, homework, and messages"),
    "tests":    ("Graded Exercises", "Scores recorded by teachers"),
}

def render(title, view, body, counts=None, show_stats=False, flash=None, flash_cls="ok"):
    scraping = _scrape_lock.locked()
    if CLOUD_MODE and not scraping:
        try:
            run = _gh_latest_run()
            if run.get("status") in ("queued", "in_progress"):
                scraping = True
        except Exception:
            pass
    page_title, page_sub = PAGE_TITLES.get(view, (title, ""))
    return render_template_string(
        SHELL, title=title, view=view, body=body, css=BASE_CSS,
        counts=counts or {"upcoming": 0, "today": 0, "week": 0, "total": 0, "done": 0,
                          "overdue": 0, "tests_week": 0},
        show_stats=show_stats, scraping=scraping, last_run=_last_run(),
        student="ANAMAN, Philippe · Bruxelles IV",
        flash=flash, flash_cls=flash_cls,
        cloud_mode=CLOUD_MODE,
        unread=unread_message_count(),
        page_title=page_title, page_sub=page_sub,
    )


def render_card(a):
    done_cls = "done" if a["done"] else ""
    state_cls = ""
    when_cls = "later"
    days = a["days_until"]
    if a["done"]:
        pass
    elif a["is_overdue"]:
        state_cls, when_cls = "overdue", "overdue"
    elif a["is_today"]:
        state_cls, when_cls = "today", "today"
    elif days is not None and 1 <= days <= 3:
        state_cls, when_cls = "soon", "soon"

    if days is None:
        days_lbl = "—"
    elif a["is_today"]:
        days_lbl = "Today"
    elif days == 1:
        days_lbl = "Tomorrow"
    elif days < 0:
        days_lbl = f"{-days}d ago"
    else:
        days_lbl = f"in {days}d"

    hue = subject_hue(a.get("subject", ""))
    check_cls = "check checked" if a["done"] else "check"
    pill = '<span class="pill test">🔥 Test</span>' if is_test_entry(a) else ''
    teacher = teacher_label(a.get("subject", ""))
    teacher_html = f'<span class="teacher">· {esc(teacher)}</span>' if teacher else ''
    desc = esc(a.get("description", ""))

    # Render attachments
    atts_html = ""
    atts = a.get("attachments") or []
    if atts:
        chips = []
        for att in atts:
            icon = {"file": "📎", "image": "🖼", "link": "🔗"}.get(att.get("kind"), "🔗")
            name = esc(att.get("name", "attachment"))[:60]
            href = esc(att.get("href", "#"))
            chips.append(f'<a class="att" href="{href}" target="_blank" rel="noopener">{icon} {name}</a>')
        atts_html = '<div class="atts">' + "".join(chips) + "</div>"

    return f"""
    <div class="card {done_cls} {state_cls}">
      <button class="{check_cls}" data-key="{esc(a['key'])}" aria-label="Toggle done"></button>
      <div class="when {when_cls}">
        <div class="date">{esc(a['pretty_date'])}</div>
        <div class="days">{days_lbl}</div>
      </div>
      <div class="body">
        <div class="meta">
          <span class="subj" style="--h:{hue}">{esc(a.get('subject',''))}</span>
          {teacher_html}
          {pill}
        </div>
        <div class="desc">{desc}</div>
        {atts_html}
        <input class="note-input" type="text" data-key="{esc(a['key'])}"
               value="{esc(a.get('note',''))}" placeholder="Add a note…">
      </div>
    </div>
    """


# ---------- routes ----------

@app.route("/")
def home():
    assignments, _, _, _, _ = load_all()
    show = request.args.get("show", "upcoming")
    subject = request.args.get("subject", "")
    hide_done = request.args.get("hide_done") == "1"

    subjects = sorted({a["subject"] for a in assignments if a["subject"]})
    rows = assignments
    if show == "upcoming":
        rows = [a for a in rows if a["is_upcoming"] or a["is_overdue"]]
    elif show == "week":
        rows = [a for a in rows if a["days_until"] is not None and -1 <= a["days_until"] <= 7]
    elif show == "past":
        rows = [a for a in rows if not a["is_upcoming"] and not a["is_overdue"]]
    if subject:
        rows = [a for a in rows if a["subject"] == subject]
    if hide_done:
        rows = [a for a in rows if not a["done"]]

    qbase = {"subject": subject, "hide_done": "1" if hide_done else ""}
    def seg(val, lbl):
        on = "on" if show == val else ""
        q = {**qbase, "show": val}
        qs = "&".join(f"{k}={esc(v)}" for k, v in q.items() if v)
        return f'<a class="{on}" href="?{qs}">{lbl}</a>'

    filters = [
        '<div class="filters">',
        '<div class="seg">',
        seg("upcoming", "Upcoming"),
        seg("week", "Next 7 days"),
        seg("past", "Past"),
        seg("all", "All"),
        '</div>',
        '<form method="get" style="display:inline-flex; gap:.5rem; align-items:center">',
        f'<input type="hidden" name="show" value="{esc(show)}">',
        '<select name="subject" onchange="this.form.submit()">',
        f'<option value="">All subjects ({len(subjects)})</option>',
    ]
    for s in subjects:
        sel = "selected" if subject == s else ""
        filters.append(f'<option value="{esc(s)}" {sel}>{esc(s)}</option>')
    filters.append('</select>')
    chk = "checked" if hide_done else ""
    filters.append(
        f'<label class="toggle"><input type="checkbox" name="hide_done" value="1" {chk}'
        f' onchange="this.form.submit()"> Hide done</label></form></div>'
    )

    if not rows:
        cards = """
        <div class="empty">
          <div class="icon">✨</div>
          <h3>All caught up</h3>
          <p>No assignments match this filter.</p>
        </div>"""
    else:
        cards = '<div class="card-list">' + "".join(render_card(a) for a in rows) + '</div>'

    body = "".join(filters) + cards
    return render("Homework", "home", body, _count_summary(assignments), show_stats=True,
                  flash=request.args.get("flash"), flash_cls=request.args.get("cls", "ok"))


@app.route("/diary")
def diary_view():
    assignments, diary, _, _, _ = load_all()
    subjects = sorted({d["subject"] for d in diary if d["subject"]})
    subject = request.args.get("subject", "")
    rows = [d for d in diary if not subject or d["subject"] == subject]

    filters = ['<div class="filters"><form method="get" style="display:inline-flex; gap:.5rem">',
               '<select name="subject" onchange="this.form.submit()">',
               f'<option value="">All subjects ({len(subjects)})</option>']
    for s in subjects:
        sel = "selected" if subject == s else ""
        filters.append(f'<option value="{esc(s)}" {sel}>{esc(s)}</option>')
    filters.append('</select></form></div>')

    if not rows:
        tbl = '<div class="empty"><div class="icon">📖</div><h3>No diary entries</h3></div>'
    else:
        tr = []
        for d in rows:
            hue = subject_hue(d.get("subject", ""))
            atts = d.get("attachments") or []
            atts_html = ""
            if atts:
                chips = []
                for att in atts:
                    icon = {"file": "📎", "image": "🖼", "link": "🔗"}.get(att.get("kind"), "🔗")
                    chips.append(
                        f'<a class="att" href="{esc(att.get("href","#"))}" target="_blank" rel="noopener">'
                        f'{icon} {esc(att.get("name",""))[:50]}</a>'
                    )
                atts_html = '<div class="atts" style="margin-top:.4rem">' + "".join(chips) + "</div>"
            tr.append(
                f'<tr><td style="white-space:nowrap">{esc(d["pretty_date"])}</td>'
                f'<td><span class="subj" style="--h:{hue}">{esc(d["subject"])}</span></td>'
                f'<td><div style="white-space:pre-wrap">{esc(d.get("description",""))}</div>{atts_html}</td></tr>'
            )
        tbl = ('<table class="data-table"><thead><tr><th>Date</th><th>Subject</th><th>Content</th></tr>'
               '</thead><tbody>' + "".join(tr) + '</tbody></table>')
    return render("Course Diary", "diary", "".join(filters) + tbl, _count_summary(assignments))


@app.route("/schedule")
def schedule_view():
    assignments, _, _, schedule, _ = load_all()
    if not schedule:
        body = '<div class="empty"><div class="icon">📅</div><h3>No schedule items</h3></div>'
    else:
        tr = []
        for s in schedule:
            tr.append(
                f'<tr><td style="white-space:nowrap">{esc(s.get("start",""))}</td>'
                f'<td><strong>{esc(s.get("title",""))}</strong></td>'
                f'<td>{esc(s.get("text",""))[:400]}</td></tr>'
            )
        body = ('<table class="data-table"><thead><tr><th>Start</th><th>Title</th><th>Details</th></tr>'
                '</thead><tbody>' + "".join(tr) + '</tbody></table>')
    return render("Schedule", "schedule", body, _count_summary(assignments))


@app.route("/tests")
def tests_view():
    assignments, _, tests, _, _ = load_all()
    if not tests:
        body = ('<div class="empty"><div class="icon">💯</div>'
                '<h3>No graded exercises yet</h3>'
                '<p>Grades will appear here once teachers record them.</p></div>')
    else:
        tr = []
        for t in tests:
            tr.append(
                f'<tr><td>{esc(t.get("date",""))}</td><td>{esc(t.get("subject",""))}</td>'
                f'<td>{esc(t.get("test_type",""))}</td><td>{esc(t.get("description",""))}</td>'
                f'<td>{esc(t.get("weight",""))}</td><td><strong>{esc(t.get("grade",""))}</strong></td></tr>'
            )
        body = ('<table class="data-table"><thead><tr><th>Date</th><th>Subject</th><th>Type</th>'
                '<th>Description</th><th>Weight</th><th>Grade</th></tr></thead><tbody>'
                + "".join(tr) + '</tbody></table>')
    return render("Graded Exercises", "tests", body, _count_summary(assignments))


@app.route("/files")
def files_view():
    """Aggregate all file attachments across entries + messages."""
    assignments, _, _, _, _ = load_all()
    rows: list[dict] = []
    if USE_SUPABASE:
        sb = _db.get_client()
        # entries with attachments
        entries = sb.table("entries").select(
            "kind,subject,entry_date,entry_date_text,description,attachments"
        ).range(0, 9999).execute().data or []
        for e in entries:
            for a in (e.get("attachments") or []):
                if not a.get("name") or not a.get("href"):
                    continue
                rows.append({
                    "source": "diary" if e["kind"] == "course_diary" else "assignment",
                    "subject": e.get("subject", ""),
                    "date": e.get("entry_date_text") or e.get("entry_date") or "",
                    "iso_date": e.get("entry_date") or "",
                    "context": (e.get("description") or "")[:120],
                    "name": a.get("name", ""),
                    "href": a.get("href", ""),
                    "kind": a.get("kind", "file"),
                })
        # messages with attachments
        msgs = sb.table("messages").select(
            "subject,sender,sent_label,sent_date,attachments"
        ).range(0, 999).execute().data or []
        for m in msgs:
            for a in (m.get("attachments") or []):
                if not a.get("name") or not a.get("href"):
                    continue
                rows.append({
                    "source": "message",
                    "subject": m.get("sender", ""),
                    "date": m.get("sent_label", "") or m.get("sent_date", ""),
                    "iso_date": m.get("sent_date") or "",
                    "context": (m.get("subject") or "")[:120],
                    "name": a.get("name", ""),
                    "href": a.get("href", ""),
                    "kind": "file",
                })

    q = (request.args.get("q") or "").lower().strip()
    src = request.args.get("src", "")
    if src:
        rows = [r for r in rows if r["source"] == src]
    if q:
        rows = [r for r in rows
                if q in r["name"].lower() or q in r["context"].lower() or q in r["subject"].lower()]
    # Sort latest first
    rows.sort(key=lambda r: r.get("iso_date") or "", reverse=True)

    # Filter UI
    filters = ['<div class="filters">']
    filters.append('<div class="seg">')
    for val, lbl in [("", f"All ({len(rows)})"), ("diary", "Diary"),
                     ("assignment", "Homework"), ("message", "Messages")]:
        on = "on" if src == val else ""
        qs = f"src={val}" if val else ""
        if q: qs = (qs + f"&q={esc(q)}") if qs else f"q={esc(q)}"
        filters.append(f'<a class="{on}" href="?{qs}">{lbl}</a>')
    filters.append('</div>')
    filters.append(f'<form method="get" style="flex:1; display:flex; gap:.4rem">')
    if src: filters.append(f'<input type="hidden" name="src" value="{esc(src)}">')
    filters.append(f'<input type="text" name="q" value="{esc(q)}" placeholder="Search filename, context, subject…" style="flex:1">')
    filters.append('<button class="btn" type="submit">Search</button></form></div>')

    if not rows:
        body = '<div class="empty"><div class="icon">📁</div><h3>No files yet</h3><p>Files will appear here once scraped.</p></div>'
    else:
        body_rows = []
        src_pill = {
            "diary": '<span class="pill diary">DIARY</span>',
            "assignment": '<span class="pill assign">HOMEWORK</span>',
            "message": '<span class="pill" style="background:var(--warn-soft); color:var(--warn)">MESSAGE</span>',
        }
        icons = {"file": "📎", "image": "🖼", "link": "🔗"}
        for r in rows:
            ic = icons.get(r["kind"], "📎")
            hue = subject_hue(r["subject"])
            body_rows.append(
                f'<tr>'
                f'<td style="white-space:nowrap">{esc(r["date"])}</td>'
                f'<td>{src_pill.get(r["source"], "")}</td>'
                f'<td><span class="subj" style="--h:{hue}">{esc(r["subject"])}</span></td>'
                f'<td><a class="att" href="{esc(r["href"])}" target="_blank" rel="noopener">{ic} {esc(r["name"][:80])}</a></td>'
                f'<td class="muted-small">{esc(r["context"])}</td>'
                f'</tr>'
            )
        body = (
            '<table class="data-table"><thead><tr>'
            '<th>Date</th><th>From</th><th>Subject</th><th>File</th><th>Context</th>'
            '</tr></thead><tbody>' + "".join(body_rows) + '</tbody></table>'
        )

    return render("Files", "files", "".join(filters) + body, _count_summary(assignments))


@app.route("/messages")
def messages_view():
    assignments, _, _, _, _ = load_all()
    msgs = _db.fetch_messages() if USE_SUPABASE else []
    only_unread = request.args.get("unread") == "1"
    if only_unread:
        msgs = [m for m in msgs if m.get("unread")]

    filters = [
        '<div class="filters"><div class="seg">',
        f'<a class="{"on" if not only_unread else ""}" href="?">All</a>',
        f'<a class="{"on" if only_unread else ""}" href="?unread=1">Unread</a>',
        '</div></div>',
    ]

    if not msgs:
        body = '<div class="empty"><div class="icon">📬</div><h3>Inbox empty</h3><p>No announcements scraped yet.</p></div>'
        return render("Messages", "messages", "".join(filters) + body, _count_summary(assignments))

    cards = []
    for m in msgs:
        sender = esc(m.get("sender") or "—")
        subj = esc(m.get("subject") or "")
        sent = esc(m.get("sent_label") or "")
        excerpt = esc(m.get("excerpt") or "")[:300]
        unread_cls = "unread" if m.get("unread") else ""
        atts = m.get("attachments") or []
        chips = []
        for a in atts:
            name = esc((a.get("name") or "")[:60])
            href = esc(a.get("href") or "#")
            if not name or not href or href == "#":
                continue
            chips.append(f'<a class="att" href="{href}" target="_blank" rel="noopener">📎 {name}</a>')
        atts_html = f'<div class="atts">{"".join(chips)}</div>' if chips else ""
        cards.append(f"""
        <div class="msg-card {unread_cls}">
          <div class="msg-meta">
            <span class="msg-sender">{sender}</span>
            <span class="msg-date">{sent}</span>
          </div>
          <h4 class="msg-subject">{subj}</h4>
          {f'<div class="msg-excerpt">{excerpt}</div>' if excerpt else ''}
          {atts_html}
        </div>""")
    body = "".join(filters) + '<div class="card-list">' + "".join(cards) + '</div>'
    return render("Messages", "messages", body, _count_summary(assignments))


@app.route("/grades")
def grades_view():
    assignments, _, _, _, _ = load_all()
    reports = _db.fetch_term_reports() if USE_SUPABASE else []
    if not reports:
        body = ('<div class="empty"><div class="icon">📊</div>'
                '<h3>No term reports yet</h3>'
                '<p>The scraper will pick them up as they are published.</p></div>')
        return render("Grades", "grades", body, _count_summary(assignments))

    # group by year_label
    from collections import defaultdict
    by_year = defaultdict(list)
    for r in reports:
        by_year[r.get("year_label") or "—"].append(r)

    sections = []
    for year in sorted(by_year.keys(), reverse=True):
        rows = by_year[year]
        items = []
        for r in rows:
            items.append(
                f'<a class="report-card" href="{esc(r.get("download_url") or "#")}" target="_blank" rel="noopener">'
                f'<div class="report-icon">📄</div>'
                f'<div><div class="report-label">{esc(r.get("label") or "")}</div>'
                f'<div class="report-sub">Click to download PDF</div></div>'
                f'</a>'
            )
        sections.append(
            f'<h3 class="year-heading">{esc(year)}</h3>'
            f'<div class="report-grid">{"".join(items)}</div>'
        )
    body = "".join(sections)
    return render("Grades", "grades", body, _count_summary(assignments))


@app.post("/toggle-done")
def toggle_done():
    key = request.form.get("key", "")
    if USE_SUPABASE:
        try:
            new_val = _db.toggle_done(key)
            return jsonify(ok=True, done=new_val)
        except Exception as e:
            print(f"[!] Supabase toggle_done failed: {e}")
    # fallback
    s = load_state()
    s["done"][key] = not s["done"].get(key, False)
    save_state(s)
    return jsonify(ok=True, done=s["done"][key])


@app.post("/set-note")
def set_note():
    key = request.form.get("key", "")
    note = request.form.get("note", "").strip()
    if USE_SUPABASE:
        try:
            _db.set_note(key, note)
            return jsonify(ok=True)
        except Exception as e:
            print(f"[!] Supabase set_note failed: {e}")
    s = load_state()
    if note:
        s["notes"][key] = note
    else:
        s["notes"].pop(key, None)
    save_state(s)
    return jsonify(ok=True)


def _run_scrape_bg():
    with _scrape_lock:
        _last_scrape["status"] = "running"
        _last_scrape["started"] = datetime.now().isoformat(timespec="seconds")
        try:
            r = subprocess.run(
                [sys.executable, str(ROOT / "scraper.py")],
                capture_output=True, text=True, timeout=600,
            )
            _last_scrape["output"] = (r.stdout or "")[-1000:] + "\n" + (r.stderr or "")[-500:]
            _last_scrape["status"] = "ok" if r.returncode == 0 else "error"
        except Exception as e:
            _last_scrape["status"] = "error"
            _last_scrape["output"] = str(e)
        _last_scrape["finished"] = datetime.now().isoformat(timespec="seconds")


GITHUB_TOKEN = _os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = _os.environ.get("GITHUB_REPO", "fanaman74/sms-dashboard").strip()
GITHUB_WORKFLOW = _os.environ.get("GITHUB_WORKFLOW", "scrape.yml").strip()


def _gh_trigger_run() -> tuple[bool, str]:
    """Dispatch the scrape workflow on GitHub Actions."""
    import requests as _rq
    if not GITHUB_TOKEN:
        return False, "GITHUB_TOKEN not configured"
    try:
        r = _rq.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW}/dispatches",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": "main"},
            timeout=15,
        )
        if r.status_code == 204:
            return True, "dispatched"
        return False, f"{r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def _gh_latest_run() -> dict:
    import requests as _rq
    if not GITHUB_TOKEN:
        return {}
    try:
        r = _rq.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW}/runs",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            params={"per_page": 1},
            timeout=10,
        )
        data = r.json()
        runs = data.get("workflow_runs") or []
        if not runs:
            return {}
        run = runs[0]
        return {
            "id": run.get("id"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "html_url": run.get("html_url"),
            "created_at": run.get("created_at"),
        }
    except Exception:
        return {}


@app.post("/scrape-now")
def scrape_now():
    if CLOUD_MODE:
        ok, msg = _gh_trigger_run()
        if ok:
            return redirect(url_for("home", flash="GitHub Actions scrape started — refresh in ~2 min.", cls="ok"))
        return redirect(url_for("home", flash=f"Could not trigger: {msg}", cls="err"))
    if _scrape_lock.locked():
        return redirect(url_for("home", flash="Scrape already running", cls="err"))
    threading.Thread(target=_run_scrape_bg, daemon=True).start()
    return redirect(url_for("home", flash="Fetching fresh data…", cls="ok"))


@app.post("/api/ingest")
def api_ingest():
    """Accept a data bundle from the local scraper and overwrite output files."""
    token = request.headers.get("X-Ingest-Token", "")
    if not INGEST_TOKEN or token != INGEST_TOKEN:
        return jsonify(ok=False, error="unauthorized"), 401
    try:
        payload = request.get_json(force=True)
    except Exception as e:
        return jsonify(ok=False, error=f"bad json: {e}"), 400
    if not isinstance(payload, dict):
        return jsonify(ok=False, error="expected object"), 400

    OUT.mkdir(exist_ok=True)
    allowed = {
        "homework.json", "course_diary.json", "tests.json",
        "schedule.json", "summary.json",
    }
    written = []
    for name, body in payload.items():
        if name not in allowed:
            continue
        (OUT / name).write_text(json.dumps(body, ensure_ascii=False, indent=2))
        written.append(name)
    with (OUT / "run_log.txt").open("a") as f:
        f.write(f"[{datetime.now().isoformat(timespec='seconds')}] ingested {written} "
                f"from {request.remote_addr}\n")
    return jsonify(ok=True, written=written)


@app.get("/api/status")
def api_status():
    if CLOUD_MODE:
        run = _gh_latest_run()
        running = run.get("status") in ("queued", "in_progress")
        return jsonify({
            "scraping": running,
            "source": "github_actions",
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "html_url": run.get("html_url"),
            "started": run.get("created_at"),
        })
    return jsonify({"scraping": _scrape_lock.locked(), "source": "local", **_last_scrape})


if __name__ == "__main__":
    import os
    port_raw = os.environ.get("PORT", "").strip() or "5055"
    port = int(port_raw)
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    app.run(host=host, port=port, debug=False)
