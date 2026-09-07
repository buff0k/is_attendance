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
		if (frm.doc.status === "Pending Import") {
			frm.page.set_primary_action(__("Start Import"), () => {
				frappe.confirm(
					__(
						"This runs in the background and will create Employee Checkin records once done. Continue?"
					),
					() => {
						frm.call("queue_import").then(() => {
							frm.reload_doc();
						});
					}
				);
			});
		} else if (frm.doc.file) {
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
		frm.dashboard.set_headline_alert(
			`<div class="row"><div class="col-xs-12">${__(
				"Missing information - {0}. Resolve this, then save.",
				[parts.join("; ")]
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
