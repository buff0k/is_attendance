// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

frappe.ui.form.on("IS Attendance User Branch", {
	user(frm, cdt, cdn) {
		const row = locals[cdt][cdn];

		if (!row.user) {
			frappe.model.set_value(cdt, cdn, "email_address", "");
			return;
		}

		frappe.db.get_value("User", row.user, "email").then((r) => {
			const email = r && r.message ? r.message.email : null;
			frappe.model.set_value(cdt, cdn, "email_address", email || "");
		});
	},
});
