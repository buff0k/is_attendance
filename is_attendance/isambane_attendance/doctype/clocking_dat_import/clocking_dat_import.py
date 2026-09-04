# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Legacy clocking system .DAT file import.

The .DAT files are tab-separated text (CRLF line endings), one punch per
line, 6 columns and no header - confirmed against real ``13_attlog.dat`` /
``20_attlog.dat`` samples from two different terminals:

    <PIN>\t<DateTime>\t<MachineID>\t<Status>\t<VerifyMode>\t<Reserved>
       691\t2026-06-30 15:56:56\t13\t0\t15\t0

- PIN: the employee's badge/enrollment number on the device (space-padded),
  matched against ``Employee.attendance_device_id`` - the same field `ir`'s
  Monthly Attendance report already uses for this purpose.
- DateTime: "YYYY-MM-DD HH:MM:SS".
- MachineID: constant for an entire file (matches the "13"/"20" in the
  filename). Recorded on the resulting Employee Checkin's device_id (its
  plain, core-field meaning - the physical device ID) and isa_clocking_machine
  for traceability only - it does **not** determine the checkin's Branch. A
  checkin always clocks against the employee's own actual Branch
  (Employee.branch), never the machine's - a machine's Branch (see IS
  Attendance Settings > Clocking Machines) only describes where that
  physical device sits, which says nothing about which branch the employee
  using it actually belongs to (an employee can clock in at a machine away
  from their home branch). Fallback Branch on this doctype only covers
  employees who have no Branch set on their own Employee record at all.
- Status: device-reported 0/1. In the sample data this does **not**
  reliably alternate per visit (it can stay constant for a user across many
  days/times of day - some terminals only change it when an operator
  presses a mode key at the device, not per punch), so it's mapped to
  IN/OUT as a best-effort label only. Actual working-hours computation
  never trusts stored log_type - attendance_sync.py's own clustering
  already re-derives IN/OUT purely from chronological order per employee
  per day, which is what makes this safe.
- VerifyMode/Reserved: not used.

Workflow
--------
This doctype is submittable, and the actual row-by-row import work always
runs in a background job - never inline inside an interactive request -
because a large file can take far longer than a web request/reverse-proxy
timeout allows (gunicorn on this bench is `-t 120`). Status moves through
six values, matching the interim states between plain Draft/Submitted that
testing showed were needed:

- **Not Parsed**: no file attached yet.
- **Missing Information**: file parsed, but some employee codes and/or
  employees-without-a-Branch remain unresolved (see below).
- **Pending Import**: everything resolves - ready for "Start Import".
- **Importing**: the background job is running.
- **Completed**: the background job succeeded and the document is
  Submitted.
- **Error**: the background job failed - see the sequence below for why
  this is a real, reachable state despite queue_import() already having
  verified readiness before enqueueing.

The sequence:

1. Draft (Not Parsed / Missing Information / Pending Import): every save
   (``validate``) re-parses the attached file, groups unmatched employee
   codes into the ``issues`` child table, and - for any issue row the user
   has already filled an Employee into - permanently records that mapping
   on ``Employee.attendance_device_id`` so future imports resolve it
   automatically, then drops that row from ``issues`` since it's no longer
   a problem. It also lists any employees with no Branch of their own and
   no Fallback Branch to cover them in ``employees_without_branch``. Status
   is "Missing Information" while either list is non-empty, else "Pending
   Import". This is the only part that runs inline - it's just parsing + a
   permission-free grouping pass, confirmed under a second even for ~5,000
   rows.
2. User clicks "Start Import" (``queue_import``, not the native Submit
   button - see clocking_dat_import.js): re-checks everything is resolved,
   sets status to "Importing", and enqueues ``run_import_job`` on the
   "long" queue. Returns immediately - no row processing happens on this
   request.
3. ``run_import_job`` (background worker, can safely run for up to an
   hour): does the real work - creates the Employee Checkins (idempotent -
   skips rows already imported), recomputes Attendance for every affected
   employee/day, and records exactly which Checkins it created in
   ``created_checkins`` so ``on_cancel`` can cleanly revert only those.
   Only once that has fully succeeded does it set status "Completed" and
   call ``doc.submit()`` itself - the document only becomes Submitted once
   the import is actually done, never as a side effect of the user's
   click. On any failure it sets status "Error" with the traceback in
   ``import_log`` and leaves the document in Draft so it can be fixed and
   re-queued. This shouldn't normally happen - queue_import() already
   confirmed every row resolves right before enqueueing - but it's a real
   safety net, not a theoretical one: an Employee or Branch can change
   between that check and the job actually running (someone edits/deletes
   one mid-flight), or the job can hit a transient DB error.
