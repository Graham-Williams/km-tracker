#!/usr/bin/env bash
#
# backup.sh — Automated off-box backups for the self-hosted KM Tracker deployment.
#
# SCOPE: This script is specific to the self-hosted "personalserver" deployment
# (headless Ubuntu Server box running KM Tracker via Docker Compose). It is
# ORCHESTRATED on the HOST (driven by a systemd timer — see
# deploy/km-backup.timer), but the snapshot itself runs INSIDE the app container
# via `docker exec`: the live DB is WAL-mode and its -wal/-shm sidecars are owned
# by the container user (UID 10001), so SQLite's online backup API cannot take
# its read lock host-side and fails with "attempt to write a readonly database".
# The finished snapshot is copied out to the host, kept in a local ring, and
# pushed off-box to Google Drive via rclone on a throttled cadence. Because it
# shells out to `docker`, the backup user must be in the `docker` group. This
# script is NOT used in local dev or CI.
#
# RESTORING is NOT a plain `cp` of a snapshot over the live DB: the stale
# -wal/-shm sidecars must be deleted and the restored file re-owned to UID 10001
# first, or SQLite silently replays the old WAL over the restored image (you get
# the PRE-restore data back, with no error, and the next checkpoint bakes the
# stale pages in). See DEPLOY.md → "Restore from a snapshot" for the verified
# procedure.
#
# Design:
#   - Frequent, cheap LOCAL snapshots (every timer tick) using SQLite's online
#     backup API, deduplicated by sha256 so identical DBs don't pile up.
#   - Decoupled, throttled DRIVE pushes (default every ~15 min, and only when the
#     DB actually changed) so we don't hammer the Drive API.
#   - The local-snapshot half always runs even if the Drive half can't (e.g.
#     rclone not configured) — losing the off-box copy must never cost us the
#     on-box copy. If rclone isn't set up yet we exit 0 (local copy is safe); we
#     only exit non-zero when a configured remote actually errors.
#   - A long-tail DRIVE "daily/" tier keeps at most one snapshot per UTC day for
#     DAILY_RETENTION days, defending against slow logical corruption that would
#     otherwise rotate out of the flat recent-snapshot ring buffer.
#   - A snapshot whose every user table is EMPTY is refused when the previous
#     snapshot had data: entrypoint.sh runs init_db() on every container start,
#     so a /data remounted empty produces a perfectly valid, integrity-ok,
#     schema-only DB that would otherwise rotate every good copy out of the ring.
#     Override with ALLOW_EMPTY_SNAPSHOT=1 for the deliberate case.
#
# Secrets: the rclone OAuth token lives ONLY in rclone's own config
# (~/.config/rclone/rclone.conf). No tokens or secrets are read from, written to,
# or echoed by this script.
#
set -euo pipefail

# --- Helpers ----------------------------------------------------------------
log() { printf '%s backup.sh: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

# Octal permission bits of a file (Linux `stat -c` primary; macOS `stat -f`
# fallback so the script is testable off-box). Echoes e.g. "644"; non-zero rc if
# neither stat form works.
perms_of() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null
}

# True if the file is writable by group or other (a tamper risk for a file we
# `source`). Returns 2 if we can't determine the mode.
is_group_or_other_writable() {
  local mode perms group_digit other_digit
  mode="$(perms_of "$1")" || return 2
  perms="${mode: -3}"                 # last 3 octal digits (owner/group/other)
  group_digit="${perms:1:1}"
  other_digit="${perms:2:1}"
  (( (group_digit & 2) || (other_digit & 2) ))
}

# Portable sha256 of a file -> bare hex digest. Ubuntu has sha256sum; fall back
# to shasum (macOS / minimal images) so the script is testable off-box too.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

