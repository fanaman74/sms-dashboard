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


def entry_key(subject: str, due_date: str, description: str) -> str:
    h = hashlib.sha1(description.encode("utf-8")).hexdigest()[:10]
    return f"{subject}|{due_date}|{h}"


def log_run(msg: str):
    with (OUT / "run_log.txt").open("a") as f:
        f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")


async def ensure_logged_in(context, page):
    """Try using stored session; fall back to login."""
    await page.goto(URL_DASHBOARD, wait_until="domcontentloaded", timeout=20000)
    if "login" in page.url.lower():
        print("[*] Session invalid, logging in...")
        await page.goto(URL_LOGIN, wait_until="networkidle")
        await page.fill(
            'input[type="email"], input[name*="user" i], input[name*="email" i]',
            USERNAME,
        )
        await page.fill('input[type="password"]', PASSWORD)
        await page.click(
            'button[type="submit"], input[type="submit"]'
        )
        await page.wait_for_load_state("networkidle", timeout=20000)
        if "login" in page.url.lower():
            raise RuntimeError("Login failed — check credentials in .env")
        await context.storage_state(path=str(SESSION_FILE))
        print("[*] Login OK, session saved.")
    else:
        print("[*] Session still valid.")


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
        ctx_args = {"storage_state": str(SESSION_FILE)} if SESSION_FILE.exists() else {}
        context = await browser.new_context(**ctx_args)
        page = await context.new_page()

        await ensure_logged_in(context, page)
        await asyncio.sleep(random.uniform(1, 2))

        diary, assignments = await scrape_course_diary(page)
        await asyncio.sleep(random.uniform(1, 2))
        grades = await scrape_graded_exercises(page)
        await asyncio.sleep(random.uniform(1, 2))
        schedule = await scrape_schedule(page)

        save_outputs(diary, assignments, grades, schedule)

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

        push_to_cloud(diary, assignments, grades, schedule, summary)

        await context.storage_state(path=str(SESSION_FILE))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
