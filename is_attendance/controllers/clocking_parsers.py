# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Format-specific parsers for the "Clocking Import" doctype, plus the
sniff-and-dispatch step that lets one doctype accept every clocking export
shape this app knows about instead of needing one doctype per shape.

Every parser here returns rows in the shape
is_attendance.controllers.clocking_import's module docstring documents:

    {
        "employee_code": "1234",
        "time": "2026-09-03 07:58:00",
        "log_type": "IN",
        "machine_id": "13",
    }

Adding a new vendor/shape later is: write one more parse_*() function here,
add its shape check to sniff_format(), and dispatch to it in
detect_and_parse() - no new doctype, no new Issue/Checkin child doctypes,
no new JS file.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

import frappe
from frappe import _

from is_attendance.controllers.clocking_import import machine_id_from_filename

# ---------------------------------------------------------------------------
# CSV (HIKVision recordList.csv, and a tolerated legacy variant)
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = {"sJobNo", "Date", "Time"}
DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d")


def parse_hikvision_csv(content: bytes | str, filename: str | None = None) -> list[dict]:
	"""
	Parse a gateway.py recordList CSV export - or a tolerated legacy
	variant - into a list of row dicts.

	- sJobNo: the employee's badge/enrollment number, matched against
	  Employee.attendance_device_id.
	- Date/Time: "dd/MM/yyyy" + "HH:mm:ss" (gateway.py's own format), with
	  "yyyy-MM-dd" tried as a fallback for a real legacy `recordList.csv`
	  sample confirmed from an older/different terminal system - never
	  ambiguous between the two, since one uses slashes and the other
	  dashes. That same legacy sample also has sJobNo wrapped in a stray
	  single quote, with the closing quote landing in the *next* column
	  because the value has a literal embedded comma (e.g. sJobNo parses as
	  "'48041") - stripped before use, a no-op on gateway.py's own clean
	  double-quoted CSV.
	- ClockStation: gateway.py's friendly per-terminal label - recorded as
	  machine_id when present; falls back to machine_id_from_filename when
	  absent (the legacy sample has no ClockStation column at all).
	- sName/sCard/ReadID/EventMainCode/EventSubCode/AttendanceStatus/
	  WearMask/SerialNo: not used.

	Rows missing sJobNo/Date/Time, or with an unparseable date/time, are
	skipped silently (not raised) - a hand-edited or corrupted file
	shouldn't fail the whole import.

	Returns [] if the file doesn't have this shape at all (no header, or a
	header missing sJobNo/Date/Time) - sniff_format() checks for that
	before this is ever called, so reaching here with the wrong shape
	shouldn't normally happen, but returning [] rather than raising keeps
	this function safe to call directly too.
	"""
	if isinstance(content, bytes):
		text = content.decode("utf-8-sig", errors="replace")
	else:
		text = content

	reader = csv.DictReader(io.StringIO(text))
	if reader.fieldnames is None or not REQUIRED_COLUMNS.issubset(set(reader.fieldnames)):
		return []

	fallback_machine_id = machine_id_from_filename(filename)

	rows = []
	for raw in reader:
		employee_code = (raw.get("sJobNo") or "").strip().strip("'")
		date_value = (raw.get("Date") or "").strip()
		time_value = (raw.get("Time") or "").strip()

		if not employee_code or not date_value or not time_value:
			continue

		event_datetime = None
		for date_format in DATE_FORMATS:
			try:
				event_datetime = datetime.strptime(f"{date_value} {time_value}", f"{date_format} %H:%M:%S")
				break
			except ValueError:
				continue

		if event_datetime is None:
			continue

		log_type = (raw.get("IN/OUT") or "").strip().upper()
		if log_type not in ("IN", "OUT"):
			log_type = "IN"

		machine_id = (raw.get("ClockStation") or "").strip() or fallback_machine_id

		rows.append(
			{
				"employee_code": employee_code,
				"time": event_datetime.strftime("%Y-%m-%d %H:%M:%S"),
				"log_type": log_type,
				"machine_id": machine_id,
			}
		)

	return rows


# ---------------------------------------------------------------------------
# Legacy .DAT (tab-separated, no header)
# ---------------------------------------------------------------------------