# Assert a config value is a positive integer (>= 1). A non-numeric retention
# would arithmetic-evaluate to 0 and prune EVERYTHING — fail loudly instead.
require_positive_int() {
  local name="$1" val="$2"
  [[ "${val}" =~ ^[0-9]+$ ]] || die "${name}='${val}' is not an integer (must be >= 1)"
  (( val >= 1 )) || die "${name}='${val}' must be >= 1"
}

# --- Load configuration -----------------------------------------------------
# Optional gitignored config file in the repo root. Resolve the repo root from
# this script's location so the script works regardless of the caller's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env.backup"
if [[ -f "${ENV_FILE}" ]]; then
  # Sourcing executes the file as code every timer tick. If it's writable by
  # anyone other than the owner, an attacker could drop commands in it — refuse.
  if is_group_or_other_writable "${ENV_FILE}"; then
    die "${ENV_FILE} is group/other-writable — refusing to source it (run: chmod 600 ${ENV_FILE})"
  elif (( $? == 2 )); then
    log "WARN: could not determine permissions of ${ENV_FILE}; sourcing anyway"
  fi
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

# Config vars with sane defaults (env / .env.backup override these).
DB_PATH="${DB_PATH:-${HOME}/km-tracker/data/km_tracker.db}"
# The live DB is WAL-mode and owned by the container user (UID 10001). SQLite's
# online backup API must be able to write the DB's -wal/-shm sidecars to take its
# read lock, so it MUST run as UID 10001 — i.e. INSIDE the app container. Running
# it host-side (as the unprivileged backup user) fails with "attempt to write a
# readonly database". We therefore run the snapshot via `docker exec` in the app
# container against the container-internal DB path, then copy the finished file
# out to the host. See DEPLOY.md → "Automated backups".
BACKUP_CONTAINER="${BACKUP_CONTAINER:-km-tracker-app-1}"   # prod app container name
CONTAINER_DB_PATH="${CONTAINER_DB_PATH:-/data/km_tracker.db}"  # DB path INSIDE the container
# Outputs default OUTSIDE data/ (issue #19): data/ is bind-mounted and owned by
# the container user (UID 10001), so the host `backup.sh` process (the graham
# user) can't create/write dirs inside it on a fresh deploy. DB_PATH stays inside
# data/ because that's the live DB the container writes; only our OUTPUT dirs move.
LOCAL_BACKUP_DIR="${LOCAL_BACKUP_DIR:-${HOME}/km-backups/snapshots}"
STATE_DIR="${STATE_DIR:-${HOME}/km-backups/state}"
RCLONE_DEST="${RCLONE_DEST:-}"                       # e.g. gdrive:km-tracker-backups
LOCAL_RETENTION="${LOCAL_RETENTION:-100}"            # keep newest N local snapshots
DRIVE_RETENTION="${DRIVE_RETENTION:-50}"             # keep newest N recent on Drive
DAILY_RETENTION="${DAILY_RETENTION:-30}"             # keep newest N in Drive daily/ tier
DRIVE_PUSH_INTERVAL_MIN="${DRIVE_PUSH_INTERVAL_MIN:-15}"  # min minutes between Drive pushes
# Set to 1 to accept a snapshot with zero rows in every user table even when the
# previous snapshot had data (see the empty-DB guard in the verification below).
# Only for the deliberate case (e.g. you really did just wipe the DB on purpose).
ALLOW_EMPTY_SNAPSHOT="${ALLOW_EMPTY_SNAPSHOT:-0}"

# Validate retention config BEFORE any prune runs (a bad value would delete data).
require_positive_int LOCAL_RETENTION "${LOCAL_RETENTION}"
require_positive_int DRIVE_RETENTION "${DRIVE_RETENTION}"
require_positive_int DAILY_RETENTION "${DAILY_RETENTION}"

# State files (checksums + push timestamp) live in STATE_DIR.
LOCAL_CKSUM_FILE="${STATE_DIR}/last_local.sha256"
DRIVE_CKSUM_FILE="${STATE_DIR}/last_drive.sha256"
DRIVE_PUSH_TS_FILE="${STATE_DIR}/last_drive_push.epoch"

