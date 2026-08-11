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

- `APP_PASSWORD` — **optional.** The shared sign-in password. Blank/unset leaves
  the login gate OFF; setting it turns on the `/login` gate (see
  "Sign-in — app-level shared-password gate" below). Ships dormant so you can
  deploy it before cutting over from Cloudflare Access.

- `ANTHROPIC_API_KEY` — **optional.** Enables photo score extraction (reading a
  photographed standings screen via the Claude API). Leave blank and the app
  degrades gracefully: the score forms offer attach-only photo mode and manual
  entry is unaffected. Both compose files pass it through to the prod **and**
  staging containers (staging parity — same key), so setting it in the box
  `.env` enables it everywhere after the next `up -d`.

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

## Sign-in — app-level shared-password gate

The app has a built-in login gate so access can be granted by handing friends
**one shared password** instead of adding each person to a Cloudflare Access
policy (which emails a one-time PIN on every new device/session). It is designed
to **replace** the Cloudflare Access PIN flow, but ships **dormant** so it can be
deployed before cutover.

**Env vars (in `.env`):**

- `APP_PASSWORD` — the shared password. **Set → the gate is ACTIVE** (the app
  serves a `/login` page and requires this password). **Blank/unset → the gate
  is OFF** and the app behaves exactly as before. This env-gating is the whole
  point: deploy the gate dormant while Cloudflare Access is still in front, then
  activate it at cutover just by filling this in and redeploying.
- `SESSION_SECRET` — **optional, leave blank.** The session cookie is signed with
  the existing `SECRET_KEY`; set `SESSION_SECRET` only if you want a dedicated
  signing key (`SECRET_KEY` wins if both are set).

**How it works (`password_gate_check` before_request + `/login`, `/logout` in
`app.py`):** any request that isn't a static asset, the login/logout routes, or
`/healthz`, and isn't authenticated → `302` to `/login?next=<original-path>`.
`POST /login` compares the submitted password against `APP_PASSWORD` in
**constant time** (`hmac.compare_digest`); on success it sets a **signed** session
cookie carrying only an auth marker (never the password), flagged
**HttpOnly + Secure + SameSite=Lax** with a **30-day** lifetime, then redirects to
the validated-local `next` (off-site `next` values are rejected — no open
redirect). Wrong passwords are **rate-limited per client IP** (10 failures / 15
min → `429`, keyed off `CF-Connecting-IP`). `GET /logout` clears the cookie.

`SESSION_COOKIE_SECURE` defaults ON; it can be set to `0` for plain-HTTP local
dev (the test suite does this). Leave it unset in production (HTTPS at the edge).

**Cutover (Cloudflare Access → password gate):** deploy with `APP_PASSWORD` set,
verify `/login` works, then the human removes/loosens the Cloudflare Access
policy in the dashboard and clears `CF_ACCESS_AUD`/`CF_ACCESS_TEAM_DOMAIN` from
`.env`. The two mechanisms are independent `before_request` hooks and are meant
to run **one at a time**; nothing breaks if both are briefly on (Access simply
gates first). `/healthz` is exempt from **both** gates so container/uptime
health checks always succeed.

## Updating the deployment

```bash
git pull
docker compose up -d --build
```

> If you've wired the Access env override (`docker-compose.access.yml`, see
> "Public access hardening") and/or staging (below), include those `-f` files in
> every deploy so the app containers keep their Access config. The full command
> with staging is shown in the next section.

## Staging environment

A second, isolated **staging** instance runs alongside prod at
`staging-km.graham-williams.com`. It's a safe playground for trying changes and UI
against **fake data only** — it never shares a database with prod.

### Architecture

```
                          ┌─► app container         ──► ./data/km_tracker.db          (PROD)
cloudflared (one tunnel) ─┤
                          └─► staging-app container ──► ./data/km_tracker.staging.db   (STAGING, fake data)
```

