from __future__ import annotations

import csv
import json
import logging
import threading
import time
import uuid

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from flask import Flask, Response, request
from requests.auth import HTTPDigestAuth


# ============================================================
# HIKGATEWAY CONFIGURATION
# ============================================================

BASE_DIR = Path(r"C:\HikGateway")
CSV_DIR = BASE_DIR / "records"

LOG_FILE = BASE_DIR / "gateway.log"
STATE_FILE = BASE_DIR / "state.json"

# device ip -> ISO timestamp of the end of that device's last successful
# history poll. Separate file from STATE_FILE (employee IN/OUT state) -
# different shape, different lifecycle, no reason to entangle them.
POLL_STATE_FILE = BASE_DIR / "poll_state.json"

# Device IPs/labels/credentials live outside the script now, in
# gateway.json next to it - see load_gateway_config() below for the exact
# shape. Keeps terminal admin passwords out of source control and lets
# them be updated without redeploying code.
CONFIG_FILE = BASE_DIR / "gateway.json"

SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8080

# Query every five minutes for clockings missed by live HTTP upload.
POLL_INTERVAL_SECONDS = 300

# Fallback window used only when a device has no recorded successful poll
# yet (first run, or poll_state.json was lost/reset). Existing CSV
# duplicate detection prevents records being written twice. Once a device
# has polled successfully at least once, POLL_OVERLAP_MINUTES below is
# used instead - re-requesting this whole 30-day/thousands-of-events
# window every 5 minutes forever is what was pushing terminals into the
# mid-pagination 401s this file used to hit constantly.
HISTORY_LOOKBACK_DAYS = 30

# Once a device has a recorded last-successful-poll time, later polls only
# ask for events since (that time minus this overlap) rather than the full
# HISTORY_LOOKBACK_DAYS window - the overlap is just a safety margin for
# clock skew/late-arriving events; duplicate detection makes re-covering
# it harmless.
POLL_OVERLAP_MINUTES = 15

# A 401 that shows up after earlier pages on the same paginated history
# search already succeeded is the terminal's own digest auth session
# going stale under sustained pagination - not a bad password. Retried
# with a fresh session, resuming at the same position (not restarting the
# search), since a fixed per-session page limit would otherwise recur at
# the exact same position every time and the fetch would never progress
# past it.
MAX_PAGE_AUTH_RETRIES = 5
PAGE_AUTH_RETRY_DELAY_SECONDS = 5

# An OUT may occur on the following calendar day for night-shift staff.
# After this many hours, an unmatched IN is treated as a missed clock-out
# and the next clocking starts a new shift as IN.
MAX_SHIFT_HOURS = 18

# South Africa Standard Time: UTC+2.
SAST = timezone(timedelta(hours=2))

ATTENDANCE_STATUS_DEFAULT = "undefined"

# Known verified-attendance subtypes used by the terminals.
VALID_CLOCKING_SUBTYPES = {
    "38",
    "75",
    "153",
}

# Populated by load_gateway_config() at startup, from gateway.json - see
# that function for the exact file shape. Kept as module-level names
# (rather than threading a config object through every function) since
# that's how every function in this file already refers to them.
DEVICES: list[dict[str, Any]] = []
DEVICE_LABELS: dict[str, str] = {}

CSV_FIELDS = [
    "sName",
    "sJobNo",
    "sCard",
    "Date",
    "Time",
    "IN/OUT",
    "ReadID",
    "EventMainCode",
    "EventSubCode",
    "AttendanceStatus",
    "WearMask",
    "SerialNo",
    "ClockStation",
]


# ============================================================
# INITIAL SETUP
# ============================================================

BASE_DIR.mkdir(parents=True, exist_ok=True)
CSV_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(
            LOG_FILE,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)

app = Flask(__name__)

data_lock = threading.RLock()
stop_event = threading.Event()

# employee number -> {"status": "IN" or "OUT", "date": "YYYY-MM-DD"}
employee_state: dict[str, dict[str, str]] = {}