# --- Preconditions ----------------------------------------------------------
[[ -f "${DB_PATH}" ]] || die "DB not found at DB_PATH=${DB_PATH}"
command -v docker >/dev/null 2>&1 || die "docker not found on PATH — the snapshot runs inside the app container (see DEPLOY.md)"
# The container must be running for `docker exec` to reach the DB as UID 10001.
# Keep stderr: "can't talk to the daemon" and "no such container" are different
# problems, and the first one is the NEW failure mode this design introduces —
# the backup user must now be in the `docker` group. Reporting a socket
# permission error as "container is not running" would send an operator hunting
# the wrong thing entirely.
DOCKER_INSPECT_OUT=""
if ! DOCKER_INSPECT_OUT="$(docker inspect -f '{{.State.Running}}' "${BACKUP_CONTAINER}" 2>&1)"; then
  case "${DOCKER_INSPECT_OUT}" in
    *"permission denied"*|*"Permission denied"*)
      die "cannot access the Docker socket as user '$(id -un)' — the backup user must be in the 'docker' group (sudo usermod -aG docker $(id -un), then re-login / restart the unit; see DEPLOY.md → 'Automated backups'). docker said: ${DOCKER_INSPECT_OUT}" ;;
    *"Cannot connect to the Docker daemon"*|*"Is the docker daemon running"*)
      die "cannot connect to the Docker daemon (is it running?) — cannot snapshot. docker said: ${DOCKER_INSPECT_OUT}" ;;
    *)
      die "app container '${BACKUP_CONTAINER}' not found — cannot snapshot (set BACKUP_CONTAINER in .env.backup if the name differs). docker said: ${DOCKER_INSPECT_OUT}" ;;
  esac
fi
[[ "${DOCKER_INSPECT_OUT}" == "true" ]] \
  || die "app container '${BACKUP_CONTAINER}' exists but is not running (State.Running=${DOCKER_INSPECT_OUT}) — cannot snapshot"
mkdir -p "${LOCAL_BACKUP_DIR}" "${STATE_DIR}"

