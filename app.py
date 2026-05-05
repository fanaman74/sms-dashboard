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

BASE_CSS = r"""
:root{
  --paper:#f3ead8;
  --paper-2:#ebdfc4;
  --surface:#fdf6e6;
  --surface-2:#f5ecd5;
  --terracotta:#d96b3a;
  --rust:#b14b22;
  --moss:#6e8e4e;
  --teal:#2f6b75;
  --plum:#6f3a4e;
  --ink:#2a2218;
  --ink-2:#4a3f2e;
  --ink-3:#7a6c54;
  --hair:#c8b890;
  --sand:#d8c594;
  /* semantic aliases */
  --bg:var(--paper);
  --text:var(--ink);
  --text-muted:var(--ink-3);
  --text-soft:var(--sand);
  --border:var(--ink);
  --border-light:var(--hair);
  --accent:var(--terracotta);
  --accent-hover:var(--rust);
  --accent-soft:#fce8db;
  --danger:var(--rust);
  --danger-soft:#ffe6dd;
  --warn:#9a6c00;
  --warn-soft:#fff3d9;
  --ok:var(--moss);
  --ok-soft:#dff0d4;
  --info:var(--teal);
  --info-soft:#dceef0;
  --shadow-sm:3px 3px 0 var(--ink);
  --shadow-md:5px 5px 0 var(--ink);
  --radius:18px;
  --radius-sm:999px;
  --display:'Fraunces',Georgia,serif;
  --hand:'Caveat','Comic Sans MS',cursive;
  --body:'Plus Jakarta Sans',-apple-system,system-ui,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  font-family:var(--body);
  background:var(--paper); color:var(--ink); line-height:1.55;
  -webkit-font-smoothing:antialiased;
}
body::before{
  content:""; position:fixed; inset:0; pointer-events:none; z-index:1;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='220' height='220'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.78' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 0.08 0'/></filter><rect width='100%25' height='100%25' filter='url(%23n)'/></svg>");
  mix-blend-mode:multiply; opacity:.5;
}
#root-wrap{position:relative; z-index:2}
a{color:var(--terracotta); text-decoration:none}
a:hover{color:var(--rust)}
h1,h2,h3,h4{margin:0; font-family:var(--display); font-weight:600; letter-spacing:-0.02em; color:var(--ink)}

/* layout */
.app{min-height:100vh; display:flex; flex-direction:column}
.topbar{
  position:sticky; top:0; z-index:10;
  background:var(--paper); border-bottom:2px solid var(--ink);
}
.topbar-inner{
  max-width:1200px; margin:0 auto; padding:.85rem 1.5rem;
  display:flex; justify-content:space-between; align-items:center; gap:1rem;
}
.main{padding:1.75rem 1.5rem 4rem; max-width:1200px; width:100%; margin:0 auto}

/* brand */
.brand{display:flex; align-items:center; gap:.75rem}
.brand .logo{
  width:40px; height:40px; border-radius:999px; display:grid; place-items:center; font-size:1.1rem;
  background:var(--terracotta); color:#fff; border:2px solid var(--ink); box-shadow:var(--shadow-sm);
}
.brand .brand-name{font-family:var(--display); font-weight:600; font-size:1.1rem; letter-spacing:-0.02em; color:var(--ink)}
.brand .brand-sub{font-family:var(--hand); color:var(--ink-3); font-size:.9rem; line-height:1}

/* nav tabs */
nav.tabs{
  max-width:1200px; margin:0 auto; padding:0 1.5rem;
  display:flex; gap:0; border-bottom:2px solid var(--ink);
  overflow-x:auto; scrollbar-width:none;
}
nav.tabs::-webkit-scrollbar{display:none}
nav.tabs a{
  display:inline-flex; align-items:center; gap:.45rem;
  padding:.65rem .9rem; font-size:.82rem; font-weight:700; color:var(--ink-3);
  border-bottom:3px solid transparent; margin-bottom:-2px; transition:color .12s;
  white-space:nowrap; letter-spacing:.01em;
}
nav.tabs a:hover{color:var(--ink)}
nav.tabs a.active{color:var(--terracotta); border-bottom-color:var(--terracotta)}
nav.tabs a .ico{width:14px; height:14px; flex-shrink:0}
nav.tabs a .count{
  margin-left:.2rem; padding:.1rem .45rem; font-size:.68rem; font-weight:700;
  background:var(--paper-2); border-radius:999px; color:var(--ink-3);
  border:1px solid var(--hair);
}
nav.tabs a.active .count{background:var(--accent-soft); color:var(--terracotta); border-color:var(--terracotta)}

/* topbar status */
.topbar-status{display:flex; align-items:center; gap:.5rem; font-size:.8rem; color:var(--ink-3)}
.status-dot{width:8px; height:8px; border-radius:50%; background:var(--moss); flex-shrink:0; border:1.5px solid var(--ink)}
.status-dot.running{background:var(--warn); animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1} 50%{opacity:.35}}

/* page header */
.page-head{display:flex; align-items:flex-end; justify-content:space-between; margin-bottom:1.5rem; gap:1rem; flex-wrap:wrap}
.page-head h1{font-size:2rem; line-height:1}
.page-head .sub{font-family:var(--hand); color:var(--ink-3); font-size:1rem; margin-top:.2rem; font-weight:700}

/* buttons */
.btn{
  display:inline-flex; align-items:center; gap:.45rem; padding:.55rem 1rem;
  background:var(--terracotta); color:#fff; border:2px solid var(--ink); border-radius:999px;
  box-shadow:var(--shadow-sm);
  font-family:var(--body); font-size:.83rem; font-weight:700; cursor:pointer;
  transition:transform .08s, box-shadow .08s;
}
.btn:hover{transform:translate(-1px,-1px); box-shadow:4px 4px 0 var(--ink)}
.btn:active{transform:translate(2px,2px); box-shadow:1px 1px 0 var(--ink)}
.btn:disabled{opacity:.5; cursor:not-allowed; transform:none !important; box-shadow:var(--shadow-sm) !important}
.btn.ghost{background:var(--surface); color:var(--ink)}
.btn.ghost:hover{background:var(--paper-2)}
.btn .ico{width:14px; height:14px}
.btn.sm{padding:.3rem .65rem; font-size:.78rem}

/* stats grid */
.stats{display:grid; grid-template-columns:repeat(5,1fr); gap:.75rem; margin-bottom:1.5rem}
.stat{
  background:var(--surface); border:2px solid var(--ink); border-radius:var(--radius);
  box-shadow:var(--shadow-sm);
  padding:1rem 1.25rem .85rem; position:relative;
  transition:transform .08s, box-shadow .08s; cursor:pointer; user-select:none;
  text-decoration:none; display:block; color:inherit;
}
.stat:hover{transform:translate(-1px,-1px); box-shadow:4px 4px 0 var(--ink); color:inherit}
.stat:active{transform:translate(2px,2px); box-shadow:1px 1px 0 var(--ink)}
.stat.active{background:var(--accent-soft); border-color:var(--terracotta); box-shadow:3px 3px 0 var(--terracotta)}
.stat .v{font-family:var(--display); font-size:2.6rem; font-weight:600; letter-spacing:-.04em; line-height:.9; color:var(--ink)}
.stat .l{font-family:var(--hand); font-size:.9rem; color:var(--ink-3); margin-top:.4rem; font-weight:700}
.stat.danger .v{color:var(--rust)}
.stat.warn .v{color:#9a6c00}
.stat.ok .v{color:var(--moss)}
.stat.active .v{color:var(--terracotta)}

/* filters */
.filters{
  display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; margin-bottom:1.25rem;
}
.seg{display:inline-flex; background:var(--paper-2); padding:3px; border-radius:999px; gap:2px; border:2px solid var(--ink); box-shadow:var(--shadow-sm)}
.seg a{
  padding:.3rem .75rem; font-size:.78rem; color:var(--ink-3); border-radius:999px; font-weight:700;
  transition:all .1s;
}
.seg a:hover{color:var(--ink)}
.seg a.on{background:var(--ink); color:var(--paper)}
.filters select, .filters input[type=text]{
  padding:.4rem .75rem; font-size:.82rem; background:var(--surface); color:var(--ink);
  border:2px solid var(--ink); border-radius:999px; outline:none;
  font-family:var(--body); font-weight:600; transition:box-shadow .12s;
}
.filters select:focus, .filters input:focus{box-shadow:0 0 0 3px var(--accent-soft)}
.filters .toggle{display:inline-flex; align-items:center; gap:.4rem; font-size:.82rem; color:var(--ink-3); cursor:pointer; padding:.4rem .75rem; background:var(--surface); border:2px solid var(--ink); border-radius:999px; font-weight:600}
.filters .toggle input{accent-color:var(--terracotta)}

/* ── Section groups ── */
.hw-section{margin-bottom:1.25rem; border:2px solid var(--ink); border-radius:var(--radius); box-shadow:var(--shadow-sm); overflow:hidden}
.hw-section-head{
  display:flex; align-items:center; justify-content:space-between;
  padding:.65rem 1rem .6rem;
  background:var(--paper-2); border-bottom:2px solid var(--ink);
}
.hw-section-head h3{
  font-family:var(--hand); font-size:1rem; font-weight:700;
  color:var(--ink-2); margin:0; letter-spacing:.01em;
}
.hw-section-head .badge{
  font-size:.68rem; font-weight:700; padding:.2rem .55rem; border-radius:999px;
  background:var(--ink); color:var(--paper); letter-spacing:.04em;
}
.hw-section.s-overdue .hw-section-head{background:var(--danger-soft); border-bottom-color:var(--rust)}
.hw-section.s-overdue .hw-section-head h3{color:var(--rust)}
.hw-section.s-overdue .hw-section-head .badge{background:var(--rust)}
.hw-section.s-today .hw-section-head{background:var(--warn-soft); border-bottom-color:#9a6c00}
.hw-section.s-today .hw-section-head h3{color:#7a5500}
.hw-section.s-today .hw-section-head .badge{background:#9a6c00}
.hw-section.s-done .hw-section-head{background:var(--ok-soft)}
.hw-section.s-done .hw-section-head h3{color:var(--moss)}
.hw-section-body{background:var(--surface)}

/* ── Card rows ── */
.card{
  display:flex; align-items:flex-start; gap:.75rem;
  padding:.7rem 1rem;
  border-bottom:1.5px solid var(--hair);
  transition:background .1s;
  position:relative;
}
.card:last-child{border-bottom:0}
.card:hover{background:var(--paper-2)}
.card.done{opacity:.5}
.card.done .card-desc{text-decoration:line-through; text-decoration-thickness:1px; text-decoration-color:var(--ink-3)}

/* checkbox */
.check{
  flex-shrink:0; width:20px; height:20px; border:2px solid var(--ink); border-radius:5px;
  background:var(--surface); cursor:pointer; display:grid; place-items:center; padding:0; margin-top:1px;
  transition:all .1s;
}
.check:hover{background:var(--accent-soft); border-color:var(--terracotta)}
.check.checked{background:var(--terracotta); border-color:var(--terracotta); color:#fff}
.check.checked::after{content:""; width:9px; height:5px; border-left:2.5px solid #fff; border-bottom:2.5px solid #fff; transform:rotate(-45deg) translate(1px,-1px)}

/* subject dot */
.card-dot{
  flex-shrink:0; width:10px; height:10px; border-radius:50%;
  background:hsl(var(--h) 60% 48%); margin-top:5px; border:1.5px solid var(--ink);
}

/* card body */
.card-body{flex:1; min-width:0; display:flex; flex-direction:column; gap:.3rem}
.card-row1{display:flex; align-items:center; gap:.5rem; flex-wrap:wrap}
.card-desc{font-size:.88rem; font-weight:600; color:var(--ink); line-height:1.4; flex:1; min-width:0}
.card-meta{display:flex; align-items:center; gap:.4rem; flex-wrap:wrap; margin-left:auto; flex-shrink:0}
.card-subj{
  font-size:.65rem; font-weight:700; padding:.15rem .5rem; border-radius:999px;
  letter-spacing:.05em; text-transform:uppercase;
  background:hsl(var(--h) 65% 90%); color:hsl(var(--h) 50% 25%);
  border:1.5px solid hsl(var(--h) 45% 70%);
}
.card-date{font-family:var(--hand); font-size:.9rem; color:var(--ink-3); font-weight:700; white-space:nowrap}
.card-status{
  display:inline-flex; align-items:center; font-size:.65rem; font-weight:700;
  padding:.15rem .55rem; border-radius:999px; letter-spacing:.05em; white-space:nowrap;
  border:1.5px solid currentColor;
}
.card-status.overdue{background:var(--danger-soft); color:var(--rust)}
.card-status.today{background:var(--warn-soft); color:#7a5500}
.card-status.soon{background:var(--info-soft); color:var(--teal)}
.card-status.test-pill{background:#f0e0ec; color:var(--plum); border-color:var(--plum)}
.card-teacher{font-family:var(--hand); font-size:.88rem; color:var(--ink-3); font-weight:700}
.card-note{
  font-size:.78rem; padding:.3rem .6rem; background:transparent; color:var(--ink);
  border:1.5px dashed var(--hair); border-radius:8px; width:100%; font-family:var(--body);
  transition:all .12s;
}
.card-note:hover{background:var(--paper-2); border-color:var(--ink-3)}
.card-note:focus{outline:none; border-color:var(--terracotta); border-style:solid; box-shadow:0 0 0 3px var(--accent-soft); background:var(--surface)}
.card-note::placeholder{color:var(--sand)}
.card-atts{display:flex; flex-wrap:wrap; gap:.35rem}
.att{
  display:inline-flex; align-items:center; gap:.3rem; font-size:.73rem; padding:.2rem .55rem;
  background:var(--paper-2); color:var(--ink-2); border:1.5px solid var(--hair);
  border-radius:999px; text-decoration:none; transition:all .1s; font-weight:600;
}
.att:hover{border-color:var(--terracotta); color:var(--terracotta); background:var(--accent-soft)}

/* messages */
.msg-card{
  background:var(--surface); border:2px solid var(--ink); border-radius:var(--radius);
  box-shadow:var(--shadow-sm);
  padding:.85rem 1rem; transition:transform .08s, box-shadow .08s; margin-bottom:.65rem;
}
.msg-card:hover{transform:translate(-1px,-1px); box-shadow:4px 4px 0 var(--ink)}
.msg-card.unread{border-left:4px solid var(--terracotta); padding-left:calc(1rem - 2px)}
.msg-meta{display:flex; justify-content:space-between; align-items:center; font-size:.78rem; color:var(--ink-3); margin-bottom:.2rem}
.msg-sender{font-weight:700; color:var(--ink)}
.msg-subject{margin:.1rem 0 .3rem; font-size:.93rem; font-weight:600; color:var(--ink); line-height:1.35}
.msg-card.unread .msg-subject{color:var(--terracotta)}
.msg-excerpt{font-size:.83rem; color:var(--ink-2); line-height:1.45; max-height:3em; overflow:hidden}

/* grades */
.year-heading{margin:1rem 0 .5rem; font-family:var(--hand); font-size:1rem; color:var(--ink-3); font-weight:700; letter-spacing:.03em}
.report-grid{display:grid; grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); gap:.65rem; margin-bottom:1rem}
.report-card{
  display:flex; align-items:center; gap:.7rem; padding:.75rem 1rem; background:var(--surface);
  border:2px solid var(--ink); border-radius:var(--radius); box-shadow:var(--shadow-sm);
  text-decoration:none; color:var(--ink); transition:transform .08s, box-shadow .08s;
}
.report-card:hover{transform:translate(-1px,-1px); box-shadow:4px 4px 0 var(--ink)}
.report-icon{font-size:1.5rem; line-height:1}
.report-label{font-size:.88rem; font-weight:600; color:var(--ink)}
.report-sub{font-family:var(--hand); font-size:.85rem; color:var(--ink-3); margin-top:1px; font-weight:700}
.muted-small{color:var(--ink-3); font-family:var(--hand); font-size:.95rem; max-width:320px}

.subj{
  display:inline-flex; align-items:center; gap:.3rem; font-size:.65rem; font-weight:700;
  padding:.15rem .5rem; border-radius:999px; letter-spacing:.05em; text-transform:uppercase;
  background:hsl(var(--h) 65% 90%); color:hsl(var(--h) 50% 25%); border:1.5px solid hsl(var(--h) 45% 70%);
}

/* tables */
.data-table{
  width:100%; border-collapse:collapse; background:var(--surface);
  border:2px solid var(--ink); border-radius:var(--radius); overflow:hidden; box-shadow:var(--shadow-sm);
}
.data-table th, .data-table td{
  padding:.65rem .9rem; text-align:left; border-bottom:1.5px solid var(--hair); font-size:.88rem; vertical-align:top;
}
.data-table th{
  background:var(--paper-2); border-bottom:2px solid var(--ink);
  font-family:var(--hand); font-size:.9rem; font-weight:700;
  color:var(--ink-2); letter-spacing:.03em;
}
.data-table tr:last-child td{border-bottom:0}
.data-table tr:hover td{background:var(--paper-2)}

/* empty */
.empty{
  text-align:center; padding:3.5rem 1rem; background:var(--surface); border:2px solid var(--ink);
  border-radius:var(--radius); box-shadow:var(--shadow-sm); color:var(--ink-3);
}
.empty .icon{font-size:2.5rem; margin-bottom:.6rem; opacity:.7}
.empty h3{font-family:var(--hand); margin:.2rem 0; color:var(--ink); font-weight:700; font-size:1.2rem}
.empty p{margin:.25rem 0 0; font-size:.875rem; max-width:360px; margin-left:auto; margin-right:auto}

/* toast */
.toast{
  position:fixed; bottom:1.25rem; right:1.25rem; padding:.7rem 1.1rem; border-radius:999px;
  background:var(--surface); color:var(--ink); border:2px solid var(--ink); font-size:.86rem; font-weight:600;
  box-shadow:var(--shadow-sm); animation:slideUp .3s ease, fadeOut .4s 3.5s forwards; z-index:100;
  display:flex; align-items:center; gap:.5rem; max-width:420px;
}
.toast::before{content:""; width:8px; height:8px; border-radius:50%; background:var(--moss); border:1.5px solid var(--ink); flex-shrink:0}
.toast.err{color:var(--rust)}
.toast.err::before{background:var(--rust)}
@keyframes slideUp{from{transform:translateY(12px); opacity:0} to{transform:none; opacity:1}}
@keyframes fadeOut{to{opacity:0; transform:translateY(-6px)}}

/* atts shared */
.atts{display:flex; flex-wrap:wrap; gap:.35rem; margin-top:.15rem}
.atts .att{
  display:inline-flex; align-items:center; gap:.3rem; font-size:.73rem; padding:.2rem .55rem;
  background:var(--paper-2); color:var(--ink-2); border:1.5px solid var(--hair);
  border-radius:999px; text-decoration:none; transition:all .1s; font-weight:600;
}
.atts .att:hover{border-color:var(--terracotta); color:var(--terracotta); background:var(--accent-soft)}

/* responsive */
@media (max-width:860px){
  .topbar-inner{padding:.7rem 1rem}
  .brand .brand-sub{display:none}
  nav.tabs{padding:0 .75rem}
  nav.tabs a{padding:.6rem .65rem; font-size:.78rem}
  .main{padding:1.25rem 1rem 3rem}
  .page-head h1{font-size:1.6rem}
  .stats{grid-template-columns:repeat(3,1fr)}
  .stat .v{font-size:2.1rem}
}
@media (max-width:560px){
  .stats{grid-template-columns:repeat(2,1fr)}
  .card-meta{margin-left:0; width:100%}
}

form.inline{display:inline; margin:0}

/* ── Hero ────────────────────────────────────────────────────── */
.hero{position:relative; width:100%; height:280px; overflow:hidden; background:#B4D9EE; flex-shrink:0}
.hero-svg{position:absolute; top:0; left:0; width:100%; height:100%}
.hero-content{
  position:absolute; top:0; left:0; bottom:0;
  display:flex; flex-direction:column; justify-content:center;
  padding:2rem 2.5rem; z-index:2; pointer-events:none
}
.hero-badge{
  display:inline-flex; align-items:center; gap:.35rem;
  background:rgba(255,255,255,.82); border:1px solid rgba(255,255,255,.9);
  border-radius:99px; padding:.22rem .8rem;
  font-size:.68rem; font-weight:700; letter-spacing:.06em; text-transform:uppercase;
  color:#5C3315; margin-bottom:.85rem; width:fit-content;
  box-shadow:0 1px 6px rgba(0,0,0,.08)
}
.hero-title{
  font-family:'Fraunces', Georgia, 'Times New Roman', serif;
  font-size:2.7rem; font-weight:800; font-style:italic;
  line-height:1.08; color:#1C0E05; margin:0 0 .35rem 0;
  text-shadow:0 1px 4px rgba(255,255,255,.6)
}
.hero-sub{
  font-family:'Caveat', 'Comic Sans MS', cursive;
  font-size:1.45rem; font-weight:600;
  color:#4A2808; margin:0; opacity:.88
}
@media(max-width:860px){.hero{height:220px} .hero-title{font-size:2rem} .hero-sub{font-size:1.15rem}}
@media(max-width:560px){.hero{height:180px} .hero-content{padding:1.25rem 1.25rem} .hero-title{font-size:1.5rem}}
"""

