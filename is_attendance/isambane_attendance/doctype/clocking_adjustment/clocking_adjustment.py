# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Period-based review of an employee's Employee Checkins: load every clocking
for a date range into an editable table, correct times, flip IN/OUT, add a
missing punch, or remove a bad one - all in one document, printed and signed
by the Employee/HR/Manager, then submitted to actually apply the changes.

Why the sign-off gate exists before anything happens: submitting is what
mutates real Employee Checkin records (and, downstream, Attendance) - this
document is only allowed to reach that point once a signed copy is on file,
matching the same before_submit-on-an-Attach-field convention `ir`'s HR forms
already use (Warning/Suspension/Dismissal/KPI Review/etc.), not a generic
"some file is attached" check.

Why log_type on a row barely matters for the *computed* result: attendance_sync.
_normalize_log_types() completely discards whatever log_type is stored on an
Employee Checkin and re-derives IN/OUT purely by chronological alternation
within the day's shift window. Editing a row's Type here only changes what's
visibly stored on that Employee Checkin record - not how hours get computed.
What actually matters for hours is a checkin's existence, time, and count
within the window, which is why every code path below is careful to recompute
both the affected date and the day before it (see _recompute) - an edit that
moves a punch across midnight, or that's the tail end of an overnight shift,
needs both days refreshed. Same reasoning as clocking_import.py's bulk paths.

Leave awareness: get_leave_info() surfaces every Leave Application already
on file for the period (rendered as a badge per day on the roster - see
clocking_adjustment.js), and create_leave_for_date() lets a day that turns
out to actually be leave, not a missed clocking, get a proper (Draft)
Leave Application created right from this same review - so someone
reviewing a gap doesn't have to switch screens, or worse, "fix" a day that
was never actually missing in the first place.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, cint, get_datetime, getdate

from is_attendance.controllers.attendance_sync import (
	recompute_attendance_for_employee_day,
)


