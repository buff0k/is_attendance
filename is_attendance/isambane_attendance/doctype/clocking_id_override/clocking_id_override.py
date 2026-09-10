# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
A second, parallel resolution path alongside Employee.attendance_device_id -
see resolve_employee() in is_attendance.controllers.clocking_import, which
only ever consults this table for a raw clocking code that didn't already
match some Employee's own attendance_device_id directly. Deliberately never
touches the Employee record itself: the point is to let a misconfigured
device's wrong code also produce Checkins for the right person, without
disturbing that person's own, correct code or the standard resolve flow
everyone else relies on. One employee gets at most one Clocking ID Override
record (autoname: field:employee) holding every extra code that should
route to them.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class ClockingIDOverride(Document):
	def validate(self):
		self._check_duplicate_codes_within_self()
		self._check_codes_not_used_elsewhere()

	def _check_duplicate_codes_within_self(self):
		seen = set()
		for row in self.override_codes:
			code = (row.clocking_id or "").strip()
			if not code:
				frappe.throw(_("Row {0}: Clocking ID can't be blank.").format(row.idx))
			if code in seen:
				frappe.throw(_("Row {0}: Clocking ID {1} is already listed above.").format(row.idx, code))
			seen.add(code)
			row.clocking_id = code

	def _check_codes_not_used_elsewhere(self):
		codes = [row.clocking_id for row in self.override_codes]
		if not codes:
			return

		# Another employee's override already claims one of these codes -
		# always a mistake (the same raw code can't sensibly redirect to two
		# different people), never a legitimate shared mapping.
		clashing = frappe.get_all(
			"Clocking ID Override Code",
			filters={
				"clocking_id": ["in", codes],
				"parenttype": "Clocking ID Override",
				"parent": ["!=", self.name or ""],
			},
			fields=["clocking_id", "parent"],
		)
		if clashing:
			row = clashing[0]
			frappe.throw(
				_("Clocking ID {0} is already overridden to Employee {1}.").format(row.clocking_id, row.parent)
			)

		# A code that already matches some Employee's own real
		# attendance_device_id would never actually reach this table (the
		# normal match always wins first) - almost certainly not what
		# whoever set this up meant to happen, so it's caught here rather
		# than silently doing nothing.
		direct_matches = frappe.get_all(
			"Employee", filters={"attendance_device_id": ["in", codes]}, fields=["name", "attendance_device_id"]
		)
		if direct_matches:
			row = direct_matches[0]
			frappe.throw(
				_(
					"Clocking ID {0} is already Employee {1}'s own attendance_device_id - it "
					"will always resolve there directly and would never reach this override."
				).format(row.attendance_device_id, row.name)
			)
