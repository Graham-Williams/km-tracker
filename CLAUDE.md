# KM Tracker — Claude Context

## Project Overview

KM Tracker is a tracker and tooling suite for Kario Mart game nights. It records sessions, tracks stats, and provides utilities to assist with running the game. The project has persistent storage (SQLite or similar file-based DB).

This is a public GitHub repo — keep all committed content professional and general.

## Dev Workflow

- **Git:** Use conventional commits (`feat:`, `fix:`, `chore:`, etc.) with meaningful messages
- **Branches:** `feature/<name>`, `fix/<name>`, `chore/<name>`
- **Never commit or push** — Graham handles all commits and pushes
- **Other git ops:** Confirm before anything that touches working state (reset, checkout, stash, etc.)

## Tech Stack

- **Backend:** Python + Flask (server-rendered HTML via Jinja templates)
- **Database:** SQLite (via Python stdlib) — DB path is configured via `DB_PATH` in `.env` (defaults to `db/km_tracker.db`)
- **Testing:** pytest (with Flask test client)
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
