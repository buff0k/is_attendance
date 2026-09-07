# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _

from is_attendance.permissions import responsible_branches_for_user


def execute(filters=None):
	columns = get_columns()
	data = get_data(filters or {})
	return columns, data


def get_columns():
	return [
		{"label": _("Employee"), "fieldname": "employee", "fieldtype": "Link", "options": "Employee", "width": 120},
		{"label": _("Employee Name"), "fieldname": "employee_name", "fieldtype": "Data", "width": 160},
		{"label": _("Branch"), "fieldname": "branch", "fieldtype": "Link", "options": "Branch", "width": 120},
		{"label": _("Company"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 150},
		{"label": _("Attendance Date"), "fieldname": "attendance_date", "fieldtype": "Date", "width": 110},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 90},
		{"label": _("In Time"), "fieldname": "in_time", "fieldtype": "Datetime", "width": 150},
		{"label": _("Out Time"), "fieldname": "out_time", "fieldtype": "Datetime", "width": 150},
		{"label": _("Working Hours"), "fieldname": "working_hours", "fieldtype": "Float", "width": 110},
	]


def get_data(filters: dict) -> list[dict]:
	conditions, values = _build_conditions(filters)

	# Script Reports don't automatically inherit permission_query_conditions/
	# has_permission - apply the same branch restriction ourselves so an HR
	# clerk can't see other branches through this report even though they
	# can't see the underlying Employee Checkins. See is_attendance.permissions.
	branches = responsible_branches_for_user()
	if branches:
		placeholders = ", ".join(["%s"] * len(branches))
		conditions.append(f"`tabEmployee`.`branch` IN ({placeholders})")
		values.extend(branches)

	where_clause = " and ".join(conditions) if conditions else "1=1"

	return frappe.db.sql(
		f"""
		SELECT
			`tabAttendance`.`employee` AS employee,
			`tabAttendance`.`employee_name` AS employee_name,
			`tabEmployee`.`branch` AS branch,
			`tabAttendance`.`company` AS company,
			`tabAttendance`.`attendance_date` AS attendance_date,
			`tabAttendance`.`status` AS status,
			`tabAttendance`.`in_time` AS in_time,
			`tabAttendance`.`out_time` AS out_time,
			`tabAttendance`.`working_hours` AS working_hours
		FROM `tabAttendance`
		INNER JOIN `tabEmployee` ON `tabEmployee`.`name` = `tabAttendance`.`employee`
		WHERE {where_clause}
		ORDER BY `tabAttendance`.`attendance_date` DESC, `tabAttendance`.`employee_name` ASC
		""",
		values,
		as_dict=True,
	)


def _build_conditions(filters: dict) -> tuple[list[str], list]:
	conditions = ["`tabAttendance`.`docstatus` < 2"]
	values: list = []

	if filters.get("company"):
		conditions.append("`tabAttendance`.`company` = %s")
		values.append(filters["company"])

	if filters.get("branch"):
		conditions.append("`tabEmployee`.`branch` = %s")
		values.append(filters["branch"])

	if filters.get("employee"):
		conditions.append("`tabAttendance`.`employee` = %s")
		values.append(filters["employee"])

	if filters.get("department"):
		conditions.append("`tabEmployee`.`department` = %s")
		values.append(filters["department"])

	if filters.get("payroll_cost_center"):
		conditions.append("`tabEmployee`.`payroll_cost_center` = %s")
		values.append(filters["payroll_cost_center"])

	if not filters.get("include_inactive"):
		conditions.append("`tabEmployee`.`status` = 'Active'")

	if filters.get("from_date"):
		conditions.append("`tabAttendance`.`attendance_date` >= %s")
		values.append(filters["from_date"])

	if filters.get("to_date"):
		conditions.append("`tabAttendance`.`attendance_date` <= %s")
		values.append(filters["to_date"])

	return conditions, values
