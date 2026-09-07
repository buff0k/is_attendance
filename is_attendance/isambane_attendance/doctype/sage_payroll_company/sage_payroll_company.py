# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class SagePayrollCompany(Document):
	def validate(self):
		self._normalize_sage_company_no()

	def _normalize_sage_company_no(self):
		"""Zero-pad to 3 digits so this always matches what CoNo = Format(x, "000")
		writes into the generated .txt (see sage_payroll.py's build_detail_line) -
		typing "1" here should behave the same as typing "001"."""
		value = (self.sage_company_no or "").strip()
		if not value.isdigit():
			frappe.throw(_("Sage Company Number must be numeric (e.g. 001)."))
		self.sage_company_no = value.zfill(3)