# --- Make a consistent snapshot --------------------------------------------
# Use SQLite's online backup API via python3 (stdlib only — no sqlite3 CLI, no
# pip deps). This is safe to run while gunicorn is writing: the backup API copies
# a transactionally consistent image of the DB.
#
# CRITICAL: the backup MUST run INSIDE the app container (as UID 10001), because
# the live DB is WAL-mode and its -wal/-shm sidecars are owned by 10001 — the
# online backup API needs to write them to take its read lock, so running it
# host-side (as the backup user) fails with "attempt to write a readonly
# database". We snapshot to a container-local temp file, integrity-check it in
# the container, then `docker cp` it out to a host temp file and decide (by
# checksum) whether to keep it.
TMP_SNAPSHOT="$(mktemp "${LOCAL_BACKUP_DIR}/.snapshot.XXXXXX.db")"
# Arm cleanup BEFORE creating the container temp, so an early failure (e.g. the
# in-container mktemp below) still removes the stray host temp file — it's a
# hidden dotfile the km_tracker_*.db prune globs never reap. cleanup tolerates
# CONTAINER_TMP_SNAPSHOT being unset (it's created just after) under `set -u`.
cleanup() {
  # Glob the suffix too: the verify step below opens the snapshot, and since
  # .backup() copies the source's journal mode the snapshot is WAL, so SQLite
  # transiently creates <snap>-wal/-shm. A clean close removes them, but a crash
  # mid-verify would strand them next to the temp.
  rm -f "${TMP_SNAPSHOT}" "${TMP_SNAPSHOT}"-*
  [[ -n "${CONTAINER_TMP_SNAPSHOT:-}" ]] \
    && docker exec "${BACKUP_CONTAINER}" rm -f "${CONTAINER_TMP_SNAPSHOT}" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# Create the in-container temp path (as UID 10001) for the snapshot destination.
CONTAINER_TMP_SNAPSHOT="$(docker exec "${BACKUP_CONTAINER}" mktemp /tmp/km_snapshot.XXXXXX.db)" \
  || die "could not create a temp snapshot path inside container ${BACKUP_CONTAINER}"

# Run the online backup + integrity check inside the container. Paths are passed
# via `-e` env (never interpolated into the python source), and the DB is opened
# by plain path (NOT a file: URI): .backup() is read-only w.r.t. the source so we
# don't need mode=ro, a plain connection opens WAL DBs reliably, and this avoids
# any URI-param injection if a path ever contains "?".
docker exec -i \
  -e SRC_PATH="${CONTAINER_DB_PATH}" \
  -e DST_PATH="${CONTAINER_TMP_SNAPSHOT}" \
  "${BACKUP_CONTAINER}" python3 - <<'PY'
import os
import sqlite3
import sys

src_path = os.environ["SRC_PATH"]
dst_path = os.environ["DST_PATH"]

# Guard the file we are ACTUALLY backing up. The host-side `-f "${DB_PATH}"`
# precondition checks a host path; the source we read here is CONTAINER_DB_PATH
# inside the container, and those can diverge (e.g. a compose change remounts
# /data from a fresh, empty volume). Without this, sqlite3.connect() would
# CREATE the missing file and .backup() would faithfully copy an empty DB — a
# 4 KB snapshot that passes integrity_check, reports success, and then rotates
# every good copy out of the local ring and the Drive tiers. Fail loudly instead.
if not os.path.isfile(src_path):
    sys.stderr.write(f"source DB not found inside container at {src_path}\n")
    sys.exit(1)

src = sqlite3.connect(src_path)
try:
    dst = sqlite3.connect(dst_path)
    try:
        # .backup() performs the online backup (consistent, copy-on-write safe).
        src.backup(dst)
    finally:
        dst.close()
finally:
    src.close()

# Sanity check the SNAPSHOT inside the container, where UID 10001 can freely
# create the snapshot's own -wal/-shm sidecars, before we copy it to the host.
check = sqlite3.connect(dst_path)
try:
    ok = check.execute("PRAGMA integrity_check").fetchone()[0]
finally:
    check.close()
if ok != "ok":
    sys.stderr.write(f"integrity_check failed: {ok}\n")
    sys.exit(1)
PY

# Copy the finished snapshot out of the container onto the host temp file. The
# host user OWNS this file (created via mktemp), so downstream host-side reads
# (sha256, rclone) work without any container-user permission issues.
docker cp "${BACKUP_CONTAINER}:${CONTAINER_TMP_SNAPSHOT}" "${TMP_SNAPSHOT}" \
  || die "docker cp of snapshot out of ${BACKUP_CONTAINER} failed"
# Drop the container temp immediately, and clear the var ONLY if that removal
# actually succeeded, so the EXIT trap skips its redundant `docker exec` on the
# success path but still retries the removal if this one failed silently.
docker exec "${BACKUP_CONTAINER}" rm -f "${CONTAINER_TMP_SNAPSHOT}" >/dev/null 2>&1 \
  && CONTAINER_TMP_SNAPSHOT="" || true

# Re-verify the HOST copy — the file we actually keep and push off-box. The
# in-container integrity_check above validated the pre-`docker cp` file; a
# truncated or corrupted copy would otherwise ship to Drive undetected, since
# everything downstream only ever sha256s this file.
#
# This DOES open a WAL-mode DB host-side — .backup() copies the source's journal
# mode, so SQLite creates <snap>-wal/-shm here and removes them on clean close.
# That's safe, and is not the case this script exists to avoid: the snapshot and
# its directory are owned by the HOST user, so creating those sidecars succeeds.
# The failure this script fixes is reading the CONTAINER-owned live DB, whose
# sidecars the host user cannot create or write. (`main` opened the snapshot
# host-side in exactly this way too, so this is not new exposure.)
#
# The verification also guards the realistic EMPTY-DB case (see below): the
# newest snapshot already on disk is passed in as the "previous" copy to compare
# against. It is resolved here (not just at dedupe time) so both users see the
# same file, and it is opened immutable so we never touch it.
PREV_SNAPSHOT="$(ls -1 "${LOCAL_BACKUP_DIR}"/km_tracker_*.db 2>/dev/null | sort | tail -n1 || true)"
VERIFY_RC=0
python3 - "${TMP_SNAPSHOT}" "${PREV_SNAPSHOT}" "${ALLOW_EMPTY_SNAPSHOT}" <<'PY' || VERIFY_RC=$?
import pathlib
import sqlite3
import sys

path = sys.argv[1]
prev_path = sys.argv[2]          # "" when there is no previous snapshot yet
allow_empty = sys.argv[3] == "1"

EMPTY_RC = 3                     # distinct rc so the caller can explain this case


def warn(msg):
    sys.stderr.write(f"snapshot verification: {msg}\n")


def has_any_row(conn):
    """True if ANY user table holds at least one row.

    Table names come from the DB's own sqlite_master and must be interpolated —
    SQLite cannot parameterize an identifier — so each is identifier-quoted with
    doubled quotes. sqlite_* internal tables (e.g. sqlite_sequence) are excluded:
    they are bookkeeping, not content. Stops at the first non-empty table, so
    this stays cheap.
    """
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        )
    ]
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        if conn.execute(f"SELECT EXISTS(SELECT 1 FROM {quoted})").fetchone()[0]:
            return True
    return False


