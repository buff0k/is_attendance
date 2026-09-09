# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
Shared plumbing behind the "Clocking Import" doctype - branch resolution,
Issues tracking, bulk Employee Checkin creation, and the whole Draft ->
Missing Information -> Pending Import -> Importing -> Partially Imported /
Completed / Error async-submit lifecycle. A file doesn't have to be 100%
resolved to start importing - queue_import() only refuses when *nothing*
in the file resolves yet (see resolvable_row_count()); whatever does
resolve gets imported, the rest stays in Issues for a later pass, and
re-running "Start Import" at any point is always safe (create_checkins()
skips whatever it already created). Format-specific parsing (which shape a given file
is, and how to read it) lives separately in
is_attendance.controllers.clocking_parsers - this module doesn't know or
care which shape produced the rows it's handling, only their shared
contract (see below). See the doctype's own module docstring
(isambane_attendance/doctype/clocking_import/clocking_import.py) for the
full narrative explanation of *why* this pipeline is shaped the way it is
(background job, before_submit guard, etc.).

A parsed row, from any source format, is a dict shaped like:

    {
        "employee_code": "1234",      # required: matched via
                                       # Employee.attendance_device_id
        "time": "2026-09-03 07:58:00",  # required: checkin datetime
        "log_type": "IN",              # best-effort label only - never
                                        # trusted for hours (see
                                        # attendance_sync.py's clustering)
        "machine_id": "13",            # optional: recorded for
                                        # traceability, and used as a
                                        # last-resort Branch fallback (see
                                        # create_checkins) when neither the
                                        # employee's own Employee.branch
                                        # nor the import's Fallback Branch
                                        # gives one
    }

Every doctype that uses this module must:

- Have fields named exactly: file, branch (Fallback Branch), status,
  total_rows, unresolved_count, resolvable_count, employees_without_branch,
  issues (Table), created_checkins (Table), import_log.
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

import re

import frappe
from frappe import _
from frappe.utils import add_days, getdate

from is_attendance.controllers.attendance_sync import (
	recompute_attendance_for_employee_day,
)

IMPORT_JOB_TIMEOUT = 60 * 60  # a large file needs far longer than a web request allows
RUN_IMPORT_JOB_PATH = "is_attendance.controllers.clocking_import.run_import_job"
QUEUE_IMPORT_BY_NAME_PATH = "is_attendance.controllers.clocking_import.queue_import_by_name"

_ATTLOG_SUFFIX = re.compile(r"_?attlog$", re.IGNORECASE)
_KNOWN_EXTENSION = re.compile(r"\.(dat|csv|txt)$", re.IGNORECASE)


def machine_id_from_filename(filename: str | None) -> str | None:
	"""Strip a trailing known extension (.dat/.csv/.txt) and, if present,
	an "_attlog" suffix, to get the terminal identity a filename encodes -
	"13_attlog.dat" -> "13", "NYU7244600160_attlog.dat" -> "NYU7244600160",
	"recordList.csv" -> "recordList". Shared by every import doctype whose
	own per-row machine-identifying column can't be trusted (varies in
	meaning by terminal vendor, or is entirely absent) - see
	clocking_dat_import.py's and the CSV importer's own docstrings for the
	concrete cases this covers."""
	if not filename:
		return None

	stem = _KNOWN_EXTENSION.sub("", filename)
	stem = _ATTLOG_SUFFIX.sub("", stem).strip()
	return stem or None

# Every doctype that uses this shared module, and its own Issues child
# doctype - used by resync_other_drafts() to find every OTHER Draft import
# document that's still showing a given employee code as unresolved. Now
# just the one doctype since Clocking DAT Import/Clocking CSV Import were
# consolidated into a single format-sniffing "Clocking Import" (see
# is_attendance.controllers.clocking_parsers) - kept as a dict, not a bare
# constant, so a genuinely different future import pipeline (not just a new
# clocking-export shape, which only needs a new parser) can still register
# itself here without changing resync_other_drafts() itself.
IMPORT_ISSUE_DOCTYPES = {
	"Clocking Import": "Clocking Import Issue",
}


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


