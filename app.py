"""Web UI for managing SMS scraper data."""
import html as _html
import json
import subprocess
import sys
import threading
from datetime import datetime, date
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request, url_for

ROOT = Path(__file__).parent
OUT = ROOT / "output"
STATE = OUT / "_ui_state.json"

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
    if not STATE.exists():
        return {"done": {}, "notes": {}}
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"done": {}, "notes": {}}


def save_state(s):
    STATE.write_text(json.dumps(s, indent=2, ensure_ascii=False))


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


def load_all():
    state = load_state()
    today = date.today()
    assignments = enrich(load_json("homework.json", []), state, today)
    diary = enrich(load_json("course_diary.json", []), state, today)
    tests = load_json("tests.json", [])
    schedule = load_json("schedule.json", [])
    summary = load_json("summary.json", {})
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
:root{
  --bg:#f6f7fb; --surface:#ffffff; --surface-2:#f0f2f7;
  --text:#0f172a; --text-muted:#64748b; --text-soft:#94a3b8;
  --border:#e2e8f0; --border-strong:#cbd5e1;
  --accent:#4f46e5; --accent-hover:#4338ca; --accent-soft:#eef2ff;
  --danger:#dc2626; --danger-soft:#fef2f2;
  --warn:#d97706; --warn-soft:#fffbeb;
  --ok:#059669; --ok-soft:#ecfdf5;
  --info:#0284c7; --info-soft:#f0f9ff;
  --shadow-sm:0 1px 2px rgba(15,23,42,.04), 0 1px 3px rgba(15,23,42,.06);
  --shadow-md:0 4px 12px rgba(15,23,42,.08);
  --radius:10px; --radius-sm:6px;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0b1020; --surface:#121829; --surface-2:#1a2238;
    --text:#e2e8f0; --text-muted:#94a3b8; --text-soft:#64748b;
    --border:#1e293b; --border-strong:#334155;
    --accent:#818cf8; --accent-hover:#a5b4fc; --accent-soft:#1e1b4b;
    --danger:#f87171; --danger-soft:#2a1414;
    --warn:#fbbf24; --warn-soft:#2a1f0a;
    --ok:#34d399; --ok-soft:#0a2a1f;
    --info:#38bdf8; --info-soft:#0a1f2a;
    --shadow-sm:0 1px 2px rgba(0,0,0,.3);
    --shadow-md:0 4px 12px rgba(0,0,0,.4);
  }
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Inter,Helvetica,Arial,sans-serif;
  background:var(--bg); color:var(--text); line-height:1.5;
  -webkit-font-smoothing:antialiased; font-feature-settings:"cv02","cv03","cv04","cv11";
}
a{color:var(--accent); text-decoration:none}
a:hover{color:var(--accent-hover)}

