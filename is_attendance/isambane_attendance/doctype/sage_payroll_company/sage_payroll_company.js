// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

const IS_ATTENDANCE_HOURLY_AMT_DEFAULTS = {
	amt_1_source: "Normal Hours",
	amt_2_source: "Overtime 1.5",
	amt_3_source: "Overtime 2",
	amt_4_source: "Leave Hours - Annual",
	amt_5_source: "Leave Hours - Sick",
	amt_6_source: "Leave Hours - Family",
};

frappe.ui.form.on("Sage Payroll Company", {
	payroll_type(frm) {
		// Convenience prefill only, and only on a brand-new, still-blank
		// record - matches the real Bankfontein sheet's own column layout
		// (Normal/OT1.5/OT2/A-Leave/S-Leave/F-Leave). Monthly gets no
		// prefill at all - no sample sheet exists to verify a default
		// against, so leaving it blank is safer than guessing (see the
		// doctype's own Batch Column Layout help text).
		if (frm.doc.payroll_type !== "Hourly" || !frm.is_new()) {
			return;
		}

		const already_set = Object.keys(IS_ATTENDANCE_HOURLY_AMT_DEFAULTS).some(
			(fieldname) => frm.doc[fieldname] && frm.doc[fieldname] !== "Blank/Unused"
		);
		if (already_set) {
			return;
		}

		Object.entries(IS_ATTENDANCE_HOURLY_AMT_DEFAULTS).forEach(([fieldname, value]) => {
			frm.set_value(fieldname, value);
		});
	},
});