- **One tunnel, two public hostnames.** The existing `cloudflared` container
  routes `km.*` → `http://app:8080` and `staging.km.*` → `http://staging-app:8080`
  over the compose network. Neither app publishes a host port.
- **Separate DB file** on the same `./data` volume: `km_tracker.staging.db`. Prod's
  `km_tracker.db` is never touched by staging. Staging is driven purely by its
  `DB_PATH` env var (`docker-compose.staging.yml`); `entrypoint.sh` runs `init_db()`
  against it on startup (idempotent schema create).
- **Sign-in: the shared app-password gate, same as prod.** Staging is gated by
  `APP_PASSWORD` (inherited from prod unless you set `STAGING_APP_PASSWORD`).
  Its separate Cloudflare Access application was **retired 2026-08-05** and the
  edge app deleted, so the password gate is staging's only gate and
  `STAGING_CF_ACCESS_AUD` stays blank. If you ever re-gate staging with an
  Access PIN, give it its **own** AUD — **do not reuse the prod
  `CF_ACCESS_AUD`.**

### Cloudflare dashboard steps (human, one-time)

1. **Add a public hostname to the existing tunnel** (Zero Trust → Networks →
   Tunnels → your `km-tracker` tunnel → Public Hostname → Add):
   - Subdomain: `staging-km`  (Domain: `graham-williams.com`)
   - Service: **`http://staging-app:8080`**  ← the staging container name on the
     compose network.
   - **Use a single-level subdomain (`staging-km`, not `staging.km`).** The free
     Universal SSL cert only covers `graham-williams.com` and
     `*.graham-williams.com` (one label), so a two-level host like
     `staging.km.graham-williams.com` fails the TLS handshake at the edge and
     would require paid Advanced Certificate Manager.
2. **Sign-in — nothing to do in Cloudflare.** Staging is gated by the app-level
   shared password, so it needs **no** Access application. Just make sure
   `APP_PASSWORD` is set in the box `.env` (prod already requires it) and leave
   `STAGING_CF_ACCESS_AUD` blank. Set `STAGING_APP_PASSWORD` only if you want
   staging to use a *different* password from prod.

   <details><summary>Re-gating staging with a Cloudflare Access PIN instead (not the current setup)</summary>

   Add a self-hosted Access application (Zero Trust → Access → Applications →
   Add an application → Self-hosted) with application domain
   `staging-km.graham-williams.com` and the same Allow policy you use for prod,
   then copy the **new app's AUD** (that Access app → Overview → Application
   Audience (AUD) Tag) into the box `.env` as:

   ```
   STAGING_CF_ACCESS_AUD=<the staging app's AUD>
   ```

   Also ensure `CF_ACCESS_TEAM_DOMAIN` is set (shared with prod). Note that
   `break-staging` and any other automated agent can't complete an email PIN,
   which is part of why the PIN was retired.

   </details>

### Deploy (prod + staging together)

> **This command rebuilds and RESTARTS PROD.** Use it only for prod deploys
> (from `main`, and never mid-cup — see the rule at the end of this section).
> To update **staging only** — e.g. to preview a feature branch — do **not**
> run this; use the scoped procedure in "Deploy staging only" below, which
> leaves the prod container untouched.

```bash
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  -f docker-compose.staging.yml up -d --build
```

`docker-compose.access.yml` layers the Access env vars (`APP_HOST`,
`CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`) onto the prod `app`;
`docker-compose.staging.yml` adds the `staging-app` service with its own
`DB_PATH`, `APP_HOST=staging-km.graham-williams.com`,
`CF_ACCESS_AUD=${STAGING_CF_ACCESS_AUD:-}` (blank — see sign-in above), and
`APP_ENV=staging`.