SHELL = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} · SMS Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,wght@1,800&family=Caveat:wght@600&display=swap" rel="stylesheet">
<style>{{ css | safe }}</style>
</head><body>

<div class="app">
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand">
        <div class="logo">📚</div>
        <div>
          <div class="brand-name">SMS Dashboard</div>
          <div class="brand-sub">Philippe · Bruxelles IV · S5 ENC</div>
        </div>
      </div>
      <div style="display:flex; align-items:center; gap:.85rem">
        <div class="topbar-status">
          <span class="status-dot {% if scraping %}running{% endif %}"></span>
          <span>{% if scraping %}Scraping…{% elif last_run %}Updated {{ last_run }}{% else %}No data yet{% endif %}</span>
        </div>
        <form class="inline" action="{{ url_for('scrape_now') }}" method="post">
          <button class="btn" {% if scraping %}disabled{% endif %}>
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
    </div>
    <nav class="tabs">
      <a href="{{ url_for('home') }}" class="{% if view=='home' %}active{% endif %}">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
        <span>Homework</span>
        {% if counts.upcoming %}<span class="count">{{ counts.upcoming }}</span>{% endif %}
      </a>
      <a href="{{ url_for('messages_view') }}" class="{% if view=='messages' %}active{% endif %}">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
        <span>Messages</span>
        {% if unread %}<span class="count">{{ unread }}</span>{% endif %}
      </a>
      <a href="{{ url_for('diary_view') }}" class="{% if view=='diary' %}active{% endif %}">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
        <span>Course Diary</span>
      </a>
      <a href="{{ url_for('schedule_view') }}" class="{% if view=='schedule' %}active{% endif %}">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
        <span>Schedule</span>
      </a>
      <a href="{{ url_for('grades_view') }}" class="{% if view=='grades' %}active{% endif %}">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2z"/></svg>
        <span>Grades</span>
      </a>
      <a href="{{ url_for('files_view') }}" class="{% if view=='files' %}active{% endif %}">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        <span>Files</span>
      </a>
      <a href="{{ url_for('tests_view') }}" class="{% if view=='tests' %}active{% endif %}">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
        <span>Exercises</span>
      </a>
    </nav>
  </header>

