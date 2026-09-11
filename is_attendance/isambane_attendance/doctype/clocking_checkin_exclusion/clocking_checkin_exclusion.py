# Copyright (c) 2026, BuFf0k and contributors
# For license information, please see license.txt

"""
A tombstone for one specific device punch (employee + exact time +
machine) that a Clocking Adjustment deliberately deleted or moved away
from - see clocking_import.create_checkins(), which checks this table
before creating a new Employee Checkin, and Clocking Adjustment's own
_process_row_on_submit()/_process_row_on_cancel(), which write and clean
these up. Never created or edited directly by a person.
"""

from __future__ import annotations

from frappe.model.document import Document


class ClockingCheckinExclusion(Document):
	pass
