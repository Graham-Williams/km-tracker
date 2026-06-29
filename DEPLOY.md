# Deploying KM Tracker (self-hosted, behind Cloudflare)

This runbook covers hosting KM Tracker on your own headless Linux box and exposing
it to the internet through a Cloudflare named tunnel, gated by Cloudflare Access so
only people you allow can reach it. No ports are opened on your router — the tunnel
dials out to Cloudflare.

## Architecture

```
Internet
   │  https://km.yourdomain.com
   ▼
Cloudflare edge  ──(Access login: only allowed emails)──┐
   │  encrypted tunnel (outbound from your box)          │
   ▼                                                     │
cloudflared container ──(compose network)──► app container (gunicorn :8080)
                                                  │
                                                  ▼
                                            ./data/km_tracker.db  (SQLite, persisted)
```

- Two containers via Docker Compose: `app` (Flask + gunicorn) and `cloudflared` (the tunnel connector).
- Both have `restart: unless-stopped`, so they come back after crashes and reboots.
- `cloudflared` reaches the app over the compose network at `http://app:8080` — that is the
  Service you set on the tunnel's Public Hostname (Step 4). The app port is **not** published
  to the internet by Cloudflare; the tunnel is the only inbound path.
- SQLite lives on the `./data` volume on the host, so data survives container rebuilds.

## Prerequisites

- A domain added to Cloudflare (using Cloudflare as its DNS / nameservers).
- A headless Linux machine that stays on. Ubuntu Server 24.04 LTS is a good default.

## Step 1 — Provision the Linux box

Install Docker Engine + the Compose plugin via the official convenience script, then
enable Docker on boot:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable docker
# Optional: run docker without sudo (log out/in afterward)
sudo usermod -aG docker "$USER"
```

## Step 2 — Disable sleep/suspend (headless box)

A server should never suspend. Mask the sleep targets:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

If it's a laptop being used as a server, also stop it suspending when the lid closes.
Edit `/etc/systemd/logind.conf` and set:

```
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
```

Then restart the login manager:

```bash
sudo systemctl restart systemd-logind
```

## Step 3 — Clone the repo and create `.env`

```bash
git clone https://github.com/Graham-Williams/km-tracker.git
cd km-tracker
cp .env.example .env
```

Edit `.env` and fill in:

- `SECRET_KEY` — generate one with:

  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```

- `TUNNEL_TOKEN` — you'll get this in Step 4.

`.env` is gitignored — never commit it.

Then create the data directory and give it to the container user **before the first
launch**:

```bash
mkdir -p data && sudo chown -R 10001:10001 data
```

The app container runs as a non-root user with UID `10001` (see the Dockerfile).
Because `./data` is bind-mounted from the host, that host directory must be owned by
UID `10001` or the container can't create/write the SQLite DB. If you skip this, the
app will fail to start with a permission error on `km_tracker.db`.

## Step 4 — Create the Cloudflare named tunnel

In the Cloudflare **Zero Trust** dashboard:

1. **Networks → Tunnels → Create a tunnel**.
2. Type: **Cloudflared**. Give it a name (e.g. `km-tracker`).
3. Copy the tunnel **token** Cloudflare shows you and paste it into `TUNNEL_TOKEN` in `.env`.
   (You can ignore the install command it suggests — the `cloudflared` container uses the token.)
4. Add a **Public Hostname**:
   - Subdomain: `km`
   - Domain: `yourdomain.com`
   - Service: **`http://app:8080`**  ← the container name on the compose network, not localhost.
5. Save.

## Step 5 — Launch

```bash
docker compose up -d --build
```

Verify both containers are up and the tunnel registered:

```bash
docker compose ps
docker compose logs cloudflared   # should show "Registered tunnel connection" lines
```

## Step 6 — Gate with Cloudflare Access (free)

So the app isn't open to the whole internet:

1. Zero Trust → **Access → Applications → Add an application → Self-hosted**.
2. Application domain: `km.yourdomain.com`.
3. Add a policy: **Action = Allow**, and scope it to your email(s) — e.g. a rule
   `Emails` → `you@example.com` (and any others). Use email OTP (one-time PIN) or
   Google as the login method.
4. Save.

Now visitors hit a Cloudflare Access login screen before the app loads.

## Step 7 — Verify end-to-end

From a phone **on cellular** (off your home wifi, so you're really coming from the internet):

1. Open `https://km.yourdomain.com`.
2. You should get the Cloudflare Access login.
3. Sign in with an allowed email.
4. You should land on the KM Tracker app.

**Confirm the deny path before you trust Access.** Don't just verify that *you* can
get in — verify that others can't:

5. Open `https://km.yourdomain.com` in a **private/incognito window** (or with an
   account that is **not** on your allowlist).
6. You should be **blocked** at the Cloudflare Access login and never reach the app.
   If you can reach the app without authenticating, your Access policy is wrong — fix
   it before considering the deployment secure.

> **Warning:** Access only protects the path through the tunnel. The compose file
> intentionally publishes **no host port** for the app. If you ever add a host port
> mapping (e.g. `8080:8080`), anyone on the LAN can hit the app directly at that port
> and **completely bypass Cloudflare Access**. Don't add a public host port.

## Updating the deployment

```bash
git pull
docker compose up -d --build
```

## Backups

The database lives at `./data/km_tracker.db` on the host. Back it up periodically, e.g.
a nightly copy:

```bash
cp ./data/km_tracker.db ./data/km_tracker.$(date +%F).db
```

For a hot backup that's safe while the app is running, prefer SQLite's own backup:

```bash
sqlite3 ./data/km_tracker.db ".backup ./data/km_tracker.$(date +%F).db"
```
