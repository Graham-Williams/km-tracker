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
- **Soft-delete everywhere** — extend the `deleted_at` pattern (already on `cups`) to all tables so nothing is truly deleted. Helpful for debugging.
- **Visual refresh (mobile-first)** — clean, minimal, flat design. Mobile-first. Explore wheel animation library during this refactor.
- **Friendly URL** — custom domain or hostname instead of raw IP on local network.
- **Stats & Leaderboards** — standings, wins, trends across cups. Referenced in README but not yet implemented.
- **Overtime support** — ties sometimes trigger a 2-race overtime cup. Currently manual; could be formalized in the cup-session flow.
- **Race numbers beside names** — display the track/race number alongside race names for easier reference.
