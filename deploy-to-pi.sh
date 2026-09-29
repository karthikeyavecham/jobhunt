#!/usr/bin/env bash
#
# Deploy JobHunt from the Mac to the Pi. Run on the Mac:
#
#     ./jobhunt/deploy-to-pi.sh
#
# Only one instance may run at a time. Two would mean two schedulers
# double-fetching, two Socket Mode connections splitting button clicks
# unpredictably between them, and two separate databases posting duplicate
# Slack cards. This script stops the local instance before handing over.
#
set -euo pipefail

PI_HOST="${PI_HOST:-raspberrypi.local}"
PI_USER="${PI_USER:-pi}"
PI_KEY="${PI_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_DIR="${REMOTE_DIR:-/home/${PI_USER}/jobhunt}"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH="ssh -o ConnectTimeout=10 -i ${PI_KEY} ${PI_USER}@${PI_HOST}"

echo "==> Deploying ${LOCAL_DIR} -> ${PI_USER}@${PI_HOST}:${REMOTE_DIR}"

# --- 1. Stop the local instance --------------------------------------------
if pgrep -f "jobhunt.app" >/dev/null 2>&1; then
    echo "==> Stopping the local instance (only one may own the schedule)"
    pkill -f "jobhunt.app" || true
    sleep 2
fi

# --- 2. Pull the newest Pi backup to the Mac --------------------------------
# The Pi's SD card is the least durable storage in the house, and its database
# is the only copy of the tracker. Every deploy therefore fetches the newest
# nightly backup (or takes a fresh one if none exists yet), so the Mac always
# holds a recent off-Pi copy.
# This step must never block a deploy: with `set -o pipefail` a failing
# remote command inside a pipeline would abort the whole script — which is
# exactly what happened on the first run, when maintenance.py did not exist
# on the Pi yet. Hence the `|| true` inside the substitution and the path
# sanity-check before scp.
if $SSH "test -f ${REMOTE_DIR}/jobhunt.db"; then
    echo "==> Pulling a database backup from the Pi"
    NEWEST="$($SSH "cd ${REMOTE_DIR%/*} && ${REMOTE_DIR}/venv/bin/python -c '
from jobhunt import maintenance
print(maintenance.backup_database())' 2>/dev/null" | tail -1 || true)"
    case "${NEWEST}" in
        /*)
            mkdir -p "${LOCAL_DIR}/backups-from-pi"
            if scp -q -i "${PI_KEY}" "${PI_USER}@${PI_HOST}:${NEWEST}" \
                    "${LOCAL_DIR}/backups-from-pi/"; then
                echo "    saved $(basename "${NEWEST}") to backups-from-pi/"
            fi
            # Keep the five newest local copies.
            ls -1t "${LOCAL_DIR}/backups-from-pi/"jobhunt-*.db 2>/dev/null \
                | tail -n +6 | xargs rm -f 2>/dev/null || true
            ;;
        *)
            echo "    could not take a backup — continuing (deploy is not blocked)"
            ;;
    esac
fi

# --- 3. Sync code -----------------------------------------------------------
# The venv is architecture-specific and rebuilt on the Pi. The database,
# generated documents and backups are state, not code, and never synced.
echo "==> Syncing code"
$SSH "mkdir -p ${REMOTE_DIR}"
rsync -az --delete \
    --exclude 'venv/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude 'jobhunt.db*' \
    --exclude 'jobhunt.env' \
    --exclude 'documents/' \
    --exclude 'backups/' \
    --exclude 'backups-from-pi/' \
    --exclude '.DS_Store' \
    -e "ssh -o ConnectTimeout=10 -i ${PI_KEY}" \
    "${LOCAL_DIR}/" "${PI_USER}@${PI_HOST}:${REMOTE_DIR}/"

# --- 4. Database ------------------------------------------------------------
# Copied only on the first deploy. It carries the scores, the tracker, the
# secrets and — critically — notified_at, so the Pi does not re-announce jobs
# that already have Slack cards. Overwriting it later would lose whatever the
# Pi has recorded since, so that needs an explicit flag.
if $SSH "test -f ${REMOTE_DIR}/jobhunt.db"; then
    if [ "${1:-}" = "--overwrite-db" ]; then
        echo "==> Overwriting the remote database (--overwrite-db)"
        $SSH "cp ${REMOTE_DIR}/jobhunt.db ${REMOTE_DIR}/jobhunt.db.bak-\$(date +%s)"
        scp -q -i "${PI_KEY}" "${LOCAL_DIR}/jobhunt.db" \
            "${PI_USER}@${PI_HOST}:${REMOTE_DIR}/jobhunt.db"
    else
        echo "==> Remote database exists — left untouched"
        echo "    (pass --overwrite-db to replace it; a backup is taken first)"
    fi
else
    echo "==> First deploy: copying the database"
    # WAL and shared-memory sidecars must not be copied mid-write; checkpoint
    # first so everything is in the main file.
    sqlite3 "${LOCAL_DIR}/jobhunt.db" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null
    scp -q -i "${PI_KEY}" "${LOCAL_DIR}/jobhunt.db" \
        "${PI_USER}@${PI_HOST}:${REMOTE_DIR}/jobhunt.db"
fi

# --- 5. Generated documents -------------------------------------------------
if [ -d "${LOCAL_DIR}/documents" ] && [ -n "$(ls -A "${LOCAL_DIR}/documents" 2>/dev/null)" ]; then
    echo "==> Syncing generated documents"
    rsync -az -e "ssh -o ConnectTimeout=10 -i ${PI_KEY}" \
        "${LOCAL_DIR}/documents/" "${PI_USER}@${PI_HOST}:${REMOTE_DIR}/documents/"
fi

# --- 6. Install and start ---------------------------------------------------
echo "==> Running the installer on the Pi"
$SSH "cd ${REMOTE_DIR} && chmod +x install-jobhunt.sh && ./install-jobhunt.sh"

echo
echo "Deployed. http://${PI_HOST}:5002"
echo "  logs: ssh -i ${PI_KEY} ${PI_USER}@${PI_HOST} 'sudo journalctl -u jobhunt -f'"
