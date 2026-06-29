import os
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
PI_MODELS_FILE = Path(os.environ.get("PI_MODELS_FILE", "~/.pi/agent/models.json")).expanduser().resolve()
CRON_USER = os.environ.get("PI_SCHEDULER_CRON_USER", "root")
ADMIN_USERNAME = os.environ.get("PI_SCHEDULER_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("PI_SCHEDULER_PASSWORD", "pi-scheduler")
LOG_RETENTION_DAYS = int(os.environ.get("PI_SCHEDULER_LOG_RETENTION_DAYS", "30"))


def ensure_runtime_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