4. ``before_submit`` is a cheap guard, not a worker: it refuses to let
   *any* caller (the UI, the API, a console) submit unless status is
   already "Completed" - i.e. unless the background job already finished
   the real work. This makes the safety property hold regardless of how
   submit is invoked, not just from the one button in the UI.

Once submitted, the attached file itself becomes read-only like every
other non-``allow_on_submit`` field on a submitted document - the
sanctioned way to redo an import is cancel + amend.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate
from frappe.utils.file_manager import get_file

from is_attendance.controllers.attendance_sync import (
	recompute_attendance_for_employee_day,
)

IMPORT_JOB_TIMEOUT = 60 * 60  # a large file needs far longer than a web request allows


class ClockingDATImport(Document):
	def validate(self):
		if not self.file:
			self.issues = []
			self.total_rows = 0
			self.unresolved_count = 0
			self.employees_without_branch = ""
			self.status = "Not Parsed"
			return

		# queue_import() and run_import_job() both save this doc through its
		# normal lifecycle while moving it through Importing/Completed/
		# Error, having already computed status/issues themselves -
		# validate() must not re-parse and clobber that on those saves.
		if self.flags.get("_dat_import_pipeline_active"):
			return

		rows = self._parse_file()
		ready = self._refresh_readiness(rows)
		self.status = "Pending Import" if ready else "Missing Information"

	def before_submit(self):
		if self.status != "Completed":
			frappe.throw(
				_(
					"Run the import first (\"Start Import\") and wait for it to complete - "
					"this document can only be submitted once its Status is \"Completed\", "
					"not from here directly."
				)
			)

	def _refresh_readiness(self, rows: list[dict]) -> bool:
		"""Apply any newly-resolved Issues rows, rebuild the Issues table and
		the Employees Without a Branch list, and update the row/unresolved
		counts. Returns whether the document is genuinely ready to import
		(both lists empty). Shared by validate() (silent) and queue_import()
		(throws using the same freshly-computed state) so "Missing
		Information" always means exactly the same thing in both places."""
		self._apply_resolved_issues()
		self._rebuild_issues(rows)

		missing_branch = sorted(self._employees_without_branch(rows))

		self.total_rows = len(rows)
		self.unresolved_count = len(self.issues)
		self.employees_without_branch = ", ".join(missing_branch)

		return not self.issues and not missing_branch

	@frappe.whitelist()
	def queue_import(self):
		"""Whitelisted entry point for the "Start Import" button. Does the
		cheap readiness check inline (fast - same parse/issue-check as
		validate()) and, if genuinely ready, hands the real work off to
		run_import_job in the background rather than doing it here."""
		if self.docstatus != 0:
			frappe.throw(_("This import has already been submitted."))

		if not self.file:
			frappe.throw(_("Attach a DAT file first."))

		rows = self._parse_file()
		if not self._refresh_readiness(rows):
			problems = []
			if self.issues:
				problems.append(
					_("{0} employee code(s) are unresolved in the Issues table").format(
						len(self.issues)
					)
				)
			if self.employees_without_branch:
				problems.append(
					_("{0} employee(s) have no Branch and no Fallback Branch is set: {1}").format(
						len(self.employees_without_branch.split(", ")),
						self.employees_without_branch,
					)
				)
			frappe.throw(
				_("Cannot start the import - {0}. Resolve this first (see Missing Information).").format(
					"; ".join(problems)
				)
			)

		self.status = "Importing"
		self.flags._dat_import_pipeline_active = True
		self.save()

		frappe.enqueue(
			"is_attendance.isambane_attendance.doctype.clocking_dat_import."
			"clocking_dat_import.run_import_job",
			queue="long",
			timeout=IMPORT_JOB_TIMEOUT,
			job_name=f"is_attendance_dat_import_{self.name}",
			docname=self.name,
			# Only start the job once the "Importing" status is actually
			# visible in the DB - otherwise a fast worker could load the
			# doc before this request's commit lands.
			enqueue_after_commit=True,
		)

	def on_cancel(self):
		affected: set[tuple[str, object]] = set()

		for row in self.created_checkins or []:
			if not row.checkin or not frappe.db.exists("Employee Checkin", row.checkin):
				continue
			checkin = frappe.get_doc("Employee Checkin", row.checkin)
			affected.add((checkin.employee, getdate(checkin.time)))
			frappe.delete_doc("Employee Checkin", row.checkin, ignore_permissions=True, force=True)

		for employee, attendance_date in affected:
			recompute_attendance_for_employee_day(employee, attendance_date)

		self.import_log = (self.import_log or "") + "\n\nCancelled - created checkins removed."

	# ------------------------------------------------------------------
	# Parsing / issue resolution
	# ------------------------------------------------------------------

	def _parse_file(self) -> list[dict]:
		"""Cached per in-memory Document instance - validate() and
		queue_import() can both run against the same loaded doc within one
		request, and re-reading/re-decoding a multi-thousand-line file twice
		is pure waste since the attached file can't change in between."""
		cached = getattr(self, "_parsed_rows_cache", None)
		if cached is not None:
			return cached

		_filename, content = get_file(self.file)
		rows = parse_dat_file(content)
		self._parsed_rows_cache = rows
		return rows

	def _apply_resolved_issues(self):
		"""For every issues row the user has filled an Employee into, write
		that mapping onto Employee.attendance_device_id permanently (unless
		it's already correctly set), so this code resolves on its own from
		now on. Throws on a genuine conflict (code already claimed by a
		different Employee) rather than silently overwriting it."""
		for row in self.issues or []:
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

	def _rebuild_issues(self, rows: list[dict]):
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

		self.issues = []
		for code, entry in sorted(grouped.items()):
			if _resolve_employee({"employee_code": code}):
				continue
			self.append(
				"issues",
				{
					"employee_code": code,
					"machine_id": entry["machine_id"],
					"occurrence_count": entry["count"],
					"first_seen": entry["first_seen"],
				},
			)

	def _employees_without_branch(self, rows: list[dict]) -> set[str]:
		"""Employees whose checkins in this file can't get a Branch at all -
		no Branch on their own Employee record, and no Fallback Branch set
		on this import to cover the gap. Empty whenever a Fallback Branch is
		set, since that alone is then enough for every employee."""
		if self.branch:
			return set()

		branches = self._employee_branch_map(rows)
		return {employee for employee, branch in branches.items() if not branch}

	def _employee_branch_map(self, rows: list[dict]) -> dict[str, str | None]:
		"""One batch query for every distinct employee resolved from `rows`,
		rather than a per-row lookup - the whole point of this file being
		importable at all is that it can have thousands of rows."""
		employees = {employee for row in rows if (employee := _resolve_employee(row))}
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

	# ------------------------------------------------------------------
	# Checkin creation (submit only - every row must already resolve)
	# ------------------------------------------------------------------

	def _create_checkins(self, rows: list[dict]) -> tuple[int, int]:
		created = 0
		skipped = 0
		affected: set[tuple[str, object]] = set()

		# Checkins clock against the employee's own actual Branch (Employee.
		# branch), never the clocking machine's Branch - a machine's Branch
		# only describes where that device physically is (see IS Attendance
		# Settings > Clocking Machines), it says nothing about which branch
		# the employee who used it actually belongs to. Fallback Branch only
		# covers employees with no Branch of their own set.
		employee_branches = self._employee_branch_map(rows)

		# A large file can create thousands of Employee Checkins in this one
		# loop. Employee Checkin's own after_insert hook would otherwise
		# enqueue a background recompute job per row (see
		# attendance_sync.on_employee_checkin) - harmless for a single
		# manual checkin, but enough rows here trips Frappe's own
		# queue-overload guard well before we even reach the end of the
		# file. We do our own deduplicated-per-employee-per-day recompute
		# synchronously below instead, so suppress the per-row one.
		frappe.flags.in_bulk_checkin_import = True
		try:
			for row in rows:
				employee = _resolve_employee(row)
				branch = employee_branches.get(employee) or self.branch
				machine_id = row.get("machine_id")

				# Dedup key is employee+time+machine, not employee+time alone -
				# a clean, machine-scoped signal (isa_clocking_machine), unlike
				# the old device_id-with-a-"DAT-IMPORT-" prefix hack this
				# replaces. device_id itself is now left holding its plain,
				# core-field meaning (the physical device ID, same as a real
				# HIKVision-sourced checkin would report) instead of being
				# overloaded as our own dedup marker.
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
						"isa_import_doctype": self.doctype,
						"isa_import_reference": self.name,
					}
				)
				checkin.insert(ignore_permissions=True)
				self.append("created_checkins", {"checkin": checkin.name})
				created += 1
				affected.add((employee, getdate(row.get("time"))))
		finally:
			frappe.flags.in_bulk_checkin_import = False

		for employee, attendance_date in affected:
			recompute_attendance_for_employee_day(employee, attendance_date)

		return created, skipped