def clocking_machine_branch_map() -> dict[str, str]:
	"""machine_id -> Branch, from every IS Attendance Clocking Machine
	record that has one set - a device with no Branch yet is excluded
	(nothing to fall back to). Last-resort Branch source for
	create_checkins()/employees_without_branch(), behind both the
	employee's own Employee.branch and the import's own Fallback Branch
	(see those functions) - a device only ever fills a gap neither of
	those covers. Reads the master doctype directly (autoname is
	field:machine_id, so a record's own name IS the machine_id) rather
	than through IS Attendance Settings' own Clocking Machines registry -
	that registry is a Table MultiSelect of links now, purely for
	visibility/management from Settings; it isn't where the actual
	Branch/etc. data lives any more."""
	rows = frappe.get_all("IS Attendance Clocking Machine", filters={"branch": ["is", "set"]}, fields=["name", "branch"])
	return {row.name: row.branch for row in rows}


def sync_clocking_machines(rows: list[dict], source_doctype: str | None = None, source_docname: str | None = None) -> list[str]:
	"""Creates a new IS Attendance Clocking Machine record (Branch left
	blank) for every machine_id in `rows` that doesn't already have one -
	so a person only ever has to open the device's own record and set its
	Branch, never type its ID in by hand first. The device's own list view
	is the place to see/manage every known machine (IS Attendance Settings
	no longer keeps a separate registry of them - dropped once it became
	pure duplication of this doctype's own list). Called once per
	completed import job (see run_import_job), not per row - new devices
	are rare, no reason to check on every checkin. Returns the newly-added
	machine_ids, purely for logging (see run_import_job's import_log)."""
	seen_machine_ids = {row.get("machine_id") for row in rows if row.get("machine_id")}
	if not seen_machine_ids:
		return []

	existing_machine_ids = set(
		frappe.get_all("IS Attendance Clocking Machine", filters={"name": ["in", list(seen_machine_ids)]}, pluck="name")
	)
	new_machine_ids = sorted(seen_machine_ids - existing_machine_ids)
	if not new_machine_ids:
		return []

	location_notes = (
		f"Auto-detected from {source_doctype} {source_docname}"
		if source_doctype and source_docname
		else "Auto-detected from an import"
	)

	added = []
	for machine_id in new_machine_ids:
		try:
			frappe.get_doc(
				{"doctype": "IS Attendance Clocking Machine", "machine_id": machine_id, "location_notes": location_notes}
			).insert(ignore_permissions=True)
		except frappe.DuplicateEntryError:
			# Another import job's own sync_clocking_machines() call created
			# this same new device between the existence check above and
			# this insert (two files from different new machines landing
			# and importing around the same time) - already handled.
			continue
		added.append(machine_id)

	return added


def employees_without_branch(rows: list[dict], fallback_branch: str | None) -> set[str]:
	"""Employees with at least one checkin in this file that can't get a
	Branch at all - no Branch on their own Employee record, no Fallback
	Branch set on this import, and (per-row) no Branch set on that row's
	own IS Attendance Clocking Machine record either. Empty whenever a
	Fallback Branch is set, since that alone is then enough for every row
	regardless of employee or machine."""
	if fallback_branch:
		return set()

	branches = employee_branch_map(rows)
	machine_branches = clocking_machine_branch_map()

	unresolved: set[str] = set()
	for row in rows:
		employee = resolve_employee(row)
		if not employee or employee in unresolved:
			continue
		if branches.get(employee):
			continue
		if machine_branches.get(row.get("machine_id")):
			continue
		unresolved.add(employee)

	return unresolved


def resolvable_row_count(rows: list[dict], fallback_branch: str | None) -> int:
	"""How many rows could actually produce an Employee Checkin right now -
	employee resolves AND a Branch is available via any of the three
	sources create_checkins() itself uses (employee's own Branch, this
	import's Fallback Branch, or a per-machine Branch mapping). Lets
	queue_import() start an import pass on a file that isn't fully
	resolved yet - a file only ever refuses to run when this is zero, i.e.
	genuinely nothing in it can be imported. Doesn't dedupe by employee
	the way employees_without_branch does - this counts rows, since that's
	what queue_import()/the UI actually care about ("is there real work to
	do"), not distinct people."""
	if not rows:
		return 0

	branches = employee_branch_map(rows)
	machine_branches = clocking_machine_branch_map()

	count = 0
	for row in rows:
		employee = resolve_employee(row)
		if not employee:
			continue
		if branches.get(employee) or fallback_branch or machine_branches.get(row.get("machine_id")):
			count += 1

	return count


