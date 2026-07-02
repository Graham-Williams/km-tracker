# KM Tracker — Claude Context

## Project Overview

KM Tracker is a tracker and tooling suite for Kario Mart game nights. It records sessions, tracks stats, and provides utilities to assist with running the game. The project has persistent storage (SQLite or similar file-based DB).

This is a public GitHub repo — keep all committed content professional and general.

## Dev Workflow

- **Git:** Use conventional commits (`feat:`, `fix:`, `chore:`, etc.) with meaningful messages
- **Branches:** `feature/<name>`, `fix/<name>`, `chore/<name>`
- **Git operations are allowed** — branching, committing, pushing, and opening PRs
- **CI:** GitHub Actions runs pytest (unit + e2e) on every push to main and on PRs

## Tech Stack

- **Backend:** Python + Flask (server-rendered HTML via Jinja templates)
- **Database:** SQLite (via Python stdlib) — DB path is configured via `DB_PATH` in `.env` (defaults to `db/km_tracker.db`). Run with `--staging` flag to use a separate sandbox DB (`STAGING_DB_PATH`)
  - **Migrations:** fresh DBs come from `schema.sql`; changes to existing tables go in `db.run_migrations()`, which `init_db()` calls on every startup. Each step is guarded by a `PRAGMA table_info` check so it's idempotent/safe on the populated prod DB (the box rebuilds from `main` and the entrypoint runs `init_db`). Keep `schema.sql` and the migration in sync — new fresh DBs must match a fully-migrated one.
- **Testing:** pytest (Flask test client for unit/integration, Playwright for e2e)
- **Port:** 8080 (5000 conflicts with macOS AirPlay Receiver)
- **Network access:** Binds to `0.0.0.0` so other devices on the local network can reach it
- **Deployment:** Docker container with gunicorn (see `Dockerfile`, `docker-compose.yml`). The `app` container runs as a **non-root user (UID 10001)** and publishes **no host port** — the `cloudflared` connector reaches it over the compose network at `http://app:8080`. Because `./data` is bind-mounted, the host dir must be `chown`'d to UID 10001 before first launch (see `DEPLOY.md`). Local dev still uses `python app.py` directly (debug off by default; set `FLASK_DEBUG=1` to enable). Can be self-hosted on a headless Linux box via Docker + a Cloudflare named tunnel (`cloudflared` service in compose, image pinned to a released tag), gated behind Cloudflare Access. `SECRET_KEY` and `TUNNEL_TOKEN` come from a gitignored `.env` (`cloudflared` reads `TUNNEL_TOKEN` from env, not the command line; see `.env.example`). Full runbook in `DEPLOY.md`
- **Dependencies:** `requirements.txt` = prod (flask, python-dotenv, gunicorn); `requirements-dev.txt` = prod + test deps (pytest, playwright)

## Self-Hosted Deployment & Backups

The app is self-hosted on a headless Ubuntu Server box ("personalserver"), running
via Docker Compose. The box **tracks `main`**.

### Deploy procedure (box)

Until the Cloudflare tunnel is fully set up, deploy = pull + rebuild + restart with
the CI override (which publishes the host port so the box is reachable on the LAN):

```bash
cd /home/graham/km-tracker
git pull
docker compose -f docker-compose.yml -f docker-compose.ci.yml up -d --build app
```

(Once the tunnel is live, drop the `docker-compose.ci.yml` override — the tunnel
reaches the app over the internal network and no host port should be published.)

> **DEPLOY-SAFETY RULE — never restart/redeploy while a cup is in progress.**
> A live cup session is mid-write state (`cups.status = 'in_progress'`). Restarting
> the container mid-cup can lose or corrupt that session's progress. **Before any
> restart/redeploy, check that zero cups are in progress and only proceed when the
> count is 0:**
>
> ```bash
> sqlite3 data/km_tracker.db \
>   "SELECT COUNT(*) FROM cups WHERE status='in_progress' AND deleted_at IS NULL;"
> ```
>
> If it returns anything other than `0`, do **not** restart — wait until the cup is
> finished (or coordinate with whoever's running game night).

### Backups

Automated by `scripts/backup.sh` on the **host** (not in the container), driven by
the `deploy/km-backup.{service,timer}` systemd units:

- **Local snapshots:** consistent SQLite online-backup snapshots into `data/backups/`,
  deduplicated by sha256, pruned to the newest `LOCAL_RETENTION` (default 100). The
  timer fires every 5 minutes.
- **Off-box copies:** pushed to Google Drive via `rclone` on a throttled cadence
  (only when the DB changed and ≥ `DRIVE_PUSH_INTERVAL_MIN` minutes — default 15 —
  since the last push), pruned to the newest `DRIVE_RETENTION` (default 50).
- The local half always runs even if the Drive push can't (rclone unconfigured →
  reports the error, exits non-zero, but local snapshots are unaffected).
