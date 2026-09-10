// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

const IS_ATTENDANCE_POLL_INTERVAL_MS = 5000;
const IS_ATTENDANCE_POLL_MAX_TICKS = 360; // ~30 minutes of polling before giving up

frappe.ui.form.on("Clocking Import", {
	refresh(frm) {
		is_attendance.clocking_import.stop_polling(frm);

		if (frm.doc.docstatus !== 0) {
			return;
		}

		// The real import always runs in the background (see the .py module
		// docstring) - never rely on the native Submit button here, it would
		// block the request until the whole file is processed and can time
		// out on a large one. before_submit() also refuses to run it inline
		// as a second line of defense, but we don't want the user to ever
		// hit that - so replace the primary action entirely.
		//
		// Shows whenever there's at least one row ready to import, not just
		// once everything resolves - "Missing Information" and "Partially
		// Imported" both allow it now (see the .py module docstring): a file
		// only ever refuses to run when resolvable_count is 0. "Error" is
		// included too - render_status() below has always told the user to
		// "fix the issue and click Start Import again" for that status, but
		// the button itself was never actually offered there, so that
		// instruction couldn't be followed. resolvable_count still holds
		// whatever queue_import() last computed right before the failed
		// attempt (run_import_job()'s own except handler reloads the
		// document rather than leaving it in some half-updated in-memory
		// state, but doesn't touch resolvable_count), so this condition
		// works the same way for Error as it already does for the others.
		const can_start_import =
			["Pending Import", "Missing Information", "Partially Imported", "Error"].includes(frm.doc.status) &&
			frm.doc.resolvable_count;

		if (can_start_import) {
			const is_full_run = frm.doc.status === "Pending Import";
			frm.page.set_primary_action(__("Start Import"), () => {
				frappe.confirm(
					is_full_run
						? __(
								"This runs in the background and will create Employee Checkin records once done. Continue?"
						  )
						: __(
								"{0} of {1} rows are ready to import now - the rest will stay unresolved for a later run. This runs in the background. Continue?",
								[frm.doc.resolvable_count, frm.doc.total_rows]
						  ),
					() => {
						frm.call("queue_import").then(() => {
							frm.reload_doc();
						});
					}
				);
			});
		}

		if (frm.doc.file) {
			frm.add_custom_button(__("Check File for Issues"), () => frm.save());
		}

		is_attendance.clocking_import.render_status(frm);

		if (frm.doc.status === "Importing") {
			is_attendance.clocking_import.start_polling(frm);
		}
	},
});

frappe.provide("is_attendance.clocking_import");

is_attendance.clocking_import.render_status = function (frm) {
	const status = frm.doc.status;

	if (status === "Missing Information") {
		const parts = [];
		if (frm.doc.unresolved_count) {
			parts.push(
				__("{0} employee code(s) could not be matched to an Employee (see Issues below)", [
					frm.doc.unresolved_count,
				])
			);
		}
		if (frm.doc.employees_without_branch) {
			parts.push(
				__("these employees have no Branch and no Fallback Branch is set: {0}", [
					frm.doc.employees_without_branch,
				])
			);
		}
		// A file no longer has to be 100% resolved before anything imports -
		// resolvable_count says whether Start Import will actually do
		// something right now, or whether nothing here is importable yet.
		const readiness = frm.doc.resolvable_count
			? __("{0} of {1} rows are ready to import now - click Start Import to bring those in.", [
					frm.doc.resolvable_count,
					frm.doc.total_rows,
			  ])
			: __("Nothing in this file can be imported yet.");
		frm.dashboard.set_headline_alert(
			`<div class="row"><div class="col-xs-12">${__("Missing information - {0}. {1}", [
				parts.join("; "),
				readiness,
			])}</div></div>`,
			"orange"
		);
	} else if (status === "Partially Imported") {
		frm.dashboard.set_headline_alert(
			`<div class="row"><div class="col-xs-12">${__(
				"Partially imported - {0} Employee Checkin(s) created so far. {1} employee code(s) are still unresolved (see Issues below); map them and click Start Import again to bring in the rest.",
				[(frm.doc.created_checkins || []).length, frm.doc.unresolved_count]
			)}</div></div>`,
			"orange"
		);
	} else if (status === "Pending Import") {
		frm.dashboard.set_headline_alert(
			`<div class="row"><div class="col-xs-12">${__(
				"Every row resolves. Click Start Import to create the Employee Checkins in the background."
			)}</div></div>`,
			"green"
		);
	} else if (status === "Importing") {
		frm.dashboard.set_headline_alert(
			`<div class="row"><div class="col-xs-12">${__(
				"Importing in the background - this page will refresh automatically. This document will be submitted automatically once it finishes."
			)}</div></div>`,
			"blue"
		);
	} else if (status === "Error") {
		frm.dashboard.set_headline_alert(
			`<div class="row"><div class="col-xs-12">${__(
				"The last import attempt failed - see the Import Log below for details. Fix the issue and click Start Import again."
			)}</div></div>`,
			"red"
		);
	}
};

is_attendance.clocking_import.start_polling = function (frm) {
	let ticks = 0;
	const docname = frm.doc.name;

	frm._is_attendance_poll = setInterval(() => {
		ticks += 1;

		if (ticks > IS_ATTENDANCE_POLL_MAX_TICKS || cur_frm !== frm || frm.docname !== docname) {
			is_attendance.clocking_import.stop_polling(frm);
			return;
		}

		frappe.db.get_value("Clocking Import", docname, ["status", "docstatus"]).then((r) => {
			const values = r.message || {};
			if (values.status !== frm.doc.status || values.docstatus !== frm.doc.docstatus) {
				frm.reload_doc();
			}
		});
	}, IS_ATTENDANCE_POLL_INTERVAL_MS);
};

is_attendance.clocking_import.stop_polling = function (frm) {
	if (frm._is_attendance_poll) {
		clearInterval(frm._is_attendance_poll);
		frm._is_attendance_poll = null;
	}
};
