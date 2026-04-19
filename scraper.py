"""Production scraper for sms.eursc.eu — homework + tests."""
import asyncio
import hashlib
import json
import os
import random
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright

import db as _db  # Supabase helpers

load_dotenv()
USERNAME = os.getenv("SMS_USERNAME")
PASSWORD = os.getenv("SMS_PASSWORD")
HA_WEBHOOK = (os.getenv("HA_WEBHOOK_URL") or "").strip()
INGEST_URL = (os.getenv("INGEST_URL") or "").strip()
INGEST_TOKEN = (os.getenv("INGEST_TOKEN") or "").strip()

ROOT = Path(__file__).parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)
SESSION_FILE = ROOT / "session.json"

URL_LOGIN = "https://sms.eursc.eu/login"
URL_DASHBOARD = "https://sms.eursc.eu/content/common/dashboard.php"
URL_DIARY = "https://sms.eursc.eu/content/course_diary/course_diary_for_parents.php"
URL_GRADES = "https://sms.eursc.eu/content/guardian/performance_sheet.php"
URL_SCHEDULE = "https://sms.eursc.eu/content/guardian/calendar_for_parents.php"
URL_INBOX = "https://sms.eursc.eu/announcements/inbox"
URL_TERM_REPORTS = "https://sms.eursc.eu/content/guardian/term_reports.php"
URL_COURSE_INFO = "https://sms.eursc.eu/content/guardian/student_info.php"


def entry_key(subject: str, due_date: str, description: str) -> str:
    h = hashlib.sha1(description.encode("utf-8")).hexdigest()[:10]
    return f"{subject}|{due_date}|{h}"


def log_run(msg: str):
    with (OUT / "run_log.txt").open("a") as f:
        f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")