- Config: gitignored `.env.backup` (template: `.env.backup.example`). **No secrets in
  the repo** — the rclone OAuth token lives only in `~/.config/rclone/rclone.conf`.

Full setup/runbook (rclone headless auth, systemd install, restore) is in `DEPLOY.md`
→ "Automated backups".

### Staging environment

A hosted **staging** playground runs at **`staging-km.graham-williams.com`** — a safe
place to try changes/UI with **fake data only** (no real game-night data). It's a
**second `app` container** (`staging-app`) on the same box, behind the **same
Cloudflare tunnel**, but isolated from prod by design:

- **Separate DB:** `data/km_tracker.staging.db` (prod's `km_tracker.db` is never
  touched). Same `./data` volume; driven purely by the `DB_PATH` env var.
- **Separate Cloudflare Access app → separate AUD.** Staging has its own Access
  application, so its own AUD, supplied via the box `.env` as
  `STAGING_CF_ACCESS_AUD` — **never reuse the prod `CF_ACCESS_AUD`.**
- Compose override: `docker-compose.staging.yml` (layers on `docker-compose.yml` +
  `docker-compose.access.yml`). No host port published — reachable only via the tunnel.

Deploy prod + staging together:

```bash
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  -f docker-compose.staging.yml up -d --build
```

**Reseed staging on demand** (wipes + repopulates fake data; deterministic set of
6 obviously-fake players + 12 cups incl. one in-progress):

```bash
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  -f docker-compose.staging.yml exec staging-app \
  python scripts/seed_staging.py --reset
```

`scripts/seed_staging.py` has a **hard safety rail**: it refuses to run unless the
target DB's basename contains `"staging"` (override only with `--force`), so it can
never wipe the prod DB. The **never-redeploy-mid-cup** rule still applies to the
prod container. Full runbook (Cloudflare dashboard steps, first bring-up) is in
`DEPLOY.md` → "Staging environment".

> Note: `docker-compose.access.yml` also wires the prod Access env vars
> (`APP_HOST`, `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`) onto the base `app`
> service, so the prod deploy command is now
> `docker compose -f docker-compose.yml -f docker-compose.access.yml up -d --build`.

## Security Hardening (public access)

The app is exposed publicly at `km.graham-williams.com` behind a Cloudflare
tunnel + Cloudflare Access. Two `before_request` hooks in `app.py` back up Access
as defense-in-depth (full operator docs in `DEPLOY.md` → "Public access hardening"):

- **CSRF — Origin/Referer host check (`csrf_origin_check`).** On every
  `POST`/`PUT`/`PATCH`/`DELETE`, rejects (403) requests whose `Origin` (else
  `Referer`) host ≠ the app's own host. Requests with neither header (curl, the
  Flask **test client**, non-browser callers) are allowed; safe methods are never
  checked. This covers both the HTML `<form>` POSTs and the JSON/`fetch`
  endpoints (`spin`, `voto`, `half-veto`, `next-race`) without touching templates
  or JS. Expected host = `APP_HOST` / `APP_ORIGIN` env if set, else the request
  `Host` (Cloudflare forwards the real hostname, so no config needed). **On by
  default;** set `CSRF_PROTECTION=0` to disable for local dev. Playwright e2e
  issues same-origin requests, so it passes unchanged.
- **Cloudflare Access JWT verification (`cloudflare_access_check`).** Enforced
  **only when both `CF_ACCESS_TEAM_DOMAIN` and `CF_ACCESS_AUD` are set** (unset →
  skipped, so dev/tailnet keep working). Requires + validates the RS256 Access
  token from the `Cf-Access-Jwt-Assertion` header (fallback: `CF_Authorization`
  cookie): signature checked against the team JWKS
  (`https://<TEAM_DOMAIN>/cdn-cgi/access/certs`, fetched with stdlib `urllib`,
  cached in-process, auto-refreshed on unknown `kid`), plus `aud`/issuer/expiry.
  Missing/invalid → 403. `/static/*` is exempt.

