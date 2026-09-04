// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

const REPORT_NAME = "Attendance Compliance Summary";

frappe.pages["attendance-dashboard"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Attendance Dashboard"),
		single_column: true,
	});

	new is_attendance.AttendanceDashboard(page);
};

frappe.provide("is_attendance");

is_attendance.AttendanceDashboard = class AttendanceDashboard {
	constructor(page) {
		this.page = page;

		// The Table MultiSelect control reads its linked child doctype's
		// meta synchronously (frappe.get_meta) - on a real document form
		// that's always pre-loaded before the form renders, but a bare
		// Page never fetches it, so creating the field before this
		// resolves throws and aborts the rest of the page's setup. Load it
		// explicitly first.
		frappe.model.with_doctype("Attendance Dashboard Employee", () => {
			this.make_filters();
			this.make_body();
			this.run();
		});
	}

	make_filters() {
		this.filters = {};

		this.filters.employees = this.page.add_field({
			fieldname: "employees",
			label: __("Employees"),
			fieldtype: "Table MultiSelect",
			options: "Attendance Dashboard Employee",
			change: () => this.run(),
		});

		this.filters.company = this.page.add_field({
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			change: () => this.run(),
		});

		this.filters.branch = this.page.add_field({
			fieldname: "branch",
			label: __("Branch"),
			fieldtype: "Link",
			options: "Branch",
			change: () => this.run(),
		});

		this.filters.from_date = this.page.add_field({
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
			change: () => this.run(),
		});

		this.filters.to_date = this.page.add_field({
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			change: () => this.run(),
		});

		this.filters.start_time = this.page.add_field({
			fieldname: "start_time",
			label: __("Start Time"),
			fieldtype: "Time",
			default: "06:00:00",
			change: () => this.run(),
		});

		this.filters.end_time = this.page.add_field({
			fieldname: "end_time",
			label: __("End Time"),
			fieldtype: "Time",
			default: "16:00:00",
			change: () => this.run(),
		});

		this.filters.threshold = this.page.add_field({
			fieldname: "threshold",
			label: __("Threshold (min)"),
			fieldtype: "Int",
			default: 15,
			change: () => this.run(),
		});

		this.page.set_primary_action(__("Refresh"), () => this.run(), "refresh");
		this.page.add_inner_button(__("Export to Excel"), () => this.export_excel());
	}

	make_body() {
		this.$body = $(`
			<div class="attendance-dashboard">
				<div class="attendance-summary row" style="margin: 15px 0;"></div>
				<div class="attendance-table" style="margin-top: 15px; overflow-x: auto;"></div>
			</div>
		`).appendTo(this.page.main);

		this.$summary = this.$body.find(".attendance-summary");
		this.$table = this.$body.find(".attendance-table");
	}

	get_filter_values() {
		const values = {};
		for (const key of Object.keys(this.filters)) {
			values[key] = this.filters[key].get_value();
		}
		// Table MultiSelect's value is an array of child rows ({employee:
		// "..."}), not plain employee names - the report/export filters
		// want the names.
		values.employees = (values.employees || []).map((row) => row.employee);
		return values;
	}

	run() {
		const filters = this.get_filter_values();
		if (!filters.from_date || !filters.to_date) return;

		frappe.call({
			method: "frappe.desk.query_report.run",
			args: {
				report_name: REPORT_NAME,
				filters: filters,
			},
			callback: (r) => this.render(r.message),
		});
	}

	render(message) {
		this.columns = (message && message.columns) || [];
		this.rows = (message && message.result) || [];
		this.render_summary(this.rows);
		this.render_table(this.columns, this.rows);
	}

	render_summary(rows) {
		const totals = { missed: 0, no_out: 0, no_in: 0, late_in: 0, early_out: 0 };
		const day_types = ["weekday", "saturday", "sunday"];

		for (const row of rows) {
			for (const metric of Object.keys(totals)) {
				for (const day_type of day_types) {
					totals[metric] += row[`${day_type}_${metric}`] || 0;
				}
			}
		}

		const labels = {
			missed: __("Missed"),
			no_out: __("No Out"),
			no_in: __("No In"),
			late_in: __("Late In"),
			early_out: __("Early Out"),
		};

		this.$summary.empty();
		for (const metric of Object.keys(totals)) {
			$(`
				<div class="col-sm-2">
					<div class="frappe-card" style="padding: 15px; text-align: center;">
						<div style="font-size: 22px; font-weight: bold;">${totals[metric]}</div>
						<div class="text-muted">${labels[metric]}</div>
					</div>
				</div>
			`).appendTo(this.$summary);
		}
	}

	render_table(columns, rows) {
		if (!columns.length) {
			this.$table.html(`<p class="text-muted">${__("No data for the selected filters.")}</p>`);
			return;
		}

		const header = columns.map((c) => `<th>${frappe.utils.escape_html(c.label)}</th>`).join("");
		const body = rows
			.map(
				(row) =>
					`<tr>${columns
						.map((c) => `<td>${frappe.utils.escape_html(row[c.fieldname] ?? "")}</td>`)
						.join("")}</tr>`
			)
			.join("");

		this.$table.html(`
			<table class="table table-bordered table-sm" style="white-space: nowrap;">
				<thead><tr>${header}</tr></thead>
				<tbody>${body || `<tr><td colspan="${columns.length}" class="text-muted text-center">${__("No records")}</td></tr>`}</tbody>
			</table>
		`);
	}

	export_excel() {
		open_url_post(frappe.request.url, {
			cmd: "is_attendance.isambane_attendance.page.attendance_dashboard.attendance_dashboard.export_compliance_excel",
			filters: JSON.stringify(this.get_filter_values()),
		});
	}
};