def run_import_job(docname: str) -> None:
	"""Background worker entry point (queue="long") for the actual bulk
	import - enqueued by ClockingDATImport.queue_import(), never called
	directly. See the module docstring for the full flow.

	Runs with the permissions of whoever queued the import (frappe.enqueue
	preserves the enqueuing user - frappe/utils/background_jobs.py's
	execute_job calls frappe.set_user() before dispatch), so the final
	doc.submit() below is permission-checked exactly as if that user had
	clicked a (theoretical, instant) native Submit button themselves.
	"""
	doc = frappe.get_doc("Clocking DAT Import", docname)
	doc.flags._dat_import_pipeline_active = True

	# Status is already "Importing" and committed - queue_import() saved and
	# committed it before this job was even enqueued (enqueue_after_commit).
	# Nothing else needs to happen before the real work starts.
	try:
		rows = doc._parse_file()
		created, skipped = doc._create_checkins(rows)

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
		# the document become Submitted. before_submit() also independently
		# refuses to let this (or any other caller) proceed unless status is
		# already "Completed", so this property doesn't depend on nothing
		# else calling submit() early.
		doc.submit()
	except Exception:
		# Whatever partial work this attempt did (some checkins inserted,
		# in-memory created_checkins rows, etc.) is undone here - roll back
		# to the "Importing" checkpoint already committed by queue_import(),
		# then reload to discard the now-inconsistent in-memory state before
		# recording the failure. This is the "should theoretically not be
		# possible" case: queue_import() already verified every row resolves
		# before enqueueing this job, so reaching here means something
		# changed *after* that check (an Employee/Branch got edited or
		# deleted mid-flight, a DB error, a genuine bug) - Error exists as a
		# safety net for exactly that gap, not because the happy-path
		# validation is expected to fail.
		frappe.db.rollback()
		doc.reload()
		doc.flags._dat_import_pipeline_active = True
		doc.status = "Error"
		doc.import_log = frappe.get_traceback()
		doc.save(ignore_permissions=True)
		frappe.db.commit()


