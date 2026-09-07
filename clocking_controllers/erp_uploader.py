"""
erp_uploader.py - uploads gateway.py's recordList CSVs to one or more
Frappe instances as Clocking Import documents.

Deliberately separate from gateway.py, which keeps doing exactly what it
already does (capture from the HIKVision terminals, write CSVs) and knows
nothing about Frappe. This script only *reads* gateway.py's CSV_DIR - it
never moves, renames, or edits a CSV. Safe to point at the same folder
gateway.py is actively writing to.

Config (erp_uploader.json, next to this script):

    {
      "instances": [
        {
          "name": "eben",
          "base_url": "https://eben.isambane.co.za",
          "api_key": "...",
          "api_secret": "...",
          "enabled": true
        },
        {
          "name": "jorrie",
          "base_url": "https://jorrie.isambane.co.za",
          "api_key": "",
          "api_secret": "",
          "enabled": false
        }
      ]
    }

Each instance is independent: its own base URL/credentials, its own
enabled flag, its own upload state (erp_uploader_state.json, keyed by
instance name), and its own log file (logs/<name>.log). Disabled or
credential-less instances are skipped every cycle.

Why per-instance state, not a single global one: this is what makes "add
credentials for isambane later and it catches up automatically" work with
no special-casing. A brand-new instance name has no entry in the state
file at all, so on its first enabled cycle *every* existing CSV looks new
to it and gets uploaded - full history, automatically. An instance that's
been running a while only re-uploads files whose size has changed since
its own last successful upload of that file.

Idempotency at the file level (same file uploaded to the same instance
repeatedly as it grows through the day) relies entirely on the ERP side's
own dedup (employee+time+isa_clocking_machine, in
is_attendance.controllers.clocking_import.create_checkins) - this script
does not need to know or care which specific rows were already imported,
only whether the *file* has grown since it last sent it.

Whether a freshly-uploaded document imports immediately or waits for a
person: every upload creates the document and lets the ERP's own
validate() parse it. If every employee code in the file already resolves,
its status is already "Pending Import" and this script queues it right
away. If any code is unresolved, it lands in "Missing Information" and is
left alone - nothing here submits it. Once a person resolves the Issues in
Desk (which flips status to "Pending Import" via that same validate()),
the next cycle's retry_pending_imports() finds it and queues it - so
mapping happens once, by a human, in the UI, and everything after that is
automatic.
"""

from __future__ import annotations

import json
import logging
import time

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(r"C:\HikGateway\ErpUploader")
LOG_DIR = BASE_DIR / "logs"

CONFIG_FILE = BASE_DIR / "erp_uploader.json"
STATE_FILE = BASE_DIR / "erp_uploader_state.json"
MAIN_LOG_FILE = BASE_DIR / "erp_uploader.log"

# Read-only - gateway.py's own working directory. This script never
# writes here.
SOURCE_CSV_DIR = Path(r"C:\HikGateway\records")

UPLOAD_CYCLE_INTERVAL_SECONDS = 300

SAST = timezone(timedelta(hours=2))


# ============================================================
# INITIAL SETUP
# ============================================================

BASE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(MAIN_LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

_instance_loggers: dict[str, logging.Logger] = {}


def get_instance_logger(instance_name: str) -> logging.Logger:
    """One dedicated log file per instance - so eben's, jorrie's, and
    (later) isambane's upload history can each be reviewed on their own,
    without the others' noise. Also mirrors every line to the shared
    console/main log."""
    logger = _instance_loggers.get(instance_name)
    if logger is not None:
        return logger

    logger = logging.getLogger(f"erp_uploader.{instance_name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    file_handler = logging.FileHandler(
        LOG_DIR / f"{instance_name}.log",
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter(f"%(asctime)s [{instance_name}] %(levelname)s %(message)s")
    )
    logger.addHandler(console_handler)

    _instance_loggers[instance_name] = logger
    return logger


# ============================================================
# CONFIG / STATE
# ============================================================

def load_instances() -> list[dict[str, Any]]:
    if not CONFIG_FILE.exists():
        raise RuntimeError(
            f"Missing config file: {CONFIG_FILE}. See this script's module "
            "docstring for the expected shape."
        )

    with CONFIG_FILE.open("r", encoding="utf-8") as file:
        raw_config = json.load(file)

    instances = raw_config.get("instances")
    if not isinstance(instances, list) or not instances:
        raise RuntimeError(f"{CONFIG_FILE} has no 'instances' configured.")

    return instances


def load_state() -> dict[str, dict[str, int]]:
    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        logging.exception(
            "Failed to load %s - starting with empty state.",
            STATE_FILE,
        )
        return {}


def save_state(state: dict[str, dict[str, int]]) -> None:
    temp_file = STATE_FILE.with_suffix(".tmp")

    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, sort_keys=True)

    temp_file.replace(STATE_FILE)


# ============================================================
# ERP CALLS
# ============================================================

def auth_headers(instance: dict[str, Any]) -> dict[str, str]:
    return {
        "Authorization": f"token {instance['api_key']}:{instance['api_secret']}"
    }


