# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ISAttendanceSettings(Document):
	def validate(self):
		self._sync_branch_access_emails()

	def _sync_branch_access_emails(self):
		"""Keep every hr_per_branch row's email_address in sync with its linked
		User's current email, on every save - not just filled in once while
		blank. Mirrors ir_role_restrictions.py::_sync_recipient_emails so a
		later User rename doesn't leave a stale address behind here too."""
		for row in self.get("hr_per_branch") or []:
			user = getattr(row, "user", None)
			if not user:
				continue
			email = frappe.db.get_value("User", user, "email")
			if email and row.email_address != email:
				row.email_address = email