def apply_resolved_issues(doc) -> set[str]:
	"""For every issues row the user has filled an Employee into, write
	that mapping onto Employee.attendance_device_id permanently (unless
	it's already correctly set), so this code resolves on its own from now
	on - across every import doctype that uses this module, not just this
	one document. Throws on a genuine conflict (code already claimed by a
	different Employee) rather than silently overwriting it.

	Returns the set of employee codes this call just resolved for the
	first time - used by refresh_readiness() to propagate the fix to every
	other Draft import document still showing the same code as unresolved
	(see resync_other_drafts()), so a mapping only ever needs to be typed
	in once, not once per document that happened to hit it."""
	newly_resolved: set[str] = set()

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
			newly_resolved.add(row.employee_code)

	return newly_resolved


def resync_other_drafts(doc, newly_resolved_codes: set[str]) -> None:
	"""After this document's own save just resolved one or more employee
	codes globally (apply_resolved_issues), find every OTHER Draft
	Clocking * Import document (any doctype in IMPORT_ISSUE_DOCTYPES) that
	still lists one of those codes as unresolved, and simply re-save it.
	Its own validate() (validate_import -> refresh_readiness) then redoes
	its own readiness check with the benefit of the mapping this document
	just wrote - clearing the stale Issue and, if that was its last one,
	flipping it straight to "Pending Import" (which in turn schedules its
	own auto-queue via after_save_hook() below) - without a human needing
	to open and re-save that earlier document by hand.

	Safe against runaway recursion: a resynced document's own issues rows
	never have `employee` pre-filled (only a human editing Issues in Desk
	sets that), so its own apply_resolved_issues() always returns an empty
	set, and this function immediately no-ops for it."""
	if not newly_resolved_codes:
		return

	for other_doctype, issue_doctype in IMPORT_ISSUE_DOCTYPES.items():
		parent_names = frappe.get_all(
			issue_doctype,
			filters={
				"employee_code": ["in", sorted(newly_resolved_codes)],
				"parenttype": other_doctype,
			},
			pluck="parent",
			distinct=True,
		)

		for parent_name in parent_names:
			if other_doctype == doc.doctype and parent_name == doc.name:
				continue  # this document already just handled itself

			other_doc = frappe.get_doc(other_doctype, parent_name)
			if other_doc.docstatus != 0:
				continue  # only a Draft can still have open Issues

			other_doc.save(ignore_permissions=True)


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
	uses this module.

	Also propagates a newly-resolved employee code to every other Draft
	import document still stuck on it (resync_other_drafts), and flags a
	background import pass to be queued automatically once the save
	commits (see after_save_hook() below) whenever this save left *more*
	rows resolvable than before - not only once every row resolves.
	Resolving a mapping is the only manual step that should ever be
	required, whether that fix finishes the file outright or only chips
	away at it; a partial fix deserves the same "just happens" treatment
	the fully-resolved case already gets, not a second "Start Import"
	click nobody's told to expect. Also covers the very first validate()
	on a freshly-attached file (resolvable_count starts at 0), so a file
	with some immediately-resolvable rows starts importing on its own,
	which is what unattended callers like erp_uploader.py already assume
	happens.

	Also computes resolvable_count (see resolvable_row_count()) - distinct
	from `ready`, which still means "100% resolved" and drives Pending
	Import/Completed exactly as before. resolvable_count is what lets
	queue_import() start a *partial* pass on a file that isn't there yet.
	"""
	previous_resolvable_count = doc.resolvable_count or 0

	newly_resolved = apply_resolved_issues(doc)
	rebuild_issues(doc, rows)
	resync_other_drafts(doc, newly_resolved)

	missing_branch = sorted(employees_without_branch(rows, doc.branch))

	doc.total_rows = len(rows)
	doc.unresolved_count = len(doc.issues)
	doc.employees_without_branch = ", ".join(missing_branch)
	doc.resolvable_count = resolvable_row_count(rows, doc.branch)

	ready = not doc.issues and not missing_branch

	# Strictly more resolvable than before - covers reaching full
	# readiness (subsumes the old "ready and wasn't already Pending
	# Import" check: nothing to gain from re-triggering a save that
	# changed nothing) and every partial improvement in between, without
	# re-triggering a save that didn't actually change what can import
	# (e.g. an unrelated field edit, or two documents racing to resolve
	# the same code via resync_other_drafts).
	if doc.resolvable_count > previous_resolvable_count:
		doc.flags._auto_queue_after_save = True

	return ready


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
	clocking_import.queue_import(self)

	Doesn't require every row to resolve - only refuses when *nothing* in
	the file resolves yet (doc.resolvable_count == 0, from
	refresh_readiness()). A file with some resolvable rows and some
	genuine stragglers proceeds; run_import_job() decides afterward
	whether that lands on "Completed" or "Partially Imported" based on
	what's actually still unresolved once the pass finishes."""
	if doc.docstatus != 0:
		frappe.throw(_("This import has already been submitted."))

	if not doc.file:
		frappe.throw(_("Attach a file first."))

	rows = doc.parse_file()
	ready = refresh_readiness(doc, rows)

	if not ready and not doc.resolvable_count:
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
			_("Nothing in this file can be imported yet - {0}. Resolve at least one of these first.").format(
				"; ".join(problems)
			)
		)

	doc.status = "Importing"
	# This call is itself already the explicit "start importing" action -
	# no need for the auto-queue after_save_hook() to also react to
	# whatever refresh_readiness() just above set on the way here.
	doc.flags._auto_queue_after_save = False
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


