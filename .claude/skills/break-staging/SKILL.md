---
name: break-staging
description: >-
  Adversarial exploratory-QA sweep against the km-tracker STAGING instance. Fans
  out parallel subagents that throw hostile/malformed input, out-of-order and
  double-submit session flows, and concurrent requests at every endpoint, then
  audits the staging DB for broken invariants and writes up findings as proposed
  regression tests. Use ON DEMAND after a staging deploy (or a feature preview on
  staging) as the exploratory-QA step in the coding pipeline — when you want to
  try to break the app before it reaches prod. STAGING ONLY: it reseeds and
  trashes the staging DB by design and must never touch prod. It reports bugs; it
  does not fix them or open PRs.
---

# break-staging — adversarial exploratory QA for the staging instance

You are running a red-team pass against the **km-tracker staging** instance. Your
job is to try hard to break it, prove each break with a concrete repro, audit the
database for corrupted invariants, and hand back findings that drop cleanly into
the pytest suite. You do **not** fix bugs and you do **not** open PRs — this skill
informs.

---

## 0. HARD GUARDRAILS (non-negotiable — read before doing anything)

These are absolute. If any one cannot be satisfied, **abort and report why** — do
not improvise around them.

1. **STAGING ONLY. Prove the target is staging before you touch it.** All three
   must hold before any write/attack:
   - the target DB path basename contains `staging` (staging DB is
     `/data/km_tracker.staging.db`; prod is `/data/km_tracker.db`), AND
   - the app reports `APP_ENV=staging` / `is_staging` (its browser tab title is
     prefixed `[STG] `), AND
   - you are operating on the `staging-app` service / a throwaway container built
     from it — **never** the `app` (prod) service.
   If you cannot positively confirm all three, **abort. Do not attack.**

2. **Never run while a cup is in progress on staging.** BEFORE reseeding or
   attacking, query the staging DB for live cups:
   ```
   SELECT COUNT(*) FROM cups WHERE status='in_progress' AND deleted_at IS NULL;
   ```
   Another human or agent may be actively using staging right now. If this returns
   anything **other than 0 before your first reseed, ABORT** — someone is mid-cup;
   do not trash their session. (After *your own* reseed there will be exactly one
   in-progress fake cup — that one is yours to attack. The check is about not
   stomping a pre-existing session.)

3. **Never touch production.** Do not run any command against the `app` container,
   the prod DB `km_tracker.db`, or the prod hostname `km.graham-williams.com`. Do
   not run any full-stack `docker compose up` (that rebuilds/restarts prod). Only
   the `staging-app` service and disposable one-off containers are in scope. The
   repo's **"never restart/redeploy while a cup is in progress"** rule still
   applies to prod — but you should not be redeploying anything at all.

4. **Reseed to a known state before AND after.** Staging is a throwaway sandbox —
   trash it guilt-free, but leave it clean. Reseed with
   `scripts/seed_staging.py --reset` (exact command in §2/§6) **before** you start
   (known deterministic state) and **after** you finish (leave it clean). The seed
   script has its own hard rail: it refuses to run unless the DB basename contains
   `staging` (bypass only with `--force`, which you must never pass).

---

## 1. Access path (how an agent reaches staging)

The public hostname `staging-km.graham-williams.com` sits behind **Cloudflare
Access one-time-email-PIN**, which an agent cannot complete, and the app *also*
enforces a Cloudflare Access JWT check server-side when `CF_ACCESS_TEAM_DOMAIN` +
`CF_ACCESS_AUD` are set. So do **not** go through the public URL. Reach staging
**on the box** instead.

The box is the self-hosted server (Tailscale): `ssh graham@100.101.1.28`
(hostname `personalserver`). The repo lives at `/home/graham/km-tracker`; the
staging DB is on the bind-mounted `./data` volume as `km_tracker.staging.db`. The
`staging-app` container publishes **no host port**, so you can't curl it from the
LAN, and the live gunicorn has the Access JWT check active — so don't attack the
live container directly.

