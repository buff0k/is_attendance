# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Names a list of Employees someone wants to keep an eye on and a list of
Users who should hear about them, on up to three independently-toggleable
cadences (Send Daily/Weekly/Monthly all live on the same record - want
both a daily and a weekly digest of the same people? tick both boxes here
rather than creating two groups). The actual sending happens in
is_attendance.controllers.watch_group_digest, on a schedule (see
hooks.py's scheduler_events) - this doctype only holds the configuration.

Also usable as a `watch_group` filter on the Attendance Compliance
Summary report and the Attendance Dashboard (see that report's own
_resolve_employees()) - restricts either view to just this group's own
Employees, independent of whether any digest is ever sent for it.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class WatchGroup(Document):
	def validate(self):
		if not (self.send_daily or self.send_weekly or self.send_monthly):
			frappe.msgprint(
				_(
					"No cadence is ticked (Send Daily/Weekly/Monthly) - this group won't send any digest until "
					"at least one is on. Still usable as a Watch Group filter on the report/Dashboard in the "
					"meantime."
				),
				indicator="orange",
				alert=True,
			)
