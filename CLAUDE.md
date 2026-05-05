# CLAUDE.md — sms-scraper / SMS Dashboard

## Architecture

Single-file Flask app (`app.py`). All HTML is server-rendered via `render_template_string`.
No frontend build step, no React, no bundler.

- **Lines 1–315**: Python imports, Supabase helpers, data enrichment (`enrich()`, `load_all()`), utilities (`teacher_label()`, `is_test_entry()`, `subject_hue()`, `esc()`) — **never touch these without good reason**
- **Lines ~316–665**: `BASE_CSS` — all styling lives here as a raw Python string (`r"""`)
- **Lines ~666–804**: `SHELL` — the HTML shell (topbar, nav tabs, stats grid, body slot)
- **`render_card(a)`** — renders one homework row (Proseed-style)
- **`render_section(label, css_cls, items)`** — wraps rows in a labeled section container
- **`home()` route** — groups assignments into sections; passes flat list for filtered stat views
- **Lines ~1259+**: API endpoints (`/toggle-done`, `/set-note`, `/scrape-now`, `/api/status`, `/api/ingest`) — **keep unchanged**

## Design system (Proseed-inspired, adopted this session)

| Token | Value | Usage |
|-------|-------|-------|
| `--accent` | `#F97316` | Orange — checkboxes, active nav, stat borders |
| `--bg` | `#FAFAF8` | Warm off-white page background |
| `--surface` | `#ffffff` | Card / container background |
| `--border` | `#E8E8E5` | All 1px borders |
| `--radius` | `10px` | Card corner radius |
| Font weight headings | 800 | Bold, tight tracking |

Card row layout: `[checkbox] [colored dot] [description] → [status pill] [subject tag] [teacher] [date]`

Section containers: white card with a labeled header + item count badge. Overdue = red tint header, Today = amber tint.

Homework groups (home view): **Overdue → Due Today → This Week → Upcoming → Done**

## Deployment

- Hosted on **Railway** — auto-deploys on push to `main`
- Database: **Supabase PostgreSQL** (project `clvffimduxfalwxjrhii`)
- Remote: `https://github.com/fanaman74/sms-dashboard.git`

## Git / auth

- Use a **classic PAT** (`ghp_…`) for `git push` — fine-grained PATs cause 403 even when the API shows admin permissions, because "Contents: Read and Write" scope must be explicitly granted per-repo on fine-grained tokens
- Classic PAT with `repo` scope just works

## CSS editing rules

- `BASE_CSS` is a raw string (`r"""`) — backslashes are literal, no escaping needed
- Dark-mode media query was removed in the Proseed redesign; add back only if needed
- Subject color dots / tags use CSS custom property `--h` (HSL hue from `subject_hue()`)
- Note inputs use class `card-note` (not `note-input` from the old design)