def upload_csv(
    instance: dict[str, Any],
    csv_path: Path,
    logger: logging.Logger,
) -> str | None:
    """Upload one CSV as a fresh Clocking Import on `instance`.

    Returns the created document's name on success, or None on failure.
    Never modifies csv_path - opened read-only.
    """
    base_url = instance["base_url"]
    timestamp = datetime.now(SAST).strftime("%Y%m%dT%H%M%S")
    upload_name = f"{csv_path.stem}_{instance['name']}_{timestamp}{csv_path.suffix}"

    try:
        with csv_path.open("rb") as file_handle:
            upload_response = requests.post(
                f"{base_url}/api/method/upload_file",
                headers=auth_headers(instance),
                data={"is_private": "1"},
                files={"file": (upload_name, file_handle, "text/csv")},
                timeout=60,
            )
        upload_response.raise_for_status()
        file_url = upload_response.json()["message"]["file_url"]

        create_response = requests.post(
            f"{base_url}/api/resource/Clocking Import",
            headers={**auth_headers(instance), "Content-Type": "application/json"},
            json={"file": file_url},
            timeout=30,
        )
        create_response.raise_for_status()
        doc = create_response.json()["data"]
        import_name = doc["name"]
        status = doc.get("status")

        logger.info(
            "UPLOAD OK: file=%s -> %s (status=%s)",
            csv_path.name,
            import_name,
            status,
        )

        if status == "Pending Import":
            queue_import(instance, import_name, logger)
        elif status == "Missing Information":
            logger.warning(
                "%s needs manual employee mapping before it can import - "
                "resolve its Issues in Desk. It will be queued "
                "automatically on a later cycle once that's done.",
                import_name,
            )

        return import_name

    except requests.RequestException:
        logger.exception("Upload failed for %s", csv_path)
        return None

    except (KeyError, ValueError):
        logger.exception("Unexpected response uploading %s", csv_path)
        return None


def queue_import(
    instance: dict[str, Any],
    import_name: str,
    logger: logging.Logger,
) -> bool:
    base_url = instance["base_url"]

    try:
        response = requests.post(
            f"{base_url}/api/method/run_doc_method",
            headers={**auth_headers(instance), "Content-Type": "application/json"},
            json={
                "dt": "Clocking Import",
                "dn": import_name,
                "method": "queue_import",
            },
            timeout=30,
        )
        response.raise_for_status()
        logger.info("QUEUE OK: %s", import_name)
        return True

    except requests.RequestException:
        logger.exception("queue_import failed for %s", import_name)
        return False


def retry_pending_imports(instance: dict[str, Any], logger: logging.Logger) -> None:
    """
    Finds documents already sitting at status="Pending Import",
    docstatus=0 - either freshly uploaded this cycle and already handled
    by upload_csv() above, or ones that started as "Missing Information"
    on an earlier cycle and have since been manually mapped by a person in
    Desk (their save() flips status via the ERP's own validate()). This is
    what actually finishes an import once mapping is done - a human never
    has to click "Start Import" themselves.
    """
    base_url = instance["base_url"]

    try:
        response = requests.get(
            f"{base_url}/api/resource/Clocking Import",
            headers=auth_headers(instance),
            params={
                "filters": json.dumps(
                    [["status", "=", "Pending Import"], ["docstatus", "=", 0]]
                ),
                "fields": json.dumps(["name"]),
                "limit_page_length": 0,
            },
            timeout=30,
        )
        response.raise_for_status()
        pending_names = [row["name"] for row in response.json().get("data", [])]

    except requests.RequestException:
        logger.exception("Failed to check for Pending Import documents.")
        return

    for import_name in pending_names:
        queue_import(instance, import_name, logger)


# ============================================================
# UPLOAD CYCLE
# ============================================================

def upload_cycle_for_instance(
    instance: dict[str, Any],
    state: dict[str, dict[str, int]],
) -> None:
    logger = get_instance_logger(instance["name"])
    instance_state = state.setdefault(instance["name"], {})

    for csv_path in sorted(SOURCE_CSV_DIR.glob("recordList_*.csv")):
        size = csv_path.stat().st_size

        if instance_state.get(csv_path.name) == size:
            # Nothing new for this instance since its own last upload of
            # this file - a brand-new instance has no entry here at all,
            # so every existing file looks new to it the first time it
            # runs, which is exactly the "catch up on full history when
            # credentials are added" behaviour.
            continue

        if upload_csv(instance, csv_path, logger):
            instance_state[csv_path.name] = size

    retry_pending_imports(instance, logger)


def run_upload_cycle() -> None:
    instances = load_instances()
    state = load_state()

    processed_count = 0

    for instance in instances:
        name = instance.get("name")

        if not instance.get("enabled"):
            logging.info("Instance '%s' is disabled - skipping.", name)
            continue

        if not instance.get("api_key") or not instance.get("api_secret"):
            logging.warning(
                "Instance '%s' is enabled but has no api_key/api_secret set - skipping.",
                name,
            )
            continue

        if not SOURCE_CSV_DIR.exists():
            logging.warning(
                "Source CSV folder missing: %s - skipping this cycle.",
                SOURCE_CSV_DIR,
            )
            return

        upload_cycle_for_instance(instance, state)
        processed_count += 1

    save_state(state)

    logging.info(
        "Cycle finished: %s/%s instance(s) processed.",
        processed_count,
        len(instances),
    )


def main() -> None:
    logging.info(
        "erp_uploader started. Interval=%s seconds source=%s",
        UPLOAD_CYCLE_INTERVAL_SECONDS,
        SOURCE_CSV_DIR,
    )

    while True:
        try:
            run_upload_cycle()
        except Exception:
            logging.exception("Unhandled upload-cycle exception.")

        time.sleep(UPLOAD_CYCLE_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("erp_uploader stopped.")