# Duplicate-identification keys already written to CSV.
processed_keys: set[str] = set()


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_value(value: Any) -> str:
    if value is None:
        return ""

    return str(value).strip()


def first_value(
    source: dict[str, Any],
    *field_names: str,
) -> Any:
    for field_name in field_names:
        value = source.get(field_name)

        if value is not None and clean_value(value):
            return value

    return None


def normalize_mask(mask_value: Any) -> str:
    value = clean_value(mask_value).lower()

    if value in {
        "1",
        "yes",
        "true",
        "mask",
        "withmask",
        "with mask",
    }:
        return "yes"

    return "no"


def parse_hik_datetime(value: str) -> datetime | None:
    value = clean_value(value)

    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SAST)

        return parsed.astimezone(SAST)

    except ValueError:
        logging.error(
            "Unable to parse Hikvision datetime: %r",
            value,
        )
        return None


def get_csv_path(event_datetime: datetime) -> Path:
    iso_date = event_datetime.strftime("%Y-%m-%d")

    return CSV_DIR / f"recordList_{iso_date}.csv"


def station_for_ip(
    device_ip: str,
    device_name: Any = None,
) -> str:
    # Friendly mapping is preferred so the station remains predictable.
    return (
        DEVICE_LABELS.get(device_ip)
        or clean_value(device_name)
        or device_ip
        or "Unknown"
    )


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_employee_state() -> dict[str, dict[str, str]]:
    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            loaded = json.load(file)

        if not isinstance(loaded, dict):
            return {}

        result: dict[str, dict[str, str]] = {}

        for employee_no, saved_state in loaded.items():
            employee_key = clean_value(employee_no)

            if not employee_key:
                continue

            # Current shift-aware format:
            # {"123": {"status": "IN", "date": "2026-09-02",
            #          "timestamp": "2026-09-02T18:00:00+02:00"}}
            if isinstance(saved_state, dict):
                status = clean_value(
                    saved_state.get("status")
                ).upper()
                state_date = clean_value(
                    saved_state.get("date")
                )
                state_timestamp = clean_value(
                    saved_state.get("timestamp")
                )

                if status not in {"IN", "OUT"}:
                    continue

                try:
                    datetime.strptime(
                        state_date,
                        "%Y-%m-%d",
                    )
                except ValueError:
                    continue

                state_record = {
                    "status": status,
                    "date": state_date,
                }

                # The previous date-aware version did not have a complete
                # timestamp. Retain it for safe same-day alternation; the
                # next written clocking upgrades it automatically.
                if (
                    state_timestamp
                    and parse_hik_datetime(state_timestamp)
                    is not None
                ):
                    state_record["timestamp"] = state_timestamp

                result[employee_key] = state_record
                continue

            # Backward compatibility with the old format:
            # {"123": "IN"}
            # No date can safely be inferred, so the employee's next
            # clocking starts a new day as IN.
            status = clean_value(saved_state).upper()

            if status in {"IN", "OUT"}:
                logging.info(
                    "Legacy state found for employee=%s; "
                    "next clocking will start as IN.",
                    employee_key,
                )

        return result

    except Exception:
        logging.exception(
            "Failed to load employee state file."
        )
        return {}


def save_employee_state() -> None:
    temporary_file = STATE_FILE.with_suffix(".tmp")

    try:
        with temporary_file.open(
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                employee_state,
                file,
                indent=2,
                sort_keys=True,
            )

        temporary_file.replace(STATE_FILE)

    except Exception:
        logging.exception(
            "Failed to save employee state."
        )


def load_poll_state() -> dict[str, str]:
    if not POLL_STATE_FILE.exists():
        return {}

    try:
        with POLL_STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            loaded = json.load(file)

        return loaded if isinstance(loaded, dict) else {}

    except Exception:
        logging.exception(
            "Failed to load poll state file."
        )
        return {}


def save_poll_state(poll_state: dict[str, str]) -> None:
    temporary_file = POLL_STATE_FILE.with_suffix(".tmp")

    try:
        with temporary_file.open(
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                poll_state,
                file,
                indent=2,
                sort_keys=True,
            )

        temporary_file.replace(POLL_STATE_FILE)

    except Exception:
        logging.exception(
            "Failed to save poll state."
        )


