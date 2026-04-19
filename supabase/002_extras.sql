-- Announcements / inbox
create table if not exists messages (
  id bigserial primary key,
  message_key text unique not null,
  subject text,
  sender text,
  excerpt text,
  sent_label text,
  sent_date date,
  attachments jsonb not null default '[]'::jsonb,
  unread boolean not null default false,
  first_seen timestamptz not null default now(),
  last_seen timestamptz not null default now()
);
create index if not exists messages_sent_date on messages (sent_date desc nulls last);

-- Term reports (PDF downloads)
create table if not exists term_reports (
  id bigserial primary key,
  report_id text unique not null,
  label text,
  year_label text,
  download_url text,
  scraped_at timestamptz not null default now()
);

-- Course info (teacher map)
create table if not exists courses (
  course_code text primary key,
  course_name text,
  teachers jsonb not null default '[]'::jsonb,
  course_description text,
  scraped_at timestamptz not null default now()
);