def after_save_hook(doc) -> None:
	"""Shared on_update() body. Callers: def on_update(self):
	clocking_import.after_save_hook(self)

	If this save is the one that just made the document ready to import
	(refresh_readiness() flips on doc.flags._auto_queue_after_save when
	that happens - see its docstring), queue the import automatically
	rather than requiring a human to click "Start Import" or wait for an
	external poller like erp_uploader.py to notice. Resolving the mapping
	*is* the human action the original design asked for ("wait for user
	mapping... before importing"); nothing further should be required once
	that's done - independent of which doctype this is, and independent of
	whether any external uploader happens to be running.

	Deferred to a background job rather than calling queue_import()
	directly here: queue_import() does its own doc.save(), which would be
	a reentrant save on this same document if called from inside its own
	still-in-flight on_update()."""
	if not doc.flags.get("_auto_queue_after_save"):
		return

	doc.flags._auto_queue_after_save = False

	frappe.enqueue(
		QUEUE_IMPORT_BY_NAME_PATH,
		queue="short",
		job_name=f"is_attendance_auto_queue_import_{doc.doctype}_{doc.name}",
		doctype=doc.doctype,
		docname=doc.name,
		enqueue_after_commit=True,
	)


def queue_import_by_name(doctype: str, docname: str) -> None:
	"""Background entry point for after_save_hook()'s deferred auto-queue -
	reloads the document fresh (past the triggering save's own in-memory
	state) and starts the import exactly as queue_import()'s "Start
	Import" button would, but only if it's still genuinely a Draft with
	something resolvable - something could in principle have changed in
	the moment between the save and this job running. Status can be
	"Pending Import" (fully resolved), "Missing Information" (partially
	resolved, or the file's very first validate()), or "Partially
	Imported" (already ran once, more resolved now) - refresh_readiness()
	flags the auto-queue for all three whenever resolvable_count went up,
	not only the fully-resolved case."""
	doc = frappe.get_doc(doctype, docname)
	if doc.docstatus == 0 and doc.resolvable_count:
		queue_import(doc)


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
		created, skipped, skipped_unresolved = create_checkins(doc, rows)
		new_machines = sync_clocking_machines(rows, doc.doctype, doc.name)

		# queue_import()'s own refresh_readiness() call, just before this job
		# was enqueued, already computed doc.issues/employees_without_branch
		# for the current file - create_checkins() doesn't resolve employee
		# codes or change that, so it's still accurate to check here rather
		# than recomputing (which would also needlessly re-run
		# resync_other_drafts()).
		fully_done = not doc.issues and not doc.employees_without_branch

		log_lines = [
			f"Rows in file: {len(rows)}",
			f"Checkins created: {created}",
			f"Rows already imported (skipped): {skipped}",
		]
		if skipped_unresolved:
			log_lines.append(f"Rows still blocked (unresolved employee/branch): {skipped_unresolved}")
		if new_machines:
			log_lines.append(
				f"New clocking machine(s) detected, added to IS Attendance Settings with "
				f"no Branch set yet: {', '.join(new_machines)}"
			)
		doc.import_log = "\n".join(log_lines)

		if fully_done:
			doc.status = "Completed"
			doc.save(ignore_permissions=True)

			# Only now - once the import has actually, fully succeeded - does
			# the document become Submitted. before_submit_guard() also
			# independently refuses to let this (or any other caller) proceed
			# unless status is already "Completed", so this property doesn't
			# depend on nothing else calling submit() early.
			doc.submit()
		else:
			# Some rows imported, some genuinely still blocked - stays a
			# Draft (never submitted) so it can be re-run once the
			# stragglers resolve, exactly like "Missing Information" was
			# already a resting Draft state, just now reachable *after*
			# real Checkins have been created rather than only before any
			# import ran. Resolving the last Issue later flips status back
			# to "Pending Import" (validate_import(), unchanged) and
			# auto-queues a finishing pass (refresh_readiness()'s existing
			# _auto_queue_after_save flag) with no further code needed here.
			doc.status = "Partially Imported"
			doc.save(ignore_permissions=True)
			frappe.db.commit()
	except Exception as error:
		frappe.db.rollback()

		# A deadlock or lock-wait-timeout here is transient contention, not
		# a real failure - e.g. several imports becoming ready at once (a
		# bulk mapping resync, or several CSVs landing together) and their
		# background jobs running in parallel across multiple workers, all
		# inserting Employee Checkins and colliding on the same
		# naming-series counter row. Frappe's own job executor
		# (frappe.utils.background_jobs.execute_job) already retries
		# exactly this class of error automatically, up to 5 times with
		# backoff - but only if it actually sees the exception. Re-raise
		# instead of parking the document at "Error" so that retry gets
		# the chance to happen; a file that's perfectly importable
		# shouldn't need a human to notice and manually re-click "Start
		# Import" just because it happened to race another import.
		#
		# Checking isinstance against Frappe's own QueryDeadlockError/
		# QueryTimeoutError, not frappe.db.is_deadlocked()/is_timedout() -
		# those two expect the *raw* driver exception (e.args[0] is the
		# numeric MySQL error code), but frappe.db.sql() has already
		# classified and wrapped it into one of these two by the time it
		# gets here (see frappe/database/database.py's own sql()) - calling
		# is_deadlocked() again on the wrapper compares the wrong thing
		# (args[0] is the original exception object, not a code) and always
		# returns False, silently defeating this whole retry path. Confirmed
		# live: a real error 1020 ("Record has changed since last read in
		# table 'tabSeries'") - genuine naming-series contention from many
		# imports becoming ready at once - was landing at "Error" instead of
		# being retried, exactly because of this mismatch.
		if isinstance(error, (frappe.QueryDeadlockError, frappe.QueryTimeoutError)):
			raise

		# Genuine failure - the "should theoretically not be possible" case:
		# queue_import() already verified at least one row resolves before
		# enqueueing this job (create_checkins() itself tolerates the rest
		# not resolving, see its own skipped_unresolved handling), so
		# reaching here means something else went wrong (an Employee/Branch
		# got edited or deleted mid-flight, a real bug) or was a
		# non-transient DB error. Whatever partial work this attempt did
		# (some checkins inserted, in-memory created_checkins rows, etc.) is
		# undone by the rollback above; reload discards the now-inconsistent
		# in-memory state before recording the failure.
		doc.reload()
		doc.flags._clocking_import_pipeline_active = True
		doc.status = "Error"
		doc.import_log = frappe.get_traceback()
		doc.save(ignore_permissions=True)
		frappe.db.commit()