def next_inout(
    employee_no: str,
    event_datetime: datetime,
) -> str:
    event_datetime = event_datetime.astimezone(SAST)
    event_date = event_datetime.strftime("%Y-%m-%d")
    event_timestamp = event_datetime.isoformat(
        timespec="seconds"
    )
    previous = employee_state.get(employee_no)
    previous_status = (
        clean_value(previous.get("status")).upper()
        if previous
        else ""
    )
    previous_datetime = (
        parse_hik_datetime(
            clean_value(previous.get("timestamp"))
        )
        if previous
        else None
    )

    within_shift_window = False

    if previous_status == "IN" and previous_datetime:
        elapsed = event_datetime - previous_datetime
        within_shift_window = (
            timedelta(0)
            <= elapsed
            <= timedelta(hours=MAX_SHIFT_HOURS)
        )

    # Backward compatibility with the previous date-aware state format,
    # which did not store a full timestamp.
    legacy_same_day_in = (
        previous_status == "IN"
        and previous_datetime is None
        and previous
        and previous.get("date") == event_date
    )

    if within_shift_window or legacy_same_day_in:
        current = "OUT"
    else:
        current = "IN"

    employee_state[employee_no] = {
        "status": current,
        "date": event_date,
        "timestamp": event_timestamp,
    }
    save_employee_state()

    return current


# ============================================================
# DUPLICATE CONTROL
# ============================================================

def build_serial_key(
    device_ip: str,
    serial_no: Any,
) -> str | None:
    serial = clean_value(serial_no)

    if not serial:
        return None

    return (
        f"SERIAL|{clean_value(device_ip)}|{serial}"
    )


def build_fallback_key(
    device_ip: str,
    employee_no: Any,
    event_datetime: datetime,
    subtype: Any,
) -> str:
    return "|".join(
        [
            "EVENT",
            clean_value(device_ip),
            clean_value(employee_no),
            event_datetime.strftime(
                "%Y-%m-%dT%H:%M:%S"
            ),
            clean_value(subtype),
        ]
    )


def build_event_keys(
    device_ip: str,
    employee_no: Any,
    event_datetime: datetime,
    subtype: Any,
    serial_no: Any,
) -> set[str]:
    keys = {
        build_fallback_key(
            device_ip=device_ip,
            employee_no=employee_no,
            event_datetime=event_datetime,
            subtype=subtype,
        )
    }

    serial_key = build_serial_key(
        device_ip,
        serial_no,
    )

    if serial_key:
        keys.add(serial_key)

    return keys


def load_existing_csv_keys() -> None:
    processed_keys.clear()

    for csv_path in CSV_DIR.glob("recordList_*.csv"):
        try:
            with csv_path.open(
                "r",
                newline="",
                encoding="utf-8-sig",
            ) as file:
                reader = csv.DictReader(file)

                for row in reader:
                    station = clean_value(
                        row.get("ClockStation")
                    )

                    # Translate friendly station label back to an IP
                    # where possible.
                    device_ip = ""

                    for ip_address, label in DEVICE_LABELS.items():
                        if station in {
                            ip_address,
                            label,
                        }:
                            device_ip = ip_address
                            break

                    if not device_ip:
                        device_ip = station

                    employee_no = clean_value(
                        row.get("sJobNo")
                    )

                    serial_no = clean_value(
                        row.get("SerialNo")
                    )

                    subtype = clean_value(
                        row.get("EventSubCode")
                    )

                    date_value = clean_value(
                        row.get("Date")
                    )

                    time_value = clean_value(
                        row.get("Time")
                    )

                    try:
                        event_datetime = datetime.strptime(
                            f"{date_value} {time_value}",
                            "%d/%m/%Y %H:%M:%S",
                        ).replace(tzinfo=SAST)

                    except ValueError:
                        continue

                    keys = build_event_keys(
                        device_ip=device_ip,
                        employee_no=employee_no,
                        event_datetime=event_datetime,
                        subtype=subtype,
                        serial_no=serial_no,
                    )

                    processed_keys.update(keys)

        except Exception:
            logging.exception(
                "Failed to read existing CSV: %s",
                csv_path,
            )