`APP_ENV` tells the app which environment it is: `staging` prefixes the browser
tab title with `[STG] ` (and is exposed to all templates as
`app_env`/`is_staging` for future environment-specific differences). Anything
else — including unset — behaves as production, so the prod container needs no
configuration (`docker-compose.yml` sets `APP_ENV=production` explicitly for
clarity). Both values are baked into the compose files, not the box `.env`, so
a normal redeploy picks them up automatically.

### Deploy staging only (branch preview — leaves prod running)

Staging doesn't build from the main checkout. The box carries a **box-local,
untracked** compose override, `docker-compose.staging-preview.yml` (it lives
only at `/home/<user>/km-tracker` on the box and is not in git), whose job is
to point the `staging-app` **build context** at a **linked git worktree**:

```yaml
# /home/<user>/km-tracker/docker-compose.staging-preview.yml  (box-local, untracked)
services:
  staging-app:
    build:
      context: /home/<user>/km-tracker-staging
```

Two checkouts of the same repo:

- `/home/<user>/km-tracker` — the **main checkout**. Stays on `main`; prod
  deploys build from here.
- `/home/<user>/km-tracker-staging` — a linked worktree (`git worktree add`),
  kept on a **detached HEAD** of whatever ref staging is previewing. Detached
  because git refuses to check out, in a worktree, a branch that's already
  checked out in the main checkout (and vice versa).

**Step by step**, to put `<branch>` on staging:

```bash
# 1. Fetch (the worktree shares the main checkout's object store, so one
#    fetch covers both):
git -C /home/<user>/km-tracker fetch origin

# 2. Point the staging worktree at the ref (detached HEAD):
git -C /home/<user>/km-tracker-staging checkout --detach origin/<branch>

# 3. From the MAIN checkout — never the worktree — rebuild/restart ONLY
#    the staging-app service:
cd /home/<user>/km-tracker
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  -f docker-compose.staging.yml -f docker-compose.staging-preview.yml \
  up -d --build --no-deps staging-app

# 4. Verify prod was untouched — the prod app container's uptime must be
#    unchanged (still "Up X hours/days", not "Up N seconds"):
docker compose ps
```

`--no-deps staging-app` scopes the `up` to that single service: `app` and
`cloudflared` are neither rebuilt nor restarted, so this is safe even while a
cup is in progress on prod.

**Gotchas:**

- **Run the `up` from `/home/<user>/km-tracker`, never from the worktree.**
  `docker-compose.staging.yml` bind-mounts `./data` relative to the compose
  project directory — run it from the worktree and staging's DB mount silently
  points at `/home/<user>/km-tracker-staging/data` instead of the real
  `./data`. A different directory is also a different compose *project name*,
  so compose would create a duplicate set of containers instead of updating
  the existing ones.
- **Compose config is read from the main checkout, not the worktree.** The
  worktree only supplies the *build context* (app code); the `-f` files come
  from `/home/<user>/km-tracker`. If the previewed branch changes compose
  config itself (e.g. adds an env var like `APP_ENV`, changes a service), the
  main checkout's compose files won't have it. Temporarily flip the main
  checkout to the previewed branch for the `up`, then **flip it back to
  `main` immediately** — prod deploys assume the main checkout is on `main`:

  ```bash
  git -C /home/<user>/km-tracker checkout <branch>   # pick up compose changes
  # ... run the scoped `up` from step 3 ...
  git -C /home/<user>/km-tracker checkout main       # ALWAYS flip back
  ```

### First bring-up — seed the staging DB

On the very first launch the staging DB is empty. Populate it with fake data:

```bash
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  -f docker-compose.staging.yml exec staging-app \
  python scripts/seed_staging.py --reset
```

This creates 6 obviously-fake players (Test Toad, Dummy Diddy, Sample Shy Guy,
Mock Mario, Fake Bowser, Demo Daisy) and 12 cups (11 completed with a realistic
spread of results + 1 in-progress) so the app looks lived-in. The dataset is
**deterministic** (same every reseed).

### Reseed on demand

To wipe and repopulate staging at any time (e.g. after messing it up while
testing):

