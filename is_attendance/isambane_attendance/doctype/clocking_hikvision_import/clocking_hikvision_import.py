# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
HIKVision gateway recordList CSV import.

gateway.py (running on the Windows host next to the terminals) already
normalizes both live push events and polled device history into one CSV
shape and writes it to `recordList_<date>.csv`:

    sName,sJobNo,sCard,Date,Time,IN/OUT,ReadID,EventMainCode,EventSubCode,
    AttendanceStatus,WearMask,SerialNo,ClockStation

- sJobNo: the employee's badge/enrollment number, matched against
  Employee.attendance_device_id - same field the DAT importer and `ir`'s
  Monthly Attendance report both already use for this purpose, and the
  same field the old watch-checkins.ps1 script already (correctly) looked
  employees up by.
- Date/Time: "dd/MM/yyyy" + "HH:mm:ss".
- IN/OUT: gateway.py's own locally-tracked alternating state, carried
  through as a best-effort log_type label only - never trusted for hours
  (see attendance_sync.py's clustering; same reasoning as the DAT
  importer's Status column).
- ClockStation: gateway.py's friendly per-terminal label (e.g.
  "TNA-198-Former-197", from its own DEVICE_LABELS) - recorded as
  isa_clocking_machine/device_id for traceability only, never determines
  Branch (always the employee's own Employee.branch - see
  is_attendance.controllers.clocking_import).
- sName/sCard/ReadID/EventMainCode/EventSubCode/AttendanceStatus/WearMask/
  SerialNo: not used. In particular ReadID (a card-reader lane number,
  constant "1" across every terminal in the real export) is deliberately
  *not* used as the machine identifier - ClockStation is the real
  per-terminal signal, which is what watch-checkins.ps1 got wrong.

gateway.py uploads the day's CSV directly (see its own module docstring)
rather than through a filesystem watch-folder handoff - each upload is a
fresh Clocking HIKVision Import document with a timestamped filename, and
relies entirely on this pipeline's existing idempotency
(employee+time+isa_clocking_machine, in create_checkins()) to skip rows
already imported from an earlier upload of the same still-growing day's
file. No separate "mark this file as processed" step is needed for that
reason - see is_attendance.controllers.clocking_import's module docstring
for the full shared Draft -> Missing Information -> Pending Import ->
Importing -> Completed/Error lifecycle this doctype uses.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

import frappe
from frappe.model.document import Document
from frappe.utils.file_manager import get_file

from is_attendance.controllers import clocking_import

REQUIRED_COLUMNS = {"sJobNo", "Date", "Time"}


class ClockingHIKVisionImport(Document):
	def validate(self):
		clocking_import.validate_import(self)

	def before_submit(self):
		clocking_import.before_submit_guard(self)

	@frappe.whitelist()
	def queue_import(self):
		clocking_import.queue_import(self)

	def on_cancel(self):
		clocking_import.cancel_import(self)

	def parse_file(self) -> list[dict]:
		"""Cached per in-memory Document instance - see
		ClockingDATImport.parse_file for why."""
		cached = getattr(self, "_parsed_rows_cache", None)
		if cached is not None:
			return cached

		_filename, content = get_file(self.file)
		rows = parse_hikvision_csv(content)
		self._parsed_rows_cache = rows
		return rows


def parse_hikvision_csv(content: bytes | str) -> list[dict]:
	"""
	Parse a gateway.py recordList CSV export into a list of row dicts
	(shape: is_attendance.controllers.clocking_import's module docstring).

	Rows missing sJobNo/Date/Time, or with an unparseable date/time, are
	skipped silently (not raised) - gateway.py's own writer never produces
	those, but a hand-edited or corrupted file shouldn't fail the whole
	import.
	"""
	if isinstance(content, bytes):
		text = content.decode("utf-8-sig", errors="replace")
	else:
		text = content

	reader = csv.DictReader(io.StringIO(text))
	if reader.fieldnames is None or not REQUIRED_COLUMNS.issubset(set(reader.fieldnames)):
		return []

	rows = []
	for raw in reader:
		employee_code = (raw.get("sJobNo") or "").strip()
		date_value = (raw.get("Date") or "").strip()
		time_value = (raw.get("Time") or "").strip()

		if not employee_code or not date_value or not time_value:
			continue

		try:
			event_datetime = datetime.strptime(f"{date_value} {time_value}", "%d/%m/%Y %H:%M:%S")
		except ValueError:
			continue

		log_type = (raw.get("IN/OUT") or "").strip().upper()
		if log_type not in ("IN", "OUT"):
			log_type = "IN"

		machine_id = (raw.get("ClockStation") or "").strip() or None

		rows.append(
			{
				"employee_code": employee_code,
				"time": event_datetime.strftime("%Y-%m-%d %H:%M:%S"),
				"log_type": log_type,
				"machine_id": machine_id,
			}
		)

	return rows
