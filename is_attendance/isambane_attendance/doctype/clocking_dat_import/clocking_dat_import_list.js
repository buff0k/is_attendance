// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

frappe.listview_settings["Clocking DAT Import"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const status_map = {
			"Not Parsed": "grey",
			"Missing Information": "orange",
			"Pending Import": "yellow",
			Importing: "blue",
			Completed: "green",
			Error: "red",
		};
		const color = status_map[doc.status] || "grey";
		return [__(doc.status), color, "status,=," + doc.status];
	},
};
