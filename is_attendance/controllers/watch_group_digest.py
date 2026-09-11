# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Watch Group scheduled digests - a Watch Group (see that doctype's own
module docstring) names a list of Employees and a list of User recipients
on up to three independently-toggleable cadences, each with its own,
independently-chosen Range (Daily Range/Weekly Range/Monthly Range - how
often a digest sends and what period it covers are two separate choices;
a Daily send can cover the previous calendar month just as easily as
yesterday). Each cadence's own scheduled entry point below (see hooks.py's
scheduler_events - registered under the standard "daily"/"weekly"/
"monthly" keys, not a custom cron) finds every enabled Watch Group with
that cadence switched on, computes a summary scoped to just that group's
own Employees over that cadence's own Range, and emails every recipient a
short HTML summary with the full per-employee Excel workbook
(attendance_dashboard._build_workbook - the identical file "Export to
Excel" produces) attached.

The summary itself is NOT computed via attendance_compliance_summary.compute()
- that report uses one global Start Time/End Time/Threshold for every
employee, every day (see its own module docstring), which can't express
"Saturday differs from a weekday" or "Sunday is a Day Off for this one
employee, not for that one" - exactly what Watch Group's own Employee
Schedule/Time Buffer fields exist for. _compute_group_summary() below
reuses the same lower-level primitives that report itself uses
(_classify_day, get_sa_public_holidays, _get_leave_detail_by_day,
_get_checkins_grouped) directly, per employee per day, against that
employee's own schedule for that specific weekday - producing the exact
same summary_rows/daily_detail shape compute() does, so
_build_workbook()/_build_email_body() work unchanged either way.

One group's own failure (a bad recipient, a transient email error) is
logged and doesn't stop the rest of that cadence's run - same reasoning
daily_sync_attendance's own per-employee try/except uses.
"""

from __future__ import annotations

from datetime import date
from datetime import time as dt_time
from datetime import timedelta
from io import BytesIO

import frappe
from frappe import _
from frappe.utils import add_days, get_first_day, get_last_day, get_time, getdate, now_datetime

from is_attendance.isambane_attendance.page.attendance_dashboard.attendance_dashboard import (
	_build_workbook,
)
from is_attendance.isambane_attendance.report.attendance_compliance_summary.attendance_compliance_summary import (
	DAY_TYPES,
	DEFAULT_END_TIME,
	DEFAULT_START_TIME,
	METRICS,
	_classify_day,
	_day_type,
	_get_checkins_grouped,
	_get_employee_meta,
	_get_leave_detail_by_day,
	get_sa_public_holidays,
)

CADENCES = {
	"Daily": "send_daily",
	"Weekly": "send_weekly",
	"Monthly": "send_monthly",
}
RANGE_FIELD = {
	"Daily": "daily_range",
	"Weekly": "weekly_range",
	"Monthly": "monthly_range",
}
DEFAULT_RANGE = {
	"Daily": "Yesterday",
	"Weekly": "Last 7 Days",
	"Monthly": "Previous Calendar Month",
}
LAST_SENT_FIELD = {
	"Daily": "last_daily_sent",
	"Weekly": "last_weekly_sent",
	"Monthly": "last_monthly_sent",
}
DEFAULT_BUFFER_MINUTES = 15


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


def _resolve_range(range_key: str) -> tuple[date, date]:
	"""Every range ends no later than yesterday, deliberately - a run
	firing at any time of day always reports on fully-closed days, never
	a partial one still accumulating checkins."""
	today = getdate()
	yesterday = add_days(today, -1)

	if range_key == "Yesterday":
		return yesterday, yesterday

	if range_key == "Last 7 Days":
		return add_days(today, -7), yesterday

	if range_key == "Last 30 Days":
		return add_days(today, -30), yesterday

	if range_key == "Week to Date":
		this_week_monday = add_days(today, -today.weekday())
		return this_week_monday, yesterday

	if range_key == "Month to Date":
		return get_first_day(today), yesterday

	if range_key == "Previous Calendar Week":
		this_week_monday = add_days(today, -today.weekday())
		previous_week_monday = add_days(this_week_monday, -7)
		return previous_week_monday, add_days(previous_week_monday, 6)

	if range_key == "Previous Calendar Month":
		previous_month_start = get_first_day(today, d_months=-1)
		return previous_month_start, get_last_day(previous_month_start)

	raise ValueError(f"Unknown range: {range_key}")


def _normalize_time_value(value) -> dt_time | None:
	"""A Frappe "Time" field's own DB value can come back as either a
	datetime.time or a datetime.timedelta (duration since midnight) -
	the same confirmed gotcha attendance_sync._combine_date_time's own
	comment documents (a real Shift Type.start_time hit this live on this
	site) - normalizing here rather than trusting the caller's own type."""
	if not value:
		return None
	if isinstance(value, timedelta):
		total_seconds = int(value.total_seconds())
		return dt_time(hour=(total_seconds // 3600) % 24, minute=(total_seconds // 60) % 60, second=total_seconds % 60)
	if isinstance(value, dt_time):
		return value
	return get_time(value)


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

	range_key = group.get(RANGE_FIELD[cadence]) or DEFAULT_RANGE[cadence]
	from_date, to_date = _resolve_range(range_key)

	buffers = {row.employee: row.buffer_minutes if row.buffer_minutes is not None else DEFAULT_BUFFER_MINUTES for row in group.employees}
	schedules: dict[tuple[str, str], dict] = {}
	for row in group.employee_schedules:
		schedules[(row.employee, row.day_of_week)] = {
			"is_day_off": bool(row.is_day_off),
			"start_time": _normalize_time_value(row.start_time),
			"end_time": _normalize_time_value(row.end_time),
		}

	summary_rows, daily_detail = _compute_group_summary(employees, from_date, to_date, schedules, buffers)

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


def _compute_group_summary(
	employees: list[str],
	from_date,
	to_date,
	schedules: dict[tuple[str, str], dict],
	buffers: dict[str, int],
) -> tuple[list[dict], dict[str, list[dict]]]:
	"""Same summary_rows/daily_detail shape attendance_compliance_summary.compute()
	produces - see this module's own docstring for why it's computed here
	rather than by calling that function directly.

	`schedules` is keyed (employee, day_of_week name) -> {"is_day_off",
	"start_time", "end_time"} - a day/employee combination with no entry
	falls back to DEFAULT_START_TIME/DEFAULT_END_TIME (NOT a Day Off) -
	an employee/day genuinely never configured shouldn't silently go
	unchecked. `buffers` is keyed employee -> minutes, falling back to
	DEFAULT_BUFFER_MINUTES when not set."""
	if not employees:
		return [], {}

	employee_meta = _get_employee_meta(employees)
	holidays = get_sa_public_holidays(from_date, to_date)
	leave_detail = _get_leave_detail_by_day(employees, from_date, to_date)
	checkins_by_day = _get_checkins_grouped(employees, from_date, to_date)

	summary_rows = []
	daily_detail: dict[str, list[dict]] = {}

	for employee in employees:
		meta = employee_meta.get(employee, {})
		buffer_minutes = buffers.get(employee, DEFAULT_BUFFER_MINUTES)

		totals = {f"total_{day_type.lower()}s": 0 for day_type in DAY_TYPES}
		counts = {f"{day_type.lower()}_{metric}": 0 for day_type in DAY_TYPES for metric, _label in METRICS}
		grand_totals = {f"total_{metric}": 0 for metric, _label in METRICS}
		detail_rows = []

		current = from_date
		while current <= to_date:
			day_type = _day_type(current)
			totals[f"total_{day_type.lower()}s"] += 1

			weekday_name = current.strftime("%A")
			schedule = schedules.get((employee, weekday_name)) or {}
			is_day_off = bool(schedule.get("is_day_off"))
			start_time = schedule.get("start_time") or DEFAULT_START_TIME
			end_time = schedule.get("end_time") or DEFAULT_END_TIME

			holiday_name = holidays.get(current)
			leave_here = leave_detail.get((employee, current))
			is_exempting_leave = bool(leave_here and leave_here["is_exempting"])
			is_half_day_leave = is_exempting_leave and leave_here["half_day"]
			is_full_day_leave = is_exempting_leave and not is_half_day_leave
			leave_status = leave_here["status"] if leave_here else None

			day_checkins = checkins_by_day.get((employee, current), [])
			classification = _classify_day(day_checkins, start_time, end_time, buffer_minutes)

			if is_day_off or holiday_name or is_full_day_leave:
				# A Day Off is excluded from every flag entirely, the same
				# as a public holiday or a full leave day - not a looser
				# threshold, genuinely no clocking expected.
				flags = {"missed": False, "no_out": False, "no_in": False, "late_in": False, "early_out": False}
			else:
				flags = {key: classification[key] for key in ("missed", "no_out", "no_in", "late_in", "early_out")}
				if is_half_day_leave:
					flags["no_out"] = flags["no_in"] = flags["late_in"] = flags["early_out"] = False

				prefix = day_type.lower()
				for metric, _label in METRICS:
					if flags[metric]:
						counts[f"{prefix}_{metric}"] += 1
						grand_totals[f"total_{metric}"] += 1

			detail_rows.append(
				{
					"date": current,
					"day": current.strftime("%A"),
					"day_type": day_type,
					"in_time": classification["first_in"],
					"out_time": classification["last_out"],
					"hours_worked": classification["hours_worked"],
					"public_holiday": holiday_name or "",
					"on_leave": is_full_day_leave,
					"half_day_leave": is_half_day_leave,
					"leave_status": leave_status or "",
					**flags,
				}
			)

			current = add_days(current, 1)

		summary_rows.append(
			{
				"employee": employee,
				"employee_name": meta.get("employee_name"),
				"branch": meta.get("branch"),
				"company": meta.get("company"),
				**totals,
				**counts,
				**grand_totals,
			}
		)
		daily_detail[employee] = detail_rows

	return summary_rows, daily_detail


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