# ============================================================
# CSV WRITING
# ============================================================

def write_clocking(
    *,
    device_ip: str,
    employee_no: Any,
    person_name: Any,
    event_datetime: datetime,
    major_type: Any,
    sub_type: Any,
    serial_no: Any,
    card_no: Any = None,
    card_reader: Any = None,
    mask_value: Any = None,
    attendance_status: Any = None,
    device_name: Any = None,
    source: str,
) -> bool:
    employee_string = clean_value(employee_no)

    if not employee_string:
        logging.info(
            "Skipping event without an employee number: "
            "device=%s source=%s serial=%s",
            device_ip,
            source,
            serial_no,
        )
        return False

    event_datetime = event_datetime.astimezone(SAST)

    event_keys = build_event_keys(
        device_ip=device_ip,
        employee_no=employee_string,
        event_datetime=event_datetime,
        subtype=sub_type,
        serial_no=serial_no,
    )

    with data_lock:
        if any(
            key in processed_keys
            for key in event_keys
        ):
            logging.info(
                "Duplicate skipped: device=%s employee=%s "
                "time=%s serial=%s source=%s",
                device_ip,
                employee_string,
                event_datetime.isoformat(),
                serial_no,
                source,
            )
            return False

        inout_value = next_inout(
            employee_string,
            event_datetime,
        )

        row = {
            "sName": clean_value(person_name),
            "sJobNo": employee_string,
            "sCard": (
                clean_value(card_no)
                or employee_string
            ),
            "Date": event_datetime.strftime(
                "%d/%m/%Y"
            ),
            "Time": event_datetime.strftime(
                "%H:%M:%S"
            ),
            "IN/OUT": inout_value,
            "ReadID": clean_value(card_reader) or "1",
            "EventMainCode": clean_value(major_type),
            "EventSubCode": clean_value(sub_type),
            "AttendanceStatus": (
                clean_value(attendance_status)
                or ATTENDANCE_STATUS_DEFAULT
            ),
            "WearMask": normalize_mask(mask_value),
            "SerialNo": clean_value(serial_no),
            "ClockStation": station_for_ip(
                device_ip,
                device_name,
            ),
        }

        csv_path = get_csv_path(event_datetime)
        file_needs_header = (
            not csv_path.exists()
            or csv_path.stat().st_size == 0
        )

        try:
            with csv_path.open(
                "a",
                newline="",
                encoding="utf-8",
            ) as file:
                writer = csv.DictWriter(
                    file,
                    fieldnames=CSV_FIELDS,
                )

                if file_needs_header:
                    writer.writeheader()

                writer.writerow(row)

        except Exception:
            logging.exception(
                "Failed writing clocking to %s",
                csv_path,
            )
            return False

        processed_keys.update(event_keys)

        logging.info(
            "CLOCKING WRITTEN: device=%s employee=%s "
            "name=%s time=%s inout=%s subtype=%s "
            "serial=%s source=%s file=%s",
            device_ip,
            employee_string,
            clean_value(person_name),
            event_datetime.isoformat(),
            inout_value,
            clean_value(sub_type),
            clean_value(serial_no),
            source,
            csv_path,
        )

        return True


# ============================================================
# LIVE HTTP EVENT HANDLER
# ============================================================

def extract_live_payload() -> dict[str, Any] | None:
    form_json = request.form.get(
        "AccessControllerEvent"
    )

    if form_json:
        try:
            return json.loads(form_json)
        except json.JSONDecodeError:
            logging.exception(
                "Invalid JSON in AccessControllerEvent form field."
            )

    if request.is_json:
        payload = request.get_json(silent=True)

        if isinstance(payload, dict):
            return payload

    raw_body = request.get_data(
        as_text=True,
    ).strip()

    if raw_body.startswith("{"):
        try:
            payload = json.loads(raw_body)

            if isinstance(payload, dict):
                return payload

        except json.JSONDecodeError:
            logging.exception(
                "Invalid raw JSON body."
            )

    return None


