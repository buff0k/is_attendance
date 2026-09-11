# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Watch Group scheduled digests - a Watch Group (see that doctype's own
module docstring) names a list of Employees and a list of User recipients
on up to three independently-toggleable cadences. Each cadence's own
scheduled entry point below (see hooks.py's scheduler_events - registered
under the standard "daily"/"weekly"/"monthly" keys, not a custom cron)
finds every enabled Watch Group with that cadence switched on, computes
the exact same attendance_compliance_summary.compute() the report/
Dashboard themselves use (scoped to just that group's own Employees, over
that cadence's own period), and emails every recipient a short HTML
summary with the full per-employee Excel workbook
(attendance_dashboard._build_workbook - the identical file "Export to
Excel" produces) attached.

Period per cadence - all trailing, ending yesterday rather than "today",
so a run firing at any time of day always reports on fully-closed days,
never a partial one still accumulating checkins:

- Daily: yesterday only.
- Weekly: the 7 days ending yesterday.
- Monthly: the full previous calendar month (1st to last day) - the more
  standard "monthly report" boundary, not a trailing 30 days.

One group's own failure (a bad recipient, a transient email error) is
logged and doesn't stop the rest of that cadence's run - same reasoning
daily_sync_attendance's own per-employee try/except uses.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO

import frappe
from frappe import _
from frappe.utils import add_days, get_first_day, get_last_day, getdate, now_datetime

from is_attendance.isambane_attendance.page.attendance_dashboard.attendance_dashboard import (
	_build_workbook,
)
from is_attendance.isambane_attendance.report.attendance_compliance_summary.attendance_compliance_summary import (
	METRICS,
	compute,
)

CADENCES = {
	"Daily": "send_daily",
	"Weekly": "send_weekly",
	"Monthly": "send_monthly",
}
LAST_SENT_FIELD = {
	"Daily": "last_daily_sent",
	"Weekly": "last_weekly_sent",
	"Monthly": "last_monthly_sent",
}


def send_daily_watch_group_digests() -> None:
	_send_watch_group_digests("Daily")


def send_weekly_watch_group_digests() -> None:
	_send_watch_group_digests("Weekly")


def send_monthly_watch_group_digests() -> None:
	_send_watch_group_digests("Monthly")


def _send_watch_group_digests(cadence: str) -> None:
	group_names = frappe.get_all(
		"Watch Group",
		filters={"enabled": 1, CADENCES[cadence]: 1},
		pluck="name",
	)

	for group_name in group_names:
		try:
			_send_one_digest(group_name, cadence)
		except Exception:
			frappe.log_error(
				title=f"Watch Group digest failed: {group_name} ({cadence})",
				message=frappe.get_traceback(),
			)


def _period_for_cadence(cadence: str) -> tuple[date, date]:
	today = getdate()

	if cadence == "Daily":
		yesterday = add_days(today, -1)
		return yesterday, yesterday

	if cadence == "Weekly":
		return add_days(today, -7), add_days(today, -1)

	if cadence == "Monthly":
		previous_month_start = get_first_day(today, d_months=-1)
		previous_month_end = get_last_day(previous_month_start)
		return previous_month_start, previous_month_end

	raise ValueError(f"Unknown cadence: {cadence}")


def _send_one_digest(group_name: str, cadence: str) -> None:
	group = frappe.get_doc("Watch Group", group_name)

	employees = [row.employee for row in group.employees]
	recipient_users = [row.user for row in group.recipients]
	if not employees or not recipient_users:
		return  # nothing meaningful to send - validate() already warns on save, this is just the runtime mirror of that

	# User.name IS the email address in Frappe - no separate lookup needed,
	# just confirming each recipient User still actually exists (one could
	# have been disabled/deleted since being added here).
	recipient_emails = frappe.get_all("User", filters={"name": ["in", recipient_users]}, pluck="name")
	if not recipient_emails:
		return

	from_date, to_date = _period_for_cadence(cadence)

	filters = {
		"employees": employees,
		"from_date": from_date,
		"to_date": to_date,
		# A watched employee who left mid-period should still show for
		# that period's own digest, not silently vanish from it just
		# because they're no longer Active today.
		"include_inactive": 1,
	}
	summary_rows, daily_detail = compute(filters)

	workbook = _build_workbook(summary_rows, daily_detail)
	buffer = BytesIO()
	workbook.save(buffer)

	subject = _(
		"{0} Attendance Watch: {1} ({2} - {3})",
	).format(cadence, group.group_name, frappe.utils.formatdate(from_date), frappe.utils.formatdate(to_date))
	message = _build_email_body(group.group_name, cadence, from_date, to_date, summary_rows)

	filename = f"{frappe.scrub(group.group_name)}_{cadence.lower()}_{to_date}.xlsx"
	# Deliberately NOT now=True - that forces an immediate, synchronous
	# SMTP round trip right here, inside a loop that can be processing
	# several groups in one scheduler tick, and ties this function's own
	# success to the mail server being reachable at this exact moment.
	# Queuing (the default) hands off to Frappe's own existing Email Queue
	# flush (frappe.email.queue.flush, already scheduled under "all" in
	# core hooks) - the same mechanism every other scheduled/bulk email in
	# Frappe relies on, retries included.
	frappe.sendmail(
		recipients=recipient_emails,
		subject=subject,
		message=message,
		attachments=[{"fname": filename, "fcontent": buffer.getvalue()}],
	)

	frappe.db.set_value("Watch Group", group_name, LAST_SENT_FIELD[cadence], now_datetime())
	frappe.db.commit()


def _build_email_body(group_name: str, cadence: str, from_date, to_date, summary_rows: list[dict]) -> str:
	"""A short HTML summary - grand totals across the whole group, then one
	row per watched Employee - not a substitute for the attached workbook
	(no day-by-day detail here), just enough to see at a glance whether
	the attachment is worth opening."""
	grand_totals = {metric: 0 for metric, _label in METRICS}
	for row in summary_rows:
		for metric, _label in METRICS:
			grand_totals[metric] += row.get(f"total_{metric}") or 0

	totals_html = "".join(
		f"<td style='padding:4px 10px;text-align:center;'><b>{grand_totals[metric]}</b><br>"
		f"<span style='color:#888;font-size:11px;'>{label}</span></td>"
		for metric, label in METRICS
	)

	rows_html = "".join(
		"<tr>"
		f"<td style='padding:4px 10px;'>{frappe.utils.escape_html(row.get('employee_name') or row.get('employee') or '')}</td>"
		+ "".join(
			f"<td style='padding:4px 10px;text-align:center;'>{row.get(f'total_{metric}') or 0}</td>"
			for metric, _label in METRICS
		)
		+ "</tr>"
		for row in sorted(summary_rows, key=lambda r: r.get("employee_name") or r.get("employee") or "")
	)

	metric_headers = "".join(f"<th style='padding:4px 10px;text-align:center;'>{label}</th>" for _metric, label in METRICS)

	return f"""
		<p>{_("{0} attendance watch for <b>{1}</b>, covering {2} to {3}.").format(
			cadence, frappe.utils.escape_html(group_name), frappe.utils.formatdate(from_date), frappe.utils.formatdate(to_date)
		)}</p>
		<table style='border-collapse:collapse;margin:10px 0;'><tr>{totals_html}</tr></table>
		<p>{_("Per employee:")}</p>
		<table style='border-collapse:collapse;'>
			<tr>
				<th style='padding:4px 10px;text-align:left;'>{_("Employee")}</th>
				{metric_headers}
			</tr>
			{rows_html}
		</table>
		<p style='color:#888;font-size:12px;'>{_("Full day-by-day detail is in the attached workbook.")}</p>
	"""
