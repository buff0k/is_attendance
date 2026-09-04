# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Shared plumbing behind every "Clocking * Import" doctype (Clocking DAT
Import, Clocking HIKVision Import, and whatever comes after those two) -
branch resolution, Issues tracking, bulk Employee Checkin creation, and the
whole Draft -> Missing Information -> Pending Import -> Importing ->
Completed/Error async-submit lifecycle. See clocking_dat_import.py's module
docstring for the full narrative explanation of *why* this pipeline is
shaped the way it is (background job, before_submit guard, etc.) - that
reasoning applies unchanged to every doctype that uses this module, so it
isn't repeated per doctype.

A parsed row, from any source format, is a dict shaped like:

    {
        "employee_code": "1234",      # required: matched via
                                       # Employee.attendance_device_id
        "time": "2026-09-03 07:58:00",  # required: checkin datetime
        "log_type": "IN",              # best-effort label only - never
                                        # trusted for hours (see
                                        # attendance_sync.py's clustering)
        "machine_id": "13",            # optional: recorded for
                                        # traceability, never determines
                                        # Branch (that's always the
                                        # employee's own Employee.branch)
    }

Every doctype that uses this module must:

- Have fields named exactly: file, branch (Fallback Branch), status,
  total_rows, unresolved_count, employees_without_branch, issues (Table),
  created_checkins (Table), import_log.
- Its `issues` Table's child doctype must have fields: employee_code,
  machine_id, occurrence_count, first_seen, employee.