conn = sqlite3.connect(path)
try:
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        warn("integrity_check failed on the copied-out snapshot")
        sys.exit(1)
    # An empty-but-valid DB passes integrity_check, so assert it has content.
    if conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0] == 0:
        warn("snapshot contains no tables")
        sys.exit(1)
    snapshot_has_rows = has_any_row(conn)
finally:
    conn.close()

if snapshot_has_rows:
    sys.exit(0)

# Zero rows in EVERY user table. Having tables is not a sufficient guard: the
# container's entrypoint.sh runs init_db() on EVERY start, so if /data is ever
# remounted empty the app immediately recreates a schema-only DB — valid,
# integrity-ok, full table count, and completely empty. Snapshotting that would
# rotate every real copy out of the local ring and the Drive tiers.
#
# Only refuse when this is visibly a REGRESSION (the previous snapshot had data).
# With no previous snapshot at all an empty DB is legitimate — a first-ever run
# against a brand-new deployment — and refusing would break bootstrapping. The
# check counts rows rather than bytes so a legitimate VACUUM, or genuinely
# deleting old cups, can't false-positive.
if allow_empty:
    warn("no rows in any table; ALLOW_EMPTY_SNAPSHOT=1 — accepting it")
    sys.exit(0)
if not prev_path:
    warn("no rows in any table, but there is no previous snapshot to compare "
         "against — accepting it (first run / bootstrap)")
    sys.exit(0)
try:
    # Read the previous snapshot immutable: opening it normally would create
    # <snapshot>-wal/-shm beside it (snapshots inherit the live DB's WAL journal
    # mode) and those sidecars are NOT reaped by the km_tracker_*.db prune.
    # immutable=1 reads the file directly and creates nothing. Path -> URI via
    # pathlib so any "?" or "#" is percent-encoded, never parsed as a URI param.
    prev_uri = pathlib.Path(prev_path).resolve(strict=True).as_uri() + "?immutable=1"
    prev = sqlite3.connect(prev_uri, uri=True)
    try:
        prev_has_rows = has_any_row(prev)
    finally:
        prev.close()
except Exception as exc:  # unreadable/corrupt previous snapshot — can't compare
    warn(f"no rows in any table and the previous snapshot could not be read "
         f"({exc}) — accepting it")
    sys.exit(0)