{% if view == 'home' %}
  <div class="hero">
    <svg class="hero-svg" viewBox="0 0 1200 280" preserveAspectRatio="xMidYMid slice" xmlns="http://www.w3.org/2000/svg">
      <!-- Sky -->
      <rect width="1200" height="230" fill="#B4D9EE"/>

      <!-- Sun -->
      <g transform="translate(1100,68)">
        <circle r="46" fill="#F5C518"/>
        <g stroke="#F7D55A" stroke-width="3.5" stroke-linecap="round" opacity=".6">
          <line x1="0" y1="-60" x2="0" y2="-74"/>
          <line x1="42" y1="-42" x2="52" y2="-52"/>
          <line x1="60" y1="0" x2="74" y2="0"/>
          <line x1="42" y1="42" x2="52" y2="52"/>
          <line x1="0" y1="60" x2="0" y2="74"/>
          <line x1="-42" y1="42" x2="-52" y2="52"/>
          <line x1="-60" y1="0" x2="-74" y2="0"/>
          <line x1="-42" y1="-42" x2="-52" y2="-52"/>
        </g>
      </g>

      <!-- Clouds -->
      <g opacity=".88">
        <ellipse cx="420" cy="52" rx="72" ry="27" fill="white"/>
        <ellipse cx="462" cy="38" rx="50" ry="25" fill="white"/>
        <ellipse cx="378" cy="60" rx="42" ry="21" fill="white"/>
        <ellipse cx="750" cy="40" rx="58" ry="22" fill="white"/>
        <ellipse cx="788" cy="28" rx="40" ry="20" fill="white"/>
        <ellipse cx="718" cy="48" rx="36" ry="18" fill="white"/>
      </g>

      <!-- School building wings -->
      <rect x="358" y="128" width="55" height="102" fill="#BB4836"/>
      <rect x="597" y="128" width="55" height="102" fill="#BB4836"/>

      <!-- School building main block -->
      <rect x="390" y="103" width="230" height="127" fill="#C9503A"/>

      <!-- Roof -->
      <polygon points="372,103 505,54 638,103" fill="#9B3228"/>

      <!-- Bell tower -->
      <rect x="485" y="34" width="40" height="24" fill="#9B3228"/>
      <polygon points="481,34 505,18 529,34" fill="#7B2618"/>
      <circle cx="505" cy="50" r="7" fill="#D4A020"/>
      <!-- Flag -->
      <line x1="503" y1="18" x2="503" y2="3" stroke="#999" stroke-width="2"/>
      <polygon points="503,3 520,9 503,15" fill="#E63A3A"/>

      <!-- Windows main block -->
      <rect x="408" y="113" width="29" height="33" fill="#CBE8F8" rx="2"/>
      <rect x="453" y="113" width="29" height="33" fill="#CBE8F8" rx="2"/>
      <rect x="520" y="113" width="29" height="33" fill="#CBE8F8" rx="2"/>
      <rect x="565" y="113" width="29" height="33" fill="#CBE8F8" rx="2"/>
      <line x1="422" y1="113" x2="422" y2="146" stroke="#A8D4F0" stroke-width="1.5"/>
      <line x1="408" y1="129" x2="437" y2="129" stroke="#A8D4F0" stroke-width="1.5"/>
      <line x1="467" y1="113" x2="467" y2="146" stroke="#A8D4F0" stroke-width="1.5"/>
      <line x1="453" y1="129" x2="482" y2="129" stroke="#A8D4F0" stroke-width="1.5"/>
      <line x1="534" y1="113" x2="534" y2="146" stroke="#A8D4F0" stroke-width="1.5"/>
      <line x1="520" y1="129" x2="549" y2="129" stroke="#A8D4F0" stroke-width="1.5"/>
      <line x1="579" y1="113" x2="579" y2="146" stroke="#A8D4F0" stroke-width="1.5"/>
      <line x1="565" y1="129" x2="594" y2="129" stroke="#A8D4F0" stroke-width="1.5"/>

      <!-- Wing windows -->
      <rect x="368" y="146" width="26" height="30" fill="#CBE8F8" rx="2"/>
      <rect x="616" y="146" width="26" height="30" fill="#CBE8F8" rx="2"/>

      <!-- Door -->
      <rect x="478" y="173" width="54" height="57" fill="#6B3415" rx="3"/>
      <rect x="485" y="180" width="19" height="26" fill="#8B4A22" rx="2"/>
      <rect x="506" y="180" width="19" height="26" fill="#8B4A22" rx="2"/>
      <circle cx="497" cy="208" r="3" fill="#D4A020"/>
      <circle cx="514" cy="208" r="3" fill="#D4A020"/>
      <!-- Steps -->
      <rect x="470" y="228" width="70" height="5" fill="#A04030" rx="1"/>
      <rect x="465" y="233" width="80" height="5" fill="#984030" rx="1"/>

      <!-- Brick lines (subtle) -->
      <g stroke="#A03028" stroke-width=".6" opacity=".35">
        <line x1="390" y1="118" x2="620" y2="118"/>
        <line x1="390" y1="133" x2="620" y2="133"/>
        <line x1="390" y1="148" x2="620" y2="148"/>
        <line x1="390" y1="163" x2="620" y2="163"/>
        <line x1="390" y1="178" x2="620" y2="178"/>
        <line x1="390" y1="193" x2="620" y2="193"/>
        <line x1="390" y1="208" x2="620" y2="208"/>
        <line x1="390" y1="223" x2="620" y2="223"/>
      </g>

      <!-- Trees (right of school) -->
      <rect x="665" y="165" width="14" height="65" fill="#5C3D1E"/>
      <ellipse cx="672" cy="150" rx="37" ry="41" fill="#3A8C3F"/>
      <ellipse cx="652" cy="167" rx="25" ry="28" fill="#4A9C50"/>
      <ellipse cx="692" cy="167" rx="25" ry="28" fill="#329634"/>

      <rect x="732" y="172" width="12" height="58" fill="#5C3D1E"/>
      <ellipse cx="738" cy="157" rx="31" ry="35" fill="#2E8030"/>
      <ellipse cx="720" cy="170" rx="23" ry="26" fill="#3A8C3F"/>
      <ellipse cx="756" cy="170" rx="23" ry="26" fill="#3A8C3F"/>

      <!-- Basketball hoop post -->
      <rect x="775" y="128" width="6" height="110" fill="#777"/>
      <rect x="769" y="126" width="42" height="30" fill="none" stroke="#999" stroke-width="2" rx="2"/>
      <rect x="779" y="146" width="22" height="16" fill="none" stroke="#E8B030" stroke-width="2.5" rx="1"/>
      <path d="M779 162 Q790 174 801 162" fill="none" stroke="#BBB" stroke-width="2"/>

      <!-- Swing set -->
      <line x1="820" y1="108" x2="798" y2="205" stroke="#8B6914" stroke-width="6" stroke-linecap="round"/>
      <line x1="882" y1="108" x2="904" y2="205" stroke="#8B6914" stroke-width="6" stroke-linecap="round"/>
      <line x1="840" y1="108" x2="816" y2="205" stroke="#8B6914" stroke-width="4" stroke-linecap="round"/>
      <line x1="862" y1="108" x2="886" y2="205" stroke="#8B6914" stroke-width="4" stroke-linecap="round"/>
      <line x1="814" y1="108" x2="888" y2="108" stroke="#8B6914" stroke-width="7" stroke-linecap="round"/>
      <!-- Swing 1 -->
      <line x1="828" y1="112" x2="822" y2="170" stroke="#AAA" stroke-width="2"/>
      <line x1="848" y1="112" x2="842" y2="170" stroke="#AAA" stroke-width="2"/>
      <rect x="817" y="168" width="30" height="6" fill="#C0392B" rx="3"/>
      <!-- Swing 2 (tilted) -->
      <line x1="858" y1="112" x2="868" y2="164" stroke="#AAA" stroke-width="2"/>
      <line x1="874" y1="112" x2="884" y2="164" stroke="#AAA" stroke-width="2"/>
      <rect x="863" y="162" width="26" height="6" fill="#2980B9" rx="3"/>

      <!-- Slide -->
      <rect x="958" y="125" width="52" height="11" fill="#E8B030" rx="3"/>
      <rect x="952" y="125" width="12" height="78" fill="#D4A020"/>
      <rect x="1002" y="125" width="12" height="78" fill="#D4A020"/>
      <rect x="958" y="156" width="46" height="5" fill="#C8961E" rx="1"/>
      <rect x="958" y="173" width="46" height="5" fill="#C8961E" rx="1"/>
      <rect x="958" y="190" width="46" height="5" fill="#C8961E" rx="1"/>
      <line x1="957" y1="136" x2="916" y2="218" stroke="#E8B030" stroke-width="14" stroke-linecap="round"/>
      <line x1="1007" y1="136" x2="966" y2="218" stroke="#D4A020" stroke-width="7" stroke-linecap="round"/>

      <!-- Far right tree -->
      <rect x="1080" y="165" width="13" height="65" fill="#5C3D1E"/>
      <ellipse cx="1086" cy="149" rx="34" ry="38" fill="#2E8030"/>
      <ellipse cx="1066" cy="163" rx="23" ry="26" fill="#3A8C3F"/>
      <ellipse cx="1106" cy="163" rx="23" ry="26" fill="#329634"/>

      <!-- Ground -->
      <rect x="0" y="225" width="1200" height="55" fill="#5A9E3E"/>
      <ellipse cx="150" cy="225" rx="160" ry="13" fill="#4A8E2E"/>
      <ellipse cx="580" cy="225" rx="220" ry="11" fill="#6AB04E"/>
      <ellipse cx="950" cy="225" rx="190" ry="13" fill="#4A8E2E"/>

      <!-- Path to school door -->
      <rect x="484" y="232" width="42" height="48" fill="#D4B480" rx="4"/>
      <ellipse cx="505" cy="232" rx="28" ry="9" fill="#C8A870"/>

      <!-- Fence (foreground with pickets) -->
      <defs>
        <pattern id="pk" x="0" y="0" width="28" height="65" patternUnits="userSpaceOnUse">
          <rect x="10" y="8" width="8" height="57" fill="#9B7828" rx="1"/>
          <polygon points="10,8 14,1 18,8" fill="#9B7828"/>
        </pattern>
      </defs>
      <rect x="0" y="213" width="1200" height="70" fill="url(#pk)"/>
      <rect x="0" y="224" width="1200" height="7" fill="#B8902A" rx="1"/>
      <rect x="0" y="241" width="1200" height="7" fill="#B8902A" rx="1"/>
    </svg>

    <div class="hero-content">
      <div class="hero-badge">🏫 Bruxelles IV · Philippe · S5 ENC</div>
      <h2 class="hero-title">Bonjour, Philippe.</h2>
      <p class="hero-sub">what's on your plate today?</p>
    </div>
  </div>
{% endif %}

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
      <a class="stat {% if counts.overdue %}danger{% endif %} {% if active_stat=='overdue' %}active{% endif %}" href="?show=overdue{{ stat_qs }}"><div class="v">{{ counts.overdue }}</div><div class="l">Overdue</div></a>
      <a class="stat {% if counts.today %}warn{% endif %} {% if active_stat=='today' %}active{% endif %}" href="?show=today{{ stat_qs }}"><div class="v">{{ counts.today }}</div><div class="l">Due today</div></a>
      <a class="stat {% if active_stat=='week' %}active{% endif %}" href="?show=week{{ stat_qs }}"><div class="v">{{ counts.week }}</div><div class="l">This week</div></a>
      <a class="stat {% if counts.tests_week %}danger{% endif %} {% if active_stat=='tests' %}active{% endif %}" href="?show=tests{{ stat_qs }}"><div class="v">{{ counts.tests_week }}</div><div class="l">Tests</div></a>
      <a class="stat {% if active_stat=='upcoming' %}active{% endif %}" href="?show=upcoming{{ stat_qs }}"><div class="v">{{ counts.upcoming }}</div><div class="l">Upcoming</div></a>
    </div>
    {% endif %}

    {{ body | safe }}
  </main>
