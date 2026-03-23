# KM Tracker

A tracker and suite of tools for Kario Mart game nights.

## Features

- **Player Management** — add, edit, and remove players; mark defaults for quick cup setup
- **Cup & Score Tracking** — record cups with per-player scores, placements, and tiebreaker results
- **Lines (Handicaps)** — optional per-player handicap that adjusts scores and auto-adjusts after 3-player cups based on placement
- **Stats & Leaderboards** — track standings, wins, and trends across cups
- **Game Night Utilities** — tools to assist with running sessions smoothly

## Getting Started

### Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Running the App

```bash
.venv/bin/python app.py
```

The server starts on port 8080 with debug mode enabled (auto-reloads on code changes).

### Accessing the App

| From | URL |
|------|-----|
| Laptop (browser) | `http://localhost:8080` |
| Phone or other device | `http://<laptop-ip>:8080` |

To find your laptop's local IP, check the Flask startup output — it prints both `127.0.0.1` and your network IP. You can also run:

```bash
ifconfig | grep "inet " | grep -v 127.0.0.1
```

Your phone must be on the same WiFi network as your laptop.

### Database

The app uses SQLite. By default the database file is created at `db/km_tracker.db` in the project root. To store it elsewhere (e.g., a synced folder like Google Drive), create a `.env` file:

```
DB_PATH=/path/to/your/km_tracker.db
```

A timestamped backup (UTC) is created in a `backups/` directory alongside the database each time the app starts, organized by date:

```
backups/
  2026-03-21/
    km_tracker_20260321T031522456Z.db
    km_tracker_20260321T194837012Z.db
```

### Running Tests

```bash
.venv/bin/pytest tests/ -v
```

## License

MIT
