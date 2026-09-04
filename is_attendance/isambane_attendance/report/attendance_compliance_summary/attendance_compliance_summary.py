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
(``_cluster_checkins``/``_normalize_log_types``/``_sum_intervals``, plus
``_get_employee_leave_days`` for leave), so the numbers here never quietly
disagree with the numbers HR-Attendance actually shows.

Day classification, per employee/date:

- **Missed**: no Employee Checkins at all that day, and it isn't a South
  African public holiday (computed dynamically via the ``holidays`` PyPI
  package's ``country_holidays("ZA", ...)`` - the same technique already
  used by `ir`'s Shift Designer (``ir_shift_design.get_sa_public_holidays``),
  reused here directly rather than the Frappe "Holiday List" doctype, which
  has to be manually maintained and can silently miss a year), or covered
  by an Approved Leave Application. Applies uniformly to
  Weekdays, Saturdays and Sundays alike - a weekend day with zero
  clocking counts as Missed for every employee, not only those who had a
  Shift Assignment scheduling them to work it (confirmed with the user -
  this is deliberately the noisier, more literal interpretation).
- **No Out / No In**: only meaningful on a day with exactly one clustered
  punch (2+ punches always normalize to having both an IN and an OUT - see
  attendance_sync.py). Which side is missing is read from that single
  punch's *raw* stored log_type (device-reported) - not trusted for hours
  calculations elsewhere in this app because it doesn't reliably alternate
  across many days for the same employee, but for this one-punch-that-day
  case it's the only signal available at all, so it's used here as a
  best-effort classification.
- **Late In**: the day's first-in time (after clustering/normalizing) is
  later than Start Time + Threshold.
- **Early Out**: the day's last-out time is earlier than End Time -
  Threshold.
"""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_datetime, get_time, getdate

from is_attendance.controllers.attendance_sync import (
	CLUSTER_SECONDS,
	_cluster_checkins,
	_get_employee_leave_days,
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

	employees = _resolve_employees(filters)
	if not employees:
		return [], {}

	employee_meta = _get_employee_meta(employees)
	holidays = get_sa_public_holidays(from_date, to_date)
	leave_days = _get_leave_day_set(employees, from_date, to_date)
	checkins_by_day = _get_checkins_grouped(employees, from_date, to_date)

	summary_rows = []
	daily_detail: dict[str, list[dict]] = {}

	for employee in employees:
		meta = employee_meta.get(employee, {})

		totals = {f"total_{day_type.lower()}s": 0 for day_type in DAY_TYPES}
		counts = {f"{day_type.lower()}_{metric}": 0 for day_type in DAY_TYPES for metric, _label in METRICS}
		detail_rows = []

		current = from_date
		while current <= to_date:
			day_type = _day_type(current)
			totals[f"total_{day_type.lower()}s"] += 1

			holiday_name = holidays.get(current)
			on_leave = (employee, current) in leave_days
			day_checkins = checkins_by_day.get((employee, current), [])

			classification = _classify_day(day_checkins, start_time, end_time, threshold_minutes)

			if holiday_name or on_leave:
				flags = {"missed": False, "no_out": False, "no_in": False, "late_in": False, "early_out": False}
			else:
				flags = {key: classification[key] for key in ("missed", "no_out", "no_in", "late_in", "early_out")}
				prefix = day_type.lower()
				for metric, _label in METRICS:
					if flags[metric]:
						counts[f"{prefix}_{metric}"] += 1

			detail_rows.append(
				{
					"date": current,
					"day": current.strftime("%A"),
					"day_type": day_type,
					"in_time": classification["first_in"],
					"out_time": classification["last_out"],
					"hours_worked": classification["hours_worked"],
					"public_holiday": holiday_name or "",
					"on_leave": bool(on_leave),
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
	else:
		# Browsing without picking specific people - default to active
		# employees only, otherwise long-terminated staff clutter the grid.
		query_filters["status"] = "Active"

	if filters.get("company"):
		query_filters["company"] = filters["company"]

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


def _get_leave_day_set(employees: list[str], from_date, to_date) -> set[tuple[str, object]]:
	employee_set = set(employees)
	return {
		(employee, date)
		for employee, date in _get_employee_leave_days(from_date, to_date)
		if employee in employee_set
	}


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
