// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

frappe.pages["attendance-export"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Attendance Export"),
		single_column: true,
	});

	new is_attendance.AttendanceExport(page);
};

frappe.provide("is_attendance");

is_attendance.AttendanceExport = class AttendanceExport {
	constructor(page) {
		this.page = page;
		this.make_filters();

		$(`<p class="text-muted" style="margin: 15px 0;">
			${__(
				"Exports Attendance rows for the selected filters in a format compatible with Sage Premier import."
			)}
		</p>`).appendTo(this.page.main);

		this.page.set_primary_action(__("Export"), () => this.export(), "download");
	}

	make_filters() {
		this.filters = {};

		this.filters.company = this.page.add_field({
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
		});

		this.filters.branch = this.page.add_field({
			fieldname: "branch",
			label: __("Branch"),
			fieldtype: "Link",
			options: "Branch",
		});

		this.filters.from_date = this.page.add_field({
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
			reqd: 1,
		});

		this.filters.to_date = this.page.add_field({
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		});
	}

	get_filter_values() {
		const values = {};
		for (const key of Object.keys(this.filters)) {
			values[key] = this.filters[key].get_value();
		}
		return values;
	}

	export() {
		open_url_post(frappe.request.url, {
			cmd: "is_attendance.isambane_attendance.page.attendance_export.attendance_export.export_sage_premier",
			filters: JSON.stringify(this.get_filter_values()),
		});
	}
};
