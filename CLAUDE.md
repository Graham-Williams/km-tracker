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
- **Testing:** pytest (Flask test client for unit/integration, Playwright for e2e)
- **Port:** 8080 (5000 conflicts with macOS AirPlay Receiver)
- **Network access:** Binds to `0.0.0.0` so other devices on the local network can reach it

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