**Canonical approach — a disposable, auth-disabled app container against the same
staging DB.** Stand up a *throwaway* one-off container from the `staging-app`
service definition, with the Cloudflare Access check and CSRF turned off and a
loopback port published, pointed (via the shared `./data` volume + `DB_PATH`) at
the real staging DB. It shares the staging DB with the live `staging-app` but does
**not** rebuild, restart, or reconfigure it, and never touches prod.

```bash
# ---- ON THE BOX (ssh graham@100.101.1.28), from the repo root ----
cd /home/graham/km-tracker

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.access.yml -f docker-compose.staging.yml"

# (Verification + reseed happen first — see §2 — using $COMPOSE ... exec staging-app.)

# Launch the throwaway QA server: auth OFF, CSRF OFF, loopback port 18080,
# same ./data volume so DB_PATH=/data/km_tracker.staging.db hits the real
# staging DB. --no-deps so nothing else is started/restarted.
$COMPOSE run -d --name km-tracker-qa --no-deps \
  -p 127.0.0.1:18080:8080 \
  -e CSRF_PROTECTION=0 \
  -e CF_ACCESS_TEAM_DOMAIN= \
  -e CF_ACCESS_AUD= \
  staging-app

# Smoke-check it's up and IS staging (title should contain [STG]):
curl -s http://127.0.0.1:18080/ | grep -i '<title>'
```

Now drive HTTP attacks with `curl`/`python3` against `http://127.0.0.1:18080`
directly on the box. With CSRF off you don't need Origin/Referer headers; JSON
endpoints (`spin`, `voto`, `half-veto`, `next-race`) take `Content-Type:
application/json`.

**Optional — drive from your own machine instead** (e.g. to use Playwright for the
UI-level checks): forward the box's loopback port over SSH, then hit
`http://localhost:18080` locally:
```bash
ssh -L 18080:127.0.0.1:18080 graham@100.101.1.28   # keep this session open
```

**Teardown (always, even on failure):**
```bash
docker rm -f km-tracker-qa
```

> Why a throwaway and not the live container: it's deterministic (auth reliably
> off regardless of how the box `.env` set `STAGING_CF_ACCESS_AUD`), it needs no
> reconfiguration of the running staging service, and true request concurrency
> against the same DB row (the race-condition surface) works because the
> throwaway runs its own gunicorn workers. The live `staging-app` stays quiescent
> (nothing routes traffic to it during the sweep).

---

## 2. Pre-flight: verify staging, check for live cups, reseed

Do this on the box, in order, **before** launching the QA container or attacking.

```bash
cd /home/graham/km-tracker
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.access.yml -f docker-compose.staging.yml"

# (a) Confirm the DB path is the STAGING db and read the in-progress count in one
#     shot, straight against the DB inside the live staging container (read-only):
$COMPOSE exec -T staging-app python - <<'PY'
import os, sqlite3
db = os.environ["DB_PATH"]
assert "staging" in os.path.basename(db).lower(), f"NOT STAGING: {db} — ABORT"
c = sqlite3.connect(db)
n = c.execute("SELECT COUNT(*) FROM cups WHERE status='in_progress' AND deleted_at IS NULL").fetchone()[0]
print("DB_PATH =", db)
print("in_progress cups =", n)
PY
```

- If the assert fails (not a staging DB) → **ABORT** (guardrail 1).
- If `in_progress cups` is **not 0** → **ABORT** (guardrail 2 — someone's using
  staging).

Only if clear, reseed to the known deterministic state (6 fake players, 11
completed + 1 in-progress fake cup):

```bash
$COMPOSE exec -T staging-app python scripts/seed_staging.py --reset
```

Record the post-seed baseline (player ids, cup ids, the in-progress cup id) — you
attack against this known state and diff against it in the invariant audit.

---

## 3. Attack plan — fan out subagents in parallel

Dispatch **subagents in parallel**, one per surface below (AI budget is not a
constraint — prefer more parallel agents over serializing). Give each the access
path (§1), the baseline (§2), and the exact endpoint contract for its surface.
Each subagent returns findings in the §5 format. **Reads are free; every request
here is a deliberate write against the throwaway staging server — that is
expected and safe.**