@app.route(
    "/hik_event",
    methods=["POST", "GET"],
)
def hik_event() -> Response:
    try:
        logging.info("=== NEW LIVE REQUEST ===")
        logging.info("METHOD: %s", request.method)
        logging.info("URL: %s", request.url)
        logging.info(
            "REMOTE IP: %s",
            request.remote_addr,
        )
        logging.info(
            "HEADERS: %s",
            dict(request.headers),
        )
        logging.info(
            "FORM KEYS: %s",
            list(request.form.keys()),
        )
        logging.info(
            "FILE KEYS: %s",
            list(request.files.keys()),
        )

        payload = extract_live_payload()

        if payload is None:
            logging.warning(
                "No supported Hikvision event payload found."
            )
            logging.warning(
                "RAW BODY: %r",
                request.get_data(
                    as_text=True,
                )[:3000],
            )
            return Response("OK", status=200)

        reported_ip = clean_value(
            payload.get("ipAddress")
        )

        remote_ip = clean_value(
            request.remote_addr
        )

        device_ip = reported_ip or remote_ip

        event_type = clean_value(
            payload.get("eventType")
        )

        event_state = clean_value(
            payload.get("eventState")
        )

        date_time_value = first_value(
            payload,
            "dateTime",
            "time",
        )

        event = payload.get(
            "AccessControllerEvent",
            {},
        )

        if not isinstance(event, dict):
            event = {}

        employee_no = first_value(
            event,
            "employeeNoString",
            "employeeNo",
            "employeeID",
            "personId",
            "jobNo",
        )

        person_name = first_value(
            event,
            "name",
            "employeeName",
            "personName",
        )

        major_type = first_value(
            event,
            "majorEventType",
            "major",
        )

        sub_type = first_value(
            event,
            "subEventType",
            "minor",
        )

        serial_no = first_value(
            event,
            "serialNo",
            "frontSerialNo",
        )

        card_reader = first_value(
            event,
            "cardReaderNo",
            "readerNo",
        )

        card_no = first_value(
            event,
            "cardNo",
            "cardNumber",
        )

        mask_value = first_value(
            event,
            "mask",
            "maskStatus",
        )

        attendance_status = first_value(
            event,
            "attendanceStatus",
        )

        device_name = first_value(
            event,
            "deviceName",
        )

        logging.info(
            "LIVE PARSED: reported_ip=%s remote_ip=%s "
            "event_type=%s event_state=%s employee=%s "
            "name=%s major=%s subtype=%s serial=%s",
            reported_ip,
            remote_ip,
            event_type,
            event_state,
            employee_no,
            person_name,
            major_type,
            sub_type,
            serial_no,
        )

        if event_type != "AccessControllerEvent":
            logging.info(
                "Skipping non-access event: %s",
                event_type,
            )
            return Response("OK", status=200)

        subtype_string = clean_value(sub_type)

        if (
            subtype_string
            not in VALID_CLOCKING_SUBTYPES
        ):
            logging.info(
                "Skipping non-clock event subtype=%s",
                subtype_string,
            )
            return Response("OK", status=200)

        event_datetime = parse_hik_datetime(
            clean_value(date_time_value)
        )

        if event_datetime is None:
            logging.warning(
                "Skipping event with invalid datetime."
            )
            return Response("OK", status=200)

        write_clocking(
            device_ip=device_ip,
            employee_no=employee_no,
            person_name=person_name,
            event_datetime=event_datetime,
            major_type=major_type,
            sub_type=sub_type,
            serial_no=serial_no,
            card_no=card_no,
            card_reader=card_reader,
            mask_value=mask_value,
            attendance_status=attendance_status,
            device_name=device_name,
            source="LIVE",
        )

        return Response("OK", status=200)

    except Exception:
        logging.exception(
            "Unhandled live-event exception."
        )
        return Response("ERROR", status=500)


@app.route(
    "/health",
    methods=["GET"],
)
def health() -> Response:
    return Response(
        "HikGateway is running",
        status=200,
        mimetype="text/plain",
    )


