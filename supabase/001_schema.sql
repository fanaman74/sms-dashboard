-- sms-dashboard schema

-- ---------- entries (homework + course_diary share this table) ----------
create table if not exists entries (
  id bigserial primary key,
  entry_key text unique not null,              -- "subject|date|hash"
  kind text not null,                          -- 'assignment' | 'course_diary'
  subject text not null,
  entry_date date,
  entry_date_text text,                        -- raw "DD/MM/YYYY" from source
  entry_type text,                             -- 'Assignments' | 'Course Diaries'
  description text default '',
  attachments jsonb default '[]'::jsonb,
  html text,
  first_seen timestamptz not null default now(),
  last_seen timestamptz not null default now()
);
create index if not exists entries_kind_date on entries (kind, entry_date desc);
create index if not exists entries_subject on entries (subject);

-- ---------- tests (graded exercises) ----------
create table if not exists tests (
  id bigserial primary key,
  test_key text unique not null,
  test_date date,
  subject text,
  test_type text,
  description text,
  weight text,
  grade text,
  scraped_at timestamptz not null default now()
);

-- ---------- schedule (calendar all-day / period events) ----------
create table if not exists schedule (
  id bigserial primary key,
  schedule_key text unique not null,
  start_time text,
  title text,
  details text,
  scraped_at timestamptz not null default now()
);

-- ---------- ui_state (done flag + notes; keyed by entry_key) ----------
create table if not exists ui_state (
  entry_key text primary key references entries(entry_key) on delete cascade,
  done boolean not null default false,
  note text not null default '',
  updated_at timestamptz not null default now()
);

-- ---------- scrape_runs (history of each run) ----------
create table if not exists scrape_runs (
  id bigserial primary key,
  started_at timestamptz not null default now(),
  assignments_count int default 0,
  diary_count int default 0,
  tests_count int default 0,
  schedule_count int default 0,
  new_items_count int default 0,
  source text default 'launchd'
);

-- ---------- convenience view ----------
create or replace view entries_with_state as
select
  e.*,
  coalesce(s.done, false) as done,
  coalesce(s.note, '')    as note
from entries e
left join ui_state s on s.entry_key = e.entry_key;
