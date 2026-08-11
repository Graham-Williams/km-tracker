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
  - **Concurrency (issue #42):** every connection from `db.get_connection()` runs in **WAL mode** (`journal_mode=WAL`, so readers don't block writers) with a `busy_timeout=5000` (writers queue instead of instantly raising "database is locked"). A scoped Flask `errorhandler(sqlite3.OperationalError)` in `app.py` maps only lock-timeout errors to a controlled response (503 JSON for fetch/XHR, flash+redirect for page navs); any other `OperationalError` is re-raised so real bugs still 500. WAL leaves `-wal`/`-shm` sidecars next to the DB — `backup.sh` (online `.backup()`) and `seed_staging.py` already handle these.
  - **Migrations:** fresh DBs come from `schema.sql`; changes to existing tables go in `db.run_migrations()`, which `init_db()` calls on every startup. Each step is guarded by a `PRAGMA table_info` check so it's idempotent/safe on the populated prod DB (the box rebuilds from `main` and the entrypoint runs `init_db`). Keep `schema.sql` and the migration in sync — new fresh DBs must match a fully-migrated one.
- **Testing:** pytest (Flask test client for unit/integration, Playwright for e2e)
- **Port:** 8080 (5000 conflicts with macOS AirPlay Receiver)
- **Network access:** Binds to `0.0.0.0` so other devices on the local network can reach it
- **Deployment:** Docker container with gunicorn (see `Dockerfile`, `docker-compose.yml`). The `app` container runs as a **non-root user (UID 10001)** and publishes **no host port** — the `cloudflared` connector reaches it over the compose network at `http://app:8080`. Because `./data` is bind-mounted, the host dir must be `chown`'d to UID 10001 before first launch (see `DEPLOY.md`). Local dev still uses `python app.py` directly (debug off by default; set `FLASK_DEBUG=1` to enable). Can be self-hosted on a headless Linux box via Docker + a Cloudflare named tunnel (`cloudflared` service in compose, image pinned to a released tag), gated behind Cloudflare Access. `SECRET_KEY` and `TUNNEL_TOKEN` come from a gitignored `.env` (`cloudflared` reads `TUNNEL_TOKEN` from env, not the command line; see `.env.example`). Full runbook in `DEPLOY.md`
- **Dependencies:** `requirements.txt` = prod (flask, python-dotenv, gunicorn, PyJWT, anthropic); `requirements-dev.txt` = prod + test deps (pytest, playwright)

## Self-Hosted Deployment & Backups

The app is self-hosted on a headless Ubuntu Server box ("personalserver"), running
via Docker Compose. The box **tracks `main`**.

### Deploy procedure (box)

Until the Cloudflare tunnel is fully set up, deploy = pull + rebuild + restart with
the CI override (which publishes the host port so the box is reachable on the LAN):

```bash
cd /home/<user>/km-tracker
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

Automated by `scripts/backup.sh` (orchestrated on the **host**, but the snapshot
itself runs **inside the app container**), driven by the
`deploy/km-backup.{service,timer}` systemd units:

- **The snapshot runs INSIDE the container (UID 10001), not host-side.** The live
  DB is WAL-mode and its `-wal`/`-shm` sidecars are container-owned; SQLite's
  online backup API must write them to take its read lock, so a host-side backup
  fails `sqlite3.OperationalError: attempt to write a readonly database`. The
  script runs the `.backup()` via `docker exec` in `BACKUP_CONTAINER` (default
  `km-tracker-app-1`) against `CONTAINER_DB_PATH` (default `/data/km_tracker.db`),
  then `docker cp`s the finished snapshot out to the host. The backup user must be
  able to run `docker` (in the `docker` group). *(This was the root cause of a long silent backup
  outage. The host-side online-backup did NOT fail deterministically — it
  **flapped**, which is why it went unnoticed. A **rolling** 30-day journal query
  (`journalctl --since "30 days ago"`) run on 2026-08-10 counted 7,834 timer
  starts: 7,799 `attempt to write a readonly database` failures and just 35
  successes. Treat those as a snapshot of a moving window, not exact totals —
  re-running the same query minutes later shifts them, and a fixed
  2026-07-11→2026-08-10 window reads 7,835 starts / 7,823 failures / 12
  successes. The shape is the point: near-total failure either way. And the
  successes overstate the good news — only **3** of the 35 wrote a new snapshot
  file (`saved local snapshot`); the other 32 got past the backup step but
  produced a DB identical to the previous one and deduped it away (`no change`).
  Correspondingly only three snapshots reached Drive *in that window*
  (`20260729T193843Z`, `20260810T073719Z`, `20260810T233609Z`) — the first
  landing 23 days after the last pre-outage push (`20260706T221310Z`, one of 8
  pushed on 2026-07-06 before the outage began), then 12 days apart. It succeeds
  only when SQLite doesn't need to create or write the container-owned `-shm` —
  i.e. when the WAL is empty or a reusable read-mark already exists — so an idle
  app can make it look healthy. Fixed by this container-side approach,
  2026-08-10; nothing landed on 2026-07-28.)*
- **Local snapshots:** consistent SQLite online-backup snapshots into
  `~/km-backups/snapshots/` (default `LOCAL_BACKUP_DIR`; **outside** the
  container-owned `data/` bind-mount so the host backup process can write it —
  issue #19), deduplicated by sha256, pruned to the newest `LOCAL_RETENTION`
  (default 100). The timer fires every 5 minutes.
- **Off-box copies:** pushed to Google Drive via `rclone` on a throttled cadence
  (only when the DB changed and ≥ `DRIVE_PUSH_INTERVAL_MIN` minutes — default 15 —
  since the last push), pruned to the newest `DRIVE_RETENTION` (default 50).
- The local half always runs even if the Drive push can't (rclone not installed,
  `RCLONE_DEST` unset, or the remote missing from the rclone config → warns and
  **exits 0**, so the unit isn't marked failed every 5 minutes during setup; only
  a *configured* remote that actually errors exits non-zero, and even then the
  local snapshot is kept).
- **Empty-DB guard:** `entrypoint.sh` runs `init_db()` on EVERY container start, so
  a `/data` remounted empty makes the app recreate a schema-only DB — valid,
  `integrity_check`-clean, full table count, **zero rows**. The script refuses a
  snapshot with no rows in any user table when the previous kept snapshot had data
  (rows, not file size, so a `VACUUM` or deleting old cups can't false-positive);
  a first-ever run with no previous snapshot is still allowed, and
  `ALLOW_EMPTY_SNAPSHOT=1` overrides it deliberately.
- Config: gitignored `.env.backup` (template: `.env.backup.example`). **No secrets in
  the repo** — the rclone OAuth token lives only in `~/.config/rclone/rclone.conf`.
- **Restoring is NOT `cp snapshot data/km_tracker.db`.** The live DB is WAL-mode, so
  a stale `km_tracker.db-wal` sits next to it; copy only the main file and SQLite
  **replays that orphaned WAL over the restored image** — no error, the app serves
  the PRE-restore data, and the next checkpoint bakes the stale pages permanently
  into the file (two DB images merged = corruption). Ownership matters too: the
  restored file must end up owned by UID 10001 or the app fails every write with
  `attempt to write a readonly database` (a `sudo cp` **over the existing** file
  keeps its 10001 ownership; copying into an emptied/fresh `data/` lands as root —
  so always `chown`). Correct sequence: park the backup timer → stop the container
  → `rm -f data/km_tracker.db-{wal,shm}` → copy the snapshot → `chown 10001:10001`
  → start → **verify** (compare a known count in the snapshot against what the
  running container reads; the failure mode is silent) → restart the timer.

Full setup/runbook (rclone headless auth, systemd install, and the full verified
restore procedure) is in `DEPLOY.md` → "Automated backups" / "Restore from a
snapshot".

### Staging environment

A hosted **staging** playground runs at **`staging-km.graham-williams.com`** — a safe
place to try changes/UI with **fake data only** (no real game-night data). It's a
**second `app` container** (`staging-app`) on the same box, behind the **same
Cloudflare tunnel**, but isolated from prod by design:

- **Separate DB:** `data/km_tracker.staging.db` (prod's `km_tracker.db` is never
  touched). Same `./data` volume; driven purely by the `DB_PATH` env var.
- **Environment awareness (`APP_ENV`):** the app reads `APP_ENV` once at startup
  (`app.py`). Unset/unrecognized → `production` (safe default — prod needs no
  config). `docker-compose.staging.yml` sets `APP_ENV=staging` on `staging-app`
  (and `docker-compose.yml` sets `APP_ENV=production` explicitly on prod). A
  context processor exposes `app_env` and `is_staging` to **all** templates for
  environment-specific tweaks. Current uses (both in `base.html`, so they apply
  to every page): the browser tab title is prefixed `[STG] ` on staging (outside
  the `title` block); prod title is unchanged (`KM Tracker`). And the **favicon
  is environment-specific** — staging serves Baby Peach icons, prod/default
  serves the winged blue Spiny Shell. Two parallel icon sets live in `static/`:
  the defaults (`favicon.ico` multi-size 16/32/48, `favicon-32x32.png`,
  `android-chrome-192x192.png`, `apple-touch-icon.png` — 180px composited on
  the violet accent `#7c3aed` since iOS blackens transparency) and `-staging`
  suffixed twins; `base.html` picks via a Jinja `icon_suffix` variable
  (`'-staging' if is_staging else ''`). All PNGs are metadata-free (pixel data
  only). The prod `.ico`/32px use a tighter crop on the shell body (full winged
  artwork is illegible at 16px); the large prod icons use the full artwork.
- **Sign-in: the shared app-password gate (`APP_PASSWORD`), same as prod.**
  Staging's own Cloudflare Access application (email PIN) was **retired
  2026-08-05** and the edge app deleted, so `STAGING_CF_ACCESS_AUD` is blank and
  app-side JWT verification is off on staging. Staging inherits prod's
  `APP_PASSWORD` unless `STAGING_APP_PASSWORD` is set. (Retiring the PIN also
  means an agent can reach staging's public URL, which an email PIN made
  impossible.) If staging is ever re-gated with Access, it needs its **own**
  AUD — **never reuse the prod `CF_ACCESS_AUD`.**
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
tunnel + Cloudflare Access. Three `before_request` hooks in `app.py` handle
access control (full operator docs in `DEPLOY.md` → "Public access hardening"
and "Sign-in"):

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
  Missing/invalid → 403. `/static/*` and `/healthz` are exempt.
- **App-level shared-password login gate (`password_gate_check` + `/login`,
  `/logout`).** The intended **replacement** for the Cloudflare Access emailed-PIN
  flow: hand friends **one shared password**; entering it once grants a **30-day
  signed-cookie** session. **Env-gated on `APP_PASSWORD`** — set → gate ACTIVE
  (unauthenticated requests 302 → `/login?next=<path>`); blank/unset → gate OFF
  (app behaves as before). This lets it ship **dormant** behind Access, then
  activate at cutover by setting `APP_PASSWORD`. `POST /login` does a
  **constant-time** compare (`hmac.compare_digest`), sets a session cookie holding
  only a signed `kmauth` marker (**never** the password; signed with `SECRET_KEY`
  via Flask/itsdangerous), flagged **HttpOnly + Secure + SameSite=Lax**. `next` is
  **open-redirect-safe** (local paths only). Failures are **rate-limited per IP**
  (10/15 min → 429, keyed off `CF-Connecting-IP`; in-memory, per-process).
  **Exempt from the gate:** `/static/*`, `/login`, `/logout`, `/healthz`.
  `SESSION_SECRET` is an optional alias for the cookie-signing key (blank → reuse
  `SECRET_KEY`); `SESSION_COOKIE_SECURE` defaults ON (set `0` only for plain-HTTP
  local dev / tests). New unauthenticated **`GET /healthz`** liveness route is
  exempt from **both** gates. The two mechanisms are independent hooks meant to
  run one-at-a-time; nothing breaks if both are briefly on (Access gates first).
  Compose passes `APP_PASSWORD`/`SESSION_SECRET` (empty-safe) to prod + staging;
  staging can use `STAGING_APP_PASSWORD` to activate independently.

Dependency added for JWT verification: **`PyJWT[crypto]`** (in `requirements.txt`,
so it ships in the Docker image). Config env vars are documented in `.env.example`.
The password gate adds **no new dependencies** (uses stdlib `hmac` + Flask's
built-in signed session).

### Hardening test coverage

Both hooks have dedicated offline unit tests (no network; a real RS256 keypair
is generated in-process and the JWKS fetch is monkeypatched):

- `tests/test_cf_access.py` — CF Access JWT verification: valid/expired/wrong-aud/
  wrong-issuer/bad-signature/malformed/kid-less/HS256-confusion tokens, static
  exemption, JWKS caching + unknown-`kid` throttling + key rotation + fail-closed.
- `tests/test_csrf.py` — Origin/Referer pinning: matching vs. cross-origin,
  `null` Origin, port mismatch, `APP_HOST`/`APP_ORIGIN` pinning, safe methods,
  JSON endpoints, kill switch.
- `tests/test_password_gate.py` — the shared-password gate: gate off by default,
  unauth protected route → 302 `/login`, correct password grants access, wrong/
  empty rejected (401), logout clears session, static + `/healthz` stay open with
  the gate on, open-redirect `next` rejected (absolute/`//`/`/\`/`javascript:`),
  rate-limit trips at 10 fails (429, per-IP, cleared on success), cookie flags
  (HttpOnly/Secure/SameSite=Lax), password never in the cookie, forged/unsigned
  session cookie rejected.
- `tests/test_hostile_input.py` — malformed/hostile input on the main POST
  endpoints. Bad input must return a 4xx / flash+redirect, never a 500, and
  never persist bad state. **The input-validation bugs it documents (BUG-1 …
  BUG-10, listed in the module docstring) are now FIXED** — all cases pass as
  ordinary tests (no `xfail` remaining). The hardening in `app.py`:
  - `parse_int_field()` + the `InvalidInput` exception centralize "parse a form
    field to a valid int or reject cleanly." It calls `int()` (catches
    non-numeric) and range-checks against SQLite's signed 64-bit INTEGER
    (`SQLITE_MIN_INT`/`SQLITE_MAX_INT`) so an oversized value is rejected with a
    400/flash instead of raising `OverflowError` mid-transaction.
  - `parse_scores_from_form()` raises `InvalidInput` on a bad `player_ids[]` /
    `scores[]`; `create_cup`, `update_cup`, and `cup_session_submit` catch it
    (closing their conn first where one is open) and flash+redirect.
  - `create_score` / `update_score` / `update_player` validate ids/scores/line
    via `parse_int_field` before binding, and always close the connection on the
    error path.
  - `cup_session_create` validates `player_ids[]` **before** the cup INSERT and
    wraps the write in `try/finally: conn.close()`, so a bad id can no longer
    leak an open write transaction (the old "database is locked" bug).
  - JSON endpoints type-check scalars: `half-veto` rejects a non-scalar
    `player_id` (list/dict) with 400; `next-race` requires `map` to be a
    non-empty string. Both previously raised `sqlite3.InterfaceError` → 500.
  - **Round 2 (security-review follow-ups, F1–F4 in the module docstring):**
    - **F1** `half-veto` also RANGE-checks `player_id` via `parse_int_field`
      (type-only guard let an out-of-range int reach the bind → `OverflowError`
      500) and now wraps all DB work in `try/finally: conn.close()`.
    - **F2** `checked_line_score(score, line)` validates the SUM (`line_score`)
      against `SQLITE_MIN/MAX_INT`. Two in-range operands can still overflow;
      it's called in `parse_scores_from_form` and `create_score`/`update_score`
      so the sum can no longer 500 at bind time.
    - **F3** `update_cup` (empty/invalid date), `update_player` (empty name),
      and `update_score` (empty score) error paths now close their connection.
    - **F4** `app.config["MAX_CONTENT_LENGTH"] = 256 KB` caps request bodies
      (→ 413), and `parse_scores_from_form` rejects lists longer than
      `MAX_SCORE_ROWS` (50) — oversized-input DoS flank.
- `tests/test_session_flow_abuse.py` — out-of-order/double-submit session
  flows: double score submit (no duplicate scores / double line adjustments),
  acting on completed/cancelled cups, veto/race counters never exceed limits,
  duplicate race-number conflict, skipped steps.
- `tests/test_data_integrity.py` — regression tests for the data-integrity
  batch (issues #36/#39/#40/#41/#45); see "Data-integrity guards" below.

### Data-integrity guards (issues #36, #39, #40, #41, #45)

Stat-corrupting / crash holes found by `break-staging`, now guarded. These are
validation/atomicity fixes only — no data migration. Contracts a future agent
must not regress:

- **Standalone `POST /scores` (#40):** `create_score` writes ONLY to a cup that
  is `status='in_progress'` and not soft-deleted. A completed/cancelled/deleted/
  nonexistent target is rejected with a flash + redirect (no 500, nothing
  persisted) — a raw insert into a finalized cup would corrupt its standings
  (placements/lines are computed at completion, not on ad-hoc inserts). Note the
  standalone score form/tests therefore target an in-progress cup
  (`helpers.start_inprogress_cup`).
- **Course validation, BOTH write paths (#41):** the submitted `map` must be in
  `courses_for(cup.game_edition)`. (1) `cup_session_next_race` → 400, no race
  recorded on a bad name. (2) `cup_session_submit` (completion form) also
  re-writes race maps via `race_N` fields — each submitted (non-empty) override
  is validated the same way and rejected with flash+redirect (no write, cup
  stays in_progress) if off-edition/arbitrary. Both doors keep history/stats
  clean. (Placeholder map names in tests must be real courses now — e.g.
  "Coconut Mall", not "A".)
- **Atomic cup completion (#39):** `cup_session_submit` transitions status with a
  conditional `UPDATE cups SET status='completed' WHERE id=? AND status='in_progress'`
  and applies scores/line adjustments ONLY if `rowcount == 1`. Concurrent
  completers: only one wins (the loser gets 409, applies nothing) — lines can't
  shift twice, `line_changes` can't duplicate.
- **Atomic veto/voto counters (#36):** increments go through `increment_voto()` /
  `increment_half_veto()`, each a single cap-guarded conditional UPDATE
  (`... WHERE ... AND count < CAP`) that returns the new count or `None` when
  rejected. The cap check lives in the WHERE clause so concurrent requests can't
  exceed `MAX_VOTOES` / `MAX_HALF_VETOES`. Routes use these helpers.
- **Player/score pairing (#45):** `parse_scores_from_form` rejects a request
  where `len(player_ids[]) != len(scores[])` (ambiguous positional pairing that
  would misattribute scores). Client side, `cup_new.html` disables ALL of a
  removed row's inputs together (`player_ids[]` + `lines[]` + score inputs), so
  a removed player submits nothing and the arrays stay aligned.

## UI / Design System

As of the `feature/ui-makeover` work, the app has a shared design system instead of per-template inline `<style>` blocks.

- **`static/css/app.css`** — the single shared design system. CSS-custom-property based: all colors/spacing/radii/shadows are defined as variables in `:root` (light) and overridden in a `@media (prefers-color-scheme: dark)` block, so **light + dark mode are automatic**. Mobile-first, centered content column (`--content-width`, ~520px). Flask serves it from `/static`.
- **`templates/base.html`** — the shared layout **every template should `{% extends "base.html" %}`**. It sets doctype/viewport/`color-scheme` meta, links `app.css` via `{{ url_for('static', filename='css/app.css') }}`, and renders flash messages (uses `get_flashed_messages(with_categories=true)`: `success`→`.flash-success`, `info`→`.flash-info`, everything else incl. uncategorized→`.flash-error`).
- **No JS framework** — vanilla only; the design system is pure CSS (plus a tiny vanilla-JS theme picker).
- **`static/js/sfx.js`** — race-page sound effects, fully synthesized with the Web Audio API (no audio assets, no CDN). Exposes `SFX.tick()` (prize-wheel click, fired from the wheel's `animate()` loop on slice-boundary crossings, throttled ≥30ms), `SFX.whoosh()`/`SFX.success()`/`SFX.fail()` (half-veto coin flip — the result sound fires with the ~1100ms visual swap, never at fetch-response time), and `SFX.muted`/`SFX.toggleMute()` (persisted in localStorage `km-muted`, same pattern as `km-theme`). The AudioContext is created lazily on first play (autoplay-policy safe) and everything degrades to silent no-ops if Web Audio is unavailable. `cup_session_race.html` starts its inline script with a no-op `var SFX = window.SFX || {…}` fallback so a failed sfx.js load can never break the spin/veto handlers — keep that stub in sync if the SFX API grows. Mute button `#mute-btn` lives in the race-page controls row, next to the **skip-animations toggle** `#skip-anim-btn` (instant mode: wheel snaps to the result with no spin/ticks, coin flip reveals immediately with no whoosh — but the win/fail jingle still plays; persisted in localStorage `km-skip-anim`, same pattern as `km-muted`).

### Accent theming (user-selectable)
The **accent** color is themeable independently of the rest of the palette; **destructive styling never changes** (`--color-danger` and `.btn-danger` are fixed red regardless of theme).

- **Mechanism:** a `data-theme` attribute on `<html>` overrides only the accent variables (`--color-accent`, `--color-accent-hover`, `--color-accent-soft`, `--color-accent-gradient`; `--color-accent-contrast` stays near-white). Each theme is a `[data-theme="name"]` block in `:root`-level (light) CSS **and** a mirrored block inside the `@media (prefers-color-scheme: dark)` query, so every theme works in both modes. The **default is `violet`** — its values are also the base `:root`/dark `:root` accent values, so no attribute = violet.
- **`--color-accent-gradient`:** the primary-button background. Defaults to `var(--color-accent)` (solid). The two gradient themes (`aurora`, `sunset`) set it to a `linear-gradient(...)`, giving multi-color buttons while links/badges/focus rings still use the solid `--color-accent`. `.btn-primary` uses `background: var(--color-accent-gradient)` and hovers via `filter: brightness(1.07)` (works for solid + gradient).
- **Themes shipped (8):** `violet` (default), `indigo`, `ocean`, `teal`, `emerald`, `rose`, `aurora` (gradient violet→cyan), `sunset` (gradient orange→pink).
- **Persistence:** stored in `localStorage` under key **`km-theme`**. An inline no-FOUC `<script>` at the **top of `<head>`** (before the CSS link) reads it and sets `data-theme` before first paint — no flash of the wrong theme. The picker logic is a separate vanilla `<script>` near the end of `<body>`.
- **Picker UI:** a fixed circular "palette" trigger (`.theme-picker`/`.theme-trigger`, top-right, shows current accent) opens a popover (`.theme-menu`) of swatch buttons (`.theme-option`/`.theme-swatch`). Active theme marked via `aria-pressed="true"`. Closes on outside-click / Escape. All styled with design-system variables.
- **To add a theme:** (1) add a `[data-theme="name"]` block in the light themes section of `app.css` and a mirrored one inside the dark `@media` block (set accent/hover/soft, and gradient if it's a gradient theme); (2) add a `.theme-option` button with a hardcoded swatch color in `base.html`'s `.theme-options`. No JS changes needed — the picker reads all `.theme-option`s generically.

### base.html blocks
- `{% block title %}` — `<title>` text (default "KM Tracker"). On staging
  (`APP_ENV=staging`) base.html prefixes `[STG] ` outside the block, so every
  page's tab title gets it automatically — don't add it per-page.
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

Note: `.claude/` is gitignored **except `.claude/skills/`** (shared, committed tooling — see below). Personal per-feature context under `.claude/features/` stays local; recreate it on a new machine as needed — the convention is described here.

## Claude Code Skills

Reusable agent skills live in `.claude/skills/` and **are committed** (a `.gitignore` negation re-includes that path while the rest of `.claude/` stays personal/local). They're shared tooling any session can invoke.

### `break-staging` — adversarial exploratory QA (staging only)

`.claude/skills/break-staging/SKILL.md` — an on-demand red-team sweep that tries to break the **staging** instance before changes reach prod. It's the exploratory-QA step of the autonomous coding pipeline: run it **after a staging deploy / feature preview** to hammer the app, then feed its findings back as regression tests.

- **What it does:** fans out parallel subagents across every endpoint — hostile/malformed input, out-of-order & double-submit session flows (spin/veto/voto/next-race/complete against the state machine), concurrent request pairs against the same in-progress cup (racing the non-atomic counter guards), plus a direct **staging-DB invariant audit** (counter caps, score/line-change consistency, referential integrity). Reports each finding with severity, a concrete repro, observed-vs-expected, and a **proposed pytest regression test** (using the `client` fixture + `tests/helpers.py`).
- **Change-awareness / focus-fire:** before the broad sweep it diffs what's deployed on staging against prod/`main` (deployed-commit and `git diff origin/main...HEAD`), maps the changed routes/columns/migrations/UI, and runs a **dedicated heavier attack pass on that currently-deployed-but-unmerged feature** (new enum values, migration backfill gaps, validation bypasses, broken old invariants) on top of the broad sweep — findings for the changed surface get their own report heading so it's obvious whether the new work is safe.
- **Staging-only guardrails (non-negotiable):** proves the target is staging (DB basename contains `staging`, `APP_ENV=staging`/`[STG]` title, `staging-app` service) before touching anything; before its first reseed it **fingerprints the staging DB against the deterministic seed baseline** (the seed leaves one in-progress fake cup, so a bare in-progress count can't be used) and **aborts only if the state looks like a *real* human session** — extra/real cups or non-seed player names — never stomping an active session; never touches the prod `app` container, `km_tracker.db`, or `km.graham-williams.com`; **reseeds `scripts/seed_staging.py --reset` before and after** so staging is left clean.
- **Access path:** the public `staging-km.graham-williams.com` is behind the shared app-password gate (`APP_PASSWORD`; the old Cloudflare Access email PIN was retired 2026-08-05), so the skill reaches staging **on the box** over Tailscale SSH (box SSH target + repo path are **not committed** — the executing agent gets them from local memory / private DEPLOY notes) by launching a **throwaway one-off container** from the `staging-app` service with `CSRF_PROTECTION=0` + `APP_PASSWORD=` + `CF_ACCESS_*` unset (blanking `APP_PASSWORD` is required, or every request 302s to `/login`), publishing loopback `127.0.0.1:18080`, sharing the `./data` volume so it hits the real staging DB — without rebuilding/restarting the live `staging-app` or prod. Optional `ssh -L` port-forward lets an agent drive it (incl. Playwright) from its own machine.
- **It informs only** — it does not fix bugs or open PRs; a follow-up session turns accepted findings into tests + fixes.

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

The app supports multiple Mario Kart editions per cup session — **Wii**, **Switch (mk8dx)**, and a **mixed "Wii + Switch"** cup. The track list differs between editions, and **lines are Wii-only**; the other house rules are identical (`MAX_RACES=4`, `MAX_HALF_VETOES=3`, `MAX_VOTOES=4`, all in `app.py`).

- **`maps.py`** — `TRACK_SETS = {"wii": [...32...], "mk8dx": [...96...]}` (MK8DX = 48 base + 48 Booster Course Pass, 24 cups). **`TRACK_SETS` means "editions you can play a RACE on"** — `"mixed"` is deliberately NOT a member. `BASE_EDITIONS = ("wii", "mk8dx")` is the playable pair; `MIXED_EDITION = "mixed"`; `VALID_CUP_EDITIONS = frozenset(TRACK_SETS) | {MIXED_EDITION}` is the create-form whitelist. `EDITION_LABELS` maps the stored string to a display label (incl. `"mixed" → "Wii + Switch"`, which is what puts the third `<option>` in the picker). Helpers: `courses_for(edition)` (**raises `ValueError` for `"mixed"`** — see below), `edition_label(edition)`, `other_edition(edition)`, and `edition_order_label(cup_edition, first_edition)` → `"Wii → Switch"` for mixed, the plain label otherwise. `COURSES` remains a flat alias of the Wii list for backward compatibility.
- **Storage:** `cups.game_edition TEXT NOT NULL DEFAULT 'wii'` **plus `cups.first_edition TEXT` (nullable)** — the mixed cup's coin-flip winner; NULL for every pure cup. Chosen on the new-session screen (`cup_session_new.html` `<select name="game_edition">`), validated against `VALID_CUP_EDITIONS` in `cup_session_create`, and read back in the session/spin/complete routes to pick the right track set. **Every route that SELECTs a cup row for edition purposes must include `first_edition`**, and `row_value(row, key)` in `app.py` **enforces that loudly**: omitting the `default` argument means the column is REQUIRED and a missing one raises `KeyError`. It used to return `None` silently, which was the wrong trade — `None` → `edition_for_race` → `DEFAULT_EDITION` means a **Switch-first mixed cup renders and validates as Wii-first with no error anywhere**. Pass an explicit `default` only where absence is genuinely expected.
- **Display:** two Jinja filters, and picking the wrong one is how a mixed cup gets mislabeled.
  - **`cup_edition_label`** takes the **cup ROW** (`{{ cup|cup_edition_label }}`) and is what you want for a *cup*: it renders `"Wii"`, `"Switch"`, or a mixed cup's console order `"Wii → Switch"`. Used on `/cups` history and the cup edit page; the race/complete screens get the same string as a `cup_edition_label` context var. **The row must have `first_edition` in its SELECT** — `row_value` raises if it doesn't, deliberately, so a short SELECT is a loud 500 rather than a Switch-first cup silently labeled Wii-first.
  - **`edition_label`** takes a plain **edition string** (`{{ 'wii'|edition_label }}` → `"Wii"`) and is for a single *race* or console — e.g. the per-race console badges on the race page. It has no notion of a cup, so passing it `cup.game_edition` on a mixed cup yields the bare `"Wii + Switch"` with no order.
- **Spin wheel** (`cup_session_race.html`): slice colors are generated programmatically from `NUM_SLICES` (evenly-spaced HSL hues, alternating lightness) and label font size scales to the slice count — so any edition/size renders. Do **not** reintroduce a hardcoded per-track color array.
- **Course numbers:** each course shows its **1-based position** in the edition's `TRACK_SETS` list as `Name (N)` on the wheel, result label, and both override dropdowns — a quick "find it by number" reference (players count courses off the cup-select screen left-to-right, top-to-bottom, 4 per cup). The number is **display-only**: it's computed client-side from the slice index (`cup_session_race.html`) or `loop.index` (Jinja), and is **never stored or submitted** — `races.map`, the spin response `map`, and played-map matching all use the raw name. Because of this, **`maps.py` list order must match the on-screen cup-select grid.** Wii is 4 cups/row (nitro then retro). MK8DX is 6 cups/row and the DLC cups are **interleaved into rows 1–2, not appended**: row 1 = Mushroom, Flower, Star, Special, **Egg, Crossing**; row 2 = Shell, Banana, Leaf, Lightning, **Triforce, Bell**; then the 12 Booster cups in wave order (rows 3–4). Anchor tests in `tests/test_cup_session.py` (`test_*_courses_match_onscreen_cup_order`) pin the cup boundaries — reorder the list and they fail on purpose. Reordering the list is safe for history (names, not positions, are persisted).
- **Lines are Wii-only:** the line handicap (`-3/0/+3` deltas, `line_changes`, `players.line`) applies **only to Wii cups**. Switch (`mk8dx`) cups are lineless. **Three** authoritative server-side guards, all keyed on `cup_uses_lines(conn, cup_id)` (True iff `game_edition == 'wii'`): (1) `apply_line_adjustments()` returns early for non-Wii cups → no `line_changes`, no `players.line` change; (2) `zero_lines_if_lineless()` runs before validation/`save_scores` in `cup_session_submit` **and** `update_cup`, forcing `line=0`/`line_score=score` — so even a crafted POST carrying non-zero `lines[]` on a Switch cup can't persist a handicap; (3) the **raw score routes** `create_score` (`POST /scores`) and `update_score` (`POST /scores/<id>/edit`) zero `player_line` before computing `line_score`. **(3) is easy to miss and was a real hole** (found by the `break-staging` invariant audit, fixed 2026-08): those two routes stamp `players.line` onto the row unconditionally and never went through `zero_lines_if_lineless`, so `POST /scores` on a lineless cup persisted `line=5` / `line_score=score+5`. Any NEW path that writes `scores.line` must consult `cup_uses_lines` — the completion form is not the only door. **Mixed ("Wii + Switch") cups are lineless too** — by house rule, the line on a mixed cup is worked out manually. That falls out of the same `== 'wii'` test with **no extra branch**; don't add one. The line-entry UI is hidden per-edition in `cup_session_complete.html` and `cup_edit.html` via a **route-supplied `lines_on` context var** (`cup_uses_lines(conn, cup_id)`, passed by `cup_session_complete` and `edit_cup`) — the templates no longer re-derive it from a `game_edition == 'wii'` string test, so there is one server-side source of truth. Each row's `show_line = player.has_line and lines_on`; `data-has-line` reflects the gated value so the raw/line-score sync JS treats lineless rows as plain-points, and `cup_edit.html`'s add-player JS gates on `var linesOn = {{ lines_on|int }}` (**missing that one silently re-enables line inputs on newly added rows**). History line badges (`cups.html`) need no gate — mk8dx cups simply have no `line_changes` rows. Direct `/cups` cups have no edition picker so default to `wii` (lines apply). Tests: `test_mk8dx_cup_stays_lineless` (submits hostile non-zero lines), `test_mk8dx_edit_ignores_submitted_lines`, `test_wii_cup_still_applies_lines`, `test_*_complete_page_*_line_inputs`, and `test_{create,update}_score_route_stays_lineless` / `..._still_stamps_the_line_on_a_wii_cup` (guard 3, mixed + pure Switch, with a Wii control). **Note:** `POST /scores/<id>/edit` and `POST /scores/<id>/delete` also lack the completed-cup guard that `create_score` already has (added in #40/#52 — a friendly status/`deleted_at` SELECT plus a conditional INSERT) — that is **issue #63**, a separate concern, deliberately left open.
- **Mixed cups ("Wii + Switch", `game_edition = 'mixed'`)** — 4 races in **BLOCKS**: races 1–`RACES_PER_BLOCK` (=2) on the console a **server-side coin flip picked at cup creation**, then 2 on the other. Exactly one console swap.
  - **The per-race edition is DERIVED, never stored per race.** `edition_for_race(cup_edition, first_edition, n)` in `app.py` (pure cup → the cup's edition; mixed → `first_edition` for n ≤ 2, `other_edition` after). Why not a `races.game_edition` column? `races` rows are only INSERTed **as races are played**, so such a column could never answer "which console is the NEXT race on?" — and the wheel must be drawn before that row exists. Deriving makes the function total over n = 1..`MAX_RACES` and makes the 2-2 split structurally unbreakable. A hand-edited `'mixed'` row with a NULL/garbage `first_edition` falls back deterministically to `DEFAULT_EDITION` — never re-flips, never 500s.
  - **The flip happens exactly ONCE**, in `cup_session_create` via `flip_first_console()` (a named helper so tests monkeypatch it rather than `random.choice`), carried in the existing atomic conditional INSERT. Nothing else ever writes `first_edition`. The `#console-flip-modal` on the race page is **pure theater** — it animates a value already in the DB and rendered into the page; a `sessionStorage` key (`km-console-flip-<cup id>`) only suppresses a *replay* on refresh, so clearing it can at worst replay the animation, never re-roll the console.
  - **Row helpers** (`app.py`): `race_edition(cup, n)`, `courses_for_race(cup, n)`, `race_editions_map(cup)`, `played_maps_for_edition(cup, races, edition)`, `is_mixed_cup(cup)`.
  - **`courses_for("mixed")` RAISES `ValueError`.** It used to silently fall back to the Wii list for unknown editions — which would have let a Switch race record a Wii course. Every call site now passes a *race* edition, so the raise is unreachable in prod and fails loudly in tests if a call site is missed.
  - **Per-race course validation on BOTH write doors**, so a crafted POST can't record a Wii course on a Switch race: `cup_session_next_race` (note the **check order** — count/cap first, THEN the course check, because the race NUMBER determines the console) and the completion-form override loop in `cup_session_submit` (`courses_for_race(cup, i)` per race).
  - **Played maps are excluded PER CONSOLE** (`played_maps_for_edition`). **7 course names exist in both track lists** (Rainbow Road, Bowser's Castle, Mario Circuit, DS Peach Gardens, GCN DK Mountain, GCN Waluigi Stadium, SNES Mario Circuit 3) — a cup-wide exclusion would have removed the *other* console's same-named course from the second half's pool and greyed the wrong slice.
  - **Stale page across the swap** (two devices on one cup): the spin JSON returns `"edition"` and the client reloads on mismatch with the page's `RACE_EDITION`, rather than indexing a result into the wrong wheel. The **Manual override** path can't use that echo — it picks straight out of the page's own (possibly stale) `COURSES` — so the `next-race` handler **reloads on any error response**; without that the server's correct 400 would leave the page dead-ended, failing forever.
  - **Mixed cups are LINELESS** (house rule — the line is worked out manually). This needs **no new branch**: `cup_uses_lines` stays `game_edition == 'wii'`, so `zero_lines_if_lineless()` and `apply_line_adjustments()` already cover mixed. **Do not special-case `'mixed'` there.**
  - **Score entry is unchanged** — one combined total per player; no `scores` schema change, no stats change. The complete page shows a "combined total" note and, entering race 3, a **photo reminder** (`#swap-reminder-modal` + a persistent `#second-console-banner`): photograph the standings *before* switching, because the second console scores from zero. **That photo is never stored or extracted** by the app. Both mixed-cup modals are **one-shot per browser session** via `sessionStorage` (`km-console-flip-<id>`, `km-swap-reminder-<id>`) — their server-side gates are stateless (`no races yet` / `exactly 2 races`), so every refresh of that page would otherwise re-block the controls. The keys only suppress the *replay*; the outcome always comes from the DB, so clearing them can at worst re-show a modal, never change anything.
  - **Photo extraction — a mixed cup NEVER auto-fills.** `_players_for_extraction` returns `(edition, players, partial_half, error)`. For a mixed cup it maps the edition to `race_edition(cup, MAX_RACES)` — the second half's console, the results screen actually on display (passing `'mixed'` through would drop `default_character_field`/`build_extraction_prompt` into their unknown-edition branches) — **and sets `partial_half = True`**. `/extract-scores` then returns `scores: {}` (plus empty `ambiguous`/`unmatched_players`) and `partial_half: true`, while still returning `raw_rows` so the mapping panel works as a read-off-the-photo reference. **Why this is not optional:** that screen shows only the SECOND console's points — the first console's scoreboard is gone and its console restarted at zero — so every extracted number is a HALF total that looks entirely plausible next to a cup total (42 where the truth is 88). Auto-filling it would permanently record roughly half the true points with **no signal anywhere downstream**. The suppression is **server-side** so a stale cached `photo_score.js` can't reintroduce it; `photo_score.js` skips `fillScores()` on `partial_half` as a second lock and swaps its "Filled N scores" status for a "one console's half only — enter the combined total" message; and `cup_session_complete.html` renders a `.photo-half-warning` **inside** the `#photo-score` panel naming both consoles. **The mapping panel is READ-ONLY on a mixed cup** — `renderReferenceRows()` lists the extracted rows as plain text (`.photo-map-readonly`) under the retitled "Rows read from the photo — this console only", with **no `<select>` and no write path to any score input**. The interactive panel's own title ("Map each player to a highlighted row from the photo") would otherwise instruct the user down a **three-tap path to persisting half totals as the cup's scores**, contradicting the warning directly above it. It is deliberately **not** additive — silent arithmetic on top of a typed value is a worse footgun than either a plain overwrite or a read-only list. The manual `/cups/new` path's `edition not in TRACK_SETS` check correctly rejects `'mixed'` and always reports `partial_half = False` — that's not a bug.
  - **Display:** per-race console badges on the race page, `Race N (Wii)` / `Race N (Switch)` labels + per-console dropdowns on the completion page, and a `cup_edition_label` Jinja filter (takes the cup **row**, not a string) rendering `"Wii → Switch"` in `/cups` history and on the edit page.
  - **Staging seed** (`scripts/seed_staging.py`) includes completed mixed cups in **both** console orders and parks the in-progress cup **exactly at the console swap**, so `break-staging` always has mixed data to hammer.
  - Tests: `tests/test_mixed_cups.py`, `tests/e2e/test_mixed_cups.py`.
- **Adding an edition:** **this is now two different jobs.**
  - **A new *playable* edition** (a third console with its own track list): add the track list (in on-screen cup order — see Course numbers) + an `EDITION_LABELS` entry in `maps.py`; the select, routes, wheel, and display pick it up automatically. No schema change needed. Note editions default to Wii-style lines unless you extend the `apply_line_adjustments()`/template `lines_on` gate. Adding one to `BASE_EDITIONS` also makes `other_edition()` ambiguous — mixed cups assume a **pair**, so that would need a real redesign.
  - **A new *cup shape*** (like `mixed`) is NOT just a track list + label: it is **not** in `TRACK_SETS`, it needs its own storage (mixed added `cups.first_edition`), a derived per-race edition, per-race validation on both write doors, and its own handling in every place that assumes one track list per cup.

**Cup date / same-minute creates (issue #32, fixed):** `cups.date` is `DATETIME NOT NULL UNIQUE`. Auto-dated creates (`create_cup` default + `cup_session_create`) now stamp **second** precision (`%H:%M:%S`, not the old `%H:%M:00`), so starting/cancelling/restarting cups within the same minute no longer collides on the UNIQUE constraint. User-entered dates (`datetime-local`, minute resolution) still collide by design — that's the intended "a cup already exists at that time" guard for manual cups. Residual edge: two auto-creates in the same *second* still collide (not human-reachable; a `created_at`-column schema change would be the bulletproof fix if ever needed).

## Cup Session Rules (vetoes)

Per-cup mechanics for skipping a spun course, edition-agnostic (apply to Wii and Switch alike):
- **Half-veto** — per-player (`cup_players.half_veto_count`, cap `MAX_HALF_VETOES=3`), a **coin-flip** (50/50) attempt to skip the course; `POST /cup-session/<id>/half-veto`.
- **Voto** — shared per-cup pool (`cups.voto_count`, cap `MAX_VOTOES=4`), a **guaranteed** skip; `POST /cup-session/<id>/voto`.
- **Stale veto forfeit ("use it or lose it"):** a **one-time** check when **entering race `STALE_VETO_CHECK_RACE = 3`**. `apply_stale_veto_forfeit()` runs inside `cup_session_next_race` right after race 2 is recorded (recording race N−1 = entering race N): every player still holding **all** their half-vetoes (`half_veto_count == 0`) forfeits one (→ count 1, 2 left). Players who've used any are untouched; votoes are never affected. Idempotent (only `count == 0` rows bump, so re-running can't forfeit twice). A `flash(..., "info")` names who forfeited; it renders on the page reload the race-page JS does after `next-race`. Tests: `test_stale_veto_forfeit_*` in `tests/test_cup_session.py`.

### Mid-cup roster editing (add/remove players on an in-progress cup)

A cup's roster is set at creation but can be edited **while the cup is in progress** — no scores exist mid-cup (they're only written at completion), so only the `cup_players` join table changes; there's nothing to reconcile. UI: a compact `<details>` **"Edit players"** control in the race-page dashboard card (`cup_session_race.html`) — a ✕ per current player and an "Add a player…" `<select>` of players not yet in the cup (`available_players`, computed in `_get_cup_session`). Both are plain form POSTs that redirect back to the race page, so the reload always shows the live roster.

- **Routes** (both mirror the `create_score` / PR #58 guard style — the `status='in_progress' AND deleted_at IS NULL` check is **folded into the write** to close the TOCTOU, so a completed/cancelled/deleted/nonexistent cup is a friendly flash + redirect, never a 500; `<int:cup_id>` uses the bounded converter so an oversized id 404s):
  - `POST /cup-session/<int:cup_id>/players/add` — `cup_session_add_player`; form `player_id`. Conditional `INSERT ... SELECT ... WHERE EXISTS(in-progress cup)` with `half_veto_count=0`; `UNIQUE(cup_id, player_id)` makes a duplicate a friendly reject; a nonexistent player is rejected before the write.
  - `POST /cup-session/<int:cup_id>/players/remove` — `cup_session_remove_player`; form `player_id`. Conditional `DELETE` whose `WHERE` also carries `(SELECT COUNT(*) ...) > MIN_ROSTER_SIZE`, so the min-roster guard is atomic (two concurrent removes can't both drop below the floor). Rejects removing a player not in the cup.
- **`MIN_ROSTER_SIZE = 1`** — mirrors cup creation (`cup_session_create` requires "at least one player"); a cup can never be emptied of players.
- **Two invariants (verified + tested):**
  - **Late-add is NOT stale-veto-forfeited.** The forfeit is a one-time event in `cup_session_next_race` over the roster present when entering race 3; a player added afterwards keeps `half_veto_count=0` (all vetoes intact) — it simply wasn't there for that event. Not re-run on later races.
  - **Completion uses the LIVE roster + a freshness guard.** `cup_session_complete` renders players from `cup_players`, and `apply_line_adjustments` keys off the submitted scores (`len(scores_data)`), not any cached count. Because the roster is now mutable mid-cup, a **stale completion form** (rendered before an add/remove) would otherwise drive `save_scores`/`apply_line_adjustments` off the OLD roster — writing a score + `line_changes` row and shifting a **persistent `players.line`** for a player no longer in the cup (stale-remove), or skipping the now-applicable Wii 3-player handicap (stale-add). `cup_session_submit` therefore re-reads the live `cup_players` set and requires the submitted `player_ids[]` set to **exactly** match it (order- and duplicate-robust) before any write; a mismatch → flash "The player roster changed since this page loaded…" + redirect to the fresh `/complete` page, **nothing written, cup stays `in_progress`**. (parse_scores_from_form's #45 guard only catches unequal `player_ids[]`/`scores[]` *lengths*, not a roster mismatch.)
- Tests: `tests/test_roster_editing.py` (add/remove happy paths, all rejects incl. duplicate/below-min/not-in-cup/garbage-id/oversized-id/non-in-progress, 3→2 & 2→3 completion, late-add-not-forfeited, **stale-form 3→2 / 2→3 / duplicate-id rejected with nothing written + a matching-form positive control**).

## Photo Score Entry

Photograph the end-of-cup standings screen and pre-fill the score form (shipped from `feature/photo-score-entry`). Extraction uses the Claude API; the photo itself is always saved with the cup.

- **Optional feature, gated on `ANTHROPIC_API_KEY`.** `extraction.py:extraction_enabled()` checks the env var; a context processor exposes `photo_extraction_enabled` to all templates. **No key → graceful degradation:** `/extract-scores` returns 503, the score forms show attach-only photo controls, manual entry is untouched. The key is wired through `docker-compose.yml` **and** `docker-compose.staging.yml` (`${ANTHROPIC_API_KEY:-}`, optional) from the box `.env`; documented in `.env.example` and `DEPLOY.md`.
- **`extraction.py`** owns the Claude call — model `claude-sonnet-4-6`, `client.messages.parse()` with a Pydantic `Standings(rows: [{position, character, points, is_highlighted}])` schema, one image block (base64) + prompt. **Edition-aware + highlight-as-hint (revised 2026-07 after real-photo validation):** `extract_standings(image_b64, media_type, edition=None)` builds the prompt via `build_extraction_prompt(edition)`, and the route threads the cup's edition through. The model always returns ALL rows; `is_highlighted` marks HUMAN rows. **Wii** prompt describes the ONE real cue that holds across BOTH Wii results layouts (single vertical 12-row list *and* the two-column trophy/credits screen — positions 1–6 left, 7–12 right): a **human row is a SOLID/OPAQUE colored bar**, a **CPU row is SEMI-TRANSPARENT/TRANSLUCENT** (track shows through). The layouts differ only in shape, not in the cue (the earlier "white outline box" idea was dropped). **Switch (`mk8dx`)** has **no reliable human-vs-CPU cue**, so its prompt does NO highlight detection — it just reads position/character/points and leaves `is_highlighted=false` for every row (the "P1/P2 badge" idea was wrong and removed). `is_highlighted` defaults `False` so absent flags don't break parsing. All anthropic/API failures are wrapped in `ExtractionError`. Dev script `scripts/validate_extraction.py <photo>` calls the **real** API to sanity-check phone photos (one API call; never run by tests). **Real-photo findings that drove this:** the highlight signal is real but the model misses it under dark/glare photos and across the two Wii screens, and it's one non-deterministic read — so highlight is a **safe hint for auto-fill, and the human's dropdown is the guarantee**, never a hard filter.
- **Route `POST /extract-scores`** (JSON): `{image: <base64>, mime_type: image/jpeg|png}` plus either `cup_id` (live session — players/edition come from the cup) or `edition` + `player_ids` (manual `/cups/new` form). Response `{scores: {player_id: points}, ambiguous: [names], unmatched_players: [names], raw_rows: [{position, character, points, is_highlighted}, ...]}` — each raw row carries `is_highlighted` so the client can order the mapping dropdowns (humans first). Hostile-input posture matches the rest of the app: bad base64/mime/oversize/ids → 4xx JSON, no key → 503, upstream API failure → 502, never a 500 (`tests/test_photo_extraction.py`).
- **Matching is server-side, highlight-as-safe-hint** (`match_standings_to_players` in `app.py`): rows are matched to players by their **per-edition default character** — `players.default_character_wii` / `default_character_switch` (nullable; pickers validated against the rosters). Per player (casefold+strip compare): if exactly ONE **highlighted** row wears the player's default character → **auto-fill** it (confident human match); 2+ highlighted matches → `ambiguous`. If NO highlighted row matches but **some** row in the photo is highlighted → the character only appears on CPU rows, so leave the player **blank/`unmatched`** (the off-character protection — e.g. a player defaults to Toad but Toad is a CPU here while they actually played a highlighted Dry Bowser: we must NOT hand them the CPU Toad's points). **Zero-highlight fallback:** if the model flagged NO rows highlighted anywhere (detection miss, or **Switch — which by design never highlights**), fall back to today's **character-only** matching (unique character fills; 2+ → ambiguous; 0 → unmatched) so we never regress. So **highlight-aware safe auto-fill is effectively Wii-only today; Switch is character-only best-effort** (a deliberate future follow-up once a real Switch cue is found — the photo is still saved so Switch data accrues). A character claimed by 2+ players is still `ambiguous`; a player with no default character for the edition is `unmatched`.
- **Mix-and-match review UI** (`static/js/photo_score.js` + the shared `#photo-score`/`.photo-mapping` block in both `cup_session_complete.html` and `cup_new.html`): after extraction, a panel renders one `<select>` per roster player listing **ALL** extracted rows — **highlighted (human) rows first, each marked ★** under a "Human players ★" optgroup, the remaining (CPU) rows selectable under an "Other rows" optgroup — so the user can always hand-pick the right row even when highlight detection missed it. Each dropdown is pre-selected to the server's auto-fill (reconstructed client-side from the already-filled score inputs, preferring highlighted rows) or "— leave blank —". A row chosen by one player is disabled in every other dropdown (live, across all rows). Selecting fills that player's `.score-input` and dispatches the existing `input` event (line-sync + placement recalc run as when typed); blank clears it. **Score inputs are never disabled and a hand-typed value is never clobbered by the panel** — the dropdown is a convenience, manual entry always wins. A **count-mismatch warning** (`# highlighted rows ≠ roster size`) and an **unassigned-highlighted-row callout** show as banners, both keyed off the HIGHLIGHTED set and suppressed when zero rows are highlighted (Switch / detection miss). Panel styles in `app.css` (`.photo-mapping*`, `.photo-map-*`). Still **never auto-submits**. Covered by `tests/e2e/test_photo_attach.py::test_mapping_panel_mix_and_match`. (Mid-cup roster add/remove from the panel is a deliberate future follow-up, NOT built here.)
- **Character rosters** live in `maps.py`: `CHARACTERS = {"wii": [...25...], "mk8dx": [...50 incl. BCP DLC...]}` + `characters_for(edition)`, mirroring `TRACK_SETS`/`courses_for`.
- **Frontend** (`static/js/photo_score.js`, initialized by `cup_session_complete.html` + `cup_new.html`): Take photo (`capture="environment"`) / Upload photo buttons, canvas downscale to ≤1200px JPEG (~0.8 quality), preview, fills `.score-row[data-player-id]` inputs and dispatches `input` events so line-sync/placement JS runs. **Never auto-submits** on its own — the human always reviews. The base64 lands in a hidden `photo_data` field so the photo is saved on submit.
- **Silent-drop guards** (the attach is async, so a submit could otherwise race it or follow a failed decode unnoticed): (1) a prominent `.photo-attach-status` indicator — success-tinted "Photo attached ✓" / danger-tinted decode-error message (styles in `app.css`; a failed decode also clears `photo_data` + the preview; the muted `.photo-status` line is extraction progress only); (2) a **submit guard** on both score forms — submit while a downscale is pending is blocked and auto-resumed via `form.requestSubmit()` when it settles, and submit after a failed attach requires `confirm("Your photo didn't attach — submit without it?")`; (3) the photo buttons **ship `disabled`** (`data-photo-input` attributes, no inline onclick) and are enabled + wired by photo_score.js — if the script never loads, the picker can't open, so no pick happens unguarded. When a photo is persisted, both submit paths flash **"Cup recorded — photo saved."** (`success`).
- **Extraction busy UX:** while the `/extract-scores` fetch is in flight, a pure-CSS spinner (`.photo-extract-spinner` in `app.css`, static ring under `prefers-reduced-motion`) shows beside the `.photo-status` line and the form's submit button gets the `disabled` attribute (visual `.btn:disabled` style) — cleared on completion, error/502/network failure, or a superseding new pick. UX only: the submit guard above remains the safety backstop, and attach-only mode (no API key) never engages it. Covered by `tests/e2e/test_photo_attach.py`.
- **Photo persistence:** `cup_photos (id, cup_id, image BLOB, mime_type, created_at)` — stored as a BLOB **deliberately** so photos ride along with the existing rclone→Drive DB backups (no separate volume/backup story; photos are downscaled so growth is modest). Saved inside the same transaction as the scores in `create_cup` and `cup_session_submit`; saving never requires extraction to have run. A **malformed** `photo_data`/`photo_mime` rejects the whole submit with a flash. `GET /cups/<id>/photo` serves the newest blob with its stored mime (404 if none); `/cups` shows a thumbnail linking to it, and the **cup edit page** shows the photo full-width (linked to full-size; `edit_cup` passes `has_photo`). `MAX_CONTENT_LENGTH` was raised 256KB → **1MB** for the photo payloads.

## Planned Features

Running list of features under consideration. Not commitments — ideas to pull from when planning new work.

- **Group cups by session** — a "session" is a game night containing multiple cups played back-to-back. Add a parent Session entity so the UI can show "Game Night 2026-04-12: 4 cups" rather than a flat list.
- **Bet tracking** — players typically bet on cups or sessions. Record who bet what, who won, and settlement status. Schema TBD.
- **Record cup completion time, not start time** — live cup sessions currently stamp `cups.date` when the session starts (`status = 'in_progress'`). For accurate session history the timestamp should reflect when the cup finishes. Options: update `date` at completion, or add a separate `completed_at` column and keep `date` as start. Note the `UNIQUE` constraint on `date` — a schema change may be needed.
- **Soft-delete everywhere** — extend the `deleted_at` pattern (already on `cups`) to all tables so nothing is truly deleted. Helpful for debugging.
- **Visual refresh (mobile-first)** — clean, minimal, flat design. Mobile-first. Explore wheel animation library during this refactor.
- **Friendly URL** — custom domain or hostname instead of raw IP on local network.
- **Stats & Leaderboards** — standings, wins, trends across cups. Referenced in README but not yet implemented.
- **Overtime support** — ties sometimes trigger a 2-race overtime cup. Currently manual; could be formalized in the cup-session flow.
