#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
SCHEDULER_HOME="${PI_SCHEDULER_HOME:-${REPO_ROOT}}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"
SETUP_SCRIPT="${SCRIPT_DIR}/setup-runtime-user.sh"
# Local deploy prepares the runtime user through deploy/setup-runtime-user.sh.
# It runs the web process as root so Pi Scheduler can keep /etc/cron.d/pi-agent-jobs
# updated on every job/group change while individual jobs run as pi-scheduler-agent.

if [[ "${EUID}" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    echo "Local deploy needs root to write /etc/cron.d/pi-agent-jobs; restarting with sudo." >&2
    exec sudo -E "$0" "$@"
  fi
  echo "Error: local deploy needs root to write /etc/cron.d/pi-agent-jobs." >&2
  echo "Install sudo or run as root: $0" >&2
  exit 1
fi

"${SETUP_SCRIPT}" --home "${SCHEDULER_HOME}"

export PI_SCHEDULER_HOME="${SCHEDULER_HOME}"
export PI_SCHEDULER_CRON_FILE="${PI_SCHEDULER_CRON_FILE:-/etc/cron.d/pi-agent-jobs}"
export PI_SCHEDULER_CRON_USER="${PI_SCHEDULER_CRON_USER:-pi-scheduler-agent}"
export PI_SCHEDULER_ALLOWED_RUN_USERS="${PI_SCHEDULER_ALLOWED_RUN_USERS:-root,pi-scheduler-agent}"

cd "${SCHEDULER_HOME}"
exec "${SCHEDULER_HOME}/.venv/bin/uvicorn" app.main:app --host "${HOST}" --port "${PORT}"
