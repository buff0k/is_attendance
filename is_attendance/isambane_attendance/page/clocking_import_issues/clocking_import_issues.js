// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

/*
 * Central page for the two "clocking data isn't quite right yet" gaps
 * that used to only be visible one Clocking Import document (or one
 * Clocking Machine record) at a time - see clocking_import_issues.py's
 * own module docstring for the full reasoning.
 *
 * Layout, top to bottom: a stat-card summary row, three donut charts
 * (frappe.Chart - Frappe's own built-in charting library, already loaded
 * desk-wide, no extra bundling needed), then the two actionable tables -
 * on separate tabs rather than stacked on top of each other, since the
 * Employee Codes table can run to hundreds of rows and burying the much
 * shorter Machines table below all of that made it awkward to reach. The
 * markup uses standard Bootstrap nav-tabs classes/attributes for styling
 * and accessibility, but Desk does NOT actually wire up Bootstrap's own
 * automatic data-toggle="tab" plugin (confirmed against frappe/form/tab.js
 * - even Frappe's own internal Form Tabs manually wires a click handler
 * rather than relying on it) - wire_tabs() below does the same, switching
 * the active nav-link and tab-pane by hand on click.
 *
 * Each table row carries an inline Link control
 * (frappe.ui.form.make_control). The Employee Codes table supports
 * setting several rows' Employee before resolving anything - a picked
 * Employee just sits in that row's own control until "Resolve Selected"
 * is clicked, which resolves every row that currently has one set in a
 * single batch call (resolve_employee_codes_bulk), rather than one round
 * trip per row.
 */

