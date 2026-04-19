"""Supabase client + data-access helpers shared by scraper.py and app.py."""
import os
from typing import Optional

from supabase import Client, create_client

_CLIENT: Optional[Client] = None


def get_client() -> Optional[Client]:
    """Return a cached Supabase client. Returns None if env is not configured."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (os.environ.get("SUPABASE_SERVICE_KEY")
           or os.environ.get("SUPABASE_KEY")
           or "").strip()
    if not url or not key:
        return None
    _CLIENT = create_client(url, key)
    return _CLIENT


# ---------- writes ----------

def _dedup(rows: list[dict], key: str) -> list[dict]:
    """Within a single batch, keep only the last row per conflict key."""
    seen: dict = {}
    for r in rows:
        k = r.get(key)
        if k is None:
            continue
        seen[k] = r  # last one wins
    return list(seen.values())


def upsert_entries(rows: list[dict]) -> int:
    sb = get_client()
    if not sb or not rows:
        return 0
    rows = _dedup(rows, "entry_key")
    total = 0
    CHUNK = 500
    for i in range(0, len(rows), CHUNK):
        batch = rows[i:i + CHUNK]
        sb.table("entries").upsert(batch, on_conflict="entry_key").execute()
        total += len(batch)
    return total


def upsert_tests(rows: list[dict]) -> int:
    sb = get_client()
    if not sb or not rows:
        return 0
    rows = _dedup(rows, "test_key")
    sb.table("tests").upsert(rows, on_conflict="test_key").execute()
    return len(rows)


def upsert_schedule(rows: list[dict]) -> int:
    sb = get_client()
    if not sb or not rows:
        return 0
    rows = _dedup(rows, "schedule_key")
    sb.table("schedule").upsert(rows, on_conflict="schedule_key").execute()
    return len(rows)


def record_run(**kwargs) -> None:
    sb = get_client()
    if not sb:
        return
    sb.table("scrape_runs").insert(kwargs).execute()


def set_done(entry_key: str, done: bool) -> None:
    sb = get_client()
    if not sb:
        return
    sb.table("ui_state").upsert(
        {"entry_key": entry_key, "done": done},
        on_conflict="entry_key",
    ).execute()


def set_note(entry_key: str, note: str) -> None:
    sb = get_client()
    if not sb:
        return
    sb.table("ui_state").upsert(
        {"entry_key": entry_key, "note": note},
        on_conflict="entry_key",
    ).execute()


def toggle_done(entry_key: str) -> bool:
    """Flip and return the new value."""
    sb = get_client()
    if not sb:
        return False
    row = (
        sb.table("ui_state").select("done").eq("entry_key", entry_key).limit(1)
        .execute().data
    )
    new_val = not (row and row[0].get("done"))
    set_done(entry_key, new_val)
    return new_val


# ---------- reads ----------

def fetch_entries(kind: Optional[str] = None) -> list[dict]:
    """Return all entries (with done/note joined in)."""
    sb = get_client()
    if not sb:
        return []
    q = sb.table("entries_with_state").select("*")
    if kind:
        q = q.eq("kind", kind)
    # up to 10k rows; Supabase client default limit is 1000
    res = q.range(0, 9999).execute()
    return res.data or []


def fetch_tests() -> list[dict]:
    sb = get_client()
    if not sb:
        return []
    return sb.table("tests").select("*").order("test_date", desc=True).execute().data or []


def fetch_schedule() -> list[dict]:
    sb = get_client()
    if not sb:
        return []
    return sb.table("schedule").select("*").execute().data or []


def fetch_last_run() -> Optional[dict]:
    sb = get_client()
    if not sb:
        return None
    rows = (
        sb.table("scrape_runs").select("*").order("started_at", desc=True).limit(1)
        .execute().data
    )
    return rows[0] if rows else None
