# Copyright (c) 2026, BuFf0k and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, get_datetime, getdate

EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]

TEST_EMPLOYEE_NAME = "_Test Clocking Adjustment Employee"


class IntegrationTestClockingAdjustment(IntegrationTestCase):
	"""
	Integration tests for ClockingAdjustment.
	Use this class for testing interactions between multiple components.

	Deliberately does not use erpnext's own make_employee test helper -
	importing it triggers ERPNextTestSuite's module-level
	BootStrapTestData(), which unconditionally creates fresh Fiscal
	Year/Company/master data and collides with this site's real, already-
	populated data. This reuses whatever Company already exists instead of
	creating one.
	"""

	def setUp(self):
		self.employee = self._get_or_create_test_employee()
		frappe.db.delete("Employee Checkin", {"employee": self.employee})

	def _get_or_create_test_employee(self):
		existing = frappe.db.get_value("Employee", {"employee_name": TEST_EMPLOYEE_NAME}, "name")
		if existing:
			frappe.db.set_value("Employee", existing, "status", "Active")
			return existing

		company = frappe.db.get_value("Company", {}, "name")
		if not company:
			self.skipTest("No Company exists on this site to attach a test Employee to.")

		department = frappe.db.get_value("Department", {"company": company}, "name")

		employee = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": TEST_EMPLOYEE_NAME,
				"company": company,
				"department": department,
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"gender": "Male",
				"status": "Active",
			}
		)
		employee.insert(ignore_permissions=True)
		return employee.name

	def tearDown(self):
		frappe.db.delete("Employee Checkin", {"employee": self.employee})
		frappe.db.delete("Clocking Adjustment", {"employee": self.employee})

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------

	def _make_checkin(self, time, log_type="IN"):
		checkin = frappe.get_doc(
			{
				"doctype": "Employee Checkin",
				"employee": self.employee,
				"time": get_datetime(time),
				"log_type": log_type,
			}
		)
		checkin.insert(ignore_permissions=True)
		return checkin

	def _make_adjustment(self, from_date, to_date, reason="Testing"):
		doc = frappe.get_doc(
			{
				"doctype": "Clocking Adjustment",
				"employee": self.employee,
				"from_date": from_date,
				"to_date": to_date,
				"reason": reason,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc

	def _submit_with_signed_form(self, doc):
		doc.signed_adjustment_form = "/files/does-not-need-to-exist.pdf"
		doc.submit()
		doc.reload()
		return doc

	# ------------------------------------------------------------------
	# load_checkins
	# ------------------------------------------------------------------

	def test_load_checkins_populates_rows_correctly(self):
		checkin = self._make_checkin("2026-09-01 07:00:00", "IN")

		doc = self._make_adjustment("2026-09-01", "2026-09-01")
		doc.load_checkins()

		self.assertEqual(len(doc.checkin_rows), 1)
		row = doc.checkin_rows[0]
		self.assertEqual(row.checkin, checkin.name)
		self.assertEqual(get_datetime(row.original_time), get_datetime(checkin.time))
		self.assertEqual(row.original_log_type, "IN")
		self.assertEqual(row.log_type, "IN")

	def test_load_checkins_refuses_on_submitted_document(self):
		self._make_checkin("2026-09-01 07:00:00", "IN")
		doc = self._make_adjustment("2026-09-01", "2026-09-01")
		doc.load_checkins()
		doc = self._submit_with_signed_form(doc)

		with self.assertRaises(frappe.ValidationError):
			doc.load_checkins()

	# ------------------------------------------------------------------
	# on_submit
	# ------------------------------------------------------------------

	def test_submit_creates_checkin_for_new_row(self):
		doc = self._make_adjustment("2026-09-01", "2026-09-01")
		doc.append(
			"checkin_rows",
			{"time": get_datetime("2026-09-01 06:00:00"), "log_type": "IN"},
		)
		doc.save()
		doc = self._submit_with_signed_form(doc)

		self.assertTrue(doc.checkin_rows[0].checkin)
		self.assertTrue(
			frappe.db.exists(
				"Employee Checkin",
				{"employee": self.employee, "time": get_datetime("2026-09-01 06:00:00")},
			)
		)

	def test_submit_corrects_time_and_log_type(self):
		checkin = self._make_checkin("2026-09-01 15:59:00", "IN")
		doc = self._make_adjustment("2026-09-01", "2026-09-01")
		doc.load_checkins()
		doc.checkin_rows[0].log_type = "OUT"
		doc.save()
		doc = self._submit_with_signed_form(doc)

		checkin.reload()
		self.assertEqual(checkin.log_type, "OUT")

	def test_submit_removes_checkin_on_remove_flag(self):
		checkin = self._make_checkin("2026-09-01 07:00:00", "IN")
		doc = self._make_adjustment("2026-09-01", "2026-09-01")
		doc.load_checkins()
		doc.checkin_rows[0].remove = 1
		doc.save()
		doc = self._submit_with_signed_form(doc)

		self.assertFalse(frappe.db.exists("Employee Checkin", checkin.name))

	def test_submit_throws_when_attendance_linked_time_changes(self):
		checkin = self._make_checkin("2026-09-01 07:00:00", "IN")
		frappe.db.set_value("Employee Checkin", checkin.name, "attendance", "SOME-ATT-0001")

		doc = self._make_adjustment("2026-09-01", "2026-09-01")
		doc.load_checkins()
		doc.checkin_rows[0].time = get_datetime("2026-09-01 07:15:00")
		doc.save()

		with self.assertRaises(frappe.ValidationError):
			self._submit_with_signed_form(doc)

	def test_submit_throws_on_staleness(self):
		checkin = self._make_checkin("2026-09-01 07:00:00", "IN")
		doc = self._make_adjustment("2026-09-01", "2026-09-01")
		doc.load_checkins()
		doc.checkin_rows[0].time = get_datetime("2026-09-01 07:15:00")
		doc.save()

		# Something else touches the checkin after this adjustment was loaded.
		checkin.reload()
		checkin.time = get_datetime("2026-09-01 08:00:00")
		checkin.save(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError):
			self._submit_with_signed_form(doc)

	def test_before_submit_requires_signed_form(self):
		self._make_checkin("2026-09-01 07:00:00", "IN")
		doc = self._make_adjustment("2026-09-01", "2026-09-01")
		doc.load_checkins()
		doc.save()

		with self.assertRaises(frappe.ValidationError):
			doc.submit()

	def test_before_submit_requires_at_least_one_row(self):
		doc = self._make_adjustment("2026-09-01", "2026-09-01")
		doc.signed_adjustment_form = "/files/does-not-need-to-exist.pdf"

		with self.assertRaises(frappe.ValidationError):
			doc.submit()

	# ------------------------------------------------------------------
	# on_cancel
	# ------------------------------------------------------------------

	def test_cancel_reverses_add(self):
		doc = self._make_adjustment("2026-09-01", "2026-09-01")
		doc.append(
			"checkin_rows",
			{"time": get_datetime("2026-09-01 06:00:00"), "log_type": "IN"},
		)
		doc.save()
		doc = self._submit_with_signed_form(doc)
		created_checkin = doc.checkin_rows[0].checkin

		doc.cancel()

		self.assertFalse(frappe.db.exists("Employee Checkin", created_checkin))

	def test_cancel_reverses_correct(self):
		checkin = self._make_checkin("2026-09-01 15:59:00", "IN")
		doc = self._make_adjustment("2026-09-01", "2026-09-01")
		doc.load_checkins()
		doc.checkin_rows[0].log_type = "OUT"
		doc.save()
		doc = self._submit_with_signed_form(doc)

		doc.cancel()

		checkin.reload()
		self.assertEqual(checkin.log_type, "IN")

	def test_cancel_reverses_remove(self):
		checkin = self._make_checkin("2026-09-01 07:00:00", "IN")
		doc = self._make_adjustment("2026-09-01", "2026-09-01")
		doc.load_checkins()
		doc.checkin_rows[0].remove = 1
		doc.save()
		doc = self._submit_with_signed_form(doc)

		doc.cancel()
		doc.reload()

		# Name isn't recoverable, but a checkin at the same time/type must exist again.
		self.assertTrue(
			frappe.db.exists(
				"Employee Checkin",
				{"employee": self.employee, "time": get_datetime("2026-09-01 07:00:00"), "log_type": "IN"},
			)
		)

	# ------------------------------------------------------------------
	# Overnight-shift day-before recompute
	# ------------------------------------------------------------------

	def test_recompute_covers_day_before(self):
		# An OUT punch just after midnight on 2026-09-02 is the tail end of
		# an overnight shift attributed to 2026-09-01 - submitting an
		# adjustment touching that punch must recompute both days.
		doc = self._make_adjustment("2026-09-02", "2026-09-02")
		doc.append(
			"checkin_rows",
			{"time": get_datetime("2026-09-02 00:30:00"), "log_type": "OUT"},
		)
		doc.save()

		with patch(
			"is_attendance.isambane_attendance.doctype.clocking_adjustment.clocking_adjustment.recompute_attendance_for_employee_day"
		) as mock_recompute:
			self._submit_with_signed_form(doc)

		recomputed_dates = {call.args[1] for call in mock_recompute.call_args_list}
		self.assertIn(getdate("2026-09-02"), recomputed_dates)
		self.assertIn(add_days(getdate("2026-09-02"), -1), recomputed_dates)
