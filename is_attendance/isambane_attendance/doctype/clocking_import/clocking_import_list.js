// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

frappe.listview_settings["Clocking Import"] = {
	add_fields: ["status"],
	// Without these two, frappe.get_indicator() (frappe/public/js/frappe/
	// model/indicator.js) hard-codes docstatus 0 -> "Draft" and docstatus
	// 2 -> "Cancelled" BEFORE it ever calls get_indicator() below - every
	// status except Completed sits at docstatus 0 (this doctype only
	// reaches docstatus 1 once truly Completed), so every one of them was
	// being shown as a plain "Draft", regardless of its real status. This
	// opts back into using get_indicator() for those rows too.
	has_indicator_for_draft: true,
	has_indicator_for_cancelled: true,
	get_indicator(doc) {
		// A cancelled document's own `status` field still literally reads
		// "Completed" (nothing resets it on cancel) - checked first so
		// this shows as Cancelled, not a misleading green "Completed".
		if (doc.docstatus === 2) {
			return [__("Cancelled"), "red", "docstatus,=,2"];
		}

		const status_map = {
			"Not Parsed": "grey",
			"Missing Information": "orange",
			"Pending Import": "yellow",
			Importing: "blue",
			"Partially Imported": "orange",
			Completed: "green",
			Error: "red",
		};
		const color = status_map[doc.status] || "grey";
		return [__(doc.status), color, "status,=," + doc.status];
	},
};
