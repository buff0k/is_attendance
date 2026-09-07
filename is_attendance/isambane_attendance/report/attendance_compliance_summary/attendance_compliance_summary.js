// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

frappe.query_reports["Attendance Compliance Summary"] = {
	filters: [
		{
			fieldname: "employees",
			label: __("Employees"),
			fieldtype: "MultiSelectList",
			options: "Employee",
			get_data: function (txt) {
				return frappe.db.get_link_options("Employee", txt);
			},
		},
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
		},
		{
			fieldname: "branch",
			label: __("Branch"),
			fieldtype: "Link",
			options: "Branch",
		},
		{
			fieldname: "department",
			label: __("Department"),
			fieldtype: "Link",
			options: "Department",
		},
		{
			fieldname: "payroll_cost_center",
			label: __("Payroll Cost Center"),
			fieldtype: "Link",
			options: "Cost Center",
		},
		{
			fieldname: "include_inactive",
			label: __("Include Inactive Employees"),
			fieldtype: "Check",
			default: 0,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.month_start(),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "start_time",
			label: __("Start Time"),
			fieldtype: "Time",
			default: "06:00:00",
			description: __("Arriving after Start Time + Threshold counts as Late In."),
		},
		{
			fieldname: "end_time",
			label: __("End Time"),
			fieldtype: "Time",
			default: "16:00:00",
			description: __("Leaving before End Time - Threshold counts as Early Out."),
		},
		{
			fieldname: "threshold",
			label: __("Threshold (minutes)"),
			fieldtype: "Int",
			default: 15,
		},
	],
};