const CII_STATUS_COLORS = {
	"Not Parsed": "#94a3b8",
	"Missing Information": "#f59e0b",
	"Pending Import": "#3b82f6",
	Importing: "#6366f1",
	"Partially Imported": "#fb923c",
	Completed: "#22c55e",
	Error: "#ef4444",
};

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
		cii_ensure_style();
		this.make_body();
		this.run();
	}

	make_body() {
		this.$body = $(`
			<div class="cii-page">
				<div class="cii-stats-row"></div>
				<div class="cii-charts-row"></div>

				<ul class="nav nav-tabs cii-tabs" role="tablist">
					<li class="nav-item">
						<a class="nav-link active" data-toggle="tab" href="#cii-tab-codes" role="tab">
							${__("Unresolved Employee Codes")} <span class="cii-tab-count cii-codes-count"></span>
						</a>
					</li>
					<li class="nav-item">
						<a class="nav-link" data-toggle="tab" href="#cii-tab-machines" role="tab">
							${__("Clocking Machines Missing a Branch")} <span class="cii-tab-count cii-machines-count"></span>
						</a>
					</li>
				</ul>

				<div class="tab-content">
					<div class="tab-pane active" id="cii-tab-codes" role="tabpanel">
						<div class="cii-section">
							<div class="cii-section-header cii-section-header-with-action">
								<div>
									<p class="text-muted">${__(
										"Every badge/PIN number in a Draft Clocking Import that doesn't match any Employee - resolving one here clears it from every document stuck on it, not just one. Set as many Employees below as you like, then resolve them all in one go."
									)}</p>
								</div>
								<button class="btn btn-sm btn-primary cii-resolve-selected-btn">${__("Resolve Selected")}</button>
							</div>
							<div class="cii-codes-search-row">
								<input type="text" class="form-control input-sm cii-code-search" placeholder="${__(
									"Search Employee Code..."
								)}">
								<span class="text-muted cii-codes-search-count"></span>
							</div>
							<div class="cii-codes-table cii-table-wrap"></div>
						</div>
					</div>
					<div class="tab-pane" id="cii-tab-machines" role="tabpanel">
						<div class="cii-section">
							<div class="cii-section-header">
								<p class="text-muted">${__(
									"A device with no Branch set contributes nothing to Branch resolution during import - fill it in here, or leave it blank to keep relying on the employee's own Branch."
								)}</p>
							</div>
							<div class="cii-machines-table cii-table-wrap"></div>
						</div>
					</div>
				</div>
			</div>
		`).appendTo(this.page.main);

		this.$stats_row = this.$body.find(".cii-stats-row");
		this.$charts_row = this.$body.find(".cii-charts-row");
		this.$codes_count = this.$body.find(".cii-codes-count");
		this.$machines_count = this.$body.find(".cii-machines-count");
		this.$codes_table = this.$body.find(".cii-codes-table");
		this.$machines_table = this.$body.find(".cii-machines-table");
		this.$codes_search = this.$body.find(".cii-code-search");
		this.$codes_search_count = this.$body.find(".cii-codes-search-count");

		this.employee_controls = [];
		this.$body.find(".cii-resolve-selected-btn").on("click", () => this.resolve_selected());
		// Static markup, not re-rendered by render_codes() (which would wipe
		// the typed value and this listener) - wired once here, applied
		// again at the end of every render_codes() so an active search term
		// survives a refresh (e.g. right after resolving a batch).
		this.$codes_search.on("input", () => this.filter_codes_table());
		this.wire_tabs();
	}

	filter_codes_table() {
		const term = (this.$codes_search.val() || "").trim().toLowerCase();
		const $rows = this.$codes_table.find("tbody tr");
		let shown = 0;

		$rows.each((_, el) => {
			const $row = $(el);
			// .attr(), not .data() - jQuery's .data() auto-converts a
			// numeric-looking attribute value (most real employee codes
			// have no leading zero, e.g. "2003") into an actual JS number,
			// and .toLowerCase() on a number throws - silently killing this
			// whole .each() on the very first such row, which is exactly
			// why typing here did nothing at all.
			const code = $row.attr("data-employee-code") || "";
			const match = !term || code.toLowerCase().includes(term);
			$row.toggle(match);
			if (match) shown += 1;
		});

		this.$codes_search_count.text(term ? __("{0} of {1} shown", [shown, $rows.length]) : "");
	}

	wire_tabs() {
		// Desk doesn't actually activate Bootstrap's own automatic
		// data-toggle="tab" plugin (see the module comment up top) - switch
		// the active nav-link and tab-pane by hand instead.
		this.$body.find(".cii-tabs .nav-link").on("click", (event) => {
			event.preventDefault();
			const $link = $(event.currentTarget);
			if ($link.hasClass("active")) return;

			this.$body.find(".cii-tabs .nav-link").removeClass("active");
			$link.addClass("active");

			const target = $link.attr("href");
			this.$body.find(".tab-content > .tab-pane").removeClass("active");
			this.$body.find(target).addClass("active");
		});
	}

	resolve_selected() {
		const mappings = this.employee_controls
			.map(({ employee_code, control }) => ({ employee_code, employee: control.get_value() }))
			.filter((row) => row.employee);

		if (!mappings.length) {
			frappe.msgprint(__("Set at least one Employee first."));
			return;
		}

		frappe.call({
			method: "is_attendance.isambane_attendance.page.clocking_import_issues.clocking_import_issues.resolve_employee_codes_bulk",
			args: { mappings: mappings },
			freeze: true,
			freeze_message: __("Resolving {0} code(s)...", [mappings.length]),
			callback: (r) => {
				const result = r.message || {};
				const resolved = result.resolved || [];
				const failed = result.failed || [];

				// The Employee.attendance_device_id writes above are
				// already done by the time this callback runs, but
				// resyncing every affected Draft document (a full re-save
				// each - see resolve_employee_codes_bulk's own docstring)
				// is queued as a background job, not done inline - so
				// those documents won't reflect the resolution
				// immediately. The run() below still shows accurate
				// current state; it just may not have caught up yet for a
				// few seconds on a large batch.
				frappe.show_alert({
					message: failed.length
						? __("Resolved {0} code(s) - {1} failed. Affected documents are resyncing in the background.", [
								resolved.length,
								failed.length,
						  ])
						: __("Resolved {0} code(s) - affected documents are resyncing in the background.", [resolved.length]),
					indicator: failed.length ? "orange" : "green",
				});

				if (failed.length) {
					const rows = failed
						.map((f) => `<li>${frappe.utils.escape_html(f.employee_code)}: ${frappe.utils.escape_html(f.error)}</li>`)
						.join("");
					frappe.msgprint({
						title: __("Some codes could not be resolved"),
						indicator: "orange",
						message: `<ul>${rows}</ul>`,
					});
				}

				this.run();
			},
		});
	}

	run() {
		frappe.call({
			method: "is_attendance.isambane_attendance.page.clocking_import_issues.clocking_import_issues.get_summary_stats",
			callback: (r) => this.render_stats(r.message || {}),
		});
		frappe.call({
			method: "is_attendance.isambane_attendance.page.clocking_import_issues.clocking_import_issues.get_unresolved_codes",
			callback: (r) => this.render_codes(r.message || []),
		});
		frappe.call({
			method: "is_attendance.isambane_attendance.page.clocking_import_issues.clocking_import_issues.get_incomplete_machines",
			callback: (r) => this.render_machines(r.message || []),
		});
	}

	render_stats(stats) {
		const cards = [
			{ label: __("Total Imports"), value: stats.total_imports, tone: "neutral" },
			{ label: __("Hanging Imports"), value: stats.hanging_imports, tone: stats.hanging_imports ? "bad" : "good" },
			{ label: __("Completed Imports"), value: stats.completed_imports, tone: "good" },
			{
				label: __("Employees Missing a Clocking ID"),
				value: stats.employees_missing_code,
				sub: __("of {0} active", [stats.total_employees]),
				tone: stats.employees_missing_code ? "bad" : "good",
			},
			{
				label: __("Machines Missing a Branch"),
				value: stats.machines_missing_branch,
				sub: __("of {0} known", [stats.total_machines]),
				tone: stats.machines_missing_branch ? "bad" : "good",
			},
		];

		this.$stats_row.html(
			cards
				.map(
					(card) => `
						<div class="cii-stat-card cii-tone-${card.tone}">
							<div class="cii-stat-value">${card.value ?? 0}</div>
							<div class="cii-stat-label">${card.label}</div>
							${card.sub ? `<div class="cii-stat-sub">${card.sub}</div>` : ""}
						</div>
					`
				)
				.join("")
		);

		this.render_charts(stats);
	}

	render_charts(stats) {
		this.$charts_row.html(`
			<div class="cii-chart-card">
				<div class="cii-chart-title">${__("Import Status")}</div>
				<div id="cii-chart-status"></div>
			</div>
			<div class="cii-chart-card">
				<div class="cii-chart-title">${__("Employee Clocking ID Coverage")}</div>
				<div id="cii-chart-employees"></div>
			</div>
			<div class="cii-chart-card">
				<div class="cii-chart-title">${__("Clocking Machine Branch Coverage")}</div>
				<div id="cii-chart-machines"></div>
			</div>
		`);

		const status_counts = (stats.status_counts || []).filter((row) => row.count > 0);
		cii_render_donut(
			"#cii-chart-status",
			status_counts.map((row) => __(row.status)),
			status_counts.map((row) => row.count),
			status_counts.map((row) => CII_STATUS_COLORS[row.status] || "#94a3b8"),
			stats.total_imports
		);

		const employees_mapped = (stats.total_employees || 0) - (stats.employees_missing_code || 0);
		cii_render_donut(
			"#cii-chart-employees",
			[__("Has Clocking ID"), __("Missing")],
			[employees_mapped, stats.employees_missing_code || 0],
			["#22c55e", "#ef4444"],
			stats.total_employees
		);

		const machines_with_branch = (stats.total_machines || 0) - (stats.machines_missing_branch || 0);
		cii_render_donut(
			"#cii-chart-machines",
			[__("Has Branch"), __("Missing")],
			[machines_with_branch, stats.machines_missing_branch || 0],
			["#22c55e", "#ef4444"],
			stats.total_machines
		);
	}

	render_codes(rows) {
		this.$codes_count.text(rows.length);
		this.employee_controls = [];

		if (!rows.length) {
			this.$codes_table.html(`<div class="cii-empty">${__("No unresolved employee codes - every Draft Clocking Import fully resolves.")}</div>`);
			this.$codes_search_count.text("");
			return;
		}

		this.$codes_table.html(`
			<table class="cii-table cii-table-fixed">
				<thead>
					<tr>
						<th class="cii-col-code">${__("Employee Code")}</th>
						<th class="cii-col-narrow">${__("Occurrences")}</th>
						<th class="cii-col-narrow">${__("Documents")}</th>
						<th class="cii-col-date">${__("First Seen")}</th>
						<th class="cii-col-truncate">${__("Machine ID(s)")}</th>
						<th class="cii-col-employee">${__("Employee")}</th>
					</tr>
				</thead>
				<tbody></tbody>
			</table>
		`);

		const $tbody = this.$codes_table.find("tbody");

		rows.forEach((row) => {
			const $tr = $(`
				<tr data-employee-code="${frappe.utils.escape_html(row.employee_code)}">
					<td><span class="cii-code-pill">${frappe.utils.escape_html(row.employee_code)}</span></td>
					<td><span class="cii-badge">${row.occurrence_count}</span></td>
					<td>${row.document_count}</td>
					<td class="text-muted">${row.first_seen ? frappe.datetime.str_to_user(row.first_seen) : ""}</td>
					<td class="text-muted cii-col-truncate" title="${frappe.utils.escape_html(row.machine_ids || "")}">${frappe.utils.escape_html(row.machine_ids || "")}</td>
					<td class="employee-cell"></td>
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

			// Not resolved on its own - just remembered here so
			// "Resolve Selected" can pick up every row that has an
			// Employee set, whenever it's clicked (see resolve_selected()).
			this.employee_controls.push({ employee_code: row.employee_code, control: employee_control });
		});

		// Re-applies whatever search term is already in the box (e.g. still
		// there from before a refresh) to the freshly-rendered rows.
		this.filter_codes_table();
	}

	render_machines(rows) {
		this.$machines_count.text(rows.length);

		if (!rows.length) {
			this.$machines_table.html(`<div class="cii-empty">${__("No Clocking Machines are missing a Branch.")}</div>`);
			return;
		}

		this.$machines_table.html(`
			<table class="cii-table cii-table-fixed">
				<thead>
					<tr>
						<th class="cii-col-truncate">${__("Machine ID")}</th>
						<th class="cii-col-narrow">${__("Brand")}</th>
						<th class="cii-col-narrow">${__("Model")}</th>
						<th class="cii-col-truncate">${__("Location / Notes")}</th>
						<th class="cii-col-employee">${__("Branch")}</th>
						<th class="cii-col-action"></th>
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
					<td class="cii-col-truncate" title="${frappe.utils.escape_html(row.machine_id)}"><a href="${href}" target="_blank">${frappe.utils.escape_html(row.machine_id)}</a></td>
					<td class="text-muted">${frappe.utils.escape_html(row.brand || "")}</td>
					<td class="text-muted">${frappe.utils.escape_html(row.model || "")}</td>
					<td class="text-muted cii-col-truncate" title="${frappe.utils.escape_html(row.location_notes || "")}">${frappe.utils.escape_html(row.location_notes || "")}</td>
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

function cii_render_donut(selector, labels, values, colors, total) {
	const $container = $(selector);
	if (!total) {
		$container.html(`<div class="cii-chart-empty">${__("No data yet")}</div>`);
		return;
	}

	new frappe.Chart(selector, {
		type: "donut",
		height: 200,
		data: { labels: labels, datasets: [{ values: values }] },
		colors: colors,
		maxSlices: labels.length,
		tooltipOptions: {
			formatTooltipY: (value) => `${value} (${Math.round((value / total) * 100)}%)`,
		},
	});
}

function cii_ensure_style() {
	if (document.getElementById("cii-style")) return;

	const style = document.createElement("style");
	style.id = "cii-style";
	style.textContent = `
		.cii-page { padding-bottom: 20px; }

		.cii-stats-row {
			display: grid;
			grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
			gap: 12px;
			margin: 15px 0 20px;
		}
		.cii-stat-card {
			background: var(--card-bg, #fff);
			border: 1px solid var(--border-color, #d1d8dd);
			border-radius: 8px;
			padding: 16px;
			text-align: center;
			border-top: 3px solid var(--border-color, #d1d8dd);
		}
		.cii-stat-card.cii-tone-good { border-top-color: #22c55e; }
		.cii-stat-card.cii-tone-bad { border-top-color: #ef4444; }
		.cii-stat-value { font-size: 28px; font-weight: 700; line-height: 1.2; }
		.cii-tone-good .cii-stat-value { color: #16a34a; }
		.cii-tone-bad .cii-stat-value { color: #dc2626; }
		.cii-stat-label { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
		.cii-stat-sub { font-size: 11px; color: var(--text-light, #aaa); margin-top: 2px; }

		.cii-charts-row {
			display: grid;
			grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
			gap: 12px;
			margin-bottom: 30px;
		}
		.cii-chart-card {
			background: var(--card-bg, #fff);
			border: 1px solid var(--border-color, #d1d8dd);
			border-radius: 8px;
			padding: 12px 16px;
		}
		.cii-chart-title { font-size: 13px; font-weight: 600; color: var(--text-muted); margin-bottom: 4px; }
		.cii-chart-empty {
			height: 200px;
			display: flex;
			align-items: center;
			justify-content: center;
			color: var(--text-muted);
			font-size: 12px;
		}

		.cii-tabs {
			border-bottom-color: var(--border-color, #d1d8dd);
			margin-bottom: 0;
		}
		/* Explicit rather than relying on Bootstrap's own .tab-pane CSS
		   being present in Desk's bundle as-is - wire_tabs() switches which
		   pane has .active by hand (see its own comment), this is what
		   actually shows/hides them. */
		.cii-page .tab-pane { display: none; }
		.cii-page .tab-pane.active { display: block; }
		.cii-tabs .nav-link {
			color: var(--text-muted);
			border: none;
			border-bottom: 2px solid transparent;
			padding: 10px 4px;
			margin-right: 24px;
			font-weight: 600;
		}
		.cii-tabs .nav-link.active {
			color: var(--text-color, #1f272e);
			background: none;
			border-bottom-color: var(--primary-color, #2490ef);
		}
		.cii-tab-count {
			display: inline-block;
			min-width: 20px;
			text-align: center;
			background: var(--control-bg, #f4f5f6);
			border-radius: 10px;
			padding: 0 6px;
			font-size: 11px;
			font-weight: 700;
		}

		.tab-content .cii-section {
			border-top: none;
			border-top-left-radius: 0;
			border-top-right-radius: 0;
		}
		.cii-section {
			background: var(--card-bg, #fff);
			border: 1px solid var(--border-color, #d1d8dd);
			border-radius: 8px;
			margin-bottom: 20px;
			overflow: hidden;
		}
		.cii-section-header {
			padding: 14px 18px 10px;
			border-bottom: 1px solid var(--border-color, #d1d8dd);
		}
		.cii-section-header-with-action {
			display: flex;
			align-items: flex-start;
			justify-content: space-between;
			gap: 16px;
		}
		.cii-section-header-with-action .cii-resolve-selected-btn { flex-shrink: 0; }
		.cii-section-header h4 { margin: 0 0 4px; }
		.cii-section-header p { margin: 0; font-size: 12px; }

		.cii-codes-search-row {
			display: flex;
			align-items: center;
			gap: 10px;
			padding: 10px 18px;
			border-bottom: 1px solid var(--border-color, #d1d8dd);
		}
		.cii-code-search { max-width: 260px; }
		.cii-codes-search-count { font-size: 12px; white-space: nowrap; }

		.cii-table-wrap { overflow-x: auto; width: 100%; max-width: 100%; min-width: 0; }
		.cii-empty { padding: 24px 18px; color: var(--text-muted); font-size: 13px; }

		.cii-table { width: 100%; max-width: 100%; border-collapse: collapse; font-size: 13px; }
		/* table-layout: fixed is the actual fix here - without it, a single
		   long unbroken value (many machines on one code, a long free-text
		   note) forces its own column past its share of the table's width,
		   and the table as a whole silently renders wider than its own
		   100% - which on real data (more machines per code, longer notes
		   than this app's own lab-bench test data ever had) is exactly
		   what pushed the whole page into a horizontal scroll rather than
		   staying contained inside .cii-table-wrap's own overflow-x. Fixed
		   layout makes column widths predictable (driven by the header
		   row's own CSS, not content), so the table can never exceed its
		   own width regardless of what a row happens to contain - long
		   content in a .cii-col-truncate cell is truncated with an
		   ellipsis (full value still in its title attribute) instead. */
		.cii-table-fixed { table-layout: fixed; }
		.cii-table th {
			text-align: left;
			font-weight: 600;
			font-size: 11px;
			text-transform: uppercase;
			letter-spacing: 0.03em;
			color: var(--text-muted);
			padding: 10px 14px;
			border-bottom: 1px solid var(--border-color, #d1d8dd);
			white-space: nowrap;
			overflow: hidden;
			text-overflow: ellipsis;
		}
		.cii-table td {
			padding: 8px 14px;
			border-bottom: 1px solid var(--border-color, #eef1f2);
			vertical-align: middle;
			overflow-wrap: break-word;
		}
		.cii-table tbody tr:hover { background: var(--control-bg, #f4f5f6); }
		.cii-table tbody tr:last-child td { border-bottom: none; }
		.cii-col-code { width: 130px; }
		.cii-col-narrow { width: 90px; }
		.cii-col-date { width: 130px; }
		.cii-col-employee { width: 220px; }
		.cii-col-action { width: 90px; }
		.cii-col-truncate {
			max-width: 0; /* with table-layout: fixed, forces this column to respect its computed share instead of growing to fit content */
			overflow: hidden;
			text-overflow: ellipsis;
			white-space: nowrap;
		}

		.cii-code-pill {
			display: inline-block;
			font-family: var(--font-mono, monospace);
			background: var(--control-bg, #f4f5f6);
			border: 1px solid var(--border-color, #d1d8dd);
			border-radius: 4px;
			padding: 1px 8px;
			font-size: 12px;
		}
		.cii-badge {
			display: inline-block;
			min-width: 24px;
			text-align: center;
			background: #fff3e0;
			color: #b45309;
			border-radius: 10px;
			padding: 1px 8px;
			font-size: 12px;
			font-weight: 600;
		}
	`;
	document.head.appendChild(style);
}