/* layout */
.shell{max-width:1280px; margin:0 auto; padding:1.25rem 1.5rem 3rem}
.topbar{
  position:sticky; top:0; z-index:10; backdrop-filter:saturate(180%) blur(12px);
  background:color-mix(in srgb, var(--bg) 80%, transparent);
  border-bottom:1px solid var(--border); margin:-1.25rem -1.5rem 1.5rem; padding:.85rem 1.5rem;
  display:flex; justify-content:space-between; align-items:center; gap:1rem;
}
.brand{display:flex; align-items:center; gap:.6rem; font-weight:600; font-size:1rem}
.brand .logo{
  width:32px; height:32px; border-radius:8px; display:grid; place-items:center;
  background:linear-gradient(135deg, var(--accent), #7c3aed); color:#fff; font-size:1.1rem;
}
.brand-sub{color:var(--text-muted); font-size:.82rem; font-weight:400}

/* buttons */
.btn{
  display:inline-flex; align-items:center; gap:.4rem; padding:.5rem .9rem;
  background:var(--accent); color:#fff; border:0; border-radius:var(--radius-sm);
  font-size:.88rem; font-weight:500; cursor:pointer; transition:background .15s, transform .05s;
  box-shadow:var(--shadow-sm);
}
.btn:hover{background:var(--accent-hover)}
.btn:active{transform:translateY(1px)}
.btn:disabled{opacity:.55; cursor:not-allowed}
.btn.ghost{background:transparent; color:var(--text); border:1px solid var(--border-strong); box-shadow:none}
.btn.ghost:hover{background:var(--surface-2)}

/* nav */
nav.tabs{display:flex; gap:.25rem; border-bottom:1px solid var(--border); margin-bottom:1.25rem}
nav.tabs a{
  padding:.55rem .9rem; font-size:.88rem; font-weight:500; color:var(--text-muted);
  border-bottom:2px solid transparent; margin-bottom:-1px; border-radius:var(--radius-sm) var(--radius-sm) 0 0;
}
nav.tabs a:hover{color:var(--text); background:var(--surface-2)}
nav.tabs a.active{color:var(--accent); border-bottom-color:var(--accent); background:var(--accent-soft)}
nav.tabs a .count{
  display:inline-block; margin-left:.35rem; padding:.05rem .4rem; font-size:.72rem;
  background:var(--surface-2); border-radius:999px; color:var(--text-muted); font-weight:600;
}
nav.tabs a.active .count{background:var(--accent); color:#fff}

/* stats grid */
.stats{display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:.75rem; margin-bottom:1.25rem}
.stat{
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  padding:.9rem 1rem; box-shadow:var(--shadow-sm); position:relative; overflow:hidden;
}
.stat .v{font-size:1.6rem; font-weight:700; letter-spacing:-.02em; line-height:1}
.stat .l{font-size:.78rem; color:var(--text-muted); margin-top:.3rem; text-transform:uppercase; letter-spacing:.04em; font-weight:500}
.stat.danger .v{color:var(--danger)}
.stat.warn .v{color:var(--warn)}
.stat.ok .v{color:var(--ok)}
.stat.info .v{color:var(--info)}

/* filters */
.filters{
  display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; margin-bottom:1rem;
  background:var(--surface); padding:.6rem .75rem; border:1px solid var(--border); border-radius:var(--radius); box-shadow:var(--shadow-sm);
}
.seg{display:inline-flex; background:var(--surface-2); padding:2px; border-radius:7px; gap:2px}
.seg a{
  padding:.35rem .7rem; font-size:.82rem; color:var(--text-muted); border-radius:5px; font-weight:500;
}
.seg a.on{background:var(--surface); color:var(--text); box-shadow:var(--shadow-sm)}
.filters select, .filters input[type=text]{
  padding:.35rem .55rem; font-size:.85rem; background:var(--surface); color:var(--text);
  border:1px solid var(--border-strong); border-radius:var(--radius-sm); outline:none;
}
.filters select:focus, .filters input:focus{border-color:var(--accent)}
.filters .toggle{display:inline-flex; align-items:center; gap:.35rem; font-size:.85rem; color:var(--text-muted); cursor:pointer}

/* card list */
.card-list{display:flex; flex-direction:column; gap:.5rem}
.card{
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  padding:.85rem 1rem; display:grid; grid-template-columns:auto 110px 1fr auto;
  gap:.9rem; align-items:start; box-shadow:var(--shadow-sm); transition:box-shadow .15s, border-color .15s;
}
.card:hover{box-shadow:var(--shadow-md); border-color:var(--border-strong)}
.card.done{opacity:.55}
.card.done .desc, .card.done .meta, .card.done .subj{text-decoration:line-through}
.card.today{border-left:3px solid var(--warn); padding-left:calc(1rem - 3px)}
.card.overdue{border-left:3px solid var(--danger); padding-left:calc(1rem - 3px); background:linear-gradient(90deg, var(--danger-soft), var(--surface) 40%)}
.card.soon{border-left:3px solid var(--info); padding-left:calc(1rem - 3px)}

.check{
  width:22px; height:22px; border:2px solid var(--border-strong); border-radius:6px;
  background:var(--surface); cursor:pointer; display:grid; place-items:center; padding:0; margin-top:2px;
  transition:all .15s;
}
.check:hover{border-color:var(--accent)}
.check.checked{background:var(--accent); border-color:var(--accent); color:#fff}
.check.checked::after{content:"✓"; font-size:.85rem; font-weight:700}

.when{display:flex; flex-direction:column; gap:.15rem}
.when .date{font-size:.82rem; color:var(--text-muted); font-weight:500}
.when .days{font-size:1rem; font-weight:600; letter-spacing:-.01em}
.when.today .days{color:var(--warn)}
.when.overdue .days{color:var(--danger)}
.when.soon .days{color:var(--info)}
.when.later .days{color:var(--text-soft)}

.body{display:flex; flex-direction:column; gap:.35rem; min-width:0}
.meta{display:flex; gap:.4rem; align-items:center; flex-wrap:wrap}
.subj{
  display:inline-flex; align-items:center; gap:.3rem; font-size:.72rem; font-weight:600;
  padding:.15rem .5rem; border-radius:999px; letter-spacing:.02em;
  background:hsl(var(--h) 75% 94%); color:hsl(var(--h) 60% 32%);
}
@media (prefers-color-scheme: dark){
  .subj{background:hsl(var(--h) 50% 22%); color:hsl(var(--h) 75% 80%)}
}
.pill{
  display:inline-flex; align-items:center; gap:.25rem; padding:.1rem .45rem; border-radius:4px;
  font-size:.7rem; font-weight:600; letter-spacing:.03em; text-transform:uppercase;
}
.pill.test{background:var(--danger-soft); color:var(--danger)}
.pill.assign{background:var(--accent-soft); color:var(--accent)}
.pill.diary{background:var(--info-soft); color:var(--info)}

.desc{font-size:.92rem; color:var(--text); line-height:1.5; white-space:pre-wrap; word-wrap:break-word}
.desc a{color:var(--accent); text-decoration:underline}
.atts{display:flex; flex-wrap:wrap; gap:.35rem; margin-top:.1rem}
.atts .att{
  display:inline-flex; align-items:center; gap:.3rem; font-size:.78rem; padding:.22rem .55rem;
  background:var(--surface-2); color:var(--text); border:1px solid var(--border);
  border-radius:999px; text-decoration:none; transition:background .15s, border-color .15s;
}
.atts .att:hover{background:var(--accent-soft); border-color:var(--accent); color:var(--accent)}
.note-input{
  margin-top:.3rem; font-size:.82rem; padding:.3rem .5rem; background:var(--surface-2); color:var(--text);
  border:1px dashed var(--border-strong); border-radius:4px; width:100%; font-family:inherit;
}
.note-input:focus{outline:none; border-color:var(--accent); background:var(--surface)}
.note-input::placeholder{color:var(--text-soft)}

.actions{display:flex; align-items:start}

/* tables (diary, schedule, tests) */
.data-table{
  width:100%; border-collapse:collapse; background:var(--surface);
  border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; box-shadow:var(--shadow-sm);
}
.data-table th, .data-table td{
  padding:.6rem .85rem; text-align:left; border-bottom:1px solid var(--border); font-size:.9rem; vertical-align:top;
}
.data-table th{
  background:var(--surface-2); font-size:.72rem; font-weight:600; text-transform:uppercase;
  color:var(--text-muted); letter-spacing:.05em;
}
.data-table tr:last-child td{border-bottom:0}
.data-table tr:hover td{background:var(--surface-2)}

/* empty state */
.empty{
  text-align:center; padding:3rem 1rem; background:var(--surface); border:1px dashed var(--border-strong);
  border-radius:var(--radius); color:var(--text-muted);
}
.empty .icon{font-size:2.5rem; margin-bottom:.5rem}
.empty h3{margin:.2rem 0; color:var(--text); font-weight:600}
.empty p{margin:.2rem 0; font-size:.9rem}

/* toast */
.toast{
  position:fixed; top:1rem; right:1rem; padding:.6rem 1rem; border-radius:var(--radius-sm);
  background:var(--ok-soft); color:var(--ok); border:1px solid var(--ok); font-size:.88rem; font-weight:500;
  box-shadow:var(--shadow-md); animation:slideIn .3s, fadeOut .3s 3.5s forwards; z-index:100;
}
.toast.err{background:var(--danger-soft); color:var(--danger); border-color:var(--danger)}
@keyframes slideIn{from{transform:translateX(20px); opacity:0} to{transform:none; opacity:1}}
@keyframes fadeOut{to{opacity:0; transform:translateY(-6px)}}

.status-dot{
  display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--ok); margin-right:.4rem;
  animation:pulse 1.5s ease-in-out infinite;
}
.status-dot.running{background:var(--warn)}
@keyframes pulse{0%,100%{opacity:1} 50%{opacity:.4}}

/* responsive */
@media (max-width:720px){
  .card{grid-template-columns:auto 1fr; gap:.6rem}
  .card .when{grid-column:2; order:2}
  .card .body{grid-column:1/-1; order:3}
  .card .actions{grid-column:1/-1; order:4}
  .stats{grid-template-columns:repeat(2, 1fr)}
  .topbar{padding:.65rem 1rem; margin:-1.25rem -1rem 1rem}
  .shell{padding:1rem}
}

form.inline{display:inline; margin:0}
"""

SHELL = """
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} · SMS Dashboard</title>
<style>{{ css | safe }}</style>
</head><body>

<div class="shell">
  <div class="topbar">
    <div class="brand">
      <div class="logo">📚</div>
      <div>
        <div>SMS Dashboard</div>
        <div class="brand-sub">
          {{ student }} · <span class="status-dot {% if scraping %}running{% endif %}"></span>
          {% if scraping %}Scraping…{% elif last_run %}Updated {{ last_run }}{% else %}No data yet{% endif %}
        </div>
      </div>
    </div>
    <form class="inline" action="{{ url_for('scrape_now') }}" method="post">
      <button class="btn" {% if scraping %}disabled{% endif %}>
        {% if scraping %}⏳ Running…{% else %}🔄 Refresh now{% endif %}
      </button>
    </form>
  </div>

  {% if flash %}<div class="toast {% if flash_cls=='err' %}err{% endif %}">{{ flash }}</div>{% endif %}

  <nav class="tabs">
    <a href="{{ url_for('home') }}" class="{% if view=='home' %}active{% endif %}">
      Homework <span class="count">{{ counts.upcoming }}</span>
    </a>
    <a href="{{ url_for('diary_view') }}" class="{% if view=='diary' %}active{% endif %}">Course Diary</a>
    <a href="{{ url_for('schedule_view') }}" class="{% if view=='schedule' %}active{% endif %}">Schedule</a>
    <a href="{{ url_for('tests_view') }}" class="{% if view=='tests' %}active{% endif %}">Graded Exercises</a>
  </nav>

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
</div>

<script>
// auto-poll for scrape completion, refresh page when done
(function(){
  const running = {{ 'true' if scraping else 'false' }};
  if(!running) return;
  let tries = 0;
  const poll = () => {
    fetch('/api/status').then(r=>r.json()).then(s=>{
      if(!s.scraping){ location.reload(); }
      else if(++tries < 120){ setTimeout(poll, 2000); }
    }).catch(()=>setTimeout(poll, 4000));
  };
  setTimeout(poll, 2000);
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


def render(title, view, body, counts=None, show_stats=False, flash=None, flash_cls="ok"):
    return render_template_string(
        SHELL, title=title, view=view, body=body, css=BASE_CSS,
        counts=counts or {"upcoming": 0, "today": 0, "week": 0, "total": 0, "done": 0, "overdue": 0},
        show_stats=show_stats, scraping=_scrape_lock.locked(), last_run=_last_run(),
        student="ANAMAN, Philippe · Bruxelles IV",
        flash=flash, flash_cls=flash_cls,
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


@app.post("/toggle-done")
def toggle_done():
    key = request.form.get("key", "")
    s = load_state()
    s["done"][key] = not s["done"].get(key, False)
    save_state(s)
    if request.headers.get("X-Requested-With") or "fetch" in request.headers.get("Accept", "").lower():
        return jsonify(ok=True, done=s["done"][key])
    return redirect(request.referrer or url_for("home"))


@app.post("/set-note")
def set_note():
    key = request.form.get("key", "")
    note = request.form.get("note", "").strip()
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


@app.post("/scrape-now")
def scrape_now():
    if _scrape_lock.locked():
        return redirect(url_for("home", flash="Scrape already running", cls="err"))
    threading.Thread(target=_run_scrape_bg, daemon=True).start()
    return redirect(url_for("home", flash="Fetching fresh data…", cls="ok"))


@app.get("/api/status")
def api_status():
    return jsonify({"scraping": _scrape_lock.locked(), **_last_scrape})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5055))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    app.run(host=host, port=port, debug=False)
