// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

frappe.query_reports["Sage Payroll Control"] = {
	filters: [
		{
			fieldname: "sage_payroll_run",
			label: __("Sage Payroll Run"),
			fieldtype: "Link",
			options: "Sage Payroll Run",
			reqd: 1,
		},
	],
};
