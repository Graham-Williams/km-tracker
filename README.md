# KM Tracker

A tracker and suite of tools for Kario Mart game nights.

## Features

- **Session Tracking** — record and review game sessions over time
- **Stats & Leaderboards** — track standings, wins, and trends across sessions
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

### Running Tests

```bash
.venv/bin/pytest tests/ -v
```

## License

MIT
