# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Export Attendance data in a format compatible with importing into Sage
Premier (payroll).

``build_sage_premier_export`` is a deliberate placeholder - Sage Premier's
real import column order/delimiter/header spec hasn't been supplied yet.
It currently emits a plausible CSV (employee code, date, in/out time,
hours) so the page/permission/filter plumbing here is real and only the
column mapping needs replacing once the real spec is available.
"""

from __future__ import annotations

import csv
import io

import frappe
from frappe import _
from frappe.utils import now_datetime

EXPORT_ROLES = {"System Manager", "Payroll Manager", "Payroll User"}


@frappe.whitelist()
def export_sage_premier(filters=None):
	if not EXPORT_ROLES & set(frappe.get_roles()):
		frappe.throw(_("You are not permitted to export attendance data."), frappe.PermissionError)

	filters = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})

	from is_attendance.isambane_attendance.report.attendance_by_branch.attendance_by_branch import (
		get_data,
	)

	rows = get_data(filters)
	content = build_sage_premier_export(rows)

	frappe.response["filename"] = f"sage_premier_export_{now_datetime().strftime('%Y%m%d_%H%M%S')}.csv"
	frappe.response["filecontent"] = content
	frappe.response["type"] = "download"


def build_sage_premier_export(rows: list[dict]) -> str:
	"""
	**Placeholder format** - replace the column list/order/delimiter below
	once Sage Premier's actual clocking-import spec is available. For now
	this emits: employee code (Employee.employee_number), attendance date,
	in time, out time, working hours.
	"""
	employee_numbers = _employee_numbers([row["employee"] for row in rows if row.get("employee")])

	buffer = io.StringIO()
	writer = csv.writer(buffer)
	writer.writerow(["Employee Code", "Date", "In Time", "Out Time", "Hours"])

	for row in rows:
		writer.writerow(
			[
				employee_numbers.get(row.get("employee"), row.get("employee") or ""),
				row.get("attendance_date") or "",
				row.get("in_time") or "",
				row.get("out_time") or "",
				row.get("working_hours") or 0,
			]
		)

	return buffer.getvalue()


def _employee_numbers(employees: list[str]) -> dict[str, str]:
	if not employees:
		return {}
	rows = frappe.get_all(
		"Employee",
		filters={"name": ["in", list(set(employees))]},
		fields=["name", "employee_number"],
	)
	return {row.name: row.employee_number or row.name for row in rows}