if prev_has_rows:
    sys.exit(EMPTY_RC)
warn("no rows in any table, and neither had the previous one — accepting it")
sys.exit(0)
PY
if (( VERIFY_RC == 3 )); then
  die "refusing this snapshot: EVERY table is empty while the previous snapshot (${PREV_SNAPSHOT##*/}) has data — the live DB looks wiped (a remounted-empty /data makes the container recreate a valid, empty DB on start). Investigate before the good copies rotate out; set ALLOW_EMPTY_SNAPSHOT=1 in .env.backup if the DB really was emptied on purpose"
elif (( VERIFY_RC != 0 )); then
  die "snapshot failed verification after copy out of the container"
fi

# --- Checksum + dedupe ------------------------------------------------------
SNAP_CKSUM="$(sha256_of "${TMP_SNAPSHOT}")"
LAST_LOCAL_CKSUM=""
[[ -f "${LOCAL_CKSUM_FILE}" ]] && LAST_LOCAL_CKSUM="$(cat "${LOCAL_CKSUM_FILE}")"

LATEST_LOCAL_SNAPSHOT=""
if [[ "${SNAP_CKSUM}" == "${LAST_LOCAL_CKSUM}" && -n "${PREV_SNAPSHOT}" && -f "${PREV_SNAPSHOT}" ]]; then
  # DB unchanged since the last local snapshot AND that snapshot is still on
  # disk — don't create a duplicate file. It's what we'd push to Drive if needed.
  log "no change since last local snapshot (sha ${SNAP_CKSUM:0:12}); skipping new local file"
  LATEST_LOCAL_SNAPSHOT="${PREV_SNAPSHOT}"
else
  if [[ "${SNAP_CKSUM}" == "${LAST_LOCAL_CKSUM}" ]]; then
    # The state file says "already snapshotted" but no snapshot file exists (the
    # dir was cleared to reclaim disk, or tidied up after a restore). Dedupe here
    # would leave LATEST_LOCAL_SNAPSHOT empty, fail the Drive push, and go on
    # doing that on every tick until the DB content happened to change — silent
    # flapping, the exact failure shape this script exists to avoid. Keep the
    # fresh snapshot instead; writing the checksum below re-syncs the state.
    log "WARN: state checksum matches but no snapshot file remains in ${LOCAL_BACKUP_DIR} — keeping this snapshot to re-sync state"
  fi
  TS="$(date -u +%Y%m%dT%H%M%SZ)"
  DEST_SNAPSHOT="${LOCAL_BACKUP_DIR}/km_tracker_${TS}.db"
  # TS is 1-second resolution, so two runs in the same second (the timer plus a
  # manual `scripts/backup.sh`) would target the same name and `mv` would
  # silently destroy the first snapshot. Uniquify instead. The "_N" suffix still
  # sorts after the bare name ("_" > "."), so newest-first ordering — and the
  # daily tier's km_tracker_<date>T* match — keep working.
  COLLISION_N=1
  while [[ -e "${DEST_SNAPSHOT}" ]]; do
    DEST_SNAPSHOT="${LOCAL_BACKUP_DIR}/km_tracker_${TS}_${COLLISION_N}.db"
    COLLISION_N=$(( COLLISION_N + 1 ))
  done
  mv "${TMP_SNAPSHOT}" "${DEST_SNAPSHOT}"
  printf '%s\n' "${SNAP_CKSUM}" > "${LOCAL_CKSUM_FILE}"
  LATEST_LOCAL_SNAPSHOT="${DEST_SNAPSHOT}"
  log "saved local snapshot ${DEST_SNAPSHOT##*/} (sha ${SNAP_CKSUM:0:12})"
fi

