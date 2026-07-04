#!/usr/bin/env bash
set -euo pipefail

RUNTIME_USER="pi-scheduler-agent"
RUNTIME_GROUP="pi-scheduler"
SCHEDULER_HOME="/opt/pi-scheduler"
MODELS_FILE="/root/.pi/agent/models.json"

usage() {
  cat <<'USAGE'
Usage: setup-runtime-user.sh [options]

Options:
  --user NAME          Runtime Linux user (default: pi-scheduler-agent)
  --group NAME         Runtime group (default: pi-scheduler)
  --home PATH          Scheduler home (default: /opt/pi-scheduler)
  --models-file PATH   Source models.json (default: /root/.pi/agent/models.json)
  --help               Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      RUNTIME_USER="$2"
      shift 2
      ;;
    --group)
      RUNTIME_GROUP="$2"
      shift 2
      ;;
    --home)
      SCHEDULER_HOME="$2"
      shift 2
      ;;
    --models-file)
      MODELS_FILE="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "setup-runtime-user.sh must run as root. Try: sudo $0" >&2
  exit 1
fi

if ! getent group "${RUNTIME_GROUP}" >/dev/null; then
  groupadd "${RUNTIME_GROUP}"
fi

if ! id -u "${RUNTIME_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${RUNTIME_USER}"
fi

usermod -aG "${RUNTIME_GROUP}" "${RUNTIME_USER}"

for dir in "${SCHEDULER_HOME}/data" "${SCHEDULER_HOME}/logs" "${SCHEDULER_HOME}/locks" "${SCHEDULER_HOME}/tmp"; do
  mkdir -p "${dir}"
done

chgrp -R "${RUNTIME_GROUP}" \
  "${SCHEDULER_HOME}/data" \
  "${SCHEDULER_HOME}/logs" \
  "${SCHEDULER_HOME}/locks" \
  "${SCHEDULER_HOME}/tmp"
chmod -R g+rwX \
  "${SCHEDULER_HOME}/data" \
  "${SCHEDULER_HOME}/logs" \
  "${SCHEDULER_HOME}/locks" \
  "${SCHEDULER_HOME}/tmp"
find \
  "${SCHEDULER_HOME}/data" \
  "${SCHEDULER_HOME}/logs" \
  "${SCHEDULER_HOME}/locks" \
  "${SCHEDULER_HOME}/tmp" \
  -type d -exec chmod g+s {} \;

USER_HOME="$(getent passwd "${RUNTIME_USER}" | cut -d: -f6)"
PI_DIR="${USER_HOME}/.pi"
AGENT_DIR="${PI_DIR}/agent"
mkdir -p "${AGENT_DIR}"
chown "${RUNTIME_USER}:${RUNTIME_USER}" "${PI_DIR}" "${AGENT_DIR}"
chmod 700 "${PI_DIR}" "${AGENT_DIR}"

if [[ -f "${MODELS_FILE}" ]]; then
  cp "${MODELS_FILE}" "${AGENT_DIR}/models.json"
  chown "${RUNTIME_USER}:${RUNTIME_USER}" "${AGENT_DIR}/models.json"
  chmod 600 "${AGENT_DIR}/models.json"
  echo "Copied ${MODELS_FILE} to ${AGENT_DIR}/models.json"
else
  echo "Warning: source models file not found: ${MODELS_FILE}" >&2
  echo "Runtime user was created, but Pi model config was not copied." >&2
fi

echo "Runtime setup complete for ${RUNTIME_USER}."
