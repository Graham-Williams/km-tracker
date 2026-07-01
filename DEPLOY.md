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

## Public access hardening

Two app-side guards back up Cloudflare Access as defense-in-depth. Both are
`before_request` hooks in `app.py`.

### CSRF (Origin/Referer check) — on by default

Because Access's auth cookie is sent on cross-site requests, a malicious page
could otherwise forge state-changing requests to the app. On every
`POST`/`PUT`/`PATCH`/`DELETE`, the app rejects (403) any request whose `Origin`
(or, failing that, `Referer`) host doesn't match its own host. Requests with
neither header (curl, health checks, non-browser callers) are allowed, and safe
methods (`GET`/`HEAD`/`OPTIONS`) are never checked.

- **Recommended for production:** set `APP_HOST=km.graham-williams.com` in `.env` —
  this pins the CSRF expected-host explicitly instead of trusting the request `Host`
  header.
- Without it the check falls back to the incoming `Host` header, which Cloudflare
  forwards as the original hostname (`km.yourdomain.com`), so it still works — but
  pinning is the safer default.
- Set `CSRF_PROTECTION=0` to disable (local dev only; leave it on in production).

### Cloudflare Access JWT verification — opt-in via env

If the Access *policy* were ever misconfigured or bypassed (e.g. a stray host
port — see the warning above), the tunnel would hand requests straight to the
app. To refuse un-authenticated requests at the app itself, set both of these in
`.env`:

- `CF_ACCESS_TEAM_DOMAIN` — your team domain, e.g. `yourteam.cloudflareaccess.com`.
  Find it in **Zero Trust → Settings** (also shown on the Access login page URL
  and under Custom Pages).
- `CF_ACCESS_AUD` — the Access **application AUD tag**. Find it in **Zero Trust →
  Access → Applications → (your app) → Overview → Application Audience (AUD) Tag**.

When **both** are set, the app requires a valid Cloudflare Access identity token
on every request (from the `Cf-Access-Jwt-Assertion` header Cloudflare injects,
or the `CF_Authorization` cookie). It validates the RS256 signature against your
team's JWKS (`https://<TEAM_DOMAIN>/cdn-cgi/access/certs`, fetched with stdlib
`urllib` and cached in-process, refreshed automatically when Cloudflare rotates
keys), and checks the audience, issuer, and expiry. Invalid/missing → 403.
`/static/*` is exempt so assets still load.

When **either** var is unset (local dev, or LAN/tailnet access), verification is
skipped entirely — so this only enforces once you've configured it for
production.

## Updating the deployment

```bash
git pull
docker compose up -d --build
```

## Automated backups

The database lives at `./data/km_tracker.db` on the host. Backups are automated by
`scripts/backup.sh`, driven by a systemd timer. The script runs on the **host**
(not in the container) and:

- Takes a **consistent** snapshot using SQLite's online backup API via `python3`
  (stdlib only — no `sqlite3` CLI, no pip deps), safe to run while gunicorn writes.
