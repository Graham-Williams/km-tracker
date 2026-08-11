#!/usr/bin/env bash
#
# backup.sh — Automated off-box backups for the self-hosted KM Tracker deployment.
#
# SCOPE: This script is specific to the self-hosted "personalserver" deployment
# (headless Ubuntu Server box running KM Tracker via Docker Compose). It runs on
# the HOST — not inside the app container — and snapshots the live SQLite DB that
# is bind-mounted into the container, then pushes snapshots off-box to Google
# Drive via rclone on a throttled cadence. It is driven by a systemd timer (see
# deploy/km-backup.timer). It is NOT used in local dev or CI.
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
docker inspect -f '{{.State.Running}}' "${BACKUP_CONTAINER}" 2>/dev/null | grep -qx true \
  || die "app container '${BACKUP_CONTAINER}' is not running — cannot snapshot (set BACKUP_CONTAINER in .env.backup if the name differs)"
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
# Drop the container temp immediately, then clear the var so the EXIT trap
# doesn't fire a second, redundant `docker exec` on every successful run.
docker exec "${BACKUP_CONTAINER}" rm -f "${CONTAINER_TMP_SNAPSHOT}" >/dev/null 2>&1 || true
CONTAINER_TMP_SNAPSHOT=""

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
python3 - "${TMP_SNAPSHOT}" <<'PY' || die "snapshot failed verification after copy out of the container"
import sqlite3
import sys

path = sys.argv[1]
conn = sqlite3.connect(path)
try:
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        sys.exit(1)
    # An empty-but-valid DB passes integrity_check, so assert it has content.
    if conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0] == 0:
        sys.stderr.write("snapshot contains no tables\n")
        sys.exit(1)
finally:
    conn.close()
PY

# --- Checksum + dedupe ------------------------------------------------------
SNAP_CKSUM="$(sha256_of "${TMP_SNAPSHOT}")"
LAST_LOCAL_CKSUM=""
[[ -f "${LOCAL_CKSUM_FILE}" ]] && LAST_LOCAL_CKSUM="$(cat "${LOCAL_CKSUM_FILE}")"

LATEST_LOCAL_SNAPSHOT=""
if [[ "${SNAP_CKSUM}" == "${LAST_LOCAL_CKSUM}" ]]; then
  # DB unchanged since the last local snapshot — don't create a duplicate file.
  log "no change since last local snapshot (sha ${SNAP_CKSUM:0:12}); skipping new local file"
  # The newest existing snapshot is what we'd push to Drive if needed.
  LATEST_LOCAL_SNAPSHOT="$(ls -1 "${LOCAL_BACKUP_DIR}"/km_tracker_*.db 2>/dev/null | sort | tail -n1 || true)"
else
  TS="$(date -u +%Y%m%dT%H%M%SZ)"
  DEST_SNAPSHOT="${LOCAL_BACKUP_DIR}/km_tracker_${TS}.db"
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
  local remote_files=()
  local rf
  while IFS= read -r rf; do remote_files+=("$rf"); done \
    < <(rclone lsf "${RCLONE_DEST}" --include 'km_tracker_*.db' 2>/dev/null | sort -r || true)
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
  existing_today="$(rclone lsf "${daily_dest}" --include "km_tracker_${today}T*.db" 2>/dev/null | head -n1 || true)"
  if [[ -z "${existing_today}" ]]; then
    log "adding daily snapshot for ${today} to ${daily_dest}"
    rclone copy "${LATEST_LOCAL_SNAPSHOT}" "${daily_dest}" || { log "ERROR: rclone copy to daily/ failed"; return 1; }
    # Prune daily/ to the newest DAILY_RETENTION files.
    local daily_files=()
    local dfile
    while IFS= read -r dfile; do daily_files+=("$dfile"); done \
      < <(rclone lsf "${daily_dest}" --include 'km_tracker_*.db' 2>/dev/null | sort -r || true)
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
