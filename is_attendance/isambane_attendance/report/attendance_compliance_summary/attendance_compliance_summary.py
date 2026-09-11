# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Per-employee attendance compliance summary - Missed / No Out / No In /
Late In / Early Out, broken down by Weekday / Saturday / Sunday.

Deliberately computed straight from ``Employee Checkin``, never from
``Attendance`` - Attendance rows only exist for dates the daily sync
worklist has actually swept (checkin days, leave days, "today"), so it
can't be trusted as a complete record of every day in an arbitrary past
range the way raw Checkins can. Employee Checkin is the one central truth
every import path (DAT files, HIKVision, manual, Clocking Adjustment) all
feed into, so this report reads from it directly and reuses the exact same
clustering/pairing logic attendance_sync.py uses for official Attendance
(``_cluster_checkins``/``_normalize_log_types``/``_sum_intervals``), so the
numbers here never quietly disagree with the numbers HR-Attendance actually
shows.

Day classification, per employee/date:

- **Missed**: no Employee Checkins at all that day, and it isn't a South
  African public holiday (computed dynamically via the ``holidays`` PyPI
  package's ``country_holidays("ZA", ...)`` - the same technique already
  used by `ir`'s Shift Designer (``ir_shift_design.get_sa_public_holidays``),
  reused here directly rather than the Frappe "Holiday List" doctype, which
  has to be manually maintained and can silently miss a year), or covered
  by a *full* Leave Application whose status exempts (``Open`` or
  ``Approved`` - see ``EXEMPTING_LEAVE_STATUSES``; a still-pending request
  is treated the same as an approved one, on the assumption it'll go one
  way or the other). A ``Rejected`` or ``Cancelled`` Leave Application
  exempts nothing - the employee was expected at work either way - but its
  ``status`` is still carried through to ``leave_status`` on the daily
  detail row, so a Missed day caused by one isn't an unexplained flag; it
  visibly shows *why*. Applies uniformly to Weekdays, Saturdays and
  Sundays alike - a weekend day with zero clocking counts as Missed for
  every employee, not only those who had a Shift Assignment scheduling
  them to work it (confirmed with the user - this is deliberately the
  noisier, more literal interpretation). A half day
  (``Leave Application.half_day`` + ``half_day_date`` pinpointing this
  specific date - see ``_get_leave_detail_by_day``) still expects a genuine
  partial clocking, so Missed still fires on zero punches even on an
  exempting half-day-leave date; the leave only excuses half the day, not
  all of it.
- **No Out / No In**: only meaningful on a day with exactly one clustered
  punch (2+ punches always normalize to having both an IN and an OUT - see
  attendance_sync.py). Which side is missing is read from that single
  punch's *raw* stored log_type (device-reported) - not trusted for hours
  calculations elsewhere in this app because it doesn't reliably alternate
  across many days for the same employee, but for this one-punch-that-day
  case it's the only signal available at all, so it's used here as a
  best-effort classification. Suppressed on a half-day-leave date - a
  single punch is exactly what a real half day looks like (an employee
  called away to a personal emergency partway through, or a pre-approved
  half day either way round), not a missing pair.
- **Late In**: the day's first-in time (after clustering/normalizing) is
  later than Start Time + Threshold. Suppressed on a half-day-leave date -
  reduced hours make the full-day threshold not meaningfully applicable.
- **Early Out**: the day's last-out time is earlier than End Time -
  Threshold. Suppressed on a half-day-leave date, same reasoning as Late In
  - this is exactly the case of an employee retroactively getting a half
  day approved after being called away partway through.

