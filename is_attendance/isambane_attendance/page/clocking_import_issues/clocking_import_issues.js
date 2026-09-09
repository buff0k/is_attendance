// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

/*
 * Central page for the two "clocking data isn't quite right yet" gaps
 * that used to only be visible one Clocking Import document (or one
 * Clocking Machine record) at a time - see clocking_import_issues.py's
 * own module docstring for the full reasoning. Each table row carries an
 * inline Link control (frappe.ui.form.make_control) so resolving an
 * Employee Code or filling in a Branch never requires leaving this page.
 */

frappe.pages["clocking-import-issues"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Clocking Import Issues"),
		single_column: true,
	});

	new is_attendance.ClockingImportIssues(page);
};

frappe.provide("is_attendance");

is_attendance.ClockingImportIssues = class ClockingImportIssues {
	constructor(page) {
		this.page = page;
		this.page.set_primary_action(__("Refresh"), () => this.run(), "refresh");
		this.make_body();
		this.run();
	}

	make_body() {
		this.$body = $(`
			<div class="clocking-issues">
				<div class="clocking-issues-section" style="margin-bottom: 30px;">
					<h4>${__("Unresolved Employee Codes")}</h4>
					<p class="text-muted">${__(
						"Every badge/PIN number in a Draft Clocking Import that doesn't match any Employee - resolving one here clears it from every document stuck on it, not just one."
					)}</p>
					<div class="clocking-issues-codes-table" style="overflow-x: auto;"></div>
				</div>
				<div class="clocking-issues-section">
					<h4>${__("Clocking Machines Missing a Branch")}</h4>
					<p class="text-muted">${__(
						"A device with no Branch set contributes nothing to Branch resolution during import - fill it in here, or leave it blank to keep relying on the employee's own Branch."
					)}</p>
					<div class="clocking-issues-machines-table" style="overflow-x: auto;"></div>
				</div>
			</div>
		`).appendTo(this.page.main);

		this.$codes_table = this.$body.find(".clocking-issues-codes-table");
		this.$machines_table = this.$body.find(".clocking-issues-machines-table");
	}

	run() {
		frappe.call({
			method: "is_attendance.isambane_attendance.page.clocking_import_issues.clocking_import_issues.get_unresolved_codes",
			callback: (r) => this.render_codes(r.message || []),
		});
		frappe.call({
			method: "is_attendance.isambane_attendance.page.clocking_import_issues.clocking_import_issues.get_incomplete_machines",
			callback: (r) => this.render_machines(r.message || []),
		});
	}

	render_codes(rows) {
		if (!rows.length) {
			this.$codes_table.html(`<p class="text-muted">${__("No unresolved employee codes - every Draft Clocking Import fully resolves.")}</p>`);
			return;
		}

		this.$codes_table.html(`
			<table class="table table-bordered table-sm">
				<thead>
					<tr>
						<th>${__("Employee Code")}</th>
						<th>${__("Occurrences")}</th>
						<th>${__("Documents")}</th>
						<th>${__("First Seen")}</th>
						<th>${__("Machine ID(s)")}</th>
						<th style="width: 220px;">${__("Employee")}</th>
						<th style="width: 90px;"></th>
					</tr>
				</thead>
				<tbody></tbody>
			</table>
		`);

		const $tbody = this.$codes_table.find("tbody");

		rows.forEach((row) => {
			const $tr = $(`
				<tr>
					<td>${frappe.utils.escape_html(row.employee_code)}</td>
					<td>${row.occurrence_count}</td>
					<td>${row.document_count}</td>
					<td>${row.first_seen ? frappe.datetime.str_to_user(row.first_seen) : ""}</td>
					<td>${frappe.utils.escape_html(row.machine_ids || "")}</td>
					<td class="employee-cell"></td>
					<td><button class="btn btn-xs btn-primary resolve-btn">${__("Resolve")}</button></td>
				</tr>
			`).appendTo($tbody);

			const employee_control = frappe.ui.form.make_control({
				parent: $tr.find(".employee-cell")[0],
				df: {
					fieldtype: "Link",
					options: "Employee",
					fieldname: "employee",
					placeholder: __("Select Employee"),
					// Only an Employee with no Clocking ID of their own yet -
					// picking one that already has a different code assigned
					// would silently fail server-side (assign_employee_code's
					// own conflict check), but worse, picking one whose code
					// happens to already be THIS SAME resolved value is a
					// trap the dropdown shouldn't even offer: excluding every
					// Employee who already has any attendance_device_id set
					// means every option shown here is actually safe to pick.
					get_query: () => ({
						filters: [["attendance_device_id", "in", ["", null]]],
					}),
				},
				render_input: true,
			});
			employee_control.refresh();

			$tr.find(".resolve-btn").on("click", () => {
				const employee = employee_control.get_value();
				if (!employee) {
					frappe.msgprint(__("Select an Employee first."));
					return;
				}

				frappe.call({
					method: "is_attendance.isambane_attendance.page.clocking_import_issues.clocking_import_issues.resolve_employee_code",
					args: { employee_code: row.employee_code, employee: employee },
					freeze: true,
					callback: (r) => {
						const resynced = (r.message && r.message.resynced_documents) || [];
						frappe.show_alert({
							message: resynced.length
								? __("Resolved - {0} document(s) updated.", [resynced.length])
								: __("Already resolved - nothing to update."),
							indicator: "green",
						});
						this.run();
					},
				});
			});
		});
	}

	render_machines(rows) {
		if (!rows.length) {
			this.$machines_table.html(`<p class="text-muted">${__("No Clocking Machines are missing a Branch.")}</p>`);
			return;
		}

		this.$machines_table.html(`
			<table class="table table-bordered table-sm">
				<thead>
					<tr>
						<th>${__("Machine ID")}</th>
						<th>${__("Brand")}</th>
						<th>${__("Model")}</th>
						<th>${__("Location / Notes")}</th>
						<th style="width: 220px;">${__("Branch")}</th>
						<th style="width: 90px;"></th>
					</tr>
				</thead>
				<tbody></tbody>
			</table>
		`);

		const $tbody = this.$machines_table.find("tbody");

		rows.forEach((row) => {
			const href = `/app/is-attendance-clocking-machine/${encodeURIComponent(row.name)}`;
			const $tr = $(`
				<tr>
					<td><a href="${href}" target="_blank">${frappe.utils.escape_html(row.machine_id)}</a></td>
					<td>${frappe.utils.escape_html(row.brand || "")}</td>
					<td>${frappe.utils.escape_html(row.model || "")}</td>
					<td>${frappe.utils.escape_html(row.location_notes || "")}</td>
					<td class="branch-cell"></td>
					<td><button class="btn btn-xs btn-primary save-branch-btn">${__("Save")}</button></td>
				</tr>
			`).appendTo($tbody);

			const branch_control = frappe.ui.form.make_control({
				parent: $tr.find(".branch-cell")[0],
				df: {
					fieldtype: "Link",
					options: "Branch",
					fieldname: "branch",
					placeholder: __("Select Branch"),
				},
				render_input: true,
			});
			branch_control.refresh();

			$tr.find(".save-branch-btn").on("click", () => {
				const branch = branch_control.get_value();
				if (!branch) {
					frappe.msgprint(__("Select a Branch first."));
					return;
				}

				frappe.call({
					method: "is_attendance.isambane_attendance.page.clocking_import_issues.clocking_import_issues.set_machine_branch",
					args: { machine_id: row.name, branch: branch },
					freeze: true,
					callback: () => {
						frappe.show_alert({ message: __("Branch saved."), indicator: "green" });
						this.run();
					},
				});
			});
		});
	}
};