The app is Flask + SQLite, server-rendered. Endpoint surfaces and known
soft-spots (from `app.py`) to aim at:

### Surface A — hostile / malformed input on every endpoint
Hit each route with garbage and boundary values; look for 500s (uncaught
exceptions), silent acceptance of nonsense, or state corruption.
- **Non-integer / overflow ids & numbers:** `player_ids[]=abc`, `scores[]=xyz`,
  huge `score`, negative `score`, `line=999999999`, `tz_offset` absurdly large.
  Targets: `POST /cups`, `POST /cups/<id>/edit`, `POST /scores`,
  `POST /scores/<id>/edit`, `POST /players/<id>/edit`, `POST /cup-session/new`.
  (Score/id parsing does bare `int(...)` in several places — probe for `ValueError`
  → 500.)
- **JSON endpoints fed the wrong shape / content-type:** `POST
  .../half-veto`, `.../voto`, `.../next-race` with form-encoded bodies, missing
  `Content-Type`, `null`, empty body, wrong keys (`{"player_id":"abc"}`). Look for
  `AttributeError` on `None` JSON → 500.
- **Arbitrary map injection:** `POST /cup-session/<id>/next-race` with
  `{"map":"totally-not-a-course"}` — the map name is **not** validated against the
  edition's course list; confirm junk lands in `races.map`.
- **Tiebreaker scope:** `POST /cups` with `tiebreakers[]` referencing a player id
  **not** in `player_ids[]`; empty/oversized/duplicate tiebreaker sets.
- **String fields:** very long `name`/`notes`, unicode, HTML/`<script>` (check it's
  escaped in the rendered page, not reflected raw), whitespace-only `name`.
- **Unknown ids:** every `<int:id>` route with a nonexistent id and a deleted
  cup's id (expect 404, not 500).

### Surface B — out-of-order & double-submit session flows (state machine)
The live-cup state machine: `new → (spin / half-veto / voto / next-race)* →
complete`, or `cancel`. Guards check `cups.status='in_progress'` but do
per-request read-check-write without locking. Attack the ordering:
- **Actions on a non-in-progress cup:** `spin` / `voto` / `half-veto` /
  `next-race` / `complete` against a **completed** or **cancelled** cup (use the
  seeded completed cups). Expect clean rejection, not mutation.
- **Complete twice:** `GET /cup-session/<id>/complete` is allowed even when the
  cup is already completed — then `POST` it again. Does it overwrite scores /
  re-apply line adjustments a second time (double line delta)?
- **Cancel then act:** `cancel`, then try `voto`/`next-race`/`complete`.
- **Exceed limits sequentially:** call `voto` 5×, `half-veto` 4× for one player,
  `next-race` for a 5th race — confirm `MAX_VOTOES=4`, `MAX_HALF_VETOES=3`,
  `MAX_RACES=4` hold and the (N+1)th is rejected.
- **Two in-progress cups:** `POST /cup-session/new` while one is already
  in-progress (should redirect/refuse — verify it can't create a second).
- **Delete a player mid-session:** `POST /players/<id>/delete` for a player who is
  in the in-progress cup's `cup_players` (deletion only checks `scores`, not
  `cup_players`) — does it orphan the row / break the session screen?

### Surface C — concurrency against the same in-progress cup
Fire request **pairs simultaneously** (e.g. `curl ... & curl ... & wait`, or a
small `python3` threaded/async burst) at the same cup id and confirm the counters
can't exceed their caps under a race (read-check-write is non-atomic):
- 2–10 concurrent `POST .../voto` at `voto_count=3` → can `voto_count` exceed 4?
- 2–10 concurrent `POST .../half-veto` for one player at `half_veto_count=2` →
  can it exceed 3?
- 2–10 concurrent `POST .../next-race` at 3 races → can a 5th race appear, or does
  the `UNIQUE(cup_id, race_number)` constraint hold (IntegrityError handled, not
  500)?
