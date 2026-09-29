#!/usr/bin/env bash
#
# Install JobHunt as a systemd service. Run ON the Pi, from the app directory:
#
#     cd ~/jobhunt && ./install-jobhunt.sh
#
# Idempotent: safe to re-run after deploying new code. It rebuilds the venv
# dependencies, refreshes the unit file and restarts the service, but never
# touches jobhunt.db or jobhunt.env.
#
set -euo pipefail

APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENTDIR="$(dirname "${APPDIR}")"
RUNUSER="$(id -un)"
SERVICE=jobhunt.service
UNIT_PATH="/etc/systemd/system/${SERVICE}"

echo "==> JobHunt install"
echo "    directory : ${APPDIR}"
echo "    user      : ${RUNUSER}"

# --- 1. System packages -----------------------------------------------------
# LibreOffice converts .docx to PDF; the msttcorefonts package supplies Arial,
# without which LibreOffice silently substitutes Liberation Sans and the
# generated PDFs are not in the font the CV style guide specifies.
echo "==> Checking system packages"
MISSING=()
command -v soffice >/dev/null 2>&1 || MISSING+=(libreoffice-writer)
fc-list 2>/dev/null | grep -qi arial || MISSING+=(ttf-mscorefonts-installer)

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "    installing: ${MISSING[*]}"
    echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" \
        | sudo debconf-set-selections
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${MISSING[@]}"
    sudo fc-cache -f >/dev/null 2>&1 || true
else
    echo "    LibreOffice and Arial already present"
fi

# --- 2. Python environment --------------------------------------------------
echo "==> Python environment"
if [ ! -d "${APPDIR}/venv" ]; then
    python3 -m venv "${APPDIR}/venv"
fi
"${APPDIR}/venv/bin/pip" install -q --upgrade pip
"${APPDIR}/venv/bin/pip" install -q -r "${APPDIR}/requirements.txt"
echo "    $("${APPDIR}/venv/bin/python" -V) with dependencies installed"

# Package versions are pinned, but the interpreter is not — and tests only
# mean something if they run on the version production uses. Warn rather than
# fail: a working Pi should not be bricked by a distro upgrade.
PYMINOR="$("${APPDIR}/venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [ "${PYMINOR}" != "3.13" ]; then
    echo "    WARNING: this host runs Python ${PYMINOR}; the Mac tests against 3.13."
    echo "             Match them, or a bug can reach the Pi untested."
fi

# --- 3. Environment file ----------------------------------------------------
# Holds the shared password required before the app will bind to the LAN, and
# optionally an Anthropic key. Kept out of the database and out of git.
ENVFILE="${APPDIR}/jobhunt.env"
if [ ! -f "${ENVFILE}" ]; then
    echo "==> Creating ${ENVFILE}"
    PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 24)"
    cat > "${ENVFILE}" <<ENVEOF
# JobHunt service environment. Not in git; readable only by the owner.
# Required before the app will bind to anything but localhost.
JOBHUNT_PASSWORD=${PASSWORD}
# Signing key for session cookies. Change to invalidate all sessions.
JOBHUNT_SECRET_KEY=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)
# This host owns the schedule and the Slack connection. Exactly one machine
# may carry this: a second one would double-fetch and split button clicks.
JOBHUNT_ROLE=production
# Optional. An env var here always wins over a key stored in the database.
# ANTHROPIC_API_KEY=
ENVEOF
    chmod 600 "${ENVFILE}"
    echo "    generated a web password — see ${ENVFILE}"
else
    echo "==> Keeping existing ${ENVFILE}"
fi

# The role gate defaults to off, so an env file written before it existed would
# leave the Pi serving pages and fetching nothing. Add it if it is missing.
if ! grep -q '^JOBHUNT_ROLE=' "${ENVFILE}"; then
    echo "==> Marking this host as the production instance"
    printf '\n# Added by install: this host owns the schedule and Slack.\nJOBHUNT_ROLE=production\n' >> "${ENVFILE}"
fi

# --- 4. Permissions ---------------------------------------------------------
# The database holds Slack tokens, an API key and personal data.
[ -f "${APPDIR}/jobhunt.db" ] && chmod 600 "${APPDIR}/jobhunt.db"
mkdir -p "${APPDIR}/documents" "${APPDIR}/backups"

# --- 5. systemd unit --------------------------------------------------------
echo "==> Installing ${SERVICE}"
sed -e "s|__USER__|${RUNUSER}|g" -e "s|__APPDIR__|${APPDIR}|g" \
    -e "s|__PARENTDIR__|${PARENTDIR}|g" \
    "${APPDIR}/jobhunt.service" | sudo tee "${UNIT_PATH}" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE}" >/dev/null 2>&1
sudo systemctl restart "${SERVICE}"

# --- 6. Verify --------------------------------------------------------------
echo "==> Waiting for startup"
sleep 8
if systemctl is-active --quiet "${SERVICE}"; then
    echo "    service is active"
else
    echo "    SERVICE FAILED TO START — recent log:"
    sudo journalctl -u "${SERVICE}" -n 25 --no-pager
    exit 1
fi

CODE="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "http://127.0.0.1:5002/" || echo 000)"
echo "    HTTP ${CODE} from http://127.0.0.1:5002/"

echo
echo "Done. JobHunt is at http://$(hostname).local:5002"
echo
echo "  status : sudo systemctl status ${SERVICE}"
echo "  logs   : sudo journalctl -u ${SERVICE} -f"
echo "  stop   : sudo systemctl stop ${SERVICE}"