async def ensure_logged_in(context, page):
    """Try using stored session; fall back to login."""
    await page.goto(URL_DASHBOARD, wait_until="domcontentloaded", timeout=20000)
    if "login" not in page.url.lower():
        print("[*] Session still valid.")
        return

    print(f"[*] Session invalid (at {page.url}), logging in...")
    await page.goto(URL_LOGIN, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(1.5)

    # Fill credentials — tolerate a variety of input shapes
    try:
        await page.fill(
            'input[type="email"], input[name*="user" i], input[name*="email" i], '
            'input[id*="user" i], input[id*="email" i]',
            USERNAME,
            timeout=10000,
        )
    except Exception as e:
        # dump for debugging
        try:
            await page.screenshot(path="screenshots/login_fail_form.png", full_page=True)
            Path("login_page_html.txt").write_text(await page.content())
        except Exception:
            pass
        raise RuntimeError(f"Could not locate username field: {e}")

    await page.fill('input[type="password"]', PASSWORD)
    await asyncio.sleep(0.5)
    await page.click(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Login"), button:has-text("Sign in"), '
        'button:has-text("Connexion"), button:has-text("Anmelden")'
    )

    # Wait up to 30s for URL to move off /login OR for a dashboard marker
    for _ in range(30):
        await asyncio.sleep(1)
        if "login" not in page.url.lower():
            break
        try:
            found = await page.locator('.current-student-select, #diary_container, .nav-sidebar').count()
            if found:
                break
        except Exception:
            pass
    else:
        # Capture diagnostics for failure
        try:
            (OUT).mkdir(exist_ok=True)
            await page.screenshot(path=str(OUT / "login_fail.png"), full_page=True)
            (OUT / "login_fail.html").write_text(await page.content())
            print(f"[!] Post-login URL: {page.url}")
            # grab any visible error text
            try:
                err = await page.eval_on_selector("body", "e => e.innerText")
                print(f"[!] Body text (head): {err[:600]}")
            except Exception:
                pass
        except Exception:
            pass
        raise RuntimeError("Login failed — page didn't leave /login within 30s")

    await context.storage_state(path=str(SESSION_FILE))
    print(f"[*] Login OK, landed at {page.url}")


async def extract_diary_entries(page, kind: str):
    """Parse #diary_container into list of entries. kind = 'course_diary' or 'assignment'."""
    entries = await page.evaluate(
        r"""() => {
            const container = document.querySelector('#diary_container');
            if (!container) return [];

            // Convert a node into a clean text representation preserving structure.
            function toText(root){
                const clone = root.cloneNode(true);
                // remove heading labels
                clone.querySelectorAll('h4').forEach(e => e.remove());
                // replace <br> with newline
                clone.querySelectorAll('br').forEach(br => br.replaceWith('\n'));
                // list items -> bullets
                clone.querySelectorAll('li').forEach(li => {
                    li.textContent = '• ' + (li.textContent || '').trim();
                    li.appendChild(document.createTextNode('\n'));
                });
                // paragraph/div breaks
                clone.querySelectorAll('p, div').forEach(d => {
                    d.appendChild(document.createTextNode('\n'));
                });
                // strip images (we'll capture separately)
                clone.querySelectorAll('img').forEach(e => e.remove());
                let text = clone.textContent || '';
                // collapse excessive blank lines
                text = text.replace(/[ \t]+/g, ' ')
                           .replace(/ *\n */g, '\n')
                           .replace(/\n{3,}/g, '\n\n')
                           .trim();
                return text;
            }

            function extractAttachments(root){
                const atts = [];
                root.querySelectorAll('a[href]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const name = (a.textContent || '').trim() || href.split('/').pop();
                    if (!href) return;
                    // file attachments typically go to s3 or /files
                    const isFile = /\.(pdf|docx?|pptx?|xlsx?|zip|png|jpe?g|gif|mp4|mp3|odt|txt|csv)(\?|$)/i.test(href)
                                   || /s3\./i.test(href) || /\/files\//i.test(href);
                    atts.push({ name, href, kind: isFile ? 'file' : 'link' });
                });
                root.querySelectorAll('img[src]').forEach(img => {
                    const src = img.getAttribute('src') || '';
                    if (src.startsWith('data:')) return;
                    atts.push({ name: img.getAttribute('alt') || 'image', href: src, kind: 'image' });
                });
                return atts;
            }

            const results = [];
            container.querySelectorAll('h3').forEach(h3 => {
                const header = (h3.textContent || '').trim();
                const m = header.match(/^(\d{2}\/\d{2}\/\d{4})\s*-\s*(.+)$/);
                const date = m ? m[1] : '';
                const subject = m ? m[2].trim() : header;
                // Collect all siblings until next h3
                const wrap = document.createElement('div');
                let n = h3.nextElementSibling;
                while (n && n.tagName !== 'H3') {
                    wrap.appendChild(n.cloneNode(true));
                    n = n.nextElementSibling;
                }
                const firstH4 = wrap.querySelector('h4');
                const type = firstH4 ? (firstH4.textContent || '').trim() : '';
                const htmlBody = wrap.innerHTML;
                const description = toText(wrap);
                const atts = extractAttachments(wrap);
                results.push({ date, subject, type, description, html: htmlBody, attachments: atts });
            });
            return results;
        }"""
    )
    for e in entries:
        e["kind"] = kind
        e["key"] = entry_key(e["subject"], e["date"], e["description"])
    return entries


async def scrape_course_diary(page):
    print("[*] Course Diary (ensuring Assignments filter ON for full data)...")
    await page.goto(URL_DIARY, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(random.uniform(1, 2))
    # Make sure Assignments are included (filter is additive)
    try:
        is_checked = await page.is_checked("#filter_options")
        if not is_checked:
            await page.check("#filter_options")
            await page.wait_for_load_state("networkidle", timeout=15000)
            await asyncio.sleep(2)
    except Exception as e:
        print(f"    could not set filter: {e}")

    all_entries = await extract_diary_entries(page, "mixed")
    # Split by the h4 label captured in `type`
    diary = [e for e in all_entries if "diar" in (e.get("type") or "").lower()]
    assignments = [e for e in all_entries if "assign" in (e.get("type") or "").lower()
                   or "devoir" in (e.get("type") or "").lower()
                   or "tâche" in (e.get("type") or "").lower()
                   or "hausauf" in (e.get("type") or "").lower()]
    # Fallback if type missing: anything not a diary counts as assignment
    if not assignments:
        assignments = [e for e in all_entries if e not in diary]
    for e in diary: e["kind"] = "course_diary"
    for e in assignments: e["kind"] = "assignment"
    print(f"    diary={len(diary)}, assignments={len(assignments)}, total={len(all_entries)}")
    return diary, assignments


async def scrape_graded_exercises(page):
    print("[*] Graded Exercises...")
    await page.goto(URL_GRADES, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(random.uniform(1, 2))
    rows = await page.evaluate(
        """() => {
            const rows = [];
            document.querySelectorAll('table tbody tr').forEach(tr => {
                const cells = [...tr.querySelectorAll('td')].map(td => (td.innerText || '').trim());
                if (cells.length >= 3) rows.push(cells);
            });
            return rows;
        }"""
    )
    print(f"    got {len(rows)} grade rows")
    out = []
    for r in rows:
        # Columns: Date, Type, Description, Weight, Grade (per inspection)
        out.append({
            "date": r[0] if len(r) > 0 else "",
            "test_type": r[1] if len(r) > 1 else "",
            "description": r[2] if len(r) > 2 else "",
            "weight": r[3] if len(r) > 3 else "",
            "grade": r[4] if len(r) > 4 else "",
            "subject": "",
        })
    return out


async def scrape_inbox(page):
    print("[*] Inbox announcements...")
    await page.goto(URL_INBOX, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(random.uniform(2, 3))
    # Scroll a few times so ag-grid renders more rows
    for _ in range(4):
        await page.mouse.wheel(0, 600)
        await asyncio.sleep(0.4)
    msgs = await page.evaluate(r"""() => {
        // Each row has three cells in order: sender | subject+excerpt+attachments | date
        const out = [];
        document.querySelectorAll('[role="row"].msm-list-view-inbox-row').forEach(row => {
            const cells = [...row.querySelectorAll('[role="gridcell"]')];
            if (cells.length === 0) return;
            const sender = (cells[0]?.innerText || '').trim();
            // Subject cell
            const subjEl = cells[1]?.querySelector('.announcement-summary-renderer__subject');
            const subject = subjEl?.querySelector('.msm-limit-text')?.innerText?.trim()
                          || subjEl?.innerText?.trim() || '';
            const excerpt = cells[1]?.querySelector('.announcement-summary-renderer__excerpt')?.innerText?.trim() || '';
            const attaches = [...(cells[1]?.querySelectorAll('.announcement-summary-renderer__attachments a.msm-chip') || [])]
                .map(a => ({
                    name: a.querySelector('.msm-limit-text')?.innerText?.trim() || (a.innerText || '').trim(),
                    href: a.href,
                }))
                .filter(a => a.name);
            const sent_label = (cells[cells.length - 1]?.innerText || '').trim();
            out.push({
                rowId: row.getAttribute('row-id') || '',
                unread: row.classList.contains('msm-strong'),
                sender, subject, excerpt, sent_label,
                attachments: attaches,
            });
        });
        return out;
    }""")
    # Filter empty noise (some rows may not have rendered content)
    msgs = [m for m in msgs if m.get("subject")]
    print(f"    got {len(msgs)} messages ({sum(1 for m in msgs if m['unread'])} unread)")
    return msgs


async def scrape_term_reports(page):
    print("[*] Term reports...")
    await page.goto(URL_TERM_REPORTS, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(random.uniform(1, 2))
    reports = await page.evaluate(r"""() => {
        const out = [];
        let current_year = '';
        document.querySelectorAll('.main-content-container h3, .main-content-container a, h3, a').forEach(el => {
            if (el.tagName === 'H3') {
                const t = (el.textContent || '').trim();
                if (/^year\s/i.test(t)) current_year = t;
            } else if (el.tagName === 'A') {
                const href = el.getAttribute('href') || '';
                const m = href.match(/report_id=(\d+)/);
                if (m) {
                    out.push({
                        report_id: m[1],
                        label: (el.textContent || '').trim(),
                        year_label: current_year,
                        download_url: el.href,
                    });
                }
            }
        });
        return out;
    }""")
    print(f"    got {len(reports)} term reports")
    return reports


async def scrape_course_info(page):
    print("[*] Course info / teachers...")
    await page.goto(URL_COURSE_INFO, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(random.uniform(1, 2))
    courses = await page.evaluate(r"""() => {
        const out = [];
        document.querySelectorAll('.grid-wrapper').forEach(w => {
            const code = (w.querySelector('.wrapper-label')?.textContent || '').trim();
            if (!code) return;
            const teachers = [];
            w.querySelectorAll('label.form-element-label').forEach(lab => {
                if (/teacher/i.test(lab.textContent || '')) {
                    const span = lab.parentElement.querySelector('span.read-mode');
                    if (!span) return;
                    span.querySelectorAll('li').forEach(li => {
                        const a = li.querySelector('a[href^="mailto:"]');
                        const email = a ? a.getAttribute('href').replace(/^mailto:/,'') : '';
                        const text = (li.textContent || '').trim();
                        const name = text.replace(/\s*\(.*\)\s*$/, '').trim();
                        teachers.push({ name, email });
                    });
                }
            });
            let desc = '';
            w.querySelectorAll('label.form-element-label').forEach(lab => {
                if (/description/i.test(lab.textContent || '')) {
                    const span = lab.parentElement.querySelector('span.read-mode');
                    if (span) desc = (span.innerText || '').trim();
                }
            });
            out.push({ course_code: code, teachers, course_description: desc });
        });
        return out;
    }""")
    print(f"    got {len(courses)} courses")
    return courses


async def scrape_schedule(page):
    print("[*] Schedule (calendar all-day events)...")
    await page.goto(URL_SCHEDULE, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(random.uniform(1.5, 2.5))
    # FullCalendar-style: all-day events usually carry .fc-event with data-date/title
    events = await page.evaluate(
        """() => {
            const out = [];
            document.querySelectorAll('.fc-event, [class*=\"fc-event\"]').forEach(el => {
                const txt = (el.innerText || el.textContent || '').trim();
                const title = el.getAttribute('title') || txt;
                const start = el.getAttribute('data-date') || el.getAttribute('data-start') || '';
                if (txt) out.push({ title: title, text: txt, start: start });
            });
            return out;
        }"""
    )
    print(f"    got {len(events)} schedule items")
    return events


def save_outputs(diary, assignments, grades, schedule):
    (OUT / "course_diary.json").write_text(json.dumps(diary, indent=2, ensure_ascii=False))
    (OUT / "homework.json").write_text(json.dumps(assignments, indent=2, ensure_ascii=False))
    (OUT / "tests.json").write_text(json.dumps(grades, indent=2, ensure_ascii=False))
    (OUT / "schedule.json").write_text(json.dumps(schedule, indent=2, ensure_ascii=False))

    def to_csv(rows, cols, path):
        df = pd.DataFrame(rows)
        for c in cols:
            if c not in df.columns:
                df[c] = ""
        df = df[cols] if len(df) else pd.DataFrame(columns=cols)
        df.to_csv(path, index=False)

    to_csv(
        [{k: v for k, v in r.items() if k != "html"} for r in diary],
        ["date", "subject", "type", "description", "kind", "key"],
        OUT / "course_diary.csv",
    )
    to_csv(
        [{k: v for k, v in r.items() if k != "html"} for r in assignments],
        ["date", "subject", "type", "description", "kind", "key"],
        OUT / "homework.csv",
    )
    to_csv(grades, ["date", "subject", "test_type", "description", "weight", "grade"], OUT / "tests.csv")
    to_csv(schedule, ["start", "title", "text"], OUT / "schedule.csv")


def diff_new(prev_path: Path, current_keys: set) -> set:
    if not prev_path.exists():
        return current_keys
    try:
        prev = set(json.loads(prev_path.read_text()))
    except Exception:
        prev = set()
    return current_keys - prev


def send_ha(payload: dict):
    if not HA_WEBHOOK:
        return
    try:
        r = requests.post(HA_WEBHOOK, json=payload, timeout=10)
        print(f"[*] Home Assistant POST → {r.status_code}")
    except Exception as e:
        print(f"[!] HA webhook failed: {e}")


def _iso_date(ddmmyyyy: str) -> str | None:
    try:
        return datetime.strptime(ddmmyyyy, "%d/%m/%Y").date().isoformat()
    except Exception:
        return None


def _entry_row(e: dict, kind: str) -> dict:
    return {
        "entry_key": e["key"],
        "kind": kind,
        "subject": e.get("subject", ""),
        "entry_date": _iso_date(e.get("date", "")),
        "entry_date_text": e.get("date", ""),
        "entry_type": e.get("type", ""),
        "description": e.get("description", ""),
        "attachments": e.get("attachments") or [],
        "html": e.get("html"),
        "last_seen": datetime.now(tz=None).isoformat(),
    }


def _test_key(t: dict) -> str:
    raw = f"{t.get('subject','')}|{t.get('date','')}|{t.get('description','')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _sched_key(s: dict) -> str:
    raw = f"{s.get('start','')}|{s.get('title','')}|{s.get('text','')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def push_to_supabase(diary, assignments, grades, schedule):
    if _db.get_client() is None:
        print("[*] Supabase not configured — skipping cloud sync.")
        return
    try:
        entry_rows = [_entry_row(e, "course_diary") for e in diary] + \
                     [_entry_row(e, "assignment") for e in assignments]
        n_e = _db.upsert_entries(entry_rows)

        test_rows = [{
            "test_key": _test_key(t),
            "test_date": _iso_date(t.get("date", "")),
            "subject": t.get("subject", ""),
            "test_type": t.get("test_type", ""),
            "description": t.get("description", ""),
            "weight": t.get("weight", ""),
            "grade": t.get("grade", ""),
        } for t in grades]
        n_t = _db.upsert_tests(test_rows)

        sched_rows = [{
            "schedule_key": _sched_key(s),
            "start_time": s.get("start", ""),
            "title": s.get("title", ""),
            "details": s.get("text", ""),
        } for s in schedule]
        n_s = _db.upsert_schedule(sched_rows)

        print(f"[*] Supabase: entries={n_e} tests={n_t} schedule={n_s}")
    except Exception as e:
        print(f"[!] Supabase sync failed: {e}")


def _parse_sent_date(label: str) -> str | None:
    """Parse 'April 17' / '14 April' style labels into ISO date (assumes current year)."""
    if not label:
        return None
    label = label.strip()
    now = datetime.now()
    months_en = {m.lower(): i for i, m in enumerate(
        ["January","February","March","April","May","June",
         "July","August","September","October","November","December"], start=1)}
    # Try "Month Day" then "Day Month"
    import re as _re
    m = _re.match(r"^([A-Za-z]+)\s+(\d{1,2})$", label)
    if m and m.group(1).lower() in months_en:
        mo = months_en[m.group(1).lower()]; day = int(m.group(2))
    else:
        m = _re.match(r"^(\d{1,2})\s+([A-Za-z]+)$", label)
        if m and m.group(2).lower() in months_en:
            day = int(m.group(1)); mo = months_en[m.group(2).lower()]
        else:
            return None
    year = now.year
    # If derived date would be in the future by more than a month, assume previous year
    try:
        d = datetime(year, mo, day)
        if (d - now).days > 30:
            d = datetime(year - 1, mo, day)
        return d.date().isoformat()
    except Exception:
        return None


def _message_key(m: dict) -> str:
    raw = f"{m.get('sender','')}|{m.get('sent_label','')}|{m.get('subject','')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def push_extras_to_supabase(messages, term_reports, course_info):
    if _db.get_client() is None:
        return
    try:
        msg_rows = []
        for m in messages:
            msg_rows.append({
                "message_key": _message_key(m),
                "subject": m.get("subject", ""),
                "sender": m.get("sender", ""),
                "excerpt": m.get("excerpt", ""),
                "sent_label": m.get("sent_label", ""),
                "sent_date": _parse_sent_date(m.get("sent_label", "")),
                "attachments": m.get("attachments") or [],
                "unread": bool(m.get("unread", False)),
                "last_seen": datetime.now().isoformat(),
            })
        n_m = _db.upsert_messages(msg_rows)

        report_rows = [{
            "report_id": r["report_id"],
            "label": r.get("label", ""),
            "year_label": r.get("year_label", ""),
            "download_url": r.get("download_url", ""),
        } for r in term_reports]
        n_r = _db.upsert_term_reports(report_rows)

        course_rows = [{
            "course_code": c["course_code"],
            "teachers": c.get("teachers") or [],
            "course_description": c.get("course_description") or "",
        } for c in course_info if c.get("course_code")]
        n_c = _db.upsert_courses(course_rows)

        print(f"[*] Supabase extras: messages={n_m} term_reports={n_r} courses={n_c}")
    except Exception as e:
        print(f"[!] Supabase extras sync failed: {e}")


def push_to_cloud(diary, assignments, grades, schedule, summary):
    if not INGEST_URL or not INGEST_TOKEN:
        return
    bundle = {
        "homework.json": assignments,
        "course_diary.json": diary,
        "tests.json": grades,
        "schedule.json": schedule,
        "summary.json": summary,
    }
    try:
        r = requests.post(
            INGEST_URL,
            json=bundle,
            headers={"X-Ingest-Token": INGEST_TOKEN, "Content-Type": "application/json"},
            timeout=30,
        )
        if r.status_code == 200:
            print(f"[*] Pushed to cloud → {r.json().get('written')}")
        else:
            print(f"[!] Cloud push failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[!] Cloud push error: {e}")


def next_due(items):
    dates = []
    today = datetime.today().date()
    for it in items:
        d = it.get("date", "")
        try:
            dt = datetime.strptime(d, "%d/%m/%Y").date()
            if dt >= today:
                dates.append(dt)
        except Exception:
            continue
    return min(dates).isoformat() if dates else None


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx_args = {
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/128.0.0.0 Safari/537.36",
            "viewport": {"width": 1440, "height": 900},
            "locale": "en-GB",
        }
        if SESSION_FILE.exists():
            ctx_args["storage_state"] = str(SESSION_FILE)
        context = await browser.new_context(**ctx_args)
        page = await context.new_page()

        await ensure_logged_in(context, page)
        await asyncio.sleep(random.uniform(1, 2))

        diary, assignments = await scrape_course_diary(page)
        await asyncio.sleep(random.uniform(1, 2))
        grades = await scrape_graded_exercises(page)
        await asyncio.sleep(random.uniform(1, 2))
        schedule = await scrape_schedule(page)
        await asyncio.sleep(random.uniform(1, 2))
        try:
            messages = await scrape_inbox(page)
        except Exception as e:
            print(f"[!] inbox scrape failed: {e}")
            messages = []
        await asyncio.sleep(random.uniform(1, 2))
        try:
            term_reports = await scrape_term_reports(page)
        except Exception as e:
            print(f"[!] term_reports scrape failed: {e}")
            term_reports = []
        await asyncio.sleep(random.uniform(1, 2))
        try:
            course_info = await scrape_course_info(page)
        except Exception as e:
            print(f"[!] course_info scrape failed: {e}")
            course_info = []

        save_outputs(diary, assignments, grades, schedule)
        # Save extras as JSON backups too
        (OUT / "messages.json").write_text(json.dumps(messages, indent=2, ensure_ascii=False))
        (OUT / "term_reports.json").write_text(json.dumps(term_reports, indent=2, ensure_ascii=False))
        (OUT / "courses.json").write_text(json.dumps(course_info, indent=2, ensure_ascii=False))

        # Track new items across runs using assignment keys
        keys_path = OUT / "_prev_keys.json"
        current_keys = {a["key"] for a in assignments}
        new_keys = diff_new(keys_path, current_keys)
        keys_path.write_text(json.dumps(list(current_keys)))
        new_items = [a for a in assignments if a["key"] in new_keys]

        summary = {
            "homework_count": len(assignments),
            "tests_count": len(grades),
            "diary_count": len(diary),
            "schedule_count": len(schedule),
            "new_items": [
                {"date": a["date"], "subject": a["subject"], "description": a["description"][:200]}
                for a in new_items
            ],
            "next_due": next_due(assignments),
        }
        (OUT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"[*] Summary: {summary['homework_count']} assignments, "
              f"{summary['tests_count']} grades, {len(new_items)} new")
        log_run(
            f"assignments={summary['homework_count']} diary={summary['diary_count']} "
            f"grades={summary['tests_count']} schedule={summary['schedule_count']} new={len(new_items)}"
        )

        if HA_WEBHOOK:
            send_ha(summary)

        # Primary sync: Supabase
        push_to_supabase(diary, assignments, grades, schedule)
        push_extras_to_supabase(messages, term_reports, course_info)
        try:
            _db.record_run(
                assignments_count=len(assignments),
                diary_count=len(diary),
                tests_count=len(grades),
                schedule_count=len(schedule),
                new_items_count=len(new_items),
                source="launchd",
            )
        except Exception as e:
            print(f"[!] record_run failed: {e}")

        # Legacy fallback: push JSON bundle to /api/ingest if still configured
        push_to_cloud(diary, assignments, grades, schedule, summary)

        await context.storage_state(path=str(SESSION_FILE))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
