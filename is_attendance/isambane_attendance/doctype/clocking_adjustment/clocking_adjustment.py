# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

from is_attendance.controllers.attendance_sync import (
	recompute_attendance_for_employee_day,
)


class ClockingAdjustment(Document):
	def validate(self):
		if self.adjustment_type == "Add Missing Checkin":
			if not self.log_type or not self.adjusted_time:
				frappe.throw(_("Log Type and Adjusted Time are required to add a checkin."))
		else:
			if not self.reference_checkin:
				frappe.throw(_("Reference Checkin is required for {0}.").format(self.adjustment_type))
			if not frappe.db.exists("Employee Checkin", self.reference_checkin):
				frappe.throw(_("Reference Checkin {0} no longer exists.").format(self.reference_checkin))

	def on_submit(self):
		if self.adjustment_type == "Add Missing Checkin":
			self._add_checkin()
		elif self.adjustment_type == "Correct Time":
			self._correct_checkin()
		elif self.adjustment_type == "Remove Checkin":
			self._remove_checkin()

		self._recompute()

	def on_cancel(self):
		if self.adjustment_type == "Add Missing Checkin":
			self._revert_add_checkin()
		elif self.adjustment_type == "Correct Time":
			self._revert_correct_checkin()
		elif self.adjustment_type == "Remove Checkin":
			self._revert_remove_checkin()

		self._recompute()

	# ------------------------------------------------------------------
	# Add Missing Checkin
	# ------------------------------------------------------------------

	def _add_checkin(self):
		checkin = frappe.get_doc(
			{
				"doctype": "Employee Checkin",
				"employee": self.employee,
				"time": self.adjusted_time,
				"log_type": self.log_type,
				"isa_branch": self.branch,
				"isa_import_doctype": self.doctype,
				"isa_import_reference": self.name,
			}
		)
		checkin.insert(ignore_permissions=True)
		self.db_set("resulting_checkin", checkin.name)

	def _revert_add_checkin(self):
		if self.resulting_checkin and frappe.db.exists("Employee Checkin", self.resulting_checkin):
			frappe.delete_doc(
				"Employee Checkin",
				self.resulting_checkin,
				ignore_permissions=True,
				force=True,
			)

	# ------------------------------------------------------------------
	# Correct Time
	# ------------------------------------------------------------------

	def _correct_checkin(self):
		checkin = frappe.get_doc("Employee Checkin", self.reference_checkin)
		self.db_set("previous_time", checkin.time)
		self.db_set("previous_log_type", checkin.log_type)
		self.db_set("resulting_checkin", checkin.name)

		checkin.time = self.adjusted_time
		checkin.log_type = self.log_type
		checkin.save(ignore_permissions=True)

	def _revert_correct_checkin(self):
		if not self.resulting_checkin or not frappe.db.exists("Employee Checkin", self.resulting_checkin):
			return
		checkin = frappe.get_doc("Employee Checkin", self.resulting_checkin)
		checkin.time = self.previous_time
		checkin.log_type = self.previous_log_type
		checkin.save(ignore_permissions=True)

	# ------------------------------------------------------------------
	# Remove Checkin
	# ------------------------------------------------------------------

	def _remove_checkin(self):
		checkin = frappe.get_doc("Employee Checkin", self.reference_checkin)
		self.db_set("previous_time", checkin.time)
		self.db_set("previous_log_type", checkin.log_type)
		self.db_set("resulting_checkin", checkin.name)

		frappe.delete_doc(
			"Employee Checkin",
			self.reference_checkin,
			ignore_permissions=True,
			force=True,
		)

	def _revert_remove_checkin(self):
		if not self.resulting_checkin or not self.previous_time:
			return
		checkin = frappe.get_doc(
			{
				"doctype": "Employee Checkin",
				"employee": self.employee,
				"time": self.previous_time,
				"log_type": self.previous_log_type,
				"isa_branch": self.branch,
				"isa_import_doctype": self.doctype,
				"isa_import_reference": self.name,
			}
		)
		checkin.insert(ignore_permissions=True)
		self.db_set("resulting_checkin", checkin.name)

	# ------------------------------------------------------------------
	# Shared
	# ------------------------------------------------------------------

	def _recompute(self):
		recompute_attendance_for_employee_day(self.employee, getdate(self.attendance_date))
