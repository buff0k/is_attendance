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

		this.filters.department = this.page.add_field({
			fieldname: "department",
			label: __("Department"),
			fieldtype: "Link",
			options: "Department",
			change: () => this.run(),
		});

		this.filters.payroll_cost_center = this.page.add_field({
			fieldname: "payroll_cost_center",
			label: __("Payroll Cost Center"),
			fieldtype: "Link",
			options: "Cost Center",
			change: () => this.run(),
		});

		this.filters.include_inactive = this.page.add_field({
			fieldname: "include_inactive",
			label: __("Include Inactive Employees"),
			fieldtype: "Check",
			default: 0,
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

		// Reset on every full re-render (filters changed, Refresh clicked) -
		// a stale employee's cached daily detail from a previous period/
		// filter set must never be shown under a new set of results.
		this._detail_cache = {};

		const metric_labels = {
			total_missed: __("Missed"),
			total_no_out: __("No Out"),
			total_no_in: __("No In"),
			total_late_in: __("Late In"),
			total_early_out: __("Early Out"),
		};
		const metric_keys = Object.keys(metric_labels);
		const colspan = metric_keys.length + 5;

		const header = `
			<th style="width: 24px;"></th>
			<th>${__("Employee")}</th>
			<th>${__("Employee Name")}</th>
			<th>${__("Branch")}</th>
			${metric_keys.map((key) => `<th>${metric_labels[key]}</th>`).join("")}
			<th></th>
		`;

		const body = rows
			.map((row) => {
				const employee = frappe.utils.escape_html(row.employee ?? "");
				return `
					<tr class="summary-row" data-employee="${employee}">
						<td class="drill-toggle" style="cursor: pointer;">&#9656;</td>
						<td>${employee}</td>
						<td>${frappe.utils.escape_html(row.employee_name ?? "")}</td>
						<td>${frappe.utils.escape_html(row.branch ?? "")}</td>
						${metric_keys.map((key) => `<td>${row[key] ?? 0}</td>`).join("")}
						<td><button class="btn btn-xs btn-default create-adjustment-btn">${__("Create Clocking Adjustment")}</button></td>
					</tr>
					<tr class="detail-row" data-employee="${employee}" style="display: none;">
						<td colspan="${colspan}"><div class="detail-container text-muted">${__("Loading...")}</div></td>
					</tr>
				`;
			})
			.join("");

		this.$table.html(`
			<table class="table table-bordered table-sm" style="white-space: nowrap;">
				<thead><tr>${header}</tr></thead>
				<tbody>${body || `<tr><td colspan="${colspan}" class="text-muted text-center">${__("No records")}</td></tr>`}</tbody>
			</table>
		`);

		this.wire_table_events();
	}

	wire_table_events() {
		this.$table.off("click", ".drill-toggle").on("click", ".drill-toggle", (event) => {
			const $row = $(event.currentTarget).closest("tr");
			this.toggle_detail($row.data("employee"), $row);
		});

		this.$table.off("click", ".create-adjustment-btn").on("click", ".create-adjustment-btn", (event) => {
			const $row = $(event.currentTarget).closest("tr");
			this.create_adjustment($row.data("employee"));
		});
	}

	toggle_detail(employee, $summary_row) {
		const $detail_row = $summary_row.next(".detail-row");
		const $toggle = $summary_row.find(".drill-toggle");

		if ($detail_row.is(":visible")) {
			$detail_row.hide();
			$toggle.html("&#9656;");
			return;
		}

		$detail_row.show();
		$toggle.html("&#9662;");

		if (this._detail_cache[employee]) {
			this.render_detail($detail_row, this._detail_cache[employee]);
			return;
		}

		frappe.call({
			method: "is_attendance.isambane_attendance.page.attendance_dashboard.attendance_dashboard.get_daily_detail",
			args: { filters: JSON.stringify(this.get_filter_values()), employee },
			callback: (r) => {
				this._detail_cache[employee] = r.message || [];
				this.render_detail($detail_row, this._detail_cache[employee]);
			},
		});
	}

	render_detail($detail_row, day_rows) {
		const $container = $detail_row.find(".detail-container");

		if (!day_rows.length) {
			$container.html(`<span class="text-muted">${__("No days in range.")}</span>`);
			return;
		}

		const flag_labels = {
			missed: __("Missed"),
			no_out: __("No Out"),
			no_in: __("No In"),
			late_in: __("Late In"),
			early_out: __("Early Out"),
		};

		const rows_html = day_rows
			.map((day) => {
				const flags = Object.keys(flag_labels)
					.filter((key) => day[key])
					.map((key) => `<span class="indicator-pill red" style="margin-right: 4px;">${flag_labels[key]}</span>`)
					.join("");
				const holiday = day.public_holiday
					? `<span class="indicator-pill blue" style="margin-right: 4px;">${frappe.utils.escape_html(day.public_holiday)}</span>`
					: "";
				const on_leave = day.on_leave
					? `<span class="indicator-pill green" style="margin-right: 4px;">${__("On Leave")}</span>`
					: "";
				const half_day_leave = day.half_day_leave
					? `<span class="indicator-pill orange" style="margin-right: 4px;">${__("Half Day Leave")}</span>`
					: "";

				return `
					<tr>
						<td>${day.date ? frappe.datetime.str_to_user(day.date) : ""}</td>
						<td>${frappe.utils.escape_html(day.day || "")}</td>
						<td>${day.in_time ? frappe.datetime.str_to_user(day.in_time, true) : ""}</td>
						<td>${day.out_time ? frappe.datetime.str_to_user(day.out_time, true) : ""}</td>
						<td>${(day.hours_worked || 0).toFixed(2)}</td>
						<td>${flags}${holiday}${on_leave}${half_day_leave}</td>
					</tr>
				`;
			})
			.join("");

		$container.html(`
			<table class="table table-bordered table-sm" style="margin: 0;">
				<thead>
					<tr>
						<th>${__("Date")}</th>
						<th>${__("Day")}</th>
						<th>${__("In")}</th>
						<th>${__("Out")}</th>
						<th>${__("Hours Worked")}</th>
						<th>${__("Flags")}</th>
					</tr>
				</thead>
				<tbody>${rows_html}</tbody>
			</table>
		`);
	}

	create_adjustment(employee) {
		const filters = this.get_filter_values();
		frappe.new_doc("Clocking Adjustment", {
			employee: employee,
			from_date: filters.from_date,
			to_date: filters.to_date,
		});
	}

	export_excel() {
		open_url_post(frappe.request.url, {
			cmd: "is_attendance.isambane_attendance.page.attendance_dashboard.attendance_dashboard.export_compliance_excel",
			filters: JSON.stringify(this.get_filter_values()),
		});
	}
};