Dependency added for JWT verification: **`PyJWT[crypto]`** (in `requirements.txt`,
so it ships in the Docker image). Config env vars are documented in `.env.example`.

## UI / Design System

As of the `feature/ui-makeover` work, the app has a shared design system instead of per-template inline `<style>` blocks.

- **`static/css/app.css`** — the single shared design system. CSS-custom-property based: all colors/spacing/radii/shadows are defined as variables in `:root` (light) and overridden in a `@media (prefers-color-scheme: dark)` block, so **light + dark mode are automatic**. Mobile-first, centered content column (`--content-width`, ~520px). Flask serves it from `/static`.
- **`templates/base.html`** — the shared layout **every template should `{% extends "base.html" %}`**. It sets doctype/viewport/`color-scheme` meta, links `app.css` via `{{ url_for('static', filename='css/app.css') }}`, and renders flash messages (uses `get_flashed_messages(with_categories=true)`: `success`→`.flash-success`, `info`→`.flash-info`, everything else incl. uncategorized→`.flash-error`).
- **No JS framework** — vanilla only; the design system is pure CSS (plus a tiny vanilla-JS theme picker).

### Accent theming (user-selectable)
The **accent** color is themeable independently of the rest of the palette; **destructive styling never changes** (`--color-danger` and `.btn-danger` are fixed red regardless of theme).

