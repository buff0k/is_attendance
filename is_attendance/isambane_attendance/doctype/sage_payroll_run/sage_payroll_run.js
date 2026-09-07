// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

frappe.ui.form.on("Sage Payroll Run", {
	refresh(frm) {
		if (frm.doc.docstatus !== 0) {
			return;
		}

		if (frm.doc.status === "Draft" || frm.doc.status === "Pull Requested") {
			frm.add_custom_button(
				frm.doc.status === "Pull Requested" ? __("Request Employee Pull Again") : __("Request Employee Pull"),
				() => {
					frappe.confirm(
						__(
							"Marks this Run as waiting for the Windows-host heartbeat (sage_employee_puller.py) to pull this Company's employees from Sage on its next poll - nothing happens here directly. Continue?"
						),
						() => {
							frm.call("request_employee_pull").then(() => frm.reload_doc());
						}
					);
				}
			);
		}

		if (frm.doc.status === "Employees Loaded" || frm.doc.status === "Computed") {
			frm.add_custom_button(__("Compute Hours & Leave"), () => {
				frappe.confirm(
					__(
						"Recomputes Normal / Overtime 1.5 / Leave hours for every Matched employee on this Run from Employee Checkin and Leave Application data. Continue?"
					),
					() => {
						frm.call("compute_hours_and_leave").then(() => frm.reload_doc());
					}
				);
			});
		}

		if (frm.doc.status === "Computed" || frm.doc.status === "Exported") {
			frm.add_custom_button(__("Export .txt"), () => {
				open_url_post(frappe.request.url, {
					cmd: "run_doc_method",
					dt: frm.doc.doctype,
					dn: frm.doc.name,
					method: "export_txt",
				});
				// export_txt() itself flips Status to "Exported" server-side -
				// reload shortly after so the form catches up without the
				// user needing to refresh by hand.
				setTimeout(() => frm.reload_doc(), 2000);
			});
		}

		if (frm.doc.status === "Pull Requested") {
			frm.dashboard.set_headline_alert(
				__(
					"Waiting for the Windows-host heartbeat to pick this up (requested {0}). If this sits here a long time, check sage_employee_puller.py is actually running on the Windows host.",
					[frm.doc.pull_requested_at ? frappe.datetime.str_to_user(frm.doc.pull_requested_at) : __("just now")]
				),
				"orange"
			);
		} else if (frm.doc.status === "Draft" && !frm.doc.employee_count) {
			frm.dashboard.set_headline_alert(
				__('No employees ingested yet - click "Request Employee Pull" above.'),
				"orange"
			);
		}
	},
});