class ClockingAdjustment(Document):
	def validate(self):
		if self.from_date and self.to_date and self.to_date < self.from_date:
			frappe.throw(_("To Date cannot be before From Date."))

	def before_submit(self):
		if not self.checkin_rows:
			frappe.throw(_('Load and review this period\'s checkins ("Load Checkins for Period") before submitting.'))

		if not self.signed_adjustment_form:
			frappe.throw(_("Attach the signed adjustment form before submitting."))

	@frappe.whitelist()
	def load_checkins(self):
		"""Whitelisted so it's independently reachable (not just gated by
		hiding the JS button) - refuses on an already-submitted document for
		the same reason. Wipes and rebuilds checkin_rows from every Employee
		Checkin this employee has in [from_date, to_date] - see checkin_rows'
		own field description for what happens to any unsaved edits."""
		if self.docstatus != 0:
			frappe.throw(_("Checkins can only be (re)loaded on a Draft document."))

		if not self.employee or not self.from_date or not self.to_date:
			frappe.throw(_("Set Employee, From Date, and To Date first."))

		start = get_datetime(f"{self.from_date} 00:00:00")
		end = get_datetime(f"{self.to_date} 23:59:59")

		checkins = frappe.get_all(
			"Employee Checkin",
			filters={"employee": self.employee, "time": ["between", [start, end]]},
			fields=["name", "time", "log_type"],
			order_by="time asc",
		)

		self.set("checkin_rows", [])
		for checkin in checkins:
			self.append(
				"checkin_rows",
				{
					"checkin": checkin.name,
					"original_time": checkin.time,
					"original_log_type": checkin.log_type,
					"time": checkin.time,
					"log_type": checkin.log_type,
					"remove": 0,
				},
			)

	@frappe.whitelist()
	def get_leave_info(self) -> dict:
		"""Every non-cancelled Leave Application covering [from_date, to_date]
		for this employee, keyed by date string - lets the roster show which
		days already have leave on file (submitted OR still pending approval,
		not just Approved - this is for awareness while deciding whether a
		day needs a clocking correction at all, a broader net than
		attendance_compliance_summary's own Approved-only compliance scope)
		before someone spends time correcting a day that's actually leave."""
		if not self.employee or not self.from_date or not self.to_date:
			return {}

		applications = frappe.get_all(
			"Leave Application",
			filters={
				"employee": self.employee,
				"docstatus": ["!=", 2],
				"from_date": ["<=", self.to_date],
				"to_date": [">=", self.from_date],
			},
			fields=["name", "leave_type", "status", "half_day", "half_day_date", "from_date", "to_date"],
		)

		info: dict[str, dict] = {}
		for application in applications:
			app_start = max(getdate(application.from_date), getdate(self.from_date))
			app_end = min(getdate(application.to_date), getdate(self.to_date))
			half_day_date = (
				getdate(application.half_day_date) if cint(application.half_day) and application.half_day_date else None
			)

			current = app_start
			while current <= app_end:
				info[str(current)] = {
					"leave_application": application.name,
					"leave_type": application.leave_type,
					"status": application.status,
					"half_day": bool(half_day_date and half_day_date == current),
				}
				current = add_days(current, 1)

		return info

	@frappe.whitelist()
	def create_leave_for_date(self, date, leave_type, half_day=0):
		"""Creates (but does not submit) a Leave Application for a single
		date, for this Clocking Adjustment's own employee - lets a day that
		turns out to actually be leave (not a missed clocking) get its proper
		record created right from this same review, without switching
		screens. Left as a Draft deliberately: approval stays HR's own normal
		Leave Application workflow, this doesn't grant it - it only creates
		the record so it exists to be approved."""
		if self.docstatus != 0:
			frappe.throw(_("Leave can only be created from a Draft Clocking Adjustment."))
		if not self.employee:
			frappe.throw(_("Set Employee first."))

		half_day = cint(half_day)
		leave_application = frappe.get_doc(
			{
				"doctype": "Leave Application",
				"employee": self.employee,
				"leave_type": leave_type,
				"from_date": date,
				"to_date": date,
				"half_day": half_day,
				"half_day_date": date if half_day else None,
				"company": self.company,
				"description": _("Created from Clocking Adjustment {0}{1}").format(
					self.name or _("(unsaved)"), f": {self.reason}" if self.reason else ""
				),
			}
		)
		leave_application.insert(ignore_permissions=True)
		return leave_application.name

	def on_submit(self):
		affected: set[tuple[str, object]] = set()

		frappe.flags.in_bulk_checkin_import = True
		try:
			for row in self.checkin_rows:
				self._process_row_on_submit(row, affected)
		finally:
			frappe.flags.in_bulk_checkin_import = False

		self._recompute(affected)

	def on_cancel(self):
		affected: set[tuple[str, object]] = set()

		frappe.flags.in_bulk_checkin_import = True
		try:
			for row in self.checkin_rows:
				self._process_row_on_cancel(row, affected)
		finally:
			frappe.flags.in_bulk_checkin_import = False

		self._recompute(affected)

	# ------------------------------------------------------------------
	# Per-row submit
	# ------------------------------------------------------------------

	def _process_row_on_submit(self, row, affected: set) -> None:
		try:
			if row.remove and row.checkin:
				checkin = frappe.get_doc("Employee Checkin", row.checkin)
				affected.add((self.employee, getdate(checkin.time)))
				frappe.delete_doc("Employee Checkin", row.checkin, ignore_permissions=True, force=True)

			elif not row.checkin and not row.remove:
				checkin = frappe.get_doc(
					{
						"doctype": "Employee Checkin",
						"employee": self.employee,
						"time": row.time,
						"log_type": row.log_type,
						"isa_branch": self.branch,
						"isa_import_doctype": self.doctype,
						"isa_import_reference": self.name,
					}
				)
				checkin.insert(ignore_permissions=True)
				row.db_set("checkin", checkin.name)
				affected.add((self.employee, getdate(row.time)))

			elif row.checkin and not row.remove and (
				row.time != row.original_time or row.log_type != row.original_log_type
			):
				checkin = frappe.get_doc("Employee Checkin", row.checkin)

				# Staleness check: something else (a live device punch, another
				# adjustment, a re-import) may have touched this checkin since
				# Load - don't silently clobber it. Message spells out the
				# actual mismatch so a real occurrence is self-diagnosing
				# rather than needing to be reproduced blind.
				#
				# get_datetime() on both sides matters here, not just style:
				# checkin.time comes from a fresh frappe.get_doc() SQL load (a
				# real datetime object), but row.original_time comes from the
				# document as reconstructed from the client's submitted JSON
				# payload, which leaves Datetime child-table fields as plain
				# strings rather than coercing them - a bare != between a
				# datetime object and an identical-looking string is always
				# True in Python, producing exactly this false positive.
				if get_datetime(checkin.time) != get_datetime(row.original_time) or checkin.log_type != row.original_log_type:
					frappe.throw(
						_(
							"This checkin changed since you loaded this period - "
							'reload ("Load Checkins for Period") and redo your edits. '
							"(loaded snapshot: {0} {1} - current record: {2} {3})"
						).format(row.original_time, row.original_log_type, checkin.time, checkin.log_type)
					)

				if row.time != row.original_time and checkin.attendance:
					frappe.throw(
						_(
							"Linked Attendance {0} must be cancelled before this clocking's time can be changed."
						).format(checkin.attendance)
					)

				affected.add((self.employee, getdate(row.original_time)))
				checkin.time = row.time
				checkin.log_type = row.log_type
				checkin.save(ignore_permissions=True)
				affected.add((self.employee, getdate(row.time)))

			# else: genuinely unchanged row, nothing to do.

		except Exception as error:
			frappe.throw(_("Row {0}: {1}").format(row.idx, str(error)))

	# ------------------------------------------------------------------
	# Per-row cancel (mirrors the submit cases above)
	# ------------------------------------------------------------------

	def _process_row_on_cancel(self, row, affected: set) -> None:
		if row.remove and row.original_time:
			# This row's checkin was deleted at submit - recreate it.
			# The original name isn't recoverable, same limitation as before.
			checkin = frappe.get_doc(
				{
					"doctype": "Employee Checkin",
					"employee": self.employee,
					"time": row.original_time,
					"log_type": row.original_log_type,
					"isa_branch": self.branch,
					"isa_import_doctype": self.doctype,
					"isa_import_reference": self.name,
				}
			)
			checkin.insert(ignore_permissions=True)
			row.db_set("checkin", checkin.name)
			affected.add((self.employee, getdate(row.original_time)))

		elif not row.original_time and row.checkin:
			# This row created a brand-new checkin at submit - remove it.
			affected.add((self.employee, getdate(row.time)))
			if frappe.db.exists("Employee Checkin", row.checkin):
				frappe.delete_doc("Employee Checkin", row.checkin, ignore_permissions=True, force=True)

		elif row.checkin and (row.time != row.original_time or row.log_type != row.original_log_type):
			# This row corrected an existing checkin - restore it.
			if frappe.db.exists("Employee Checkin", row.checkin):
				checkin = frappe.get_doc("Employee Checkin", row.checkin)
				checkin.time = row.original_time
				checkin.log_type = row.original_log_type
				checkin.save(ignore_permissions=True)
				affected.add((self.employee, getdate(row.original_time)))
				affected.add((self.employee, getdate(row.time)))

		# else: unchanged row, nothing to revert.

	# ------------------------------------------------------------------
	# Shared
	# ------------------------------------------------------------------

	def _recompute(self, affected: set) -> None:
		"""Recomputes every affected date *and the day before it* - a
		correction/add/remove can be the tail end of an overnight shift
		whose Attendance is attributed to the day before this checkin's own
		date (see attendance_sync._get_shift_window's overnight handling),
		same reasoning as clocking_import.py's bulk import paths."""
		all_dates: set[tuple[str, object]] = set()
		for employee, date in affected:
			all_dates.add((employee, date))
			all_dates.add((employee, add_days(date, -1)))

		for employee, date in all_dates:
			recompute_attendance_for_employee_day(employee, date)