```bash
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  -f docker-compose.staging.yml exec staging-app \
  python scripts/seed_staging.py --reset
```

**Safety rail:** `scripts/seed_staging.py` refuses to run unless the resolved DB
path's basename contains `"staging"` (override only with `--force`). This makes it
effectively impossible to seed/wipe the production `km_tracker.db` by accident. It
reads the DB path from `DB_PATH` (set by the staging container) or a `--db PATH`
arg; without `--reset` it refuses to seed a DB that already has data (no
double-seeding).

> The **"never restart/redeploy while a cup is in progress"** rule still applies —
> redeploying with the full-stack command in "Deploy (prod + staging together)"
> rebuilds and restarts the **prod** `app` container too. Check
> `cups.status='in_progress'` on the prod DB is `0` first (see CLAUDE.md →
> deploy-safety rule). Staging-only deploys via the scoped
> `--no-deps staging-app` procedure don't touch prod.

## Automated backups

The database lives at `./data/km_tracker.db` on the host. Backups are automated by
`scripts/backup.sh`, driven by a systemd timer. The script is orchestrated on the
**host** but runs the actual snapshot **inside the app container**, and:

- Takes a **consistent** snapshot using SQLite's online backup API via `python3`
  (stdlib only — no `sqlite3` CLI, no pip deps), safe to run while gunicorn writes.
  The snapshot is run via `docker exec` **inside the app container** (default
  container `km-tracker-app-1`, override with `BACKUP_CONTAINER`; DB path inside
  the container is `/data/km_tracker.db`, override with `CONTAINER_DB_PATH`), then
  the finished file is `docker cp`-ed out to a host temp file.
  **Why inside the container:** the live DB is WAL-mode and its `-wal`/`-shm`
  sidecars are owned by the container user (UID 10001). SQLite's online backup API
  must write those sidecars to take its read lock, so running it host-side (as the
  unprivileged backup user) fails with `sqlite3.OperationalError: attempt to write
  a readonly database`. Running as UID 10001 (inside the container) is the fix.
  The backup user must be able to run `docker` (in the `docker` group).
- Keeps **frequent local snapshots** in `~/km-backups/snapshots/` (default;
  `LOCAL_BACKUP_DIR`), deduplicated by sha256 (an unchanged DB doesn't create a
  new file), pruned to the newest `LOCAL_RETENTION` (default 100). The snapshot
  and state dirs default **outside** the bind-mounted `data/` dir, which is
  owned by the container user (UID 10001) and so isn't writable by the host
  backup process on a fresh deploy.
- Pushes snapshots **off-box to Google Drive** via `rclone` on a throttled cadence
  (only when the DB changed *and* at least `DRIVE_PUSH_INTERVAL_MIN` minutes —
  default 15 — since the last push), pruning the recent ring buffer on Drive to
  the newest `DRIVE_RETENTION` (default 50).
- Maintains a **`daily/` long-tail tier** on Drive: at most one snapshot per UTC
  day, retained for the newest `DAILY_RETENTION` days (default 30). The recent
  ring buffer can rotate out within hours when pushes are frequent, so the daily
  tier ensures a logical corruption that goes unnoticed for a day or two still has
  a clean copy to restore from.