# ============================================================
# STORED-EVENT POLLING
# ============================================================

def fetch_device_history(
    device: dict[str, Any],
    start_datetime: datetime,
    end_datetime: datetime,
) -> list[dict[str, Any]]:
    """
    Paginates the terminal's ISAPI AcsEvent search. Every page repeats the
    full search criteria (searchID, startTime/endTime/major/minor) plus a
    position offset, so this looks like a stateless criteria+offset query
    rather than a live server-side cursor - meaning it should be safe to
    keep the same searchID/position across an auth-session refresh below.

    On large history pulls the terminal's own digest auth session can go
    stale partway through - a 401 that shows up only after earlier pages
    on the same session already succeeded, which is not a bad password.
    When that happens, this re-authenticates with a fresh session and
    resumes at the SAME position rather than restarting the search from 0
    - important, because if the terminal enforces some fixed limit on how
    long/how many requests one auth session may make, restarting from
    scratch would just hit that same limit at the same position every
    time and the fetch would never make it past page one.
    """
    device_ip = clean_value(device["ip"])

    url = (
        f"http://{device_ip}"
        "/ISAPI/AccessControl/AcsEvent?format=json"
    )

    def new_session() -> requests.Session:
        fresh_session = requests.Session()
        fresh_session.auth = HTTPDigestAuth(
            clean_value(device["username"]),
            clean_value(device["password"]),
        )
        return fresh_session

    session = new_session()

    def post_page(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal session

        position_requested = payload["AcsEventCond"]["searchResultPosition"]

        for attempt in range(1, MAX_PAGE_AUTH_RETRIES + 1):
            response = session.post(
                url,
                json=payload,
                timeout=30,
            )

            if response.status_code == 200:
                return response.json()

            stale_session_mid_pagination = (
                response.status_code == 401
                and position_requested > 0
                and attempt < MAX_PAGE_AUTH_RETRIES
            )

            if stale_session_mid_pagination:
                logging.warning(
                    "device=%s got HTTP 401 mid-pagination at "
                    "position=%s (attempt %s/%s) - retrying with a "
                    "fresh session in %ss.",
                    device_ip,
                    position_requested,
                    attempt,
                    MAX_PAGE_AUTH_RETRIES,
                    PAGE_AUTH_RETRY_DELAY_SECONDS,
                )
                stop_event.wait(PAGE_AUTH_RETRY_DELAY_SECONDS)
                session = new_session()
                continue

            raise RuntimeError(
                f"HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        raise RuntimeError(
            f"device={device_ip}: giving up after "
            f"{MAX_PAGE_AUTH_RETRIES} attempts at "
            f"position={position_requested}."
        )

    search_id = str(uuid.uuid4())
    position = 0
    page_size = 30

    all_events: list[dict[str, Any]] = []

    while not stop_event.is_set():
        payload = {
            "AcsEventCond": {
                "searchID": search_id,
                "searchResultPosition": position,
                "maxResults": page_size,
                "major": 0,
                "minor": 0,
                "startTime": start_datetime.isoformat(
                    timespec="seconds"
                ),
                "endTime": end_datetime.isoformat(
                    timespec="seconds"
                ),
                "timeReverseOrder": False,
            }
        }

        result = post_page(payload)
        event_result = result.get(
            "AcsEvent",
            {},
        )

        events = event_result.get(
            "InfoList",
            [],
        )

        if isinstance(events, dict):
            events = [events]

        if not isinstance(events, list):
            events = []

        valid_events = [
            event
            for event in events
            if isinstance(event, dict)
        ]

        all_events.extend(valid_events)

        returned_count = len(valid_events)

        total_matches = int(
            event_result.get(
                "totalMatches",
                len(all_events),
            )
            or 0
        )

        response_status = clean_value(
            event_result.get(
                "responseStatusStrg"
            )
        ).upper()

        logging.info(
            "HISTORY PAGE: device=%s position=%s "
            "received=%s total=%s status=%s",
            device_ip,
            position,
            returned_count,
            total_matches,
            response_status,
        )

        if returned_count == 0:
            break

        position += returned_count

        if position >= total_matches:
            break

        if (
            response_status != "MORE"
            and returned_count < page_size
        ):
            break

    return all_events


def normalize_history_event(
    device: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any] | None:
    device_ip = clean_value(device["ip"])

    employee_no = first_value(
        event,
        "employeeNoString",
        "employeeNo",
        "employeeID",
        "personId",
        "jobNo",
    )

    # Stored system events such as minor 1029 have no employee number.
    # They are not attendance clockings.
    if not clean_value(employee_no):
        return None

    time_value = first_value(
        event,
        "time",
        "dateTime",
    )

    event_datetime = parse_hik_datetime(
        clean_value(time_value)
    )

    if event_datetime is None:
        return None

    return {
        "device_ip": device_ip,
        "employee_no": employee_no,
        "person_name": first_value(
            event,
            "name",
            "employeeName",
            "personName",
        ),
        "event_datetime": event_datetime,
        "major_type": first_value(
            event,
            "major",
            "majorEventType",
        ),
        "sub_type": first_value(
            event,
            "minor",
            "subEventType",
        ),
        "serial_no": first_value(
            event,
            "serialNo",
            "frontSerialNo",
        ),
        "card_no": first_value(
            event,
            "cardNo",
            "cardNumber",
        ),
        "card_reader": first_value(
            event,
            "cardReaderNo",
            "readerNo",
        ),
        "mask_value": first_value(
            event,
            "mask",
            "maskStatus",
        ),
        "attendance_status": first_value(
            event,
            "attendanceStatus",
        ),
        "device_name": first_value(
            event,
            "deviceName",
        ),
        "source": "HISTORY",
    }


def poll_all_devices() -> None:
    now = datetime.now(SAST)

    # Fallback window for a device with no recorded successful poll yet -
    # first run, a brand-new device, or poll_state.json was lost/reset.
    # Duplicate checking ensures already-imported events are not written
    # again even if this whole window gets re-covered.
    default_start_datetime = (
        now - timedelta(days=HISTORY_LOOKBACK_DAYS)
    ).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    poll_state = load_poll_state()
    collected_clockings: list[dict[str, Any]] = []

    for device in DEVICES:
        if not device.get("enabled", True):
            continue

        device_ip = clean_value(device["ip"])

        last_success = parse_hik_datetime(
            clean_value(poll_state.get(device_ip))
        )

        start_datetime = (
            last_success - timedelta(minutes=POLL_OVERLAP_MINUTES)
            if last_success is not None
            else default_start_datetime
        )

        try:
            logging.info(
                "Polling stored events from %s (since %s)...",
                device_ip,
                start_datetime.isoformat(),
            )

            events = fetch_device_history(
                device=device,
                start_datetime=start_datetime,
                end_datetime=now,
            )

            logging.info(
                "Stored events received from %s: %s",
                device_ip,
                len(events),
            )

            for event in events:
                normalized = normalize_history_event(
                    device,
                    event,
                )

                if normalized is not None:
                    collected_clockings.append(
                        normalized
                    )

            # Only advance this device's watermark once its fetch has
            # actually succeeded end-to-end - a failed/partial fetch keeps
            # retrying the same (wider) window next cycle instead of
            # silently skipping whatever it didn't get to.
            poll_state[device_ip] = now.isoformat()
            save_poll_state(poll_state)

        except requests.RequestException:
            logging.exception(
                "Network error polling device %s.",
                device_ip,
            )

        except Exception:
            logging.exception(
                "Failed polling device %s.",
                device_ip,
            )

    # Write all devices in chronological order so IN/OUT remains
    # correct when employees use different terminals.
    collected_clockings.sort(
        key=lambda item: (
            item["event_datetime"],
            clean_value(item["device_ip"]),
            clean_value(item["serial_no"]),
        )
    )

    added_count = 0

    for clocking in collected_clockings:
        if write_clocking(**clocking):
            added_count += 1

    logging.info(
        "Polling cycle finished: candidates=%s added=%s",
        len(collected_clockings),
        added_count,
    )


def polling_worker() -> None:
    logging.info(
        "History polling worker started. Interval=%s seconds",
        POLL_INTERVAL_SECONDS,
    )

    while not stop_event.is_set():
        try:
            poll_all_devices()
        except Exception:
            logging.exception(
                "Unhandled polling-cycle exception."
            )

        stop_event.wait(POLL_INTERVAL_SECONDS)


# ============================================================
# PROGRAM START
# ============================================================

def load_gateway_config() -> None:
    """
    Load device IPs/labels/credentials from gateway.json (next to this
    script) into the module-level DEVICES/DEVICE_LABELS. Fails loudly, not
    silently, if the file is missing or malformed - a credentials file
    nobody notices is missing helps nobody; see this file's own history
    for what a silently-broken config costs.

    Expected shape:

        {
          "default_username": "admin",
          "default_password": "...",
          "devices": [
            {"ip": "192.168.0.101", "label": "TNA-Example-Main-Entrance", "enabled": true},
            {"ip": "192.168.0.102", "label": "TNA-Example-Side-Door", "enabled": true,
             "username": "...", "password": "..."}
          ]
        }

    "username"/"password" on a device entry are optional overrides - if
    omitted, default_username/default_password apply. At least one of
    (device override) or (defaults) must supply both, per device.
    """
    global DEVICES, DEVICE_LABELS

    if not CONFIG_FILE.exists():
        raise RuntimeError(
            f"Missing gateway config file: {CONFIG_FILE}. Create it with "
            "default_username/default_password and a devices list - see "
            "load_gateway_config()'s docstring for the exact shape."
        )

    with CONFIG_FILE.open("r", encoding="utf-8") as file:
        raw_config = json.load(file)

    default_username = clean_value(raw_config.get("default_username"))
    default_password = clean_value(raw_config.get("default_password"))
    raw_devices = raw_config.get("devices")

    if not isinstance(raw_devices, list) or not raw_devices:
        raise RuntimeError(f"{CONFIG_FILE} has no devices configured.")

    devices: list[dict[str, Any]] = []

    for entry in raw_devices:
        ip = clean_value(entry.get("ip"))
        if not ip:
            continue

        username = clean_value(entry.get("username")) or default_username
        password = clean_value(entry.get("password")) or default_password

        if not username or not password:
            raise RuntimeError(
                f"Device {ip} in {CONFIG_FILE} has no username/password, "
                "and no default_username/default_password to fall back to."
            )

        devices.append(
            {
                "ip": ip,
                "label": clean_value(entry.get("label")) or ip,
                "username": username,
                "password": password,
                "enabled": bool(entry.get("enabled", True)),
            }
        )

    if not devices:
        raise RuntimeError(f"{CONFIG_FILE}'s devices list has no usable entries.")

    DEVICES = devices
    DEVICE_LABELS = {device["ip"]: device["label"] for device in DEVICES}

    logging.info(
        "Loaded %s device(s) from %s (%s enabled).",
        len(DEVICES),
        CONFIG_FILE,
        sum(1 for device in DEVICES if device["enabled"]),
    )


def initialise_gateway() -> None:
    global employee_state

    load_gateway_config()

    employee_state = load_employee_state()
    load_existing_csv_keys()

    logging.info(
        "Employee state records loaded: %s",
        len(employee_state),
    )

    logging.info(
        "Existing duplicate keys loaded: %s",
        len(processed_keys),
    )


if __name__ == "__main__":
    initialise_gateway()

    polling_thread = threading.Thread(
        target=polling_worker,
        name="HikHistoryPolling",
        daemon=True,
    )

    polling_thread.start()

    logging.info(
        "Starting HikGateway on %s:%s",
        SERVER_HOST,
        SERVER_PORT,
    )

    try:
        app.run(
            host=SERVER_HOST,
            port=SERVER_PORT,
            debug=False,
            threaded=True,
            use_reloader=False,
        )

    finally:
        stop_event.set()
        polling_thread.join(timeout=10)
