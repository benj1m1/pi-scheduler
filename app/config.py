import os
import shutil
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_HOME = Path(os.environ.get("PI_SCHEDULER_HOME", APP_ROOT)).resolve()
DATA_DIR = Path(os.environ.get("PI_SCHEDULER_DATA_DIR", SCHEDULER_HOME / "data")).resolve()
LOG_DIR = Path(os.environ.get("PI_SCHEDULER_LOG_DIR", SCHEDULER_HOME / "logs")).resolve()
LOCK_DIR = Path(os.environ.get("PI_SCHEDULER_LOCK_DIR", SCHEDULER_HOME / "locks")).resolve()
DB_PATH = Path(os.environ.get("PI_SCHEDULER_DB", DATA_DIR / "pi-scheduler.sqlite3")).resolve()
CRON_FILE = Path(os.environ.get("PI_SCHEDULER_CRON_FILE", "/etc/cron.d/pi-agent-jobs")).resolve()
RUNNER_PATH = os.environ.get("PI_SCHEDULER_RUNNER", str(SCHEDULER_HOME / "bin" / "pi-job-runner"))
PI_BINARY = os.environ.get("PI_BINARY", "pi")
PI_NODE_ROOT = Path(os.environ.get("PI_NODE_ROOT", "~/.local/share/pi-node")).expanduser().resolve()
DEFAULT_CRON_PATH = os.environ.get("PI_SCHEDULER_CRON_PATH", "/usr/local/bin:/usr/bin:/bin")
PI_MODELS_FILE = Path(os.environ.get("PI_MODELS_FILE", "~/.pi/agent/models.json")).expanduser().resolve()
CRON_USER = os.environ.get("PI_SCHEDULER_CRON_USER", "root")
ALLOWED_RUN_USERS = os.environ.get("PI_SCHEDULER_ALLOWED_RUN_USERS", "")
RUNTIME_USER = os.environ.get("PI_SCHEDULER_RUNTIME_USER", "pi-scheduler-agent")
RUNTIME_GROUP = os.environ.get("PI_SCHEDULER_RUNTIME_GROUP", "pi-scheduler")
MODELS_SOURCE_FILE = Path(os.environ.get("PI_SCHEDULER_MODELS_SOURCE", "/root/.pi/agent/models.json")).expanduser().resolve()
ADMIN_USERNAME = os.environ.get("PI_SCHEDULER_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("PI_SCHEDULER_PASSWORD", "pi-scheduler")
LOG_RETENTION_DAYS = int(os.environ.get("PI_SCHEDULER_LOG_RETENTION_DAYS", "30"))


def pi_binary_dirs() -> list[str]:
    dirs: list[str] = []
    binary_path = Path(PI_BINARY)
    if binary_path.is_absolute():
        dirs.append(str(binary_path.parent))

    for candidate in sorted(PI_NODE_ROOT.glob("node-*/bin/pi"), reverse=True):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            dirs.append(str(candidate.parent.resolve()))

    found = shutil.which("pi")
    if found:
        dirs.append(str(Path(found).parent.resolve()))

    seen: set[str] = set()
    unique_dirs: list[str] = []
    for directory in dirs:
        if directory not in seen:
            seen.add(directory)
            unique_dirs.append(directory)
    return unique_dirs


def cron_path() -> str:
    parts = pi_binary_dirs() + [part for part in DEFAULT_CRON_PATH.split(":") if part]
    seen: set[str] = set()
    unique_parts: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            unique_parts.append(part)
    return ":".join(unique_parts)


def ensure_runtime_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