</div>

<script>
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
        setTimeout(poll, 3000);
      }
    }).catch(()=>setTimeout(poll, 6000));
  };
  setTimeout(poll, 3000);
})();
document.querySelectorAll('.card-note').forEach(i => {
  i.addEventListener('blur', () => {
    const key = i.dataset.key;
    const fd = new FormData();
    fd.append('key', key); fd.append('note', i.value);
    fetch('/set-note', {method:'POST', body:fd});
  });
});
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

def render(title, view, body, counts=None, show_stats=False, flash=None, flash_cls="ok",
           active_stat="", stat_qs=""):
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
        active_stat=active_stat, stat_qs=stat_qs,
    )


def render_card(a):
    """Render a Proseed-style horizontal card row."""
    done_cls = "done" if a["done"] else ""
    days = a["days_until"]

    # Status pill
    if a["done"]:
        status_html = ""
    elif a["is_overdue"]:
        status_html = '<span class="card-status overdue">Overdue</span>'
    elif a["is_today"]:
        status_html = '<span class="card-status today">Due Today</span>'
    elif days is not None and 1 <= days <= 3:
        status_html = f'<span class="card-status soon">in {days}d</span>'
    else:
        status_html = ""

    if days is None:
        date_lbl = esc(a["pretty_date"])
    elif a["is_today"]:
        date_lbl = "Today"
    elif days == 1:
        date_lbl = "Tomorrow"
    elif days < 0:
        date_lbl = esc(a["pretty_date"])
    else:
        date_lbl = esc(a["pretty_date"])

    hue = subject_hue(a.get("subject", ""))
    check_cls = "check checked" if a["done"] else "check"
    test_pill = '<span class="card-status test-pill">🔥 Test</span>' if is_test_entry(a) else ''
    teacher = teacher_label(a.get("subject", ""))
    teacher_html = f'<span class="card-teacher">· {esc(teacher)}</span>' if teacher else ''
    desc = esc(a.get("description", "") or "")

    # Attachments
    atts_html = ""
    atts = a.get("attachments") or []
    if atts:
        chips = []
        for att in atts:
            icon = {"file": "📎", "image": "🖼", "link": "🔗"}.get(att.get("kind"), "🔗")
            name = esc(att.get("name", "attachment"))[:60]
            href = esc(att.get("href", "#"))
            chips.append(f'<a class="att" href="{href}" target="_blank" rel="noopener">{icon} {name}</a>')
        atts_html = '<div class="card-atts">' + "".join(chips) + "</div>"

    note_val = esc(a.get("note", "") or "")

    return f"""<div class="card {done_cls}">
      <button class="{check_cls}" data-key="{esc(a['key'])}" aria-label="Toggle done"></button>
      <div class="card-dot" style="--h:{hue}"></div>
      <div class="card-body">
        <div class="card-row1">
          <span class="card-desc">{desc}</span>
          <div class="card-meta">
            {test_pill}
            {status_html}
            <span class="card-subj" style="--h:{hue}">{esc(a.get('subject',''))}</span>
            {teacher_html}
            <span class="card-date">{date_lbl}</span>
          </div>
        </div>
        {atts_html}
        <input class="card-note" type="text" data-key="{esc(a['key'])}"
               value="{note_val}" placeholder="Add a note…">
      </div>
    </div>"""


