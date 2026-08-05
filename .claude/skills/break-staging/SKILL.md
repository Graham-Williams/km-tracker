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

2. **Never run while a *real* human session is live on staging.** BEFORE reseeding
   or attacking, inspect the staging DB. The naive check —
   ```
   SELECT COUNT(*) FROM cups WHERE status='in_progress' AND deleted_at IS NULL;
   ```
   is **not sufficient on its own**: a clean, idle, freshly-seeded staging *always*
   has **exactly one** in-progress cup, because `scripts/seed_staging.py --reset`
   (which §7 runs at the end of every sweep) leaves one seeded in-progress fake cup
   behind. A bare count of `> 0` would therefore falsely ABORT on normal leftover
   seed state. Instead, distinguish the **deterministic seed baseline** from **real
   activity**:
   - If in-progress cups == **0** → safe, proceed.
   - If the DB **exactly matches the seed fingerprint** (the seed script's fake
     player-name set, and its player / cup / score counts, with the lone in-progress
     cup being the seeded one — no scores, its seeded field of players + races) →
     it's leftover from a prior run's cleanup reseed, **not** a human. Safe to
     proceed and reseed.
   - **Otherwise** (extra or missing cups, non-seed / real-looking player names,
     unexpected counts, or an in-progress cup that isn't the seeded one) → a human
     may be mid-session → **ABORT and report.**

   The concrete fingerprint check (a python snippet that derives the baseline from
   the actual seed script so it can't drift) lives in §2 — run it, don't eyeball it.

3. **Never touch production.** Do not run any command against the `app` container,
   the prod DB `km_tracker.db`, or the prod hostname `km.graham-williams.com`. Do
   not run any full-stack `docker compose up` (that rebuilds/restarts prod). Only
   the `staging-app` service and disposable one-off containers are in scope. The
   repo's **"never restart/redeploy while a cup is in progress"** rule still
   applies to prod — but you should not be redeploying anything at all.

4. **Reseed to a known state before AND after.** Staging is a throwaway sandbox —
   trash it guilt-free, but leave it clean. Reseed with
   `scripts/seed_staging.py --reset` (exact command in §2/§7) **before** you start
   (known deterministic state) and **after** you finish (leave it clean). The seed
   script has its own hard rail: it refuses to run unless the DB basename contains
   `staging` (bypass only with `--force`, which you must never pass).

---

## 1. Access path (how an agent reaches staging)

The public hostname `staging-km.graham-williams.com` sits behind the **shared
app-password gate** (`APP_PASSWORD`) — every non-static path 302s to `/login`
until a session cookie is issued. (It used to be a Cloudflare Access
one-time-email-PIN; that was retired in favour of the password gate, matching
prod.) So do **not** go through the public URL. Reach staging **on the box**
instead.

Reach staging on the self-hosted box over Tailscale SSH. The staging DB lives on
the box's bind-mounted `./data` volume as `km_tracker.staging.db`. The
`staging-app` container publishes **no host port**, so you can't curl it from the
LAN, and the live gunicorn has the password gate active — so don't attack the
live container directly.

> **Box coordinates are NOT committed** (this repo is public). The concrete SSH
> target/user (`<BOX_SSH_TARGET>`) and on-box repo path (`<BOX_REPO_PATH>`) are
> deliberately kept out of every tracked file — per this repo's convention that
> box reachability + specifics live in local memory / private config, not in the
> repo. The executing agent already has them from the Hopper session context
> (personal-assistant memory + global config) or from the repo's private DEPLOY
> notes; substitute them for the placeholders below. Every command's *shape* is
> exactly what you run — only the address/user/path are placeheld.

**Canonical approach — a disposable, auth-disabled app container against the same
staging DB.** Stand up a *throwaway* one-off container from the `staging-app`
service definition, with the password gate, the Cloudflare Access check and CSRF
turned off and a
loopback port published, pointed (via the shared `./data` volume + `DB_PATH`) at
the real staging DB. It shares the staging DB with the live `staging-app` but does
**not** rebuild, restart, or reconfigure it, and never touches prod.

```bash
# ---- ON THE BOX (ssh <BOX_SSH_TARGET>), from the repo root ----
cd <BOX_REPO_PATH>

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.access.yml -f docker-compose.staging.yml"

# (Verification + reseed happen first — see §2 — using $COMPOSE ... exec staging-app.)

# Launch the throwaway QA server: auth OFF, CSRF OFF, loopback port 18080,
# same ./data volume so DB_PATH=/data/km_tracker.staging.db hits the real
# staging DB. --no-deps so nothing else is started/restarted.
#
# APP_PASSWORD= is REQUIRED: staging inherits the shared password gate, and with
# it set every path 302s to /login, so the whole sweep would measure the login
# page instead of the app. Blank turns the gate off in this throwaway only.
$COMPOSE run -d --name km-tracker-qa --no-deps \
  -p 127.0.0.1:18080:8080 \
  -e CSRF_PROTECTION=0 \
  -e APP_PASSWORD= \
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
ssh -L 18080:127.0.0.1:18080 <BOX_SSH_TARGET>   # keep this session open
```

**Teardown (always, even on failure):**
```bash
docker rm -f km-tracker-qa
```

> Why a throwaway and not the live container: it's deterministic (auth reliably
> off regardless of how the box `.env` set `APP_PASSWORD`/`STAGING_CF_ACCESS_AUD`), it needs no
> reconfiguration of the running staging service, and true request concurrency
> against the same DB row (the race-condition surface) works because the
> throwaway runs its own gunicorn workers. The live `staging-app` stays quiescent
> (nothing routes traffic to it during the sweep).

---

## 2. Pre-flight: verify staging, check for live cups, reseed

Do this on the box, in order, **before** launching the QA container or attacking.

```bash
cd <BOX_REPO_PATH>
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.access.yml -f docker-compose.staging.yml"

# (a) Confirm the DB is the STAGING db AND decide whether it's safe to reseed, in
#     one read-only shot against the DB inside the live staging container. The
#     safety decision distinguishes the deterministic seed baseline (leftover from
#     a prior sweep's §7 cleanup reseed) from a real human session — see guardrail
#     2. The expected fingerprint is derived FROM the actual seed script at runtime
#     (fake player names, and player/cup/score counts) so it never drifts: if the
#     seed roster/shape changes, this check reads the new values straight from
#     scripts/seed_staging.py.
$COMPOSE exec -T staging-app python - <<'PY'
import os, sqlite3, sys, tempfile, importlib.util

# --- Guardrail 1: prove this is the staging DB before reading anything. ---
db = os.environ["DB_PATH"]
assert "staging" in os.path.basename(db).lower(), f"NOT STAGING: {db} — ABORT"

# --- Derive the seed fingerprint from the real seed script (no drift). ---
# Load /app/scripts/seed_staging.py as a module; put /app on the path so its
# own `from db import ...` resolves. Then re-run seed() against a throwaway temp
# DB to get the EXACT deterministic counts (players/cups/scores) it produces,
# and read its fake-player roster + in-progress-cup constant directly.
sys.path.insert(0, "/app")
spec = importlib.util.spec_from_file_location("seed_staging", "/app/scripts/seed_staging.py")
seed_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_mod)

expected_names = {p["name"] for p in seed_mod.FAKE_PLAYERS}
expected_in_progress = seed_mod.NUM_IN_PROGRESS_CUPS          # deterministic (== 1)
tmp = os.path.join(tempfile.mkdtemp(), "fingerprint.staging.db")
try:
    baseline = seed_mod.seed(tmp)                            # {'players','cups','scores'}
finally:
    for sfx in ("", "-journal", "-wal", "-shm"):
        try: os.remove(tmp + sfx)
        except OSError: pass
expected = {"players": baseline["players"], "cups": baseline["cups"],
            "scores": baseline["scores"]}

# --- Read the LIVE staging DB state. ---
c = sqlite3.connect(db)
live_names = {r[0] for r in c.execute("SELECT name FROM players")}
live = {
    "players": c.execute("SELECT COUNT(*) FROM players").fetchone()[0],
    "cups":    c.execute("SELECT COUNT(*) FROM cups").fetchone()[0],
    "scores":  c.execute("SELECT COUNT(*) FROM scores").fetchone()[0],
}
inprog_rows = c.execute(
    "SELECT id FROM cups WHERE status='in_progress' AND deleted_at IS NULL"
).fetchall()
inprog = len(inprog_rows)

# Is the single in-progress cup shaped like the seeded one? The seed's
# _insert_in_progress_cup: no scores, a field of 5 cup_players
# (rng.sample(player_ids, 5)), and one race per IN_PROGRESS_MAPS entry. Deriving
# race count from IN_PROGRESS_MAPS keeps it drift-free; the field of 5 is a plain
# literal in the seed, so mirror it here (bump if the seed changes the field size).
SEED_INPROG_FIELD = 5
seeded_shaped_inprog = False
if inprog == expected_in_progress == 1:
    cid = inprog_rows[0][0]
    n_scores  = c.execute("SELECT COUNT(*) FROM scores WHERE cup_id=?", (cid,)).fetchone()[0]
    n_players = c.execute("SELECT COUNT(*) FROM cup_players WHERE cup_id=?", (cid,)).fetchone()[0]
    n_races   = c.execute("SELECT COUNT(*) FROM races WHERE cup_id=?", (cid,)).fetchone()[0]
    seeded_shaped_inprog = (n_scores == 0 and n_players == SEED_INPROG_FIELD
                            and n_races == len(seed_mod.IN_PROGRESS_MAPS))

matches_seed_baseline = (
    live == expected
    and live_names == expected_names
    and inprog == expected_in_progress
    and seeded_shaped_inprog
)

print("DB_PATH =", db)
print("in_progress cups =", inprog)
print("live   =", live, "names:", sorted(live_names))
print("expect =", expected, "names:", sorted(expected_names))
print("seeded-shaped in-progress cup =", seeded_shaped_inprog)

# --- Guardrail 2 decision. ---
if inprog == 0:
    print("SAFE: no in-progress cup — proceed.")
elif matches_seed_baseline:
    print("SAFE: DB exactly matches the deterministic seed baseline "
          "(leftover from a prior cleanup reseed) — proceed.")
else:
    print("ABORT: staging state does not match the seed baseline and has a live "
          "in-progress cup — a human may be mid-session. Do NOT reseed or attack.")
    sys.exit(3)
PY
```

- If the assert fails (not a staging DB) → **ABORT** (guardrail 1).
- If the snippet prints **ABORT** / exits non-zero → **ABORT** (guardrail 2 — a
  real human session may be live). Only proceed on a `SAFE:` line.

Only if clear, reseed to the known deterministic state (the seed's fake-player
roster; 11 completed + 1 in-progress fake cup):

```bash
$COMPOSE exec -T staging-app python scripts/seed_staging.py --reset
```

Record the post-seed baseline (player ids, cup ids, the in-progress cup id) — you
attack against this known state and diff against it in the invariant audit.

---

## 3. Change-awareness — find what's new on staging and focus-fire it

Staging almost always carries a **not-yet-merged feature** that prod/`main` lacks.
Before the broad sweep, pin down exactly what that change is and build a
**dedicated, heavier attack pass** aimed straight at it. This runs **in ADDITION**
to §4's broad fan-out — it never replaces it. New code has the least test
coverage; assume the freshest change is the weakest.

### 3a. Discover the staging-vs-prod diff

On the box, prod (`app`) and staging (`staging-app`) build from the same repo at
`<BOX_REPO_PATH>`, but staging is usually deployed from a feature branch /
newer commit. Establish the delta from three angles; use whichever resolves.

> **Primary method is the host-checkout branch delta (i) below.** The container
> `git rev-parse` cross-check (ii) is **best-effort only** — `git` is **not
> installed in the app image**, so those in-container calls usually fail (they
> already carry `2>/dev/null` + a fallback). Don't rely on them; lead with the
> host-checkout diff.

```bash
cd <BOX_REPO_PATH>
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.access.yml -f docker-compose.staging.yml"
git fetch origin --quiet

# (i) Branch delta — most reliable: staging is deployed from the current feature
#     branch, prod from main. This is the code you're about to test.
git log  --oneline origin/main..HEAD                 # commits on staging, not prod
git diff --stat    origin/main...HEAD                # changed files
git diff           origin/main...HEAD -- app.py migrations/ templates/ static/

# (ii) Deployed-commit cross-check — confirm what's ACTUALLY running in each
#      container (not just what the working tree says). If git is in the image:
$COMPOSE exec -T app         git rev-parse HEAD 2>/dev/null   # prod SHA  (READ ONLY)
$COMPOSE exec -T staging-app git rev-parse HEAD 2>/dev/null   # staging SHA
#      then diff the two SHAs from the checkout:  git diff <PROD_SHA>..<STAGING_SHA>
#      If git isn't in the image, fall back to a deployed version marker
#      (e.g. VERSION file / build label) or the image cross-check below.

# (iii) Image cross-check — catch a stale/rebuilt container and compare build times:
$COMPOSE images app staging-app
```

If prod and staging report the **same** commit and the branch shows no delta, there
is no unmerged change to focus-fire — note that and skip to §4.

### 3b. Map the diff to changed surfaces

From the diff, enumerate concrete targets:
- **Routes:** `git diff origin/main...HEAD -- app.py | grep -nE "@app\.route|def "`
  — new/modified endpoints and handlers.
- **DB columns / migrations:** new files under `migrations/`, and
  `git diff origin/main...HEAD | grep -iE "ADD COLUMN|ALTER TABLE|CREATE TABLE"`
  — new columns (like the recent `game_edition` on `cups`) and enum-like fields.
- **State-machine transitions:** changed spin / voto / half-veto / next-race /
  complete / cancel guards, new `status` values, changed caps/deltas.
- **UI flows:** changed `templates/` — new form fields, new pickers (e.g. an
  edition selector), new hidden inputs.

Write a short target list (endpoints, columns, params) and carry it into 3c.

### 3c. Dedicated heavier attack pass on the changed surface

Dispatch a **dedicated subagent** (separate from and parallel to §4's broad
fan-out) that attacks **only the changed surface, harder**. First reason
explicitly about how THIS change could break, then probe each hypothesis:

- **New enum/choice column with unhandled values** (e.g. `game_edition`): POST
  every route that writes it with values outside the allowed set — empty, `null`,
  unknown string, wrong case, integer, and a value valid for *another* edition used
  where it shouldn't be. Does junk reach the DB? Does course/track validation key
  off it and either 500 or accept anything?
- **Migration leaving old rows in a bad state:** did the migration backfill
  existing rows (old cups → `game_edition='wii'`)? Query the new column on
  **pre-existing** seeded rows for NULL/empty/unexpected values, then exercise a
  read path that touches the column on an old row.
- **New field bypassing existing validation:** does the new input skip the
  `int(...)` / range / scope checks the older fields get? Hit it with the full
  Surface A hostile set and see if the new path is unguarded.
- **Changed flow breaking an old invariant:** if the feature touched a cap,
  counter, score/line formula, race numbering, or a state transition, re-run the
  relevant §5 invariant checks and prove the old guarantee still holds.
- Throw the **full Surface A/B/C toolkit** (malformed input, out-of-order,
  double-submit, concurrency) at the new/changed routes specifically.

Report every finding here under the dedicated changed-surface heading (§6).

---

## 4. Attack plan — fan out subagents in parallel

Dispatch **subagents in parallel**, one per surface below (AI budget is not a
constraint — prefer more parallel agents over serializing). Give each the access
path (§1), the baseline (§2), and the exact endpoint contract for its surface.
Each subagent returns findings in the §6 format. **Reads are free; every request
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

## 5. Invariant audit (after the chaos, before the final reseed)

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

## 6. Report format

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
- **Findings in the changed surface: <feature>** — a dedicated heading collecting
  every finding from the §3 focus-fire pass, so the human sees at a glance whether
  the currently-deployed unmerged change is safe, *separately* from pre-existing
  issues. If §3 found nothing, say so explicitly ("changed surface: no findings").
- **Invariant-audit summary** — each §5 check and pass/fail.
- **Confirmation staging was reseeded clean** (§7) and the throwaway container
  removed.

Return the report as your final message. Do **not** commit it, do not fix the
bugs, do not open a PR — a follow-up session turns accepted findings into tests
and fixes.

---

## 7. Cleanup & exit criteria

Always, even if the sweep errored partway:

```bash
cd <BOX_REPO_PATH>
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
- Every surface in §4 attempted (or explicitly noted as skipped, with reason).
- Changed surface identified (§3) and given its dedicated focus-fire pass (or
  explicitly noted that staging carries no unmerged change vs prod/main).
- Invariant audit (§5) completed.
- Throwaway container removed; staging reseeded clean (§7).
- Prod untouched (verified via `docker compose ps` uptime).
- Report (§6) delivered.
