// Copyright (c) 2026, BuFf0k and contributors
// For license information, please see license.txt

/*
 * "Connected" summary, right on the list view - a controller is Connected
 * if it's heartbeated within the last 3 minutes (well past any reasonable
 * heartbeat_interval_seconds, which defaults to 60), Stale if it's been
 * longer than that but under 15 minutes, and Disconnected beyond that (or
 * Never Connected if last_heartbeat is still blank). Computed client-side
 * from last_heartbeat rather than a stored status field, so it's always
 * current the moment this list is opened - no background job keeping a
 * separate "status" column in sync.
 */

const CII_CONNECTED_THRESHOLD_MINUTES = 3;
const CII_STALE_THRESHOLD_MINUTES = 15;

frappe.listview_settings["Sage Remote Controller"] = {
	add_fields: ["last_heartbeat", "enabled"],
	get_indicator(doc) {
		if (!doc.enabled) {
			return [__("Disabled"), "grey", "enabled,=,0"];
		}
		if (!doc.last_heartbeat) {
			return [__("Never Connected"), "grey", "last_heartbeat,is,not set"];
		}

		const minutes_ago = frappe.datetime.get_minute_diff(frappe.datetime.now_datetime(), doc.last_heartbeat);
		if (minutes_ago <= CII_CONNECTED_THRESHOLD_MINUTES) {
			return [__("Connected"), "green", "last_heartbeat,>=," + doc.last_heartbeat];
		}
		if (minutes_ago <= CII_STALE_THRESHOLD_MINUTES) {
			return [__("Stale"), "orange", "last_heartbeat,>=," + doc.last_heartbeat];
		}
		return [__("Disconnected"), "red", "last_heartbeat,>=," + doc.last_heartbeat];
	},
};