def render_section(label, css_cls, items):
    """Wrap a list of card rows in a Proseed-style section container."""
    if not items:
        return ""
    count = len(items)
    rows = "".join(render_card(a) for a in items)
    return f"""<div class="hw-section {css_cls}">
  <div class="hw-section-head">
    <h3>{label}</h3>
    <span class="badge">{count}</span>
  </div>
  <div class="hw-section-body">{rows}</div>
</div>"""


# ---------- routes ----------

@app.route("/")
def home():
    assignments, _, _, _, _ = load_all()
    show = request.args.get("show", "upcoming")
    subject = request.args.get("subject", "")
    hide_done = request.args.get("hide_done") == "1"

    STAT_FILTERS = {"overdue", "today", "week", "tests", "upcoming"}
    active_stat = show if show in STAT_FILTERS else ""

    subjects = sorted({a["subject"] for a in assignments if a["subject"]})
    in_week = lambda a: a["days_until"] is not None and 0 <= a["days_until"] <= 7 and not a["done"]

    rows = assignments
    if show == "overdue":
        rows = [a for a in rows if a["is_overdue"]]
    elif show == "today":
        rows = [a for a in rows if a["is_today"] and not a["done"]]
    elif show == "week":
        rows = [a for a in rows if in_week(a)]
    elif show == "tests":
        rows = [a for a in rows if in_week(a) and is_test_entry(a)]
    elif show == "upcoming":
        rows = [a for a in rows if a["is_upcoming"] or a["is_overdue"]]
    elif show == "past":
        rows = [a for a in rows if not a["is_upcoming"] and not a["is_overdue"]]
    if subject:
        rows = [a for a in rows if a["subject"] == subject]
    if hide_done:
        rows = [a for a in rows if not a["done"]]

    stat_qs_parts = []
    if subject:
        stat_qs_parts.append(f"&subject={esc(subject)}")
    if hide_done:
        stat_qs_parts.append("&hide_done=1")
    stat_qs = "".join(stat_qs_parts)

    # Filters bar
    filters = [
        '<div class="filters">',
        '<form method="get" style="display:inline-flex; gap:.5rem; align-items:center; flex-wrap:wrap">',
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
        f' onchange="this.form.submit()"> Hide done</label>'
    )
    filters.append('<div class="seg">')
    for val, lbl in [("past", "Past"), ("all", "All")]:
        on = "on" if show == val else ""
        q_parts = [f"show={val}"]
        if subject: q_parts.append(f"subject={esc(subject)}")
        if hide_done: q_parts.append("hide_done=1")
        filters.append(f'<a class="{on}" href="?{"&".join(q_parts)}">{lbl}</a>')
    filters.append('</div></form></div>')

    if not rows:
        cards = """<div class="empty">
          <div class="icon">✨</div>
          <h3>All caught up</h3>
          <p>No assignments match this filter.</p>
        </div>"""
    else:
        # For upcoming/all/subject views: group into Proseed-style sections
        if show in ("upcoming", "all", "week") or subject:
            overdue  = [a for a in rows if a["is_overdue"] and not a["done"]]
            today    = [a for a in rows if a["is_today"]   and not a["done"] and not a["is_overdue"]]
            days_2_7 = [a for a in rows if not a["is_overdue"] and not a["is_today"]
                        and a["days_until"] is not None and 1 <= a["days_until"] <= 7 and not a["done"]]
            upcoming = [a for a in rows if not a["is_overdue"] and not a["is_today"]
                        and (a["days_until"] is None or a["days_until"] > 7) and not a["done"]]
            done_    = [a for a in rows if a["done"]]
            sections = (
                render_section("Overdue", "s-overdue", overdue) +
                render_section("Due Today", "s-today", today) +
                render_section("This Week", "s-week", days_2_7) +
                render_section("Upcoming", "s-upcoming", upcoming) +
                render_section("Done", "s-done", done_)
            )
            cards = sections if sections.strip() else """<div class="empty">
              <div class="icon">✨</div><h3>All caught up</h3>
              <p>No assignments match this filter.</p></div>"""
        else:
            # For flat views (overdue only, today only, tests, past): plain list in one section
            label_map = {
                "overdue": ("Overdue", "s-overdue"),
                "today":   ("Due Today", "s-today"),
                "tests":   ("Tests this week", "s-week"),
                "past":    ("Past", "s-upcoming"),
            }
            lbl, cls = label_map.get(show, ("Results", "s-upcoming"))
            cards = render_section(lbl, cls, rows)

    body = "".join(filters) + cards
    return render("Homework", "home", body, _count_summary(assignments), show_stats=True,
                  flash=request.args.get("flash"), flash_cls=request.args.get("cls", "ok"),
                  active_stat=active_stat, stat_qs=stat_qs)


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