Mirrors attendance_sync.py's own distinction between Attendance status
"Half Day" and "On Leave" (see ``_derive_status_from_leave_and_hours``) -
a half day is never treated the same as a full leave day there either, so
this report doesn't invent a separate interpretation.
"""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_datetime, get_time, getdate

from is_attendance.controllers.attendance_sync import (
	CLUSTER_SECONDS,
	_cluster_checkins,
	_normalize_log_types,
	_sum_intervals,
)
from is_attendance.permissions import responsible_branches_for_user

DAY_TYPES = ["Weekday", "Saturday", "Sunday"]
METRICS = [
	("missed", "Missed"),
	("no_out", "No Out"),
	("no_in", "No In"),
	("late_in", "Late In"),
	("early_out", "Early Out"),
]

DEFAULT_START_TIME = dt_time(6, 0, 0)
DEFAULT_END_TIME = dt_time(16, 0, 0)


def execute(filters=None):
	columns = get_columns()
	data = get_data(filters or {})
	return columns, data


def get_columns() -> list[dict]:
	columns = [
		{"label": _("Employee"), "fieldname": "employee", "fieldtype": "Link", "options": "Employee", "width": 100},
		{"label": _("Employee Name"), "fieldname": "employee_name", "fieldtype": "Data", "width": 160},
		{"label": _("Branch"), "fieldname": "branch", "fieldtype": "Link", "options": "Branch", "width": 110},
		{"label": _("Company"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 150},
	]

	for day_type in DAY_TYPES:
		columns.append(
			{
				"label": _(f"Total {day_type}s"),
				"fieldname": f"total_{day_type.lower()}s",
				"fieldtype": "Int",
				"width": 95,
			}
		)
		for metric, label in METRICS:
			columns.append(
				{
					"label": _(f"{day_type} {label}"),
					"fieldname": f"{day_type.lower()}_{metric}",
					"fieldtype": "Int",
					"width": 95,
				}
			)

	# Grand totals - Weekday+Saturday+Sunday combined per metric. Appended
	# after the full day-type breakdown (not reordered to the front) so
	# nothing relying on this list's existing order/length - none currently
	# does, but no reason to risk it - is affected.
	for metric, label in METRICS:
		columns.append(
			{
				"label": _(f"Total {label}"),
				"fieldname": f"total_{metric}",
				"fieldtype": "Int",
				"width": 95,
			}
		)

	return columns


def get_data(filters: dict) -> list[dict]:
	summary_rows, _daily = compute(filters)
	return summary_rows


def compute(filters: dict) -> tuple[list[dict], dict[str, list[dict]]]:
	"""Does the actual work behind get_data(): returns both the per-employee
	summary rows (what the Script Report and the Dashboard table show) and a
	parallel per-employee list of daily detail rows (date-by-date, what the
	Excel export's per-employee sheets show) from a single pass over the
	same fetched data, so the two views can never disagree with each other."""
	from_date = getdate(filters.get("from_date"))
	to_date = getdate(filters.get("to_date"))
	if not from_date or not to_date:
		frappe.throw(_("From Date and To Date are required."))
	if from_date > to_date:
		frappe.throw(_("From Date cannot be after To Date."))

	start_time = _parse_time(filters.get("start_time")) or DEFAULT_START_TIME
	end_time = _parse_time(filters.get("end_time")) or DEFAULT_END_TIME
	threshold_minutes = cint(filters.get("threshold"))

	# Weekends have never been flag-exempt by default (a Saturday/Sunday
	# with zero clocking counts as Missed for every employee - deliberate,
	# see module docstring), so this defaults off when absent, same as any
	# other unset Check filter. Public holidays are the opposite: they've
	# always been unconditionally flag-exempt (also documented above) -
	# defaulting this on when the filter key is missing entirely (not just
	# falsy) keeps every existing caller that doesn't know about this new
	# filter (a saved/scheduled report run, a direct API call) behaving
	# exactly as before; only an explicit uncheck turns that exemption off.
	exclude_weekends = bool(cint(filters.get("exclude_weekends")))
	exclude_public_holidays = (
		True if filters.get("exclude_public_holidays") is None else bool(cint(filters.get("exclude_public_holidays")))
	)

	employees = _resolve_employees(filters)
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

		totals = {f"total_{day_type.lower()}s": 0 for day_type in DAY_TYPES}
		counts = {f"{day_type.lower()}_{metric}": 0 for day_type in DAY_TYPES for metric, _label in METRICS}
		grand_totals = {f"total_{metric}": 0 for metric, _label in METRICS}
		detail_rows = []

		current = from_date
		while current <= to_date:
			day_type = _day_type(current)
			totals[f"total_{day_type.lower()}s"] += 1

			holiday_name = holidays.get(current)
			leave_here = leave_detail.get((employee, current))
			# Open (pending) and Approved both exempt, identically - see
			# EXEMPTING_LEAVE_STATUSES. Rejected/Cancelled never exempt
			# anything; leave_status is still carried through below so a
			# Missed day caused by one of those isn't an unexplained flag.
			is_exempting_leave = bool(leave_here and leave_here["is_exempting"])
			is_half_day_leave = is_exempting_leave and leave_here["half_day"]
			is_full_day_leave = is_exempting_leave and not is_half_day_leave
			leave_status = leave_here["status"] if leave_here else None
			day_checkins = checkins_by_day.get((employee, current), [])

			classification = _classify_day(day_checkins, start_time, end_time, threshold_minutes)

			is_excluded_weekend = exclude_weekends and day_type in ("Saturday", "Sunday")
			is_excluded_holiday = bool(holiday_name) and exclude_public_holidays

			if is_excluded_holiday or is_full_day_leave or is_excluded_weekend:
				# Public holiday (only while "Exclude Public Holidays" is on
				# - see above), a full leave day (always exempt), or a
				# weekend (only while "Exclude Weekends" is on) - fully
				# exempt from the 5 flags. Doesn't touch
				# total_weekdays/total_saturdays/total_sundays above - those
				# stay a pure calendar-day tally regardless of either toggle
				# (confirmed with the user - "exclude from calculations"
				# means the 5 compliance flags, not the day-count columns).
				flags = {"missed": False, "no_out": False, "no_in": False, "late_in": False, "early_out": False}
			else:
				flags = {key: classification[key] for key in ("missed", "no_out", "no_in", "late_in", "early_out")}
				if is_half_day_leave:
					# A half day still expects a genuine partial clocking -
					# Missed stays as computed (zero punches is still worth
					# flagging; the leave only excuses half the day), but a
					# single punch, or arriving late/leaving early, is
					# exactly what a real half day looks like, not a
					# violation of it. Mirrors attendance_sync.py's own
					# distinction between "Half Day" and "On Leave" status -
					# this report never disagrees with what the official
					# Attendance record already shows for the date.
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


# ---------------------------------------------------------------------------
# Day classification
# ---------------------------------------------------------------------------


def _classify_day(checkins: list[dict], start_time: dt_time, end_time: dt_time, threshold_minutes: int) -> dict:
	"""One clustering pass per day, producing everything both the summary
	counts and the Excel daily-detail sheet need: the five compliance
	flags, first-in/last-out (for display - a lone punch shows under
	whichever of In/Out its raw log_type actually says, rather than always
	being forced into "In" the way normalization does for the
	alternating-pairs hours calculation), and hours_worked (same
	total_seconds/3600 attendance_sync.py itself computes for Attendance,
	so this never disagrees with what HR-Attendance shows for that day)."""
	base = {
		"missed": False,
		"no_out": False,
		"no_in": False,
		"late_in": False,
		"early_out": False,
		"first_in": None,
		"last_out": None,
		"hours_worked": 0.0,
	}

	if not checkins:
		base["missed"] = True
		return base

	ordered = sorted(checkins, key=lambda c: c["time"])
	clustered = _cluster_checkins(ordered, seconds=CLUSTER_SECONDS)

	if len(clustered) == 1:
		# Only one real punch that day - fall back to its raw device
		# log_type to guess which side is missing (see module docstring).
		raw_type = clustered[0].get("log_type")
		no_in = raw_type == "OUT"
		no_out = not no_in
		punch_time = get_datetime(clustered[0]["time"])

		base.update(
			{
				"no_out": no_out,
				"no_in": no_in,
				"first_in": None if no_in else punch_time,
				"last_out": punch_time if no_in else None,
				"late_in": (not no_in) and _is_late_in(punch_time, start_time, threshold_minutes),
				"early_out": (not no_out) and _is_early_out(punch_time, end_time, threshold_minutes),
			}
		)
		return base

	normalized = _normalize_log_types(clustered)
	total_seconds, first_in, last_out = _sum_intervals(normalized)

	base.update(
		{
			"first_in": first_in,
			"last_out": last_out,
			"hours_worked": round(total_seconds / 3600.0, 2),
			"late_in": bool(first_in) and _is_late_in(first_in, start_time, threshold_minutes),
			"early_out": bool(last_out) and _is_early_out(last_out, end_time, threshold_minutes),
		}
	)
	return base


def _is_late_in(punch_time: datetime, start_time: dt_time, threshold_minutes: int) -> bool:
	cutoff = datetime.combine(punch_time.date(), start_time) + timedelta(minutes=threshold_minutes)
	return punch_time > cutoff


def _is_early_out(punch_time: datetime, end_time: dt_time, threshold_minutes: int) -> bool:
	cutoff = datetime.combine(punch_time.date(), end_time) - timedelta(minutes=threshold_minutes)
	return punch_time < cutoff


def _day_type(date) -> str:
	weekday = date.weekday()  # Monday=0 ... Sunday=6
	if weekday == 5:
		return "Saturday"
	if weekday == 6:
		return "Sunday"
	return "Weekday"


def _parse_time(value) -> dt_time | None:
	if not value:
		return None
	return get_time(value)


# ---------------------------------------------------------------------------
# Bulk data fetching
# ---------------------------------------------------------------------------


def _resolve_employees(filters: dict) -> list[str]:
	employees = filters.get("employees")
	if isinstance(employees, str):
		employees = frappe.parse_json(employees) if employees else []

	query_filters: dict = {}
	if employees:
		query_filters["name"] = ["in", employees]

	# Active-only unless explicitly overridden - a real, always-respected
	# toggle now, not just an implicit default for the no-selection case
	# (previously an inactive employee hand-picked via the multi-select
	# would show regardless; now it only does with the box ticked, which
	# is more predictable).
	if not filters.get("include_inactive"):
		query_filters["status"] = "Active"

	if filters.get("company"):
		query_filters["company"] = filters["company"]

	if filters.get("department"):
		query_filters["department"] = filters["department"]

	if filters.get("occupational_level"):
		# za_local's own Employment Equity Act field (Employee.za_occupational_level) -
		# reused directly rather than introducing a second classification.
		query_filters["za_occupational_level"] = filters["occupational_level"]

	if filters.get("payroll_cost_center"):
		query_filters["payroll_cost_center"] = filters["payroll_cost_center"]

	branches = responsible_branches_for_user()
	if filters.get("branch"):
		if branches and filters["branch"] not in branches:
			return []
		query_filters["branch"] = filters["branch"]
	elif branches:
		query_filters["branch"] = ["in", branches]

	return frappe.get_all("Employee", filters=query_filters, pluck="name")


def _get_employee_meta(employees: list[str]) -> dict[str, dict]:
	rows = frappe.get_all(
		"Employee",
		filters={"name": ["in", employees]},
		fields=["name", "employee_name", "branch", "company"],
	)
	return {row.name: row for row in rows}


def get_sa_public_holidays(start_date, end_date) -> dict:
	"""South African public holidays, computed dynamically (Easter-based
	ones included, e.g. Good Friday/Family Day) rather than read from a
	manually-maintained Holiday List doctype - the same technique `ir`'s
	Shift Designer already uses
	(``ir.industrial_relations.page.ir_shift_design.ir_shift_design.
	get_sa_public_holidays``), reimplemented here directly rather than
	imported cross-app, since is_attendance has no other dependency on
	`ir`. Returns {date: holiday_name}, applies nationally - not per
	employee/company, since SA public holidays aren't employee-specific."""
	start = getdate(start_date)
	end = getdate(end_date)
	if end < start:
		return {}

	try:
		from holidays import country_holidays
	except ImportError:
		frappe.throw(
			_(
				"Unable to compute South African public holidays. "
				"The 'holidays' Python package is not installed."
			)
		)

	years = list(range(start.year, end.year + 1))
	za_holidays = country_holidays("ZA", years=years)

	return {
		getdate(holiday_date): holiday_name
		for holiday_date, holiday_name in za_holidays.items()
		if start <= getdate(holiday_date) <= end
	}


# A Leave Application's docstatus/status combination, per HRMS's own
# on_submit() ("Only Leave Applications with status 'Approved' and
# 'Rejected' can be submitted" - hrms/hr/doctype/leave_application/
# leave_application.py): Open sits at docstatus 0 (not yet decided);
# Approved AND Rejected both reach docstatus 1 (both are "submitted",
# distinguished only by `status`); Cancelled is docstatus 2. Open and
# Approved are treated identically here - a pending request means the
# same "no clocking expected" outcome as an approved one, on the
# reasonable assumption it'll go one way or the other; Rejected/Cancelled
# mean the opposite (the employee should have been at work) and are
# surfaced for context rather than silently dropped, so a Missed day
# with a rejected/cancelled leave form on file isn't an unexplained flag.
EXEMPTING_LEAVE_STATUSES = {"Open", "Approved"}


def _get_leave_detail_by_day(employees: list[str], from_date, to_date) -> dict[tuple[str, object], dict]:
	"""Per (employee, date) leave detail for every date covered by ANY
	Leave Application in range (any status/docstatus except an
	entirely-discarded draft never submitted at all is still included via
	status "Open" - see EXEMPTING_LEAVE_STATUSES above) - not just a plain
	membership set, since a half-day date needs different missed-clocking
	handling than a full leave day, and a non-exempting status
	(Rejected/Cancelled) needs to be told apart from an exempting one
	(see compute()'s use of this). Mirrors attendance_sync._get_leave_info's
	own half_day_date condition exactly: a leave application's
	half_day/half_day_date fields pinpoint at most ONE date within its own
	from_date-to_date range as being the half day; every other date in
	that same application's range is a full leave day.

	When more than one application covers the same date (a rejected
	request followed by a fresh approved one, say), an exempting one
	always wins over a non-exempting one for that date - the most recent
	application (`order_by="modified desc"`) wins among applications that
	agree on exempting-or-not, since that's the current state of affairs.

	Fetches directly rather than reusing attendance_sync._get_employee_leave_days
	because that function only returns a flat (employee, date) membership
	set of Approved-only leave (enough for attendance_sync's own
	daily_sync_attendance worklist) - this report needs the
	half_day/half_day_date fields and every status, batched for a specific
	employee list rather than one query per employee/day."""
	if not employees:
		return {}

	applications = frappe.get_all(
		"Leave Application",
		filters={
			"employee": ["in", employees],
			"docstatus": ["!=", 2],  # Cancelled (docstatus 2) fetched separately below - see its own comment
			"from_date": ("<=", to_date),
			"to_date": (">=", from_date),
		},
		fields=["employee", "leave_type", "status", "half_day", "half_day_date", "from_date", "to_date", "modified"],
		order_by="modified desc",
	)

	# A cancelled application's own `status` field may still read "Approved"
	# or "Rejected" (before_cancel() only forces it to "Cancelled" as part
	# of the cancel action itself - see leave_application.py - a document
	# cancelled some other way, or one whose status field was set before
	# that hook's own save, could still show its pre-cancel status). Docstatus
	# 2 is the one unambiguous signal for "this application no longer
	# stands" regardless of what `status` happens to say, so cancelled ones
	# are fetched as a separate, deliberately status-blind pass and always
	# treated as non-exempting.
	cancelled_applications = frappe.get_all(
		"Leave Application",
		filters={
			"employee": ["in", employees],
			"docstatus": 2,
			"from_date": ("<=", to_date),
			"to_date": (">=", from_date),
		},
		fields=["employee", "leave_type", "half_day", "half_day_date", "from_date", "to_date", "modified"],
		order_by="modified desc",
	)
	for application in cancelled_applications:
		application["status"] = "Cancelled"
	applications += cancelled_applications

	detail: dict[tuple[str, object], dict] = {}
	for application in applications:
		is_exempting = application.status in EXEMPTING_LEAVE_STATUSES
		app_start = max(getdate(application.from_date), getdate(from_date))
		app_end = min(getdate(application.to_date), getdate(to_date))
		half_day_date = getdate(application.half_day_date) if cint(application.half_day) and application.half_day_date else None

		current = app_start
		while current <= app_end:
			key = (application.employee, current)
			existing = detail.get(key)
			# First application seen for this date wins outright (query is
			# already modified-desc, so that's the most recent one) - UNLESS
			# it's non-exempting and a later (older, since we're iterating
			# most-recent-first) application for the same date WAS exempting,
			# in which case the exempting one takes precedence per this
			# function's own docstring.
			if existing and (existing["is_exempting"] or not is_exempting):
				current = add_days(current, 1)
				continue

			detail[key] = {
				"leave_type": application.leave_type,
				"status": application.status,
				"is_exempting": is_exempting,
				"half_day": bool(half_day_date and half_day_date == current),
			}
			current = add_days(current, 1)

	return detail


def _get_checkins_grouped(employees: list[str], from_date, to_date) -> dict[tuple[str, object], list[dict]]:
	start_dt = get_datetime(f"{from_date} 00:00:00")
	end_dt = get_datetime(f"{to_date} 23:59:59")

	rows = frappe.get_all(
		"Employee Checkin",
		filters={"employee": ["in", employees], "time": ["between", [start_dt, end_dt]]},
		fields=["employee", "time", "log_type"],
		order_by="employee asc, time asc",
	)

	grouped: dict[tuple[str, object], list[dict]] = {}
	for row in rows:
		key = (row.employee, getdate(row.time))
		grouped.setdefault(key, []).append({"time": row.time, "log_type": row.log_type})

	return grouped