- **Refuses a catastrophically empty snapshot.** `entrypoint.sh` runs `init_db()`
  on every container start, so if `/data` is ever remounted empty the app happily
  recreates a schema-only DB — valid, `integrity_check`-clean, right table count,
  zero rows. Backing that up would rotate every real copy out of the local ring
  and the Drive tiers. If the new snapshot has **no rows in any table** while the
  previous kept snapshot **has** data, the script fails loudly instead (rows, not
  file size, so a `VACUUM` or genuinely deleting old cups can't false-positive).
  A first-ever run with no previous snapshot is still allowed through, and
  `ALLOW_EMPTY_SNAPSHOT=1` overrides the guard for the deliberate case.
- Always keeps local snapshots even if the Drive push can't run. If rclone isn't
  set up yet (not installed, or the remote isn't configured), it logs a warning
  and **exits 0** — the local snapshot is already safe, so the systemd unit won't
  be marked failed on every timer tick during setup. It only exits non-zero when a
  *configured* remote actually errors.

The timer fires every 5 minutes (frequent local snapshots); the script itself
throttles the off-box push to ~15 minutes. No secrets live in the repo — the
rclone OAuth token is stored only in `~/.config/rclone/rclone.conf`.

**Going the other way — restoring — is not a plain `cp`.** See
"[Restore from a snapshot](#restore-from-a-snapshot)" below before you touch
`data/km_tracker.db`: the stale WAL sidecars have to go, and the file has to be
re-owned to UID 10001, or the restore silently gives you the old data back.

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
cd /home/<user>/km-tracker
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

The units assume the repo at `/home/<user>/km-tracker` and run as user `<user>` —
edit the `User=` line and the `ExecStart` path in `deploy/km-backup.service` to
match your box before the `cp` above (or edit the installed copy, then
`daemon-reload`).

### 5. Verify

```bash
# Timer is scheduled:
systemctl list-timers | grep km-backup

# Run it once now and watch the log:
sudo systemctl start km-backup.service
journalctl -u km-backup.service --no-pager -n 50

# A local snapshot should appear (default LOCAL_BACKUP_DIR, outside data/):
ls -1 ~/km-backups/snapshots/

# And the file should appear in Drive:
rclone lsf gdrive:km-tracker-backups
```

### Restore from a snapshot

> **A restore is NOT just `cp snapshot data/km_tracker.db`.** That looks like it
> works and silently gives you the **old** data back:
>
> - The live DB is **WAL-mode**, so `data/` normally also holds
>   `km_tracker.db-wal` and `km_tracker.db-shm`. If you replace only the main DB
>   file, the next open finds the **stale `-wal` sitting next to the new DB and
>   replays it** — SQLite has no way to know the two don't belong together (the
>   restored file's header still matches), so it raises **no error at all** and the
>   app comes back serving the **pre-restore** rows. The first checkpoint after
>   that then writes those stale WAL pages *permanently into* the restored file:
>   page-level corruption, two different database images merged into one. The
>   sidecars must be deleted in the same breath as the swap.
> - `cp` as your login user also leaves the file **owned by you**, not by the
>   container user (**UID 10001**) — so the app then fails every write with
>   `attempt to write a readonly database`, the exact bug class the backup script
>   itself had to work around.
>
> Follow the whole sequence below, including the verification step — the failure
> mode is silent, so "the app came back up" proves nothing.

The usual "**never restart while a cup is in progress**" rule applies (see
CLAUDE.md → deploy-safety); a restore is far more disruptive than a restart.

```bash
cd /home/<user>/km-tracker

# 1. Pick the snapshot to restore — a local one:
ls -1 ~/km-backups/snapshots/
#    ...or pull one down from Drive (recent ring, or the daily/ long-tail tier):
rclone lsf gdrive:km-tracker-backups
rclone lsf gdrive:km-tracker-backups/daily
mkdir -p ~/km-backups/restore
rclone copy gdrive:km-tracker-backups/km_tracker_<TS>.db ~/km-backups/restore/
#    Then pin it to a variable so every step below uses the same file:
SNAP=~/km-backups/snapshots/km_tracker_<TS>.db     # or ~/km-backups/restore/...

# 2. Note what that snapshot CONTAINS — step 7 compares against this.
#    (Reading a snapshot host-side is fine, it's your own file; immutable=1 means
#    the read can't create -wal/-shm sidecars next to it.)
python3 -c "import sqlite3,pathlib,sys;u=pathlib.Path(sys.argv[1]).expanduser().resolve().as_uri()+'?immutable=1';c=sqlite3.connect(u,uri=True);print(c.execute('SELECT COUNT(*), MAX(id), MAX(date) FROM cups').fetchone())" "$SNAP"

# 3. Capture the CURRENT state first, so a bad restore is reversible. Run the
#    backup script while the app is still up: its in-container online backup
#    folds the WAL in, whereas copying data/km_tracker.db by hand would miss
#    whatever is still sitting in the -wal.
scripts/backup.sh && ls -1t ~/km-backups/snapshots/ | head -n1

# 4. Stop the app. Nothing may hold the DB open while it is swapped.
docker compose stop app

# 5. DELETE THE WAL SIDECARS. This is the step whose absence silently
#    resurrects the old data: an orphaned -wal is replayed over whatever DB
#    file it finds. (sudo: data/ is owned by UID 10001.)
sudo rm -f data/km_tracker.db-wal data/km_tracker.db-shm

# 6. Put the snapshot in place and hand it back to the container user — a file
#    owned by your login user makes the app fail with "attempt to write a
#    readonly database" on its first write.
sudo cp "$SNAP" data/km_tracker.db
sudo chown 10001:10001 data/km_tracker.db
sudo chmod 644 data/km_tracker.db

# 7. Start the app.
docker compose up -d app
docker compose ps
```

**8. Verify the restore actually took.** Ask the *running app's* DB the same
question you asked the snapshot in step 2 — this is the only way to tell a real
restore from a silent WAL replay:

```bash
docker exec km-tracker-app-1 python3 -c \
  "import sqlite3; c=sqlite3.connect('/data/km_tracker.db'); print(c.execute('SELECT COUNT(*), MAX(id), MAX(date) FROM cups').fetchone())"
```

The three values **must match step 2's**. Pick a figure you know differs between
the snapshot and the DB you replaced (cup count / newest cup date is the obvious
one); if they'd be identical either way the check proves nothing — compare
something that changed. Then load the site and confirm the expected cups are
there.

If you instead see the **pre-restore** numbers, the stale `-wal` was replayed:
`docker compose stop app` immediately (every minute it runs risks a checkpoint
baking those stale pages permanently into the file) and redo from step 5 — this
time deleting the sidecars.

The next backup run picks the restored DB up on its own. Two notes:

- If you also cleared out `~/km-backups/snapshots/` while restoring, that's
  fine — the script notices its state no longer matches any file on disk and
  writes a fresh snapshot instead of deduplicating against nothing.
- If you deliberately restored an **empty** DB, the script will refuse to back it
  up ("EVERY table is empty while the previous snapshot has data" — the guard
  against a remounted-empty `/data`). Run it once with `ALLOW_EMPTY_SNAPSHOT=1`,
  or set that in `.env.backup`, to confirm you meant it.

### Manual one-off backup (without the timer)

```bash
# Hot backup that's safe while the app is running. Run it INSIDE the container
# (as UID 10001) — a host-side backup of the WAL-mode DB fails with "attempt to
# write a readonly database" because it can't write the container-owned
# -wal/-shm sidecars. This is exactly what scripts/backup.sh does.
# NB: snapshot into the container's /tmp, NOT /data — /data is the live
# bind-mounted data dir, and a failed `docker cp` would strand a full-size copy
# of the DB right next to the real one. scripts/backup.sh uses /tmp for exactly
# this reason; keep this recipe in step with it.
docker exec km-tracker-app-1 python3 -c \
  "import sqlite3; s=sqlite3.connect('/data/km_tracker.db'); d=sqlite3.connect('/tmp/km_tracker.manual.db'); s.backup(d); d.close(); s.close()"
docker cp km-tracker-app-1:/tmp/km_tracker.manual.db ./km_tracker.$(date +%F).db
docker exec km-tracker-app-1 rm -f /tmp/km_tracker.manual.db
```

Or just run the automated script directly: `scripts/backup.sh` (it does the
in-container snapshot, dedupe, and Drive push).