def create_checkins(doc, rows: list[dict]) -> tuple[int, int, int]:
	"""Bulk-create Employee Checkins for every row that doesn't already
	exist (idempotent - dedup key is employee+time+isa_clocking_machine),
	stamping full provenance (isa_import_doctype/isa_import_reference via
	Dynamic Link, so any future import type just needs to call this same
	function - no new custom field required) and recomputing Attendance
	once per affected employee/day at the end.

	Branch priority per row: the employee's own Employee.branch, then this
	import's Fallback Branch, then - only if neither of those gives one -
	that row's own IS Attendance Clocking Machine record's Branch (see
	clocking_machine_branch_map()). A device's Branch is always the last
	resort, never an override of the employee's own Branch or an
	explicitly-set Fallback Branch.

	Rows whose employee code doesn't resolve at all, or resolves but has
	no Branch from any of the three sources, are counted
	(skipped_unresolved) and left alone rather than raised - queue_import()
	no longer guarantees every row resolves before this runs (see its own
	docstring), so this has to tolerate genuine stragglers instead of
	assuming they can't appear. Returns (created, skipped, skipped_unresolved)."""
	created = 0
	skipped = 0
	skipped_unresolved = 0
	affected: set[tuple[str, object]] = set()

	branches = employee_branch_map(rows)
	machine_branches = clocking_machine_branch_map()

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
			machine_id = row.get("machine_id")
			branch = branches.get(employee) or doc.branch or machine_branches.get(machine_id) if employee else None

			if not employee or not branch:
				skipped_unresolved += 1
				continue

			if frappe.db.exists(
				"Employee Checkin",
				{"employee": employee, "time": row.get("time"), "isa_clocking_machine": machine_id},
			):
				skipped += 1
				continue

			# HRMS's own Employee Checkin.validate_duplicate_log() runs on
			# every insert regardless of our own dedup key above, and it
			# checks employee+time+log_type - not machine-aware. Two
			# different machines (or two different imports) legitimately
			# can report what's really the same physical clocking event
			# with an identical employee/time/log_type but a different
			# machine_id (e.g. a terminal swap where the old and new
			# device both briefly saw the same swipe). Our own key would
			# call that "new"; HRMS's core check would then throw and
			# abort the whole import. Checking HRMS's own condition here
			# too, and skipping on the same terms it would, keeps this
			# import job from failing over something that isn't actually
			# a bug - it's exactly the duplicate HRMS itself says it is.
			if frappe.db.exists(
				"Employee Checkin",
				{"employee": employee, "time": row.get("time"), "log_type": row.get("log_type")},
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

			# Also mark the day *before* this row's own calendar date as
			# affected, not just its own date. gateway.py writes one CSV
			# per calendar day, so an overnight shift's IN (day N, late
			# evening) and its OUT (day N+1, early morning) routinely land
			# in two separate daily CSVs - imported as two separate
			# documents, possibly hours or days apart. Without this, the
			# import that adds day N+1's OUT checkin would only recompute
			# day N+1 (its own row's date), never revisiting day N - whose
			# shift-window recompute is what's actually able to pair it
			# with the already-imported IN checkin (see
			# attendance_sync._get_shift_window's overnight handling). Same
			# reasoning as on_employee_checkin's live-checkin path; this is
			# the bulk-import equivalent of that same gap, and the more
			# common way it actually happens in practice for HIKVision
			# imports specifically. Harmless no-op recompute on every row
			# that isn't the tail of an overnight shift.
			checkin_date = getdate(row.get("time"))
			affected.add((employee, checkin_date))
			affected.add((employee, add_days(checkin_date, -1)))
	finally:
		frappe.flags.in_bulk_checkin_import = False

	for employee, attendance_date in affected:
		recompute_attendance_for_employee_day(employee, attendance_date)

	return created, skipped, skipped_unresolved


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
