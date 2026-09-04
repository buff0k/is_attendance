# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Legacy clocking system .DAT file import.

The .DAT files are tab-separated text (CRLF line endings), one punch per
line, 6 columns and no header - confirmed against real ``13_attlog.dat`` /
``20_attlog.dat`` samples from two different terminals:

    <PIN>\t<DateTime>\t<MachineID>\t<Status>\t<VerifyMode>\t<Reserved>
       691\t2026-06-30 15:56:56\t13\t0\t15\t0

- PIN: the employee's badge/enrollment number on the device (space-padded),
  matched against ``Employee.attendance_device_id`` - the same field `ir`'s
  Monthly Attendance report already uses for this purpose.
- DateTime: "YYYY-MM-DD HH:MM:SS".
- MachineID: constant for an entire file (matches the "13"/"20" in the
  filename). Recorded on the resulting Employee Checkin's device_id (its
  plain, core-field meaning - the physical device ID) and isa_clocking_machine
  for traceability only - it does **not** determine the checkin's Branch.
- Status: device-reported 0/1. In the sample data this does **not**
  reliably alternate per visit (it can stay constant for a user across many
  days/times of day - some terminals only change it when an operator
  presses a mode key at the device, not per punch), so it's mapped to
  IN/OUT as a best-effort label only. Actual working-hours computation
  never trusts stored log_type - attendance_sync.py's own clustering
  already re-derives IN/OUT purely from chronological order per employee
  per day, which is what makes this safe.
- VerifyMode/Reserved: not used.

Workflow
--------
This doctype is submittable, and the actual row-by-row import work always
runs in a background job - see is_attendance.controllers.clocking_import's
module docstring for the full shared Draft -> Missing Information ->
Pending Import -> Importing -> Completed/Error lifecycle (that module is
what actually drives every method below - this file only owns file-format
parsing and doctype registration).
"""

from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils.file_manager import get_file

from is_attendance.controllers import clocking_import


class ClockingDATImport(Document):
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
		"""Cached per in-memory Document instance - validate() and
		queue_import() can both run against the same loaded doc within one
		request, and re-reading/re-decoding a multi-thousand-line file
		twice is pure waste since the attached file can't change in
		between."""
		cached = getattr(self, "_parsed_rows_cache", None)
		if cached is not None:
			return cached

		_filename, content = get_file(self.file)
		rows = parse_dat_file(content)
		self._parsed_rows_cache = rows
		return rows


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


def parse_dat_file(content: bytes | str) -> list[dict]:
	"""
	Parse a legacy clocking terminal .DAT export into a list of row dicts
	(shape: is_attendance.controllers.clocking_import's module docstring).

	Tab-separated, no header, one punch per line:

		<PIN>\t<DateTime>\t<MachineID>\t<Status>\t<VerifyMode>\t<Reserved>

	e.g. ``   691\t2026-06-30 15:56:56\t13\t0\t15\t0`` - confirmed against
	real 13_attlog.dat / 20_attlog.dat samples. See the module docstring for
	the full column notes.

	Lines with fewer than 4 columns, or a blank PIN/DateTime, are skipped
	silently (not raised) - real exports are clean, but a stray blank line
	shouldn't fail the whole import.
	"""
	if isinstance(content, bytes):
		text = content.decode("utf-8-sig", errors="replace")
	else:
		text = content

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
		machine_id = fields[2].strip()
		status = fields[3].strip()

		if not pin or not time_value:
			continue

		rows.append(
			{
				"employee_code": pin,
				"time": time_value,
				"log_type": STATUS_LOG_TYPE_MAP.get(status, "IN"),
				"machine_id": machine_id or None,
			}
		)

	return rows
