"""One-off migration: upload local output/*.json + _ui_state.json to Supabase.

Run this ONCE after applying supabase/001_schema.sql.
"""
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import db

OUT = Path(__file__).parent / "output"


def run():
    sb = db.get_client()
    assert sb is not None, "SUPABASE_URL / SUPABASE_SERVICE_KEY not set"

    # 1. Upsert entries from homework.json + course_diary.json
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from scraper import _entry_row, _test_key, _sched_key, _iso_date

    diary = json.loads((OUT / "course_diary.json").read_text())
    assignments = json.loads((OUT / "homework.json").read_text())
    entry_rows = [_entry_row(e, "course_diary") for e in diary] + \
                 [_entry_row(e, "assignment") for e in assignments]
    n = db.upsert_entries(entry_rows)
    print(f"[*] Upserted {n} entries")

    # 2. Tests
    tests = json.loads((OUT / "tests.json").read_text()) if (OUT / "tests.json").exists() else []
    test_rows = [{
        "test_key": _test_key(t),
        "test_date": _iso_date(t.get("date", "")),
        "subject": t.get("subject", ""),
        "test_type": t.get("test_type", ""),
        "description": t.get("description", ""),
        "weight": t.get("weight", ""),
        "grade": t.get("grade", ""),
    } for t in tests]
    n = db.upsert_tests(test_rows)
    print(f"[*] Upserted {n} tests")

    # 3. Schedule
    schedule = json.loads((OUT / "schedule.json").read_text()) if (OUT / "schedule.json").exists() else []
    sched_rows = [{
        "schedule_key": _sched_key(s),
        "start_time": s.get("start", ""),
        "title": s.get("title", ""),
        "details": s.get("text", ""),
    } for s in schedule]
    n = db.upsert_schedule(sched_rows)
    print(f"[*] Upserted {n} schedule items")

    # 4. Migrate ui_state (done + notes)
    state_p = OUT / "_ui_state.json"
    if state_p.exists():
        st = json.loads(state_p.read_text())
        rows = []
        done_keys = {k for k, v in st.get("done", {}).items() if v}
        note_keys = st.get("notes", {})
        for k in done_keys | note_keys.keys():
            rows.append({
                "entry_key": k,
                "done": k in done_keys,
                "note": note_keys.get(k, ""),
            })
        if rows:
            # chunked upsert
            CHUNK = 500
            total = 0
            for i in range(0, len(rows), CHUNK):
                batch = rows[i:i + CHUNK]
                sb.table("ui_state").upsert(batch, on_conflict="entry_key").execute()
                total += len(batch)
            print(f"[*] Upserted {total} ui_state rows ({len(done_keys)} done, {len(note_keys)} notes)")
        else:
            print("[*] No ui_state to migrate")
    else:
        print("[*] No _ui_state.json found, skipping state migration")

    print("\n✅ Migration complete. Dashboard now reads from Supabase.")


if __name__ == "__main__":
    run()