- Its `created_checkins` Table's child doctype must have a `checkin` field.
- Implement a `parse_file(self) -> list[dict]` method (cached - see
  clocking_dat_import.py's _parse_file for the caching pattern) returning
  rows in the shape above.
- Register `run_import_job` (this module's, not a per-doctype copy) as the
  background job target - see queue_import() below, which already points
  at the shared one.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate

from is_attendance.controllers.attendance_sync import (
	recompute_attendance_for_employee_day,
)

IMPORT_JOB_TIMEOUT = 60 * 60  # a large file needs far longer than a web request allows
RUN_IMPORT_JOB_PATH = "is_attendance.controllers.clocking_import.run_import_job"


def resolve_employee(row: dict) -> str | None:
	"""Match a parsed row to an Employee via `employee_code` against
	Employee.attendance_device_id - the standard HRMS field for exactly
	this purpose (also what `ir`'s Monthly Attendance report uses)."""
	employee_code = row.get("employee_code")
	if not employee_code:
		return None
	return frappe.db.get_value("Employee", {"attendance_device_id": employee_code}, "name")


def employee_branch_map(rows: list[dict]) -> dict[str, str | None]:
	"""One batch query for every distinct employee resolved from `rows`,
	rather than a per-row lookup - the whole point of these imports being
	usable at all is that a file can have thousands of rows."""
	employees = {employee for row in rows if (employee := resolve_employee(row))}
	if not employees:
		return {}

	return {
		row.name: row.branch
		for row in frappe.get_all(
			"Employee",
			filters={"name": ["in", list(employees)]},
			fields=["name", "branch"],
		)
	}


def employees_without_branch(rows: list[dict], fallback_branch: str | None) -> set[str]:
	"""Employees whose checkins in this file can't get a Branch at all - no
	Branch on their own Employee record, and no Fallback Branch set on this
	import to cover the gap. Empty whenever a Fallback Branch is set, since
	that alone is then enough for every employee."""
	if fallback_branch:
		return set()

	branches = employee_branch_map(rows)
	return {employee for employee, branch in branches.items() if not branch}


def apply_resolved_issues(doc) -> None:
	"""For every issues row the user has filled an Employee into, write
	that mapping onto Employee.attendance_device_id permanently (unless
	it's already correctly set), so this code resolves on its own from now
	on - across every import doctype that uses this module, not just this
	one document. Throws on a genuine conflict (code already claimed by a
	different Employee) rather than silently overwriting it."""
	for row in doc.issues or []:
		if not row.employee:
			continue

		existing_owner = frappe.db.get_value(
			"Employee", {"attendance_device_id": row.employee_code}, "name"
		)
		if existing_owner and existing_owner != row.employee:
			frappe.throw(
				_(
					"Employee code {0} is already assigned to Employee {1}, not {2}. "
					"Fix the mapping on the Issues row before saving."
				).format(row.employee_code, existing_owner, row.employee)
			)

		if existing_owner != row.employee:
			frappe.db.set_value("Employee", row.employee, "attendance_device_id", row.employee_code)


def rebuild_issues(doc, rows: list[dict]) -> None:
	grouped: dict[str, dict] = {}
	for row in rows:
		code = row["employee_code"]
		entry = grouped.setdefault(
			code,
			{"machine_id": row.get("machine_id"), "count": 0, "first_seen": row["time"]},
		)
		entry["count"] += 1
		if row["time"] < entry["first_seen"]:
			entry["first_seen"] = row["time"]

	doc.issues = []
	for code, entry in sorted(grouped.items()):
		if resolve_employee({"employee_code": code}):
			continue
		doc.append(
			"issues",
			{
				"employee_code": code,
				"machine_id": entry["machine_id"],
				"occurrence_count": entry["count"],
				"first_seen": entry["first_seen"],
			},
		)


def refresh_readiness(doc, rows: list[dict]) -> bool:
	"""Apply any newly-resolved Issues rows, rebuild the Issues table and
	the Employees Without a Branch list, and update the row/unresolved
	counts. Returns whether the document is genuinely ready to import (both
	lists empty). Shared by validate() (silent) and queue_import() (throws
	using this same freshly-computed state) so "Missing Information" always
	means exactly the same thing in both places, for every doctype that
	uses this module."""
	apply_resolved_issues(doc)
	rebuild_issues(doc, rows)

	missing_branch = sorted(employees_without_branch(rows, doc.branch))

	doc.total_rows = len(rows)
	doc.unresolved_count = len(doc.issues)
	doc.employees_without_branch = ", ".join(missing_branch)

	return not doc.issues and not missing_branch


def validate_import(doc) -> None:
	"""Shared validate() body. Callers: def validate(self):
	clocking_import.validate_import(self)"""
	if not doc.file:
		doc.issues = []
		doc.total_rows = 0
		doc.unresolved_count = 0
		doc.employees_without_branch = ""
		doc.status = "Not Parsed"
		return

	# queue_import()/run_import_job() both save this doc through its normal
	# lifecycle while moving it through Importing/Completed/Error, having
	# already computed status/issues themselves - validate() must not
	# re-parse and clobber that on those saves.
	if doc.flags.get("_clocking_import_pipeline_active"):
		return

	rows = doc.parse_file()
	ready = refresh_readiness(doc, rows)
	doc.status = "Pending Import" if ready else "Missing Information"


def before_submit_guard(doc) -> None:
	"""Shared before_submit() body - a cheap guard, not a worker: refuses
	to let *any* caller (the UI, the API, a console) submit unless status
	is already "Completed", i.e. unless the background job already
	finished the real work. Callers: def before_submit(self):
	clocking_import.before_submit_guard(self)"""
	if doc.status != "Completed":
		frappe.throw(
			_(
				'Run the import first ("Start Import") and wait for it to complete - this '
				'document can only be submitted once its Status is "Completed", not from here '
				"directly."
			)
		)


def queue_import(doc) -> None:
	"""Shared queue_import() body - the whitelisted "Start Import" entry
	point. Callers: @frappe.whitelist() def queue_import(self):
	clocking_import.queue_import(self)"""
	if doc.docstatus != 0:
		frappe.throw(_("This import has already been submitted."))

	if not doc.file:
		frappe.throw(_("Attach a file first."))

	rows = doc.parse_file()
	if not refresh_readiness(doc, rows):
		problems = []
		if doc.issues:
			problems.append(
				_("{0} employee code(s) are unresolved in the Issues table").format(len(doc.issues))
			)
		if doc.employees_without_branch:
			problems.append(
				_("{0} employee(s) have no Branch and no Fallback Branch is set: {1}").format(
					len(doc.employees_without_branch.split(", ")),
					doc.employees_without_branch,
				)
			)
		frappe.throw(
			_("Cannot start the import - {0}. Resolve this first (see Missing Information).").format(
				"; ".join(problems)
			)
		)

	doc.status = "Importing"
	doc.flags._clocking_import_pipeline_active = True
	doc.save()

	frappe.enqueue(
		RUN_IMPORT_JOB_PATH,
		queue="long",
		timeout=IMPORT_JOB_TIMEOUT,
		job_name=f"is_attendance_clocking_import_{doc.doctype}_{doc.name}",
		doctype=doc.doctype,
		docname=doc.name,
		# Only start the job once the "Importing" status is actually
		# visible in the DB - otherwise a fast worker could load the doc
		# before this request's commit lands.
		enqueue_after_commit=True,
	)


def run_import_job(doctype: str, docname: str) -> None:
	"""Background worker entry point (queue="long"), shared by every
	Clocking * Import doctype - enqueued by queue_import() above, never
	called directly.

	Runs with the permissions of whoever queued the import (frappe.enqueue
	preserves the enqueuing user - frappe/utils/background_jobs.py's
	execute_job calls frappe.set_user() before dispatch), so the final
	doc.submit() below is permission-checked exactly as if that user had
	clicked a (theoretical, instant) native Submit button themselves.
	"""
	doc = frappe.get_doc(doctype, docname)
	doc.flags._clocking_import_pipeline_active = True

	# Status is already "Importing" and committed - queue_import() saved
	# and committed it before this job was even enqueued
	# (enqueue_after_commit). Nothing else needs to happen before the real
	# work starts.
	try:
		rows = doc.parse_file()
		created, skipped = create_checkins(doc, rows)

		doc.status = "Completed"
		doc.import_log = "\n".join(
			[
				f"Rows in file: {len(rows)}",
				f"Checkins created: {created}",
				f"Rows already imported (skipped): {skipped}",
			]
		)
		doc.save(ignore_permissions=True)

		# Only now - once the import has actually, fully succeeded - does
		# the document become Submitted. before_submit_guard() also
		# independently refuses to let this (or any other caller) proceed
		# unless status is already "Completed", so this property doesn't
		# depend on nothing else calling submit() early.
		doc.submit()
	except Exception:
		# Whatever partial work this attempt did (some checkins inserted,
		# in-memory created_checkins rows, etc.) is undone here - roll back
		# to the "Importing" checkpoint already committed by queue_import(),
		# then reload to discard the now-inconsistent in-memory state
		# before recording the failure. This is the "should theoretically
		# not be possible" case: queue_import() already verified every row
		# resolves before enqueueing this job, so reaching here means
		# something changed *after* that check (an Employee/Branch got
		# edited or deleted mid-flight, a DB error, a genuine bug) - Error
		# exists as a safety net for exactly that gap, not because the
		# happy-path validation is expected to fail.
		frappe.db.rollback()
		doc.reload()
		doc.flags._clocking_import_pipeline_active = True
		doc.status = "Error"
		doc.import_log = frappe.get_traceback()
		doc.save(ignore_permissions=True)
		frappe.db.commit()


def create_checkins(doc, rows: list[dict]) -> tuple[int, int]:
	"""Bulk-create Employee Checkins for every row that doesn't already
	exist (idempotent - dedup key is employee+time+isa_clocking_machine),
	stamping full provenance (isa_import_doctype/isa_import_reference via
	Dynamic Link, so any future import type just needs to call this same
	function - no new custom field required) and recomputing Attendance
	once per affected employee/day at the end."""
	created = 0
	skipped = 0
	affected: set[tuple[str, object]] = set()

	branches = employee_branch_map(rows)

	# A large file can create thousands of Employee Checkins in this one
	# loop. Employee Checkin's own after_insert hook would otherwise
	# enqueue a background recompute job per row (see
	# attendance_sync.on_employee_checkin) - harmless for a single manual
	# checkin, but enough rows here trips Frappe's own queue-overload guard
	# well before we even reach the end of the file. We do our own
	# deduplicated-per-employee-per-day recompute synchronously below
	# instead, so suppress the per-row one.
	frappe.flags.in_bulk_checkin_import = True
	try:
		for row in rows:
			employee = resolve_employee(row)
			branch = branches.get(employee) or doc.branch
			machine_id = row.get("machine_id")

			if frappe.db.exists(
				"Employee Checkin",
				{"employee": employee, "time": row.get("time"), "isa_clocking_machine": machine_id},
			):
				skipped += 1
				continue

			checkin = frappe.get_doc(
				{
					"doctype": "Employee Checkin",
					"employee": employee,
					"time": row.get("time"),
					"log_type": row.get("log_type"),
					"device_id": machine_id,
					"isa_branch": branch,
					"isa_clocking_machine": machine_id,
					"isa_import_doctype": doc.doctype,
					"isa_import_reference": doc.name,
				}
			)
			checkin.insert(ignore_permissions=True)
			doc.append("created_checkins", {"checkin": checkin.name})
			created += 1
			affected.add((employee, getdate(row.get("time"))))
	finally:
		frappe.flags.in_bulk_checkin_import = False

	for employee, attendance_date in affected:
		recompute_attendance_for_employee_day(employee, attendance_date)

	return created, skipped


def cancel_import(doc) -> None:
	"""Shared on_cancel() body - deletes exactly the Employee Checkins this
	import created (tracked in created_checkins) and recomputes Attendance
	for every affected employee/day. Callers: def on_cancel(self):
	clocking_import.cancel_import(self)"""
	affected: set[tuple[str, object]] = set()

	for row in doc.created_checkins or []:
		if not row.checkin or not frappe.db.exists("Employee Checkin", row.checkin):
			continue
		checkin = frappe.get_doc("Employee Checkin", row.checkin)
		affected.add((checkin.employee, getdate(checkin.time)))
		frappe.delete_doc("Employee Checkin", row.checkin, ignore_permissions=True, force=True)

	for employee, attendance_date in affected:
		recompute_attendance_for_employee_day(employee, attendance_date)

	doc.import_log = (doc.import_log or "") + "\n\nCancelled - created checkins removed."
