# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Clocking Import Issues - a central page for the two kinds of "clocking
data isn't quite right yet" gaps that used to only be visible one document
(or one machine record) at a time:

- **Unresolved employee codes**: every badge/PIN number that doesn't match
  any Employee.attendance_device_id, aggregated *across every Draft
  Clocking Import document* rather than shown per-document - the same
  code routinely shows up as an Issue on many separate daily import files
  (a whole month's worth, until someone maps it), so finding and fixing
  it from one place instead of hunting through documents one at a time is
  the entire point of this page. Resolving a code here does exactly what
  resolving it on any one document's own Issues table already does
  (assign_employee_code/resync_drafts_for_codes in
  is_attendance.controllers.clocking_import) - this page doesn't
  reimplement that, it's a different, doctype-agnostic entry point into
  the same mechanism.
- **Incomplete Clocking Machines**: every IS Attendance Clocking Machine
  record with no Branch set - the one piece of missing device
  information with a real functional consequence (see
  clocking_machine_branch_map()), so these can be filled in from one
  place instead of opening each device's own record to notice it's
  missing.
"""

from __future__ import annotations

import frappe
from frappe import _

from is_attendance.controllers.clocking_import import (
	IMPORT_ISSUE_DOCTYPES,
	assign_employee_code,
	resync_drafts_for_codes,
)

PAGE_ROLES = {"System Manager", "HR Manager", "HR User"}


def _check_permission():
	if not PAGE_ROLES & set(frappe.get_roles()):
		frappe.throw(_("You are not permitted to view this page."), frappe.PermissionError)


@frappe.whitelist()
def get_unresolved_codes() -> list[dict]:
	"""Every currently-unresolved employee code across every Draft
	Clocking * Import document, aggregated by code - so resolving one
	code here clears every document stuck on it in a single action,
	instead of the same code needing to be noticed and fixed separately
	on each one."""
	_check_permission()

	rows = []
	for doctype, issue_doctype in IMPORT_ISSUE_DOCTYPES.items():
		rows += frappe.get_all(
			issue_doctype,
			filters={"parenttype": doctype, "docstatus": 0},
			fields=["employee_code", "machine_id", "occurrence_count", "first_seen", "parent"],
		)

	grouped: dict[str, dict] = {}
	for row in rows:
		entry = grouped.setdefault(
			row.employee_code,
			{"machine_ids": set(), "occurrence_count": 0, "first_seen": None, "documents": set()},
		)
		if row.machine_id:
			entry["machine_ids"].add(row.machine_id)
		entry["occurrence_count"] += row.occurrence_count or 0
		entry["documents"].add(row.parent)
		if row.first_seen and (not entry["first_seen"] or row.first_seen < entry["first_seen"]):
			entry["first_seen"] = row.first_seen

	result = [
		{
			"employee_code": code,
			"machine_ids": ", ".join(sorted(entry["machine_ids"])),
			"occurrence_count": entry["occurrence_count"],
			"document_count": len(entry["documents"]),
			"first_seen": entry["first_seen"],
		}
		for code, entry in grouped.items()
	]
	result.sort(key=lambda r: r["occurrence_count"], reverse=True)
	return result


@frappe.whitelist()
def resolve_employee_code(employee_code: str, employee: str) -> dict:
	"""Assigns `employee_code` to `employee` (assign_employee_code - same
	conflict-checked write apply_resolved_issues() does from a single
	document's own Issues row) and immediately propagates it to every
	Draft import document currently stuck on that code
	(resync_drafts_for_codes) - the equivalent, from this page, of
	resolving it on one document and having resync_other_drafts() ripple
	it out to the rest, just without needing to open any document at all
	to trigger it."""
	_check_permission()

	if not employee_code or not employee:
		frappe.throw(_("Employee Code and Employee are both required."))

	changed = assign_employee_code(employee_code, employee)
	resynced = resync_drafts_for_codes({employee_code}) if changed else []

	return {"changed": changed, "resynced_documents": resynced}


@frappe.whitelist()
def get_incomplete_machines() -> list[dict]:
	"""Every IS Attendance Clocking Machine with no Branch set yet."""
	_check_permission()

	return frappe.get_all(
		"IS Attendance Clocking Machine",
		filters={"branch": ["in", ["", None]]},
		fields=["name", "machine_id", "brand", "model", "location_notes"],
		order_by="name asc",
	)


@frappe.whitelist()
def set_machine_branch(machine_id: str, branch: str) -> None:
	"""Sets Branch on one IS Attendance Clocking Machine record directly -
	the one field this page surfaces inline, rather than requiring a trip
	to the device's own form for just this."""
	_check_permission()

	if not machine_id or not branch:
		frappe.throw(_("Clocking Machine and Branch are both required."))

	if not frappe.db.exists("IS Attendance Clocking Machine", machine_id):
		frappe.throw(_("Clocking Machine {0} not found.").format(machine_id))

	frappe.db.set_value("IS Attendance Clocking Machine", machine_id, "branch", branch)
