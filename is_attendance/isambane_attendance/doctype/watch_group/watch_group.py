# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Names a list of Employees someone wants to keep an eye on and a list of
Users who should hear about them, on up to three independently-toggleable
cadences (Send Daily/Weekly/Monthly all live on the same record - want
both a daily and a weekly digest of the same people? tick both boxes here
rather than creating two groups). Each cadence's own *range* (Daily
Range/Weekly Range/Monthly Range) is a separate, independent choice from
how often it sends - a Daily send can still cover the previous calendar
month, refreshed every day, or a Monthly send can cover just the last 7
days. The actual sending happens in
is_attendance.controllers.watch_group_digest, on a schedule (see
hooks.py's scheduler_events) - this doctype only holds the configuration.

Employee Schedule (one row per Employee per day of week) and each
Employee's own Time Buffer (on the Employees table) are what let a
digest's Late In/Early Out/Missed flags reflect that employee's *actual*
expected hours that specific day, rather than one fixed window for
everyone - Saturday can differ from a weekday, Sunday can be a Day Off
(excluded entirely, not just loosely thresholded) for one employee while
someone else genuinely works it. This is more granular than
attendance_compliance_summary.compute() can express (that report uses one
global Start Time/End Time/Threshold for every employee, every day - see
its own module docstring) - watch_group_digest.py doesn't call compute()
for this reason, it reuses the same lower-level primitives
(_classify_day, holiday/leave lookups) directly, per employee per day.

Also usable as a `watch_group` filter on the Attendance Compliance
Summary report and the Attendance Dashboard (see that report's own
_resolve_employees()) - restricts either view to just this group's own
Employees, independent of whether any digest is ever sent for it. That
filter use only narrows *which employees* show; it doesn't pull in this
group's own per-employee schedule/buffer (the report has no per-employee
threshold concept to receive it into) - the digest is the only place
those actually take effect.
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

		self._check_duplicate_schedule_rows()

	def _check_duplicate_schedule_rows(self):
		seen = set()
		for row in self.employee_schedules:
			key = (row.employee, row.day_of_week)
			if key in seen:
				frappe.throw(
					_("Row {0}: {1} already has a {2} row above - only one Start/End Time per employee per day.").format(
						row.idx, row.employee, row.day_of_week
					)
				)
			seen.add(key)
