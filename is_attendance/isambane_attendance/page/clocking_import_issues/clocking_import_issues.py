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
  the same mechanism. Employee pickers can be set on as many rows as
  needed before resolving anything - resolve_employee_codes_bulk() then
  assigns every pair that has one set in a single action, rather than one
  round trip per code.
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
from frappe.utils.messages import clear_last_message

from is_attendance.controllers.clocking_import import (
	IMPORT_ISSUE_DOCTYPES,
	assign_employee_code,
	resync_drafts_for_codes,
)

PAGE_ROLES = {"System Manager", "HR Manager", "IR Manager"}

# Every real Clocking Import status, in the order the pipeline actually
# moves through them - used to give the status donut chart both a
# consistent slice order and a consistent colour per status regardless of
# which ones happen to have any documents right now. Keep in sync with
# is_attendance.controllers.clocking_import's own status lifecycle.
IMPORT_STATUS_ORDER = [
	"Not Parsed",
	"Missing Information",
	"Pending Import",
	"Importing",
	"Partially Imported",
	"Completed",
	"Error",
]


def _check_permission():
	if not PAGE_ROLES & set(frappe.get_roles()):
		frappe.throw(_("You are not permitted to view this page."), frappe.PermissionError)


@frappe.whitelist()
def get_summary_stats() -> dict:
	"""Headline numbers and chart data for the page's top summary - a
	single call rather than three, since all of it renders together on
	page load. status_counts is ordered per IMPORT_STATUS_ORDER (zero
	included for a status with no current documents) so the donut chart's
	slice order/colours never shuffle between refreshes."""
	_check_permission()

	# One count() per known status rather than a single GROUP BY - this
	# Frappe version's frappe.get_all() rejects a raw SQL aggregate string
	# in `fields` outright, and there are only 7 statuses to check.
	raw_status_counts = {status: frappe.db.count("Clocking Import", {"status": status}) for status in IMPORT_STATUS_ORDER}
	status_counts = [{"status": status, "count": raw_status_counts[status]} for status in IMPORT_STATUS_ORDER]
	total_imports = sum(raw_status_counts.values())

	total_employees = frappe.db.count("Employee", {"status": "Active"})
	employees_missing_code = frappe.db.count(
		"Employee", {"status": "Active", "attendance_device_id": ["in", ["", None]]}
	)

	total_machines = frappe.db.count("IS Attendance Clocking Machine")
	machines_missing_branch = frappe.db.count("IS Attendance Clocking Machine", {"branch": ["in", ["", None]]})

	return {
		"total_imports": total_imports,
		"status_counts": status_counts,
		"hanging_imports": raw_status_counts.get("Error", 0),
		"completed_imports": raw_status_counts.get("Completed", 0),
		"total_employees": total_employees,
		"employees_missing_code": employees_missing_code,
		"total_machines": total_machines,
		"machines_missing_branch": machines_missing_branch,
	}


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
def resolve_employee_codes_bulk(mappings) -> dict:
	"""Assigns every (employee_code, employee) pair in `mappings` -
	assign_employee_code does the same conflict-checked write
	apply_resolved_issues() does for a single document's own Issues row,
	just for as many codes at once as the page's own table has an
	Employee picked for. One pair's own conflict (a code already claimed
	by a different Employee) is caught and reported in `failed`, not
	allowed to abort the rest of the batch - the whole point of a bulk
	action is that one bad row shouldn't undo everything else that was
	genuinely fine.

	Only ONE resync_drafts_for_codes() call, across every code this batch
	actually resolved for the first time - not one per pair - so a
	document stuck on more than one of these codes only gets resaved
	once, not repeatedly.

	`mappings` is a list of {"employee_code": ..., "employee": ...} dicts
	(frappe.call sends it JSON-encoded, hence the isinstance check)."""
	_check_permission()

	if isinstance(mappings, str):
		mappings = frappe.parse_json(mappings)
	if not mappings:
		frappe.throw(_("Nothing to resolve - set at least one Employee first."))

	resolved = []
	failed = []
	newly_resolved_codes: set[str] = set()

	for mapping in mappings:
		employee_code = mapping.get("employee_code")
		employee = mapping.get("employee")
		if not employee_code or not employee:
			continue

		try:
			changed = assign_employee_code(employee_code, employee)
		except frappe.ValidationError as error:
			clear_last_message()  # this pair's own frappe.throw() - reported in `failed` below instead
			failed.append({"employee_code": employee_code, "employee": employee, "error": str(error)})
			continue

		resolved.append(employee_code)
		if changed:
			newly_resolved_codes.add(employee_code)

	resynced = resync_drafts_for_codes(newly_resolved_codes)

	return {"resolved": resolved, "failed": failed, "resynced_documents": resynced}


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