# --- Prune local snapshots to newest LOCAL_RETENTION ------------------------
# List newest-first, drop the first LOCAL_RETENTION, delete the rest.
LOCAL_SNAPSHOTS=()
while IFS= read -r f; do LOCAL_SNAPSHOTS+=("$f"); done \
  < <(ls -1 "${LOCAL_BACKUP_DIR}"/km_tracker_*.db 2>/dev/null | sort -r || true)
if (( ${#LOCAL_SNAPSHOTS[@]} > LOCAL_RETENTION )); then
  for old in "${LOCAL_SNAPSHOTS[@]:LOCAL_RETENTION}"; do
    rm -f "${old}"
    log "pruned local snapshot ${old##*/}"
  done
fi

# --- Drive push (decoupled cadence) -----------------------------------------
# Return codes:
#   0  — pushed OK, or intentionally skipped (throttle/dedup/unconfigured rclone)
#   1  — a CONFIGURED remote actually errored (worth surfacing as a unit failure)
# Rationale: while the user hasn't finished rclone setup yet, the local snapshot
# is already safe — we must NOT mark the systemd unit failed every 5 minutes. So
# "rclone not installed / remote not configured" => WARN + return 0. Only a real
# failure of a configured remote returns non-zero.
drive_push() {
  [[ -n "${RCLONE_DEST}" ]] || { log "WARN: RCLONE_DEST not set — local snapshot saved; skipping Drive push (set it in .env.backup once rclone is configured)"; return 0; }
  [[ -n "${LATEST_LOCAL_SNAPSHOT}" && -f "${LATEST_LOCAL_SNAPSHOT}" ]] || { log "ERROR: no local snapshot available to push"; return 1; }

  # rclone unconfigured? Parse the remote name (part before the first ':') and
  # check it actually exists. If not, the user hasn't finished setup — warn and
  # exit 0 rather than failing the unit on every tick.
  if ! command -v rclone >/dev/null 2>&1; then
    log "WARN: rclone not installed — local snapshot saved; skipping Drive push (see DEPLOY.md)"
    return 0
  fi
  local remote_name="${RCLONE_DEST%%:*}"
  if ! rclone listremotes 2>/dev/null | grep -qx "${remote_name}:"; then
    log "WARN: rclone remote '${remote_name}:' not configured — local snapshot saved; skipping Drive push (run 'rclone config', see DEPLOY.md)"
    return 0
  fi

  # (a) Throttle: has it been >= DRIVE_PUSH_INTERVAL_MIN since the last push?
  local now last_push elapsed_min
  now="$(date +%s)"
  last_push=0
  [[ -f "${DRIVE_PUSH_TS_FILE}" ]] && last_push="$(cat "${DRIVE_PUSH_TS_FILE}")"
  elapsed_min=$(( (now - last_push) / 60 ))
  if (( elapsed_min < DRIVE_PUSH_INTERVAL_MIN )); then
    log "last Drive push was ${elapsed_min}min ago (< ${DRIVE_PUSH_INTERVAL_MIN}min); skipping Drive push"
    return 0
  fi

  # (b) Only push if the snapshot differs from what's already on Drive.
  local last_drive_cksum=""
  [[ -f "${DRIVE_CKSUM_FILE}" ]] && last_drive_cksum="$(cat "${DRIVE_CKSUM_FILE}")"
  if [[ "${SNAP_CKSUM}" == "${last_drive_cksum}" ]]; then
    log "Drive already has current DB (sha ${SNAP_CKSUM:0:12}); skipping Drive push"
    return 0
  fi

  # Copy the latest local snapshot up. rclone auto-creates the dest folder.
  log "pushing ${LATEST_LOCAL_SNAPSHOT##*/} to ${RCLONE_DEST}"
  rclone copy "${LATEST_LOCAL_SNAPSHOT}" "${RCLONE_DEST}" || { log "ERROR: rclone copy failed"; return 1; }

  # Prune the Drive folder to newest DRIVE_RETENTION snapshots. List our snapshot
  # files, sort newest-first (timestamped names sort lexically == chronologically),
  # and deletefile anything past the retention count.
  #
  # --files-only is load-bearing: without it `lsf` also lists the daily/ SUBDIR,
  # which reverse-sorts last and therefore lands in the delete slice on every
  # single run once the listing exceeds DRIVE_RETENTION. `deletefile` refuses a
  # directory, so nothing was lost — but it logged rclone ERRORs every push
  # (making a genuinely failing prune indistinguishable from the noise) and the
  # phantom entry meant the ring actually kept DRIVE_RETENTION-1 snapshots.
  local remote_files=()
  local rf
  while IFS= read -r rf; do remote_files+=("$rf"); done \
    < <(rclone lsf "${RCLONE_DEST}" --files-only --include 'km_tracker_*.db' 2>/dev/null | sort -r || true)
  if (( ${#remote_files[@]} > DRIVE_RETENTION )); then
    for old in "${remote_files[@]:DRIVE_RETENTION}"; do
      rclone deletefile "${RCLONE_DEST}/${old}" && log "pruned Drive snapshot ${old}" || log "WARN: failed to prune Drive snapshot ${old}"
    done
  fi

  # --- Daily long-tail tier -------------------------------------------------
  # The recent ring buffer above (DRIVE_RETENTION) can rotate out within hours of
  # frequent pushes, so a logical corruption that goes unnoticed for a day or two
  # could lose its last clean copy. Keep a separate daily/ subfolder holding at
  # most ONE snapshot per UTC day, retained for DAILY_RETENTION days.
  local daily_dest="${RCLONE_DEST}/daily"
  local today
  today="$(date -u +%Y%m%d)"
  # Has a daily snapshot already been added for today? Daily files keep their
  # original km_tracker_<UTC-timestamp>.db name, so today's copy starts with
  # km_tracker_<today>T. If none exists yet, add the current snapshot.
  local existing_today
  existing_today="$(rclone lsf "${daily_dest}" --files-only --include "km_tracker_${today}T*.db" 2>/dev/null | head -n1 || true)"
  if [[ -z "${existing_today}" ]]; then
    log "adding daily snapshot for ${today} to ${daily_dest}"
    rclone copy "${LATEST_LOCAL_SNAPSHOT}" "${daily_dest}" || { log "ERROR: rclone copy to daily/ failed"; return 1; }
    # Prune daily/ to the newest DAILY_RETENTION files. (--files-only for the
    # same reason as above; daily/ holds no subdirs today, but a stray one would
    # otherwise wedge itself permanently into the delete slice here too.)
    local daily_files=()
    local dfile
    while IFS= read -r dfile; do daily_files+=("$dfile"); done \
      < <(rclone lsf "${daily_dest}" --files-only --include 'km_tracker_*.db' 2>/dev/null | sort -r || true)
    if (( ${#daily_files[@]} > DAILY_RETENTION )); then
      for old in "${daily_files[@]:DAILY_RETENTION}"; do
        rclone deletefile "${daily_dest}/${old}" && log "pruned daily snapshot ${old}" || log "WARN: failed to prune daily snapshot ${old}"
      done
    fi
  fi

  # Record successful push: timestamp + the checksum now on Drive.
  printf '%s\n' "${now}" > "${DRIVE_PUSH_TS_FILE}"
  printf '%s\n' "${SNAP_CKSUM}" > "${DRIVE_CKSUM_FILE}"
  log "Drive push complete (sha ${SNAP_CKSUM:0:12})"
}

# Run the Drive push: report its failure but keep the overall exit status clean
# as long as local snapshots succeeded and the Drive half only *skipped* (rc 0).
# A non-zero rc means a configured remote actually errored — surface that.
DRIVE_RC=0
drive_push || DRIVE_RC=$?
if (( DRIVE_RC != 0 )); then
  log "Drive push did not complete (rc=${DRIVE_RC}); local snapshots are unaffected"
  exit "${DRIVE_RC}"
fi

log "done"
