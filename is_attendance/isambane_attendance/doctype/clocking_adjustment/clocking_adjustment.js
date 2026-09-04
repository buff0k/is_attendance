// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

frappe.ui.form.on("Clocking Adjustment", {
	setup(frm) {
		frm.set_query("reference_checkin", () => ({
			filters: frm.doc.employee ? { employee: frm.doc.employee } : {},
		}));
	},
});
