# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""Thin doctype wrapper - all real logic lives in
is_attendance.controllers.sage_payroll, same split as Clocking * Import /
clocking_import.py. ingest_employees() is the one sage_employee_puller.py
(the Windows-host bridge script) calls, via the standard
/api/method/run_doc_method endpoint (dt="Sage Payroll Run", dn=<name>,
method="ingest_employees", records=[...]) - the same mechanism
erp_uploader.py already uses for queue_import()."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from is_attendance.controllers import sage_payroll


class SagePayrollRun(Document):
	def before_submit(self):
		if self.status != "Exported":
			frappe.throw(_('Export the .txt first ("Export .txt") - this Run can only be submitted once Status is "Exported".'))

	@frappe.whitelist()
	def request_employee_pull(self):
		"""Marks this Run as waiting for sage_employee_puller.py's heartbeat
		(on the Windows host, next to Sage's ODBC DSNs) to notice and pull
		its employees from Sage - the Frappe side never talks to Sage
		directly, so this only sets a flag for the heartbeat to find on its
		next poll (list_pending_pull_requests) rather than doing anything
		itself. Re-requestable any time this Run is still a Draft (status
		before "Employees Loaded"), including after a first pull already
		landed - ingest_employees() is idempotent, so a re-pull just
		refreshes the existing rows."""
		if self.docstatus != 0:
			frappe.throw(_("This Run has already been submitted."))
		if self.status not in ("Draft", "Pull Requested"):
			frappe.throw(_("Employees have already been loaded for this Run - no need to request a pull again."))

		self.db_set("status", "Pull Requested")
		self.db_set("pull_requested_at", now_datetime())

	@frappe.whitelist()
	def ingest_employees(self, records):
		return sage_payroll.ingest_employees(self, records)

	@frappe.whitelist()
	def compute_hours_and_leave(self):
		sage_payroll.compute_hours_and_leave(self)

	@frappe.whitelist()
	def export_txt(self):
		sage_payroll.export_txt(self)
