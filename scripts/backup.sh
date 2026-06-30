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
#     on-box copy.
#
# Secrets: the rclone OAuth token lives ONLY in rclone's own config
# (~/.config/rclone/rclone.conf). No tokens or secrets are read from, written to,
# or echoed by this script.
#
set -euo pipefail

# --- Load configuration -----------------------------------------------------
# Optional gitignored config file in the repo root. Resolve the repo root from
# this script's location so the script works regardless of the caller's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env.backup"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

# Config vars with sane defaults (env / .env.backup override these).
DB_PATH="${DB_PATH:-${HOME}/km-tracker/data/km_tracker.db}"
LOCAL_BACKUP_DIR="${LOCAL_BACKUP_DIR:-${HOME}/km-tracker/data/backups}"
STATE_DIR="${STATE_DIR:-${HOME}/km-tracker/data/.backup-state}"
RCLONE_DEST="${RCLONE_DEST:-}"                       # e.g. gdrive:km-tracker-backups
LOCAL_RETENTION="${LOCAL_RETENTION:-100}"            # keep newest N local snapshots
DRIVE_RETENTION="${DRIVE_RETENTION:-50}"             # keep newest N on Drive
DRIVE_PUSH_INTERVAL_MIN="${DRIVE_PUSH_INTERVAL_MIN:-15}"  # min minutes between Drive pushes

# State files (checksums + push timestamp) live in STATE_DIR.
LOCAL_CKSUM_FILE="${STATE_DIR}/last_local.sha256"
DRIVE_CKSUM_FILE="${STATE_DIR}/last_drive.sha256"
DRIVE_PUSH_TS_FILE="${STATE_DIR}/last_drive_push.epoch"

# --- Helpers ----------------------------------------------------------------
log() { printf '%s backup.sh: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

# Portable sha256 of a file -> bare hex digest. Ubuntu has sha256sum; fall back
# to shasum (macOS / minimal images) so the script is testable off-box too.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

# --- Preconditions ----------------------------------------------------------
[[ -f "${DB_PATH}" ]] || die "DB not found at DB_PATH=${DB_PATH}"
mkdir -p "${LOCAL_BACKUP_DIR}" "${STATE_DIR}"

# --- Make a consistent snapshot --------------------------------------------
# Use SQLite's online backup API via python3 (stdlib only — no sqlite3 CLI, no
# pip deps). This is safe to run while gunicorn is writing: the backup API copies
# a transactionally consistent image of the DB. We snapshot to a temp file first,
# then decide (by checksum) whether to keep it.
TMP_SNAPSHOT="$(mktemp "${LOCAL_BACKUP_DIR}/.snapshot.XXXXXX.db")"
# Clean up the temp file on any exit (it's renamed into place on the keep path).
cleanup() { rm -f "${TMP_SNAPSHOT}"; }
trap cleanup EXIT

DB_PATH="${DB_PATH}" TMP_SNAPSHOT="${TMP_SNAPSHOT}" python3 - <<'PY'
import os
import sqlite3
import sys

src_path = os.environ["DB_PATH"]
dst_path = os.environ["TMP_SNAPSHOT"]

# Open the live DB read-only via URI so we never accidentally write to it.
src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
try:
    dst = sqlite3.connect(dst_path)
    try:
        # .backup() performs the online backup (consistent, copy-on-write safe).
        src.backup(dst)
    finally:
        dst.close()
finally:
    src.close()

# Sanity check: the snapshot must be a usable SQLite DB.
check = sqlite3.connect(dst_path)
try:
    ok = check.execute("PRAGMA integrity_check").fetchone()[0]
finally:
    check.close()
if ok != "ok":
    sys.stderr.write(f"integrity_check failed: {ok}\n")
    sys.exit(1)
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
# Wrapped so any failure here is reported but does NOT fail the whole run — the
# local snapshot above is already safely on disk.
drive_push() {
  # Use `return 1` (not die's hard exit) so failures unwind back to the caller,
  # which reports them without discarding the already-saved local snapshot.
  [[ -n "${RCLONE_DEST}" ]] || { log "ERROR: RCLONE_DEST is not set (configure it in .env.backup) — skipping Drive push"; return 1; }
  command -v rclone >/dev/null 2>&1 || { log "ERROR: rclone not installed — skipping Drive push"; return 1; }
  [[ -n "${LATEST_LOCAL_SNAPSHOT}" && -f "${LATEST_LOCAL_SNAPSHOT}" ]] || { log "ERROR: no local snapshot available to push"; return 1; }

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

  # Record successful push: timestamp + the checksum now on Drive.
  printf '%s\n' "${now}" > "${DRIVE_PUSH_TS_FILE}"
  printf '%s\n' "${SNAP_CKSUM}" > "${DRIVE_CKSUM_FILE}"
  log "Drive push complete (sha ${SNAP_CKSUM:0:12})"
}

# Run the Drive push in a subshell-tolerant way: report its failure but keep the
# overall exit status clean as long as local snapshots succeeded. We capture the
# failure so the script's exit code reflects "Drive problem" without unwinding
# the (already-completed) local work.
DRIVE_RC=0
drive_push || DRIVE_RC=$?
if (( DRIVE_RC != 0 )); then
  log "Drive push did not complete (rc=${DRIVE_RC}); local snapshots are unaffected"
  exit "${DRIVE_RC}"
fi

log "done"