# Device Status column -> Employee Checkin.log_type. Only 0/1 have been seen
# in real files; 2-5 are documented ZKTeco codes (Break Out/Break In/OT
# In/OT Out) mapped here defensively even though unobserved so far.
STATUS_LOG_TYPE_MAP = {
	"0": "IN",  # Check In
	"1": "OUT",  # Check Out
	"2": "OUT",  # Break Out
	"3": "IN",  # Break In
	"4": "IN",  # OT In
	"5": "OUT",  # OT Out
}

DAT_MIN_COLUMNS = 4  # PIN, DateTime, MachineID, Status - VerifyMode/Reserved are optional


def parse_dat_file(content: bytes | str, filename: str | None = None) -> list[dict]:
	"""
	Parse a legacy clocking terminal .DAT export into a list of row dicts.

	Tab-separated, no header, one punch per line:

		<PIN>\\t<DateTime>\\t<MachineID column>\\t<Status>\\t<VerifyMode>\\t<Reserved>

	e.g. ``   691\\t2026-06-30 15:56:56\\t13\\t0\\t15\\t0``.

	The MachineID column's meaning isn't consistent across terminal
	vendors (one real sample has it as a useless constant "1" throughout),
	so the actual machine_id is derived from the **filename** instead
	(stripping a trailing "_attlog.dat"/".dat" - see
	machine_id_from_filename), falling back to this column only if the
	filename is unavailable for some reason.

	Status is device-reported 0/1 and does **not** reliably alternate per
	visit in real data, so it's mapped to IN/OUT as a best-effort label
	only - attendance_sync.py's own clustering re-derives IN/OUT purely
	from chronological order, which is what makes this safe.

	Lines with fewer than DAT_MIN_COLUMNS columns, or a blank PIN/DateTime,
	are skipped silently (not raised).
	"""
	if isinstance(content, bytes):
		text = content.decode("utf-8-sig", errors="replace")
	else:
		text = content

	machine_id_from_name = machine_id_from_filename(filename)

	rows = []
	for line in text.splitlines():
		line = line.strip()
		if not line:
			continue

		fields = line.split("\t")
		if len(fields) < DAT_MIN_COLUMNS:
			continue

		pin = fields[0].strip()
		time_value = fields[1].strip()
		machine_id_column = fields[2].strip()
		status = fields[3].strip()

		if not pin or not time_value:
			continue

		rows.append(
			{
				"employee_code": pin,
				"time": time_value,
				"log_type": STATUS_LOG_TYPE_MAP.get(status, "IN"),
				"machine_id": machine_id_from_name or machine_id_column or None,
			}
		)

	return rows


# ---------------------------------------------------------------------------
# Format sniffing / dispatch
# ---------------------------------------------------------------------------

def sniff_format(content: bytes | str) -> str | None:
	"""Identifies which known shape `content` matches from its first
	non-blank line alone - cheap, and lets detect_and_parse() give a clear
	error instead of silently importing zero rows when a file matches
	neither shape.

	- CSV: comma-separated, and the header contains every column in
	  REQUIRED_COLUMNS (order-independent - real exports don't always put
	  them in the same position).
	- DAT: no recognisable CSV header, tab-separated, with at least
	  DAT_MIN_COLUMNS fields on the first data line.
	"""
	if isinstance(content, bytes):
		text = content.decode("utf-8-sig", errors="replace")
	else:
		text = content

	first_line = ""
	for line in text.splitlines():
		if line.strip():
			first_line = line.strip()
			break

	if not first_line:
		return None

	if "," in first_line:
		header_columns = {column.strip() for column in first_line.split(",")}
		if REQUIRED_COLUMNS.issubset(header_columns):
			return "csv"

	if "\t" in first_line and len(first_line.split("\t")) >= DAT_MIN_COLUMNS:
		return "dat"

	return None


def detect_and_parse(content: bytes | str, filename: str | None = None) -> tuple[str, list[dict]]:
	"""Identifies the format and parses in one step - the one entry point
	Clocking Import's parse_file() calls. Raises rather than silently
	returning zero rows when the shape isn't recognised at all, since that
	almost always means the wrong file was attached."""
	detected_format = sniff_format(content)

	if detected_format == "csv":
		return "CSV", parse_hikvision_csv(content, filename)
	if detected_format == "dat":
		return "DAT", parse_dat_file(content, filename)

	frappe.throw(
		_(
			"Unrecognized file format - expected a HIKVision-style CSV (header row with "
			"sJobNo/Date/Time) or a legacy tab-separated .DAT export."
		)
	)
