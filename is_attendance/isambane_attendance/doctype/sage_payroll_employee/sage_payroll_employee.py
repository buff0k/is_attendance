# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

# All mutation happens through is_attendance.controllers.sage_payroll
# (ingest_employees / compute_hours_and_leave) - this controller stays a
# plain passthrough, same as the other data-holding doctypes in this app.
from frappe.model.document import Document


class SagePayrollEmployee(Document):
	pass
