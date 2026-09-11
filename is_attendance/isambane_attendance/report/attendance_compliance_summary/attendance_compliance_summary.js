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
			fieldname: "watch_group",
			label: __("Watch Group"),
			fieldtype: "Link",
			options: "Watch Group",
			description: __("Restricts to this group's own Employees - combines with Employees/Branch/etc. above, doesn't replace them."),
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
			// Employee.za_occupational_level (za_local's own Employment
			// Equity Act field) - same options list, reused directly rather
			// than a second classification.
			fieldname: "occupational_level",
			label: __("Occupational Level"),
			fieldtype: "Select",
			options: "\nTop Management\nSenior Management\nProfessionally Qualified\nSkilled Technical\nSemi-Skilled\nUnskilled\nTemporary Employees\nNon-Permanent",
		},
		{
			fieldname: "include_inactive",
			label: __("Include Inactive Employees"),
			fieldtype: "Check",
			default: 0,
		},
		{
			fieldname: "exclude_weekends",
			label: __("Exclude Weekends from Calculations"),
			fieldtype: "Check",
			default: 0,
			description: __("Saturdays/Sundays still show and still count toward the Total Saturdays/Sundays columns - they just never trigger Missed/No Out/No In/Late In/Early Out."),
		},
		{
			fieldname: "exclude_public_holidays",
			label: __("Exclude Public Holidays from Calculations"),
			fieldtype: "Check",
			default: 1,
			description: __("On by default (matches this report's long-standing behaviour) - a public holiday still shows and still counts toward its Total Weekdays/Saturdays/Sundays column, it just never triggers Missed/No Out/No In/Late In/Early Out. Uncheck to treat a public holiday as a normal day for those flags."),
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