- 2 concurrent `POST .../complete` on the same cup → duplicate scores, double
  line adjustment, or a 500 on the second?

### Surface D — UI-level weirdness (only if cheap; use Playwright via the port-forward)
- **Back-button / stale-form replay:** complete a cup, hit Back, resubmit the
  now-stale form.
- **Double-click submit:** submit a form twice rapidly.
- Skip this surface if Playwright isn't readily available — B and C cover the same
  server-side logic via HTTP.

---

## 4. Invariant audit (after the chaos, before the final reseed)

Inspect the **staging DB directly** (read-only, same `$COMPOSE ... exec -T
staging-app python`/`sqlite3` path as §2) and confirm nothing is internally
inconsistent. Report every violation as a finding. Check at least:

- **Counter caps:** no `cups.voto_count > 4`; no `cup_players.half_veto_count > 3`;
  no cup has `COUNT(races) > 4`.
- **Race uniqueness:** no `(cup_id, race_number)` collisions; race_numbers per cup
  are the expected contiguous set.
- **Score integrity:** `scores.line_score == scores.score + scores.line` for every
  row; no duplicate `(cup_id, player_id)` in `scores` or `cup_players`.
- **Line-change consistency:** every `line_changes` row's `line_after` reflects the
  documented delta (1st −3 / 2nd 0 / 3rd +3) and only exists for 3-player,
  no-tie cups; a player's current `players.line` reconciles with the sum of their
  applied `line_changes`. A double-completed cup must **not** show a doubled delta.
- **Cup state sanity:** a `completed` cup has scores; an `in_progress` cup does not
  have finalized scores; `status` is always one of the known values.
- **Referential integrity:** no `scores`/`cup_players`/`races`/`line_changes` rows
  point at a nonexistent `cup_id`/`player_id` (e.g. a deleted player).

Compare against the deterministic post-seed baseline from §2 so you can attribute
any drift to a specific attack.

---

## 5. Report format

Deliver one report. For **each finding**:

- **Title & severity** — Critical / High / Medium / Low (Critical = data
  corruption, auth/prod bypass, or persistent broken invariant; Low = cosmetic /
  ugly-but-harmless 500).
- **Concrete repro** — the exact request(s) or steps: method, path, headers, body
  (curl or python), and ordering/timing for state-machine & concurrency bugs.
- **Observed vs. expected** — what happened (status code, DB row, rendered page)
  vs. what should have happened.
- **Proposed regression test** — the target file (existing `tests/test_*.py`, or a
  new `tests/test_adversarial.py`) plus a sketch using the pytest `client` fixture
  (Flask test client, from `tests/conftest.py`) and the `tests/helpers.py`
  helpers. The test client sends no Origin/Referer so it passes CSRF unchanged —
  mirror it after the existing tests so the finding flows straight back into the
  suite. Write the sketch as real, runnable-shaped pytest, not prose.

End the report with:
- **Invariant-audit summary** — each §4 check and pass/fail.
- **Confirmation staging was reseeded clean** (§6) and the throwaway container
  removed.

Return the report as your final message. Do **not** commit it, do not fix the
bugs, do not open a PR — a follow-up session turns accepted findings into tests
and fixes.

---

## 6. Cleanup & exit criteria

Always, even if the sweep errored partway:

```bash
cd /home/graham/km-tracker
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.access.yml -f docker-compose.staging.yml"

# 1. Tear down the throwaway QA container.
docker rm -f km-tracker-qa

# 2. Reseed staging back to the clean deterministic state.
$COMPOSE exec -T staging-app python scripts/seed_staging.py --reset

# 3. Sanity: prod was never touched — its container uptime is unchanged
#    (still "Up X hours/days", NOT "Up N seconds").
$COMPOSE ps
```

**Exit criteria — all must hold:**
- Every surface in §3 attempted (or explicitly noted as skipped, with reason).
- Invariant audit (§4) completed.
- Throwaway container removed; staging reseeded clean (§6).
- Prod untouched (verified via `docker compose ps` uptime).
- Report (§5) delivered.
