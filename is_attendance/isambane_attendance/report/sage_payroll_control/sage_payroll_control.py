# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Reconciliation check for a Sage Payroll Run: every Active Employee at one
of the Run's own Branches (via its Sage Payroll Company's Paypoint
mapping) that did NOT come through as a Matched Sage Payroll Employee row.
These are people who plausibly should be in this payroll but aren't -
a missing attendance_device_id mapping, a new hire not yet loaded into
Sage, or a genuine gap. Deliberately doesn't special-case Senior/Executive
Management - they're outside Sage/VIP by design and simply never show up
in the ingested list, so a plain Sage-vs-Frappe cross-check naturally
lists them here too if they're Active at one of the Run's branches; that's
expected, not a bug in this report.
"""

from __future__ import annotations

import frappe
from frappe import _

from is_attendance.permissions import responsible_branches_for_user


def execute(filters=None):
	filters = filters or {}
	columns = get_columns()
	data = get_data(filters)
	return columns, data


def get_columns() -> list[dict]:
	return [
		{"label": _("Employee"), "fieldname": "employee", "fieldtype": "Link", "options": "Employee", "width": 100},
		{"label": _("Employee Name"), "fieldname": "employee_name", "fieldtype": "Data", "width": 180},
		{"label": _("Branch"), "fieldname": "branch", "fieldtype": "Link", "options": "Branch", "width": 120},
		{
			"label": _("Attendance Device ID"),
			"fieldname": "attendance_device_id",
			"fieldtype": "Data",
			"width": 140,
		},
	]


def get_data(filters: dict) -> list[dict]:
	run_name = filters.get("sage_payroll_run")
	if not run_name:
		frappe.throw(_("Sage Payroll Run is required."))

	run_doc = frappe.get_doc("Sage Payroll Run", run_name)
	company = frappe.get_doc("Sage Payroll Company", run_doc.sage_payroll_company)
	branches = [row.branch for row in company.paypoints if row.branch]
	if not branches:
		return []

	responsible = responsible_branches_for_user()
	if responsible:
		branches = [branch for branch in branches if branch in responsible]
	if not branches:
		return []

	employees = frappe.get_all(
		"Employee",
		filters={"status": "Active", "company": run_doc.company, "branch": ["in", branches]},
		fields=["name", "employee_name", "branch", "attendance_device_id"],
	)

	matched_employees = set(
		frappe.get_all(
			"Sage Payroll Employee",
			filters={"sage_payroll_run": run_name, "match_status": "Matched"},
			pluck="employee",
		)
	)

	return [
		{
			"employee": employee.name,
			"employee_name": employee.employee_name,
			"branch": employee.branch,
			"attendance_device_id": employee.attendance_device_id,
		}
		for employee in employees
		if employee.name not in matched_employees
	]