- **Mechanism:** a `data-theme` attribute on `<html>` overrides only the accent variables (`--color-accent`, `--color-accent-hover`, `--color-accent-soft`, `--color-accent-gradient`; `--color-accent-contrast` stays near-white). Each theme is a `[data-theme="name"]` block in `:root`-level (light) CSS **and** a mirrored block inside the `@media (prefers-color-scheme: dark)` query, so every theme works in both modes. The **default is `violet`** — its values are also the base `:root`/dark `:root` accent values, so no attribute = violet.
- **`--color-accent-gradient`:** the primary-button background. Defaults to `var(--color-accent)` (solid). The two gradient themes (`aurora`, `sunset`) set it to a `linear-gradient(...)`, giving multi-color buttons while links/badges/focus rings still use the solid `--color-accent`. `.btn-primary` uses `background: var(--color-accent-gradient)` and hovers via `filter: brightness(1.07)` (works for solid + gradient).
- **Themes shipped (8):** `violet` (default), `indigo`, `ocean`, `teal`, `emerald`, `rose`, `aurora` (gradient violet→cyan), `sunset` (gradient orange→pink).
- **Persistence:** stored in `localStorage` under key **`km-theme`**. An inline no-FOUC `<script>` at the **top of `<head>`** (before the CSS link) reads it and sets `data-theme` before first paint — no flash of the wrong theme. The picker logic is a separate vanilla `<script>` near the end of `<body>`.
- **Picker UI:** a fixed circular "palette" trigger (`.theme-picker`/`.theme-trigger`, top-right, shows current accent) opens a popover (`.theme-menu`) of swatch buttons (`.theme-option`/`.theme-swatch`). Active theme marked via `aria-pressed="true"`. Closes on outside-click / Escape. All styled with design-system variables.
- **To add a theme:** (1) add a `[data-theme="name"]` block in the light themes section of `app.css` and a mirrored one inside the dark `@media` block (set accent/hover/soft, and gradient if it's a gradient theme); (2) add a `.theme-option` button with a hardcoded swatch color in `base.html`'s `.theme-options`. No JS changes needed — the picker reads all `.theme-option`s generically.

### base.html blocks
- `{% block title %}` — `<title>` text (default "KM Tracker").
- `{% block back %}` — optional back-link; put `<a class="back-link" href="/">← Home</a>` here.
- `{% block header %}` — whole header region (override only for a custom header). By default renders `<h1 class="page-title">{% block heading %}{% endblock %}</h1>` + `{% block subtitle %}{% endblock %}`.
  - `{% block heading %}` — page H1 text.
  - `{% block subtitle %}` — optional; supply `<p class="page-subtitle">…</p>`.
- `{% block styles %}` — optional `<head>` slot for rare page-specific CSS (e.g. the wheel animation on the race page). Prefer app.css classes; only use this for genuinely page-unique styling.
- `{% block content %}` — main page body.

### Refactor recipe (canonical skeleton)
```jinja
{% extends "base.html" %}
{% block title %}Cups — KM Tracker{% endblock %}
{% block back %}<a class="back-link" href="/">← Home</a>{% endblock %}
{% block heading %}Cups{% endblock %}
{% block subtitle %}<p class="page-subtitle">Optional subtitle</p>{% endblock %}
{% block content %}
  <!-- design-system markup here -->
{% endblock %}
```
Remove the old `<!DOCTYPE>`, `<html>`, `<head>`, inline `<style>`, the manual flash `{% with %}` loop, and the hand-rolled back-link — base.html provides all of them. **Preserve every `url_for`, form `action`/`method`, input `name`/`id`/`value`, `maxlength`/`required`, checkbox names, Jinja control flow, and custom filters (`|format_line`) exactly** — only markup/styling changes.

### Component-class vocabulary (use these; no need to read app.css)
- **Layout:** `.page` (provided by base.html), `.stack` (vertical full-width stack with gap).
- **Header:** `.page-header`, `.page-title`, `.page-subtitle`, `.back-link`, `.section-heading` (small uppercase divider label).
- **Buttons:** `.btn` (base) + one variant: `.btn-primary` (accent), `.btn-secondary` (outlined surface), `.btn-danger` (outlined red), `.btn-ghost` (subtle). Modifiers: `.btn-sm` (small inline action), `.btn-block` (full-width large CTA).
- **Cards:** `.card` (rounded surface w/ shadow). Forms styled as a card: `class="form card"`.
- **Lists / rows:** `.list` (rounded surface container, `<ul>`), `.list-item` (row, left content + right actions, hover state), `.list-content` (left column), `.list-title`, `.list-meta` (secondary line), `.list-actions` (right-aligned action group).
- **Badges/pills:** `.badge` (default neutral) + `.badge-line`, `.badge-status` (success-tinted), `.badge-info`.
- **Forms:** `.form` (vertical flex), `.field` (labeled field column), `.field-row` (horizontal row, children flex equally), `.check` (inline checkbox+label). Native `input`/`select`/`textarea` are styled globally (incl. focus ring + checkbox accent) — no class needed.
- **Flash:** `.flash` + `.flash-error` / `.flash-success` / `.flash-info`, wrapped in `.flashes` (base.html handles this automatically).
- **Empty state:** `.empty` (dashed centered placeholder).
- **Table:** `.table` (standings/score grids); `th.num`/`td.num` for right-aligned tabular numbers.
- **Utilities:** `.muted`, `.text-faint`, `.mt-4`, `.mb-4`.

### CSS variable names (app.css tokens)
Colors: `--color-bg`, `--color-surface`, `--color-surface-2`, `--color-border`, `--color-border-strong`, `--color-text`, `--color-muted`, `--color-faint`, `--color-accent`, `--color-accent-hover`, `--color-accent-contrast`, `--color-accent-soft`, `--color-accent-gradient` (primary-button bg; solid by default, gradient for `aurora`/`sunset` — see Accent theming), `--color-danger`(+`-soft`), `--color-success`(+`-soft`), `--color-info`(+`-soft`).
Other: `--font-sans`; spacing `--space-1`…`--space-7`; radius `--radius-sm|md|lg|pill`; `--shadow-sm`, `--shadow-md`; `--content-width`; `--transition`.

## Code Style & Conventions

- Keep it simple — don't over-engineer
- Prefer patterns already established in the codebase over introducing new ones
- Don't add abstractions for one-off use cases
- Don't add error handling for scenarios that can't happen

## Collaboration Style

Graham is a senior software engineer with ~10 years of experience. Don't simplify explanations or skip over technical details — he wants to understand decisions and tradeoffs. Ask questions before diving into non-trivial work.

## Documentation

Significant design decisions belong in the README, not buried in code comments or local notes. If a decision is worth remembering, it's worth putting where anyone reading the repo can find it.

## Feature Context (Local)

Per-feature Claude context lives in `.claude/features/` — gitignored, personal, not committed. Each feature gets a subdirectory with markdown covering: overview, design decisions, gotchas, related files, and verification steps (automated + manual).

The format is: one `context.md` per feature directory, using `.claude/features/TEMPLATE.md` as a starting point. Read the relevant feature context before working on a feature.

Note: since `.claude/` is gitignored, recreate it on a new machine as needed — the convention is described here.

## UI Testing

After any UI change, run autonomous visual testing before asking for manual verification. Use Playwright in headless mode to take screenshots at these viewport sizes:

| Device | Width x Height |
|--------|---------------|
| Desktop | 1440 x 900 |
| Tablet (iPad) | 820 x 1180 |
| Phone (iPhone 16 Pro) | 393 x 852 |
| Phone (Pixel 9 Pro) | 412 x 915 |

Save screenshots to `.claude/features/<feature-name>/screenshots/` (e.g. `desktop_index.png`, `iphone_cup_session.png`). Include all screenshots in the PR description when opening the PR.

## Game Editions

The app supports multiple Mario Kart editions per cup session; **only the track list differs** between editions — all house rules are identical (`MAX_RACES=4`, `MAX_HALF_VETOES=3`, `MAX_VOTOES=4`, line deltas -3/0/+3, all in `app.py`).

- **`maps.py`** — `TRACK_SETS = {"wii": [...32...], "mk8dx": [...96...]}` (MK8DX = 48 base + 48 Booster Course Pass, 24 cups). `EDITION_LABELS` maps the stored string to a display label. Helpers: `courses_for(edition)` and `edition_label(edition)` (both fall back to `DEFAULT_EDITION = "wii"`). `COURSES` remains a flat alias of the Wii list for backward compatibility.
- **Storage:** `cups.game_edition TEXT NOT NULL DEFAULT 'wii'`. Chosen on the new-session screen (`cup_session_new.html` `<select name="game_edition">`), validated against `TRACK_SETS` in `cup_session_create`, and read back in the session/spin/complete routes to pick the right track set.
- **Display:** `edition_label` is registered as a Jinja filter (`{{ cup.game_edition|edition_label }}`) and also passed as a context var to the race/complete screens. Shown on `/cups` history and the session headers.
- **Spin wheel** (`cup_session_race.html`): slice colors are generated programmatically from `NUM_SLICES` (evenly-spaced HSL hues, alternating lightness) and label font size scales to the slice count — so any edition/size renders. Do **not** reintroduce a hardcoded per-track color array.
- **Adding an edition:** add a track list + `EDITION_LABELS` entry in `maps.py`; the select, routes, wheel, and display pick it up automatically. No schema change needed.

## Planned Features

Running list of features under consideration. Not commitments — ideas to pull from when planning new work.

- **Group cups by session** — a "session" is a game night containing multiple cups played back-to-back. Add a parent Session entity so the UI can show "Game Night 2026-04-12: 4 cups" rather than a flat list.
- **Bet tracking** — players typically bet on cups or sessions. Record who bet what, who won, and settlement status. Schema TBD.
- **Screenshot-based score entry** — upload a photo of the end-of-cup scoreboard, auto-parse scores (OCR + vision). Mapping scores to players should be straightforward once extraction works.
- **Stale veto forfeit** — enforce the "use it or lose it" rule: entering race 3 with 3 unused half-vetoes auto-forfeits one. (Next feature up.)
- **Record cup completion time, not start time** — live cup sessions currently stamp `cups.date` when the session starts (`status = 'in_progress'`). For accurate session history the timestamp should reflect when the cup finishes. Options: update `date` at completion, or add a separate `completed_at` column and keep `date` as start. Note the `UNIQUE` constraint on `date` — a schema change may be needed.
- **Soft-delete everywhere** — extend the `deleted_at` pattern (already on `cups`) to all tables so nothing is truly deleted. Helpful for debugging.
- **Visual refresh (mobile-first)** — clean, minimal, flat design. Mobile-first. Explore wheel animation library during this refactor.
- **Friendly URL** — custom domain or hostname instead of raw IP on local network.
- **Stats & Leaderboards** — standings, wins, trends across cups. Referenced in README but not yet implemented.
- **Overtime support** — ties sometimes trigger a 2-race overtime cup. Currently manual; could be formalized in the cup-session flow.
- **Race numbers beside names** — display the track/race number alongside race names for easier reference.
