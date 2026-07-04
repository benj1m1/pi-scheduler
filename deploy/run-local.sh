#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
SCHEDULER_HOME="${PI_SCHEDULER_HOME:-${REPO_ROOT}}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"
SETUP_SCRIPT="${SCRIPT_DIR}/setup-runtime-user.sh"
# Local deploy prepares the runtime user through deploy/setup-runtime-user.sh.
# It then starts: uvicorn app.main:app

if [[ "${EUID}" -eq 0 ]]; then
  "${SETUP_SCRIPT}" --home "${SCHEDULER_HOME}"
elif command -v sudo >/dev/null 2>&1; then
  sudo "${SETUP_SCRIPT}" --home "${SCHEDULER_HOME}"
else
  echo "Warning: sudo is not available; skipping runtime user setup." >&2
  echo "Run as root: ${SETUP_SCRIPT} --home ${SCHEDULER_HOME}" >&2
fi

export PI_SCHEDULER_HOME="${SCHEDULER_HOME}"
export PI_SCHEDULER_CRON_FILE="${PI_SCHEDULER_CRON_FILE:-${SCHEDULER_HOME}/tmp/pi-agent-jobs}"
export PI_SCHEDULER_CRON_USER="${PI_SCHEDULER_CRON_USER:-pi-scheduler-agent}"
export PI_SCHEDULER_ALLOWED_RUN_USERS="${PI_SCHEDULER_ALLOWED_RUN_USERS:-root,pi-scheduler-agent}"

cd "${SCHEDULER_HOME}"
exec "${SCHEDULER_HOME}/.venv/bin/uvicorn" app.main:app --host "${HOST}" --port "${PORT}"