- Keeps **frequent local snapshots** in `data/backups/`, deduplicated by sha256
  (an unchanged DB doesn't create a new file), pruned to the newest
  `LOCAL_RETENTION` (default 100).
- Pushes snapshots **off-box to Google Drive** via `rclone` on a throttled cadence
  (only when the DB changed *and* at least `DRIVE_PUSH_INTERVAL_MIN` minutes —
  default 15 — since the last push), pruning the recent ring buffer on Drive to
  the newest `DRIVE_RETENTION` (default 50).
- Maintains a **`daily/` long-tail tier** on Drive: at most one snapshot per UTC
  day, retained for the newest `DAILY_RETENTION` days (default 30). The recent
  ring buffer can rotate out within hours when pushes are frequent, so the daily
  tier ensures a logical corruption that goes unnoticed for a day or two still has
  a clean copy to restore from.
- Always keeps local snapshots even if the Drive push can't run. If rclone isn't
  set up yet (not installed, or the remote isn't configured), it logs a warning
  and **exits 0** — the local snapshot is already safe, so the systemd unit won't
  be marked failed on every timer tick during setup. It only exits non-zero when a
  *configured* remote actually errors.

The timer fires every 5 minutes (frequent local snapshots); the script itself
throttles the off-box push to ~15 minutes. No secrets live in the repo — the
rclone OAuth token is stored only in `~/.config/rclone/rclone.conf`.

### 1. Install rclone

Prefer the distro package (or the official `.deb` with a verified checksum):

```bash
sudo apt-get update && sudo apt-get install -y rclone
```

Or download the official release `.deb` and verify its SHA256 before installing
(replace the version as needed):

```bash
# See https://github.com/rclone/rclone/releases for the current version + checksums.
curl -fsSLO https://downloads.rclone.org/v1.67.0/rclone-v1.67.0-linux-amd64.deb
curl -fsSLO https://downloads.rclone.org/v1.67.0/SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS   # must print "OK" for the .deb
sudo apt-get install -y ./rclone-v1.67.0-linux-amd64.deb
```

> **Fallback only:** the upstream one-liner
> `curl https://rclone.org/install.sh | sudo bash` pipes a remote script straight
> into root. Use it only if the package isn't available, and review/pin the script
> (download it, read it, run a known revision) before piping it to a root shell.

### 2. Configure a Google Drive remote named `gdrive` (headless)

On a **headless** box rclone can't open a browser, so you authorize on a machine
that has one (e.g. Graham's Mac) and paste the token back.

On the **box**:

```bash
rclone config
# n) New remote
# name> gdrive
# Storage> drive            (Google Drive)
# client_id> (leave blank)  client_secret> (leave blank)
# scope> 2                  ← RECOMMENDED: drive.file (rclone can only see/touch
#                             files it created, so a leaked token can't read or
#                             delete the rest of your Drive). Pick 1 (full access)
#                             ONLY if you need rclone to manage pre-existing files.
# Edit advanced config> n
# Use auto config?> n       ← IMPORTANT: say No on a headless box
```

rclone prints a command to run on a machine **with a browser**. On your **Mac**
(with rclone installed locally), run it:

```bash
rclone authorize "drive"
```

Sign in / consent in the browser; rclone prints a JSON token blob. Copy it and
paste it back into the prompt on the box. Finish with:

```bash
# Configure this as a Shared Drive?> n
# y) Yes this is OK
# q) Quit config
```

Verify the remote works:

```bash
rclone lsd gdrive:
```

Lock down rclone's config — it holds the OAuth token, so it must not be
world/group-readable:

```bash
chmod 600 ~/.config/rclone/rclone.conf
```

The destination folder (`km-tracker-backups`) is **auto-created on the first
copy** — you don't need to make it manually.

### 3. Configure the backup

```bash
cd /home/graham/km-tracker
cp .env.backup.example .env.backup
# Edit .env.backup — at minimum confirm RCLONE_DEST=gdrive:km-tracker-backups
chmod 600 .env.backup   # the script refuses to source it if group/other-writable
```

`.env.backup` is gitignored — never commit it. (There are still no secrets in it;
the OAuth token lives in rclone's config.)

### 4. Install the systemd units

```bash
sudo cp deploy/km-backup.service deploy/km-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now km-backup.timer
```

The units assume the repo at `/home/graham/km-tracker` and run as user `graham`.

### 5. Verify

```bash
# Timer is scheduled:
systemctl list-timers | grep km-backup

# Run it once now and watch the log:
sudo systemctl start km-backup.service
journalctl -u km-backup.service --no-pager -n 50

# A local snapshot should appear:
ls -1 data/backups/

# And the file should appear in Drive:
rclone lsf gdrive:km-tracker-backups
```

To restore, just copy a snapshot back over `data/km_tracker.db` while the app is
stopped (it's a plain SQLite file).

### Manual one-off backup (without the timer)

```bash
# Hot backup that's safe while the app is running:
sqlite3 ./data/km_tracker.db ".backup ./data/km_tracker.$(date +%F).db"
```