def _resolve_employee(row: dict) -> str | None:
	"""Match a parsed .DAT row to an Employee via `employee_code` against
	Employee.attendance_device_id - the standard HRMS field for exactly
	this purpose."""
	employee_code = row.get("employee_code")
	if not employee_code:
		return None
	return frappe.db.get_value("Employee", {"attendance_device_id": employee_code}, "name")


# Device Status column -> Employee Checkin.log_type. Only 0/1 have been seen
# in real files; 2-5 are documented ZKTeco codes (Break Out/Break In/OT
# In/OT Out) mapped here defensively even though unobserved so far.
STATUS_LOG_TYPE_MAP = {
	"0": "IN",  # Check In
	"1": "OUT",  # Check Out
	"2": "OUT",  # Break Out
	"3": "IN",  # Break In
	"4": "IN",  # OT In
	"5": "OUT",  # OT Out
}

DAT_MIN_COLUMNS = 4  # PIN, DateTime, MachineID, Status - VerifyMode/Reserved are optional


def parse_dat_file(content: bytes | str) -> list[dict]:
	"""
	Parse a legacy clocking terminal .DAT export into a list of row dicts.

	Tab-separated, no header, one punch per line:

		<PIN>\t<DateTime>\t<MachineID>\t<Status>\t<VerifyMode>\t<Reserved>

	e.g. ``   691\t2026-06-30 15:56:56\t13\t0\t15\t0`` - confirmed against
	real 13_attlog.dat / 20_attlog.dat samples. See the module docstring for
	the full column notes.

	Lines with fewer than 4 columns, or a blank PIN/DateTime, are skipped
	silently (not raised) - real exports are clean, but a stray blank line
	shouldn't fail the whole import.
	"""
	if isinstance(content, bytes):
		text = content.decode("utf-8-sig", errors="replace")
	else:
		text = content

	rows = []
	for line in text.splitlines():
		line = line.strip()
		if not line:
			continue

		fields = line.split("\t")
		if len(fields) < DAT_MIN_COLUMNS:
			continue

		pin = fields[0].strip()
		time_value = fields[1].strip()
		machine_id = fields[2].strip()
		status = fields[3].strip()

		if not pin or not time_value:
			continue

		rows.append(
			{
				"employee_code": pin,
				"time": time_value,
				"log_type": STATUS_LOG_TYPE_MAP.get(status, "IN"),
				"machine_id": machine_id or None,
			}
		)

	return rows
